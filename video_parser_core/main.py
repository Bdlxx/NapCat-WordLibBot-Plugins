# main.py — 解析插件主入口（NapCat 适配版）
# 复刻自 astrbot_plugin_parser，适配本项目 handle(event) 插件模式。
# 使用方式：本文件由 video_parser.py 的 handle() 调用（或直接作为独立插件）。

import asyncio
import os
import re
import sys
import time
from pathlib import Path

# 确保本包可导入
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.message.components import Json, Plain

from .config import PluginConfig
from .debounce import Debouncer
from .download import Downloader
from .parsers import BaseParser
from .render import Renderer
from .sender import MessageSender, _setup_sender, segs_to_cq
from .utils import extract_json_url


class ParserPlugin:
    """解析插件（复刻 astrbot_plugin_parser 核心）"""

    def __init__(self, send_fn=None, container_path_fn=None, cache_dir=None, video_config_path=None):
        self.cfg = PluginConfig(config_dir=cache_dir)
        self._video_config_path = video_config_path
        self.renderer = Renderer(self.cfg)
        self.debouncer = Debouncer(self.cfg)
        self.sender = None  # 延迟创建（需事件循环）
        self.downloader = None
        self.parser_map: dict[str, BaseParser] = {}
        self.key_pattern_list: list[tuple[str, re.Pattern]] = []

        # 注入发送能力
        _setup_sender(send_fn, container_path_fn)
        self._send_fn = send_fn

    def _init_async(self):
        """在事件循环内初始化异步组件（Downloader/MessageSender）"""
        if self.downloader is not None:
            return
        # 从 video_parser_config.json 加载用户配置（web 面板可调参数）
        if self._video_config_path and os.path.exists(self._video_config_path):
            try:
                self.cfg.load_from_video_config(self._video_config_path)
            except Exception as e:
                print(f"[解析器] 加载视频解析配置失败: {e}")
        self.downloader = Downloader(self.cfg)
        self.sender = MessageSender(self.cfg, self.renderer)
        self._register_parser()

    async def _init_async_async(self):
        """供持久 loop 中调用"""
        self._init_async()
        return True

    # ---------- 注册解析器 ----------
    def _register_parser(self):
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())
        enabled_classes = []
        enabled_names = []
        for cls in all_subclass:
            platform_name = cls.platform.name
            if platform_name not in enabled_platforms:
                continue
            enabled_classes.append(cls)
            enabled_names.append(platform_name)
            parser = cls(self.cfg, self.downloader)
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        patterns = []
        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))
        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns
        print(f"[解析器] 已启用平台: {'、'.join(enabled_names)}")

    # ---------- 解析入口 ----------
    async def _parse_and_send(self, event: dict, text: str):
        """从消息文本匹配链接并解析发送"""
        self._init_async()
        keyword = ""
        searched = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            m = pat.search(text)
            if m:
                keyword, searched = kw, m
                break
        if searched is None:
            return False

        aev = AstrMessageEvent(event, send_fn=self._send_fn)

        # 链接防抖
        link = searched.group(0)
        umo = aev.unified_msg_origin()
        if self.debouncer.hit_link(umo, link):
            print(f"[解析器] 链接防抖: {link}")
            return False

        # 解析
        try:
            parse_res = await self.parser_map[keyword].parse(keyword, searched)
        except Exception as e:
            print(f"[解析器] 解析失败 [{keyword}]: {e}")
            return True

        # 资源防抖
        try:
            resource_id = parse_res.get_resource_id()
            if self.debouncer.hit_resource(umo, resource_id):
                print(f"[解析器] 资源防抖: {resource_id[:8]}")
                return True
        except Exception:
            pass

        # 发送
        try:
            await self.sender.send_parse_result(aev, parse_res)
        except Exception as e:
            print(f"[解析器] 发送失败: {e}")
        return True

    async def handle_event(self, event: dict) -> bool:
        """处理 OneBot 消息事件，返回是否消费"""
        if event.get("post_type") != "message":
            return False

        # 黑名单（统一会话简化）
        umo = f"{event.get('message_type')}:{event.get('group_id') or event.get('user_id')}"
        if umo in self.cfg.blacklist:
            return False

        # 组装文本：raw_message + JSON 卡片 URL
        text = event.get("raw_message", "") or ""
        chain = event.get("message", []) or []
        for seg in chain:
            if seg.get("type") == "json":
                url = extract_json_url(seg.get("data", {}))
                if url:
                    text = url
                    break

        if not text:
            return False

        return await self._parse_and_send(event, text)


# ---------- 同步入口（供非 asyncio 环境调用）----------
_plugin_instance = None
_loop = None
_loop_thread = None


def _start_loop():
    """后台持久事件循环线程：避免每次 asyncio.run 重建 loop 导致 aiohttp session 失效"""
    global _loop
    import threading
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    t = threading.Thread(target=_run, daemon=True, name="parser_loop")
    t.start()
    return t


def get_plugin(send_fn=None, container_path_fn=None, cache_dir=None, video_config_path=None) -> ParserPlugin:
    global _plugin_instance, _loop_thread
    if _plugin_instance is None:
        if _loop_thread is None:
            _loop_thread = _start_loop()
        # 在持久 loop 中初始化异步组件
        _plugin_instance = ParserPlugin(send_fn=send_fn, container_path_fn=container_path_fn,
                                        cache_dir=cache_dir, video_config_path=video_config_path)
        fut = asyncio.run_coroutine_threadsafe(_plugin_instance._init_async_async(), _loop)
        fut.result(timeout=30)
    else:
        # 更新发送能力（send_fn 每次可能不同——绑定当前事件）
        _plugin_instance._send_fn = send_fn
        _setup_sender(send_fn, container_path_fn)
    return _plugin_instance


def handle_event_sync(event: dict, send_fn=None, container_path_fn=None, cache_dir=None) -> bool:
    """同步包装：供 video_parser.py 的 handle() 调用"""
    # 绑定当前事件：send_fn(segs) 使用传入的 event
    bound_send = (lambda segs: send_fn(event, segs)) if send_fn else None
    plugin = get_plugin(send_fn=bound_send, container_path_fn=container_path_fn, cache_dir=cache_dir)
    fut = asyncio.run_coroutine_threadsafe(plugin.handle_event(event), _loop)
    try:
        return fut.result(timeout=120)
    except Exception as e:
        print(f"[解析器] 处理异常: {e}")
        return False
