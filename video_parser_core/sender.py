# sender.py — NapCat 适配版消息发送器
# 保留原版发送策略（light/heavy 分组、阈值合并转发、失败回退文本），
# 将 AstrBot 组件转换为 OneBot CQ 消息段后经 WebSocket 发送。

from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer

# 导入我们的 NapCat 发送工具（由主入口注入）
_ws_send = None
_container_path_fn = None


def _setup_sender(send_fn, container_path_fn=None):
    """主入口注入发送能力"""
    global _ws_send, _container_path_fn
    _ws_send = send_fn
    _container_path_fn = container_path_fn


def _to_container_path(path: Path) -> str:
    """宿主机路径 → NapCat 容器内路径（若注入转换函数）"""
    if _container_path_fn:
        cp = _container_path_fn(str(path))
        if cp:
            return cp
    return str(path)


def _seg_to_cq(seg: BaseMessageComponent) -> dict:
    """消息组件 → OneBot CQ 段"""
    if isinstance(seg, Plain):
        return {"type": "text", "data": {"text": seg.text}}
    if isinstance(seg, Image):
        return {"type": "image", "data": {"file": _to_container_path(Path(seg.path or seg.file))}}
    if isinstance(seg, Video):
        return {"type": "video", "data": {"file": _to_container_path(Path(seg.path or seg.file))}}
    if isinstance(seg, Record):
        return {"type": "record", "data": {"file": _to_container_path(Path(seg.path or seg.file))}}
    if isinstance(seg, File):
        return {"type": "file", "data": {"file": _to_container_path(Path(seg.file)), "name": seg.name}}
    if isinstance(seg, Node):
        content = [_seg_to_cq(c) for c in (seg.content or [])]
        return {"type": "node", "data": {"uin": seg.uin, "name": seg.name, "content": content}}
    if isinstance(seg, Nodes):
        nodes = []
        for node in seg.nodes:
            if isinstance(node, Node):
                content = [_seg_to_cq(c) for c in (node.content or [])]
                nodes.append({"type": "node", "data": {"uin": node.uin, "name": node.name, "content": content}})
        return {"type": "forward", "data": {"messages": nodes}}
    # 兜底
    try:
        return seg.to_cq()
    except Exception:
        return {"type": "text", "data": {"text": str(seg)}}


def segs_to_cq(segs: list) -> list[dict]:
    """消息组件列表 → CQ 段列表"""
    out = []
    for seg in segs:
        if isinstance(seg, Nodes):
            out.append(_seg_to_cq(seg))
        elif isinstance(seg, list):
            out.extend(segs_to_cq(seg))
        else:
            out.append(_seg_to_cq(seg))
    return out


class MessageSender:
    """消息发送器（NapCat 适配版，保留原版策略）"""

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(self, result, contents=None, *, force_merge_override=None, render_card_override=None) -> dict:
        light, heavy = [], []
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            if isinstance(cont, (ImageContent, GraphicsContent, TextContent)):
                light.append(cont)
            elif isinstance(cont, (VideoContent, AudioContent, FileContent, DynamicContent)):
                heavy.append(cont)
            else:
                light.append(cont)

        # 视频+图文混合（如抖音实况图）：视频直发为主体，图片单独合并转发，
        # 避免把所有内容塞进一条 forward 导致图片以合并形式发送、视频丢失主体地位
        has_video = any(isinstance(c, VideoContent) for c in heavy)
        has_images = bool(light)
        split_media = has_video and has_images

        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override
        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
            # 视频+图片混合：视频直发，图片拆分为独立合并转发（不占用主消息段数）
            "split_media": split_media,
        }

    async def _send_preview_card(self, event, result, plan):
        if not plan["preview_card"]:
            return
        if image_path := await self.renderer.render_card(result):
            await event.send(event.chain_result([self._image_from_path(image_path)]))

    async def _build_segments(self, result, plan, only=None) -> list[BaseMessageComponent]:
        """构建消息段；only='light'/'heavy' 时只构建对应分组（用于视频+图片分流）"""
        segs = []
        if plan["render_card"] and plan["force_merge"] and only is None:
            if image_path := await self.renderer.render_card(result):
                segs.append(self._image_from_path(image_path))

        if only in (None, 'light'):
            for cont in plan["light"]:
                if isinstance(cont, TextContent):
                    if cont.text:
                        segs.append(Plain(cont.text))
                    continue
                try:
                    path: Path = await cont.get_path()
                except (DownloadLimitException, ZeroSizeException):
                    continue
                except DownloadException:
                    if self.cfg.show_download_fail_tip:
                        segs.append(Plain("此项媒体下载失败"))
                    continue
                if isinstance(cont, ImageContent):
                    segs.append(self._image_from_path(path))
                elif isinstance(cont, GraphicsContent):
                    segs.append(self._image_from_path(path))
                    if cont.text:
                        segs.append(Plain(cont.text))
                    if cont.alt:
                        segs.append(Plain(cont.alt))

        if only in (None, 'heavy'):
            for cont in plan["heavy"]:
                try:
                    path: Path = await cont.get_path()
                except (SizeLimitException, DurationLimitException) as exc:
                    if self.cfg.show_download_fail_tip:
                        message = "此项媒体超过时长限制" if isinstance(exc, DurationLimitException) else "此项媒体超过大小限制"
                        segs.append(Plain(message))
                    continue
                except DownloadException:
                    if self.cfg.show_download_fail_tip:
                        segs.append(Plain("此项媒体下载失败"))
                    continue
                if isinstance(cont, (VideoContent, DynamicContent)):
                    segs.append(self._video_from_path(path))
                elif isinstance(cont, AudioContent):
                    segs.append(File(name=path.name, file=self._to_file_uri(path)) if self.cfg.audio_to_file else self._record_from_path(path))
                elif isinstance(cont, FileContent):
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(self, event, segs, force_merge, result=None):
        if not force_merge or not segs:
            return segs
        nodes = Nodes([])
        self_id = event.get_self_id()
        # 节点名单行：@作者 | 简介（QQ 合并转发对 name 换行渲染异常，不用 \n）
        node_name = "解析器"
        if result is not None:
            try:
                parts = []
                if result.author and result.author.name:
                    parts.append(f"@{result.author.name}")
                if result.title:
                    parts.append(result.title[:30])
                if parts:
                    node_name = " | ".join(parts)
            except Exception:
                pass
        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name=node_name, content=[seg]))
        return [nodes]

    @staticmethod
    def _build_text_fallback(result):
        lines = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))
        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result):
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(self, event, result, group) -> bool:
        plan = self._build_send_plan(
            result, group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )
        await self._send_preview_card(event, result, plan)

        # 视频+图片混合（实况图/图文带视频）：单条合并转发，视频在前、图片在后
        # （与旧逻辑一致：聊天记录卡片上方视频、下方图片）
        if plan.get("split_media"):
            sent_ok = False
            heavy_segs = await self._build_segments(result, plan, only='heavy')
            light_segs = await self._build_segments(result, plan, only='light')
            all_segs = heavy_segs + light_segs
            if all_segs:
                all_segs = self._merge_segments_if_needed(event, all_segs, True, result)
                try:
                    await event.send(event.chain_result(all_segs))
                    sent_ok = True
                except Exception as e:
                    logger.error(f"发送混合内容失败： error={e}")
            return sent_ok

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"], result)

        if not segs:
            return False
        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as e:
            logger.error(f"发送解析结果失败： error={e}")
            return False

    async def send_parse_result(self, event, result):
        groups = self._resolve_groups(result)
        sent = False
        for group in groups:
            sent = await self._send_group(event, result, group) or sent

        if not sent:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return
            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                logger.error(f"发送解析结果失败： error={e}")
