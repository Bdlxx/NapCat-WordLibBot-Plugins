from asyncio import Task, TimeoutError, create_task, gather, sleep, to_thread
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import json
import os
import time

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout
from msgspec import Struct, convert
from tqdm.asyncio import tqdm

from astrbot.api import logger

from .config import PluginConfig
from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import LimitedSizeDict, generate_file_name, merge_av, safe_unlink

P = ParamSpec("P")
T = TypeVar("T")


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    duration: int
    """时长"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


# ---------- 分段下载（多连接 Range）----------
# 为什么要分段：B站等 CDN 对**单连接**限速/中途掐断很常见（线上实测单连接只有 65 kB/s，
# 同一资源 4 路分段能到 855 kB/s，快 13 倍）；分段还天然带"断点续传"——
# 某一段断了只补这一段，不会像单连接那样整份重来。
PARALLEL_CONNECTIONS: int = 8          # 并发段数（1 = 关闭，退回单连接）
MIN_PARALLEL_SIZE = 4 * 1024 * 1024    # 小于 4MB 不值得分段（请求开销大于收益）
CHUNK_MIN = 1024 * 1024                # 每段至少 1MB
CHUNK_RETRY = 3                        # 单段续传最多补几次


class RangeNotSupported(Exception):
    """服务端不支持 Range（忽略 Range 头、返回 200 整份）→ 上层回退单连接"""


def set_parallel_connections(n) -> None:
    """按宿主配置设置并发段数（1 = 关闭分段；非法值忽略）"""
    global PARALLEL_CONNECTIONS
    try:
        v = int(n)
    except (TypeError, ValueError):
        return
    if 1 <= v <= 16:
        PARALLEL_CONNECTIONS = v


class Downloader:
    """下载器，支持youtube-dlp 和 流式下载"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.max_size = self.cfg.source_max_size
        self.default_headers: dict[str, str] = COMMON_HEADER.copy()
        # 视频信息缓存
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()
        # 用于流式下载的客户端
        self.client = ClientSession(
            timeout=ClientTimeout(total=self.cfg.download_timeout)
        )

    async def close(self):
        """关闭网络客户端"""
        await self.client.close()

    # ---------- 断点续传基础设施 ----------
    @staticmethod
    def _part_paths(file_path: Path) -> tuple[Path, Path]:
        """下载中的临时文件与进度文件：<name>.part / <name>.part.json

        为什么不直接写目标文件：旧版把半成品写在目标路径上，于是
        ①失败重试只能从头再来；②**被掐断的半成品会被 `file_path.exists()` 当成
        "已缓存"直接返回**（合并出来的视频是坏的）。用 .part 之后，目标文件只会
        在下载完整后由 `os.replace` 原子出现。
        """
        return (file_path.with_name(file_path.name + ".part"),
                file_path.with_name(file_path.name + ".part.json"))

    @staticmethod
    def _load_state(state_path: Path, size: int) -> dict:
        """读进度文件；损坏/长度不符（换了资源）就当没有 → 返回 {}"""
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if int(data.get("size", -1)) != int(size):
                raise ValueError("size 不匹配")
            mode = str(data.get("mode") or "")
            if mode not in ("single", "parallel"):
                raise ValueError("mode 缺失")
            return {"mode": mode, "size": int(size),
                    "done": {int(k): int(v) for k, v in (data.get("done") or {}).items()}}
        except Exception:
            return {}

    @staticmethod
    def _save_state(state_path: Path, size: int, mode: str, done: dict) -> None:
        """原子写进度（写坏最多让下次从头开始，绝不会写出坏文件）"""
        try:
            tmp = state_path.with_name(state_path.name + ".tmp")
            tmp.write_text(json.dumps({"mode": mode, "size": int(size),
                                       "done": {str(k): int(v) for k, v in done.items()},
                                       "updated": int(time.time())}), encoding="utf-8")
            os.replace(tmp, state_path)
        except Exception:
            pass

    @staticmethod
    async def _discard(part_path: Path, state_path: Path) -> None:
        """丢弃半成品（致命错误、或状态不可信时用）"""
        await safe_unlink(part_path)
        await safe_unlink(state_path)

    @staticmethod
    def _finish(part_path: Path, state_path: Path, file_path: Path) -> Path:
        """下载完整 → 原子改名到目标路径，并清掉进度文件"""
        os.replace(part_path, file_path)
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return file_path

    @staticmethod
    def _reusable(part_path: Path, state_path: Path, size: int, mode: str) -> dict | None:
        """本次要用的方式能复用已有 .part 吗？返回可用的 done（None = 不可信，要重来）

        ⚠️ 关键：**绝不从文件长度推断已下载字节** —— 并行路径是预分配的稀疏文件，
        长度等于总大小，"文件有多大"与"下了多少"完全无关。
        """
        st = Downloader._load_state(state_path, size)
        if not st or st.get("mode") != mode or not part_path.exists():
            return None
        done = st.get("done") or {}
        if mode == "parallel":
            return done if part_path.stat().st_size == size else None
        # 单连接：追加写没有洞，但状态可能比文件落后一点点（异常/崩溃时）
        # → 以较小者为准，续传前把文件截到那个长度（多出来的尾巴一律丢弃，绝不拼错）
        size_on_disk = part_path.stat().st_size
        return {0: min(size_on_disk, int(done.get(0, 0)))}

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """流式下载（带跨运行断点续传）"""
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cfg.cache_dir / file_name
        part_path, state_path = self._part_paths(file_path)
        # 只有**完整**文件才算命中（半成品在 .part 里，不会误判）
        if file_path.exists():
            return file_path
        headers = headers or self.default_headers
        if proxy is ...:
            proxy = None  # aiohttp 不接受 Ellipsis，转 None
        retries = self.cfg.download_retry_times
        max_bytes = self.max_size * 1024 * 1024
        for attempt in range(retries + 1):
            try:
                async with self.client.get(
                    url, headers=headers, allow_redirects=True, proxy=proxy
                ) as response:
                    if response.status >= 400:
                        raise ClientError(f"HTTP {response.status} {response.reason}")
                    content_length = response.content_length

                    if content_length == 0:
                        logger.warning(f"媒体 url: {url}, 大小为 0, 取消下载")
                        raise ZeroSizeException
                    if content_length and content_length > max_bytes:
                        logger.warning(
                            f"媒体 url: {url} 大小 {content_length / 1024 / 1024:.2f} MB 超过 {self.max_size} MB, 取消下载"
                        )
                        raise SizeLimitException

                    # 大文件优先分段（单连接被 CDN 限速/掐断时快一个数量级，
                    # 且断了只补那一段）；服务端不支持 Range 时自动回退单连接。
                    if PARALLEL_CONNECTIONS > 1 and content_length >= MIN_PARALLEL_SIZE:
                        try:
                            await self._parallel_streamd(
                                url, part_path, state_path, headers, proxy,
                                content_length, max_bytes,
                            )
                            return self._finish(part_path, state_path, file_path)
                        except RangeNotSupported:
                            logger.info(f"分段下载不可用（服务端忽略 Range），回退单连接 | {file_name}")
                            # 预分配过的 .part 对单连接没有意义，也不可信 → 丢弃重来
                            await self._discard(part_path, state_path)

                    # 单连接：只有"状态文件的 mode=single 且长度吻合"才续传
                    reusable = self._reusable(part_path, state_path, content_length, "single") \
                        if content_length else None
                    if reusable is None:
                        await self._discard(part_path, state_path)
                        downloaded = await self._write_stream(
                            response, part_path, state_path, content_length,
                            max_bytes, file_name,
                        )
                    else:
                        downloaded = await self._resume_single(
                            url, part_path, state_path, headers, proxy, content_length,
                            max_bytes, file_name, int(reusable.get(0, 0)),
                        )

                    if downloaded == 0:
                        logger.warning(f"媒体 url: {url}, 实际大小为 0, 取消下载")
                        raise ZeroSizeException
                    if content_length and downloaded < content_length:
                        raise ClientError(
                            f"HTTP payload incomplete {downloaded}/{content_length}"
                        )

                return self._finish(part_path, state_path, file_path)
            except (ZeroSizeException, SizeLimitException):
                await self._discard(part_path, state_path)   # 致命错误，半成品没意义
                raise
            except (ClientError, TimeoutError) as exc:
                # aiohttp 可能因 TLS 指纹被 CDN 拒绝（403，如抖音 douyinpic.com）。
                # 尝试 requests 兜底（requests 的 TLS 指纹更接近浏览器），同样支持续传。
                # 若并行路径**已经有进度**（.part.json 里 done 非空），就不要用 requests
                # 兜底了：那条路只能从头下，等于把断点扔掉。这里直接进入下一次重试，
                # 让并行路径自己从断点继续（真的没进度时才用兜底去救 TLS 指纹被拒的情况）。
                st = self._load_state(state_path, content_length) if content_length else {}
                if st.get("mode") == "parallel" and any(int(v) for v in (st.get("done") or {}).values()):
                    logger.warning(f"并行已下部分内容，保留断点重试（不走 requests 兜底）| {file_name}")
                    if attempt < retries:
                        await sleep(1 + attempt)
                        continue
                    raise DownloadException("媒体下载失败") from exc
                try:
                    logger.warning(f"aiohttp 下载中断({exc})，尝试 requests 兜底: {file_name}")
                    downloaded = await to_thread(
                        self._requests_download, url, part_path, state_path,
                        headers, content_length, max_bytes,
                    )
                    if downloaded == 0:
                        raise ZeroSizeException
                    if content_length and downloaded < content_length:
                        raise ClientError(
                            f"HTTP payload incomplete {downloaded}/{content_length}"
                        )
                    return self._finish(part_path, state_path, file_path)
                except (ZeroSizeException, SizeLimitException):
                    await self._discard(part_path, state_path)
                    raise
                except Exception:
                    pass
                # 关键：**保留 .part 与状态**。重试、以及"下一条同样的链接"都从断点继续，
                # 不再像旧版那样整份重来。
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                logger.error(f"下载失败（半成品已留在 {part_path.name}，下次自动续传）"
                             f" | url: {url}")
                raise DownloadException("媒体下载失败") from exc
        raise DownloadException("媒体下载失败")

    @staticmethod
    async def _write_stream(response, part_path: Path, state_path: Path, content_length,
                            max_bytes, file_name: str) -> int:
        """把已打开的响应写进 .part（从头写），并把进度记进状态文件"""
        downloaded = 0
        holder = {"last": 0.0}

        def _mark() -> None:
            now_ = time.monotonic()
            if now_ - holder["last"] >= 1.0:
                holder["last"] = now_
                Downloader._save_state(state_path, content_length or 0, "single", {0: downloaded})

        try:
            with Downloader.get_progress_bar(file_name, content_length) as bar:
                async with aiofiles.open(part_path, "wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise SizeLimitException
                        await file.write(chunk)
                        bar.update(len(chunk))
                        _mark()
        finally:
            # 异常也要落盘：状态必须与磁盘上的真实长度一致，否则下次会被判"不可信"而重来
            if content_length:
                try:
                    real = part_path.stat().st_size
                except OSError:
                    real = downloaded
                Downloader._save_state(state_path, content_length, "single", {0: real})
        return downloaded

    async def _resume_single(self, url, part_path: Path, state_path: Path, headers, proxy,
                             content_length, max_bytes, file_name: str, have: int) -> int:
        """单连接断点续传：Range: bytes=<已有>- 追加；服务端不支持就从头来"""
        if content_length and have >= content_length:
            return have                              # 其实已经下完了（改名失败等情况）
        if have:                                     # 把可能多出来的尾巴截掉，保证"文件长度=进度"
            with open(part_path, "r+b") as f:
                f.truncate(have)
        req_headers = dict(headers)
        req_headers["Range"] = f"bytes={have}-"
        async with self.client.get(url, headers=req_headers,
                                   allow_redirects=True, proxy=proxy) as resp:
            if resp.status == 416:                   # 区间已到末尾
                if content_length and have >= content_length:
                    return have
                raise ClientError("HTTP 416 Requested Range Not Satisfiable")
            if resp.status == 200:                   # 服务端忽略 Range → 只能重来
                logger.info(f"服务端不支持续传，重新下载 | {file_name}")
                return await self._write_stream(resp, part_path, state_path,
                                                content_length, max_bytes, file_name)
            if resp.status >= 400:
                raise ClientError(f"HTTP {resp.status} {resp.reason}")
            logger.info(f"断点续传：从 {have / 1048576:.1f} MB 继续 | {file_name}")
            total = have
            holder = {"last": 0.0}

            def _mark() -> None:
                now_ = time.monotonic()
                if now_ - holder["last"] >= 1.0:
                    holder["last"] = now_
                    Downloader._save_state(state_path, content_length or 0, "single", {0: total})

            try:
                with self.get_progress_bar(file_name, content_length) as bar:
                    bar.update(have)
                    async with aiofiles.open(part_path, "ab") as file:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise SizeLimitException
                            await file.write(chunk)
                            bar.update(len(chunk))
                            _mark()
            finally:
                if content_length:
                    try:
                        real = part_path.stat().st_size
                    except OSError:
                        real = total
                    Downloader._save_state(state_path, content_length, "single", {0: real})
            return total

    @staticmethod
    def _requests_download(url, part_path: Path, state_path: Path, headers, content_length,
                           max_bytes) -> int:
        """requests 兜底下载（TLS 指纹被 CDN 拒时用），同样支持断点续传"""
        import requests as _req

        reusable = Downloader._reusable(part_path, state_path, content_length or 0, "single") \
            if content_length else None
        have = int((reusable or {}).get(0, 0))
        if reusable is None and part_path.exists():
            part_path.unlink(missing_ok=True)        # 状态不可信 → 半成品也丢掉
        req_headers = dict(headers)
        if have:
            req_headers["Range"] = f"bytes={have}-"
        r = _req.get(url, headers=req_headers, timeout=60, stream=True)
        try:
            if have and r.status_code == 200:        # 服务端忽略 Range → 重来
                have = 0
            if r.status_code not in (200, 206):
                raise ClientError(f"HTTP {r.status_code}")
            written = 0
            try:
                with open(part_path, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        f.write(chunk)
                        written += len(chunk)
                        if have + written > max_bytes:
                            raise SizeLimitException
            finally:
                # 异常也要落盘：状态与磁盘长度保持一致，下次才不会"保守重下"
                if content_length:
                    try:
                        real = part_path.stat().st_size
                    except OSError:
                        real = have + written
                    Downloader._save_state(state_path, content_length, "single", {0: real})
            return have + written
        finally:
            r.close()

    async def _fetch_range(self, url, file_path, headers, start, end, proxy, bar,
                           on_progress=None) -> int:
        """把一个字节区间写进文件的对应偏移，返回实际写入字节数。

        连接被掐断时**不抛异常**，而是返回已有进度 —— 让上层从断点续传。
        （这正是单连接版本最缺的能力：它一断就整份重来。）
        """
        req_headers = dict(headers)
        req_headers["Range"] = f"bytes={start}-{end}"
        written = 0
        try:
            async with self.client.get(url, headers=req_headers,
                                       allow_redirects=True, proxy=proxy) as resp:
                if resp.status == 200:
                    raise RangeNotSupported()          # 服务端忽略了 Range
                if resp.status >= 400:
                    raise ClientError(f"HTTP {resp.status} {resp.reason}")
                pos = start
                async with aiofiles.open(file_path, "r+b") as f:
                    await f.seek(start)
                    async for piece in resp.content.iter_chunked(256 * 1024):
                        if pos + len(piece) - 1 > end:      # 服务端多给了 → 截断
                            piece = piece[: end - pos + 1]
                        await f.write(piece)
                        pos += len(piece)
                        written += len(piece)
                        bar.update(len(piece))
                        if on_progress:
                            on_progress(pos)                # 记断点，供跨运行续传
                        if pos > end:
                            break
        except (ClientError, TimeoutError):
            if written:
                return written
            raise
        return written

    async def _download_chunk(self, url, file_path, headers, start, end, offset,
                              proxy, bar, on_progress=None) -> int:
        """下载一个区间：断了就续传，直到拿满或重试用尽（offset = 跨运行的已有进度）"""
        pos = start + max(0, int(offset or 0))
        attempt = 0
        while pos <= end:
            try:
                n = await self._fetch_range(url, file_path, headers, pos, end, proxy, bar,
                                            on_progress=on_progress)
            except RangeNotSupported:
                raise
            except (ClientError, TimeoutError):
                n = 0
            if n <= 0:
                attempt += 1
                if attempt > CHUNK_RETRY:
                    raise ClientError(f"分段 {start}-{end} 续传重试用尽")
                await sleep(min(5, attempt))
                continue
            pos += n
        return pos - start

    async def _parallel_streamd(self, url, part_path, state_path, headers, proxy,
                                size, max_bytes) -> None:
        """多连接 Range 分段下载：预分配 + 段内续传 + **跨运行续传**（进度落 .part.json）"""
        if size > max_bytes:
            raise SizeLimitException
        par = max(1, min(int(PARALLEL_CONNECTIONS), 16))
        chunk = max(CHUNK_MIN, -(-size // par))        # 向上取整
        plan = [(s, min(s + chunk - 1, size - 1)) for s in range(0, size, chunk)]
        if len(plan) <= 1:
            raise RangeNotSupported()

        # 可复用上一轮的进度吗？（mode 必须也是 parallel，且文件长度=总大小=预分配过）
        done = self._reusable(part_path, state_path, size, "parallel")
        if done is None:
            await self._discard(part_path, state_path)
            with open(part_path, "wb") as f:           # 预分配（稀疏）
                f.truncate(size)
            done = {}

        todo = [(s, e, int(done.get(s, 0))) for s, e in plan]
        todo = [(s, e, off) for s, e, off in todo if s + off <= e]
        resumed = sum(off for _s, _e, off in todo)
        holder = {"last": 0.0}

        def _mark(pos: int, s: int) -> None:
            done[s] = pos - s
            now = time.monotonic()
            if now - holder["last"] >= 1.0:            # 最多每秒落一次盘
                holder["last"] = now
                Downloader._save_state(state_path, size, "parallel", done)

        if not todo:
            logger.info(f"分段下载：断点显示已全部完成，直接合并 | {part_path.name}")
        elif resumed:
            logger.info(f"断点续传：已有 {resumed / 1048576:.1f}/{size / 1048576:.1f} MB，"
                        f"补下 {len(todo)} 段 | {part_path.name}")
        else:
            logger.info(f"分段下载 {len(plan)} 路 | {part_path.name} {size / 1048576:.1f} MB")

        with self.get_progress_bar(part_path.name, size) as bar:
            bar.update(resumed)                        # 进度条从断点开始
            results = await gather(
                *[self._download_chunk(url, part_path, headers, s, e, off, proxy, bar,
                                       on_progress=(lambda pos, s=s: _mark(pos, s)))
                  for s, e, off in todo],
                return_exceptions=True,
            )
        self._save_state(state_path, size, "parallel", done)   # 收尾落盘

        for r in results:
            if isinstance(r, RangeNotSupported):
                raise r                                # 让上层回退单连接
            if isinstance(r, BaseException):
                raise ClientError(f"分段下载失败: {r}") from r

        for s, e in plan:                              # 完整性：每段都必须拿满
            if int(done.get(s, 0)) < e - s + 1:
                raise ClientError(f"HTTP payload incomplete 段 {s}-{e} 只到 {done.get(s, 0)}")

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        """获取进度条 bar

        Args:
            desc (str): 描述
            total (int | None): 总大小. Defaults to None.

        Returns:
            tqdm: 进度条
        """
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(
            url, file_name=video_name, headers=headers, proxy=proxy
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url, file_name=audio_name, headers=headers, proxy=proxy
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url, file_name=file_name, headers=headers, proxy=proxy
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(url, file_name=img_name, headers=headers, proxy=proxy)

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> list[Path]:
        paths_or_errs = await gather(
            *[self.download_img(url, headers=headers, proxy=proxy) for url in urls],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> Path:
        """
        download video and audio file by url with stream and merge
        """
        v_path, a_path = await gather(
            self.download_video(v_url, headers=headers, proxy=proxy),
            self.download_audio(a_url, headers=headers, proxy=proxy),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path

    async def ytdlp_extract_info(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")
        # 兜底缺失字段（yt-dlp 在无 JS runtime/网络受限时可能缺 timestamp 等）
        for field, default in (
            ("timestamp", 0),
            ("duration", 0),
            ("thumbnail", ""),
            ("description", ""),
            ("channel_id", ""),
            ("channel", ""),
            ("uploader", ""),
        ):
            if raw.get(field) is None:
                raw[field] = default
        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def ytdlp_extract_raw(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> dict[str, Any]:
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not isinstance(raw, dict):
                raise ParseException("yt-dlp 返回数据异常")
            return raw  # type: ignore

    @auto_task
    async def ytdlp_download_video(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> Path:
        info = await self.ytdlp_extract_info(
            url, cookiefile=cookiefile, headers=headers, proxy=proxy
        )
        if info.duration > self.cfg.max_duration:
            raise DurationLimitException

        video_path = self.cfg.cache_dir / generate_file_name(url, ".mp4")
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(video_path),
            "merge_output_format": "mp4",
            # "format": f"bv[filesize<={info.duration // 10 + 10}M]+ba/b[filesize<={info.duration // 8 + 10}M]",
            # "format": "bv*[height<=720]+ba/b[height<=720]",
            "format": format or "best",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            await to_thread(ydl.download, [url])
        return video_path

    @auto_task
    async def ytdlp_download_video_relaxed(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> Path:
        file_stem = generate_file_name(url)
        video_path = self.cfg.cache_dir / f"{file_stem}.mp4"
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_stem) + ".%(ext)s",
            "merge_output_format": "mp4",
            "format": format or None,
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
            "quiet": True,
            "no_warnings": True,
        }
        if not opts["format"]:
            opts.pop("format")
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            await to_thread(ydl.download, [url])
        if video_path.exists():
            return video_path

        candidates = sorted(self.cfg.cache_dir.glob(f"{file_stem}*.mp4"))
        if candidates:
            return candidates[0]
        raise DownloadException("yt-dlp 视频下载失败")

    @auto_task
    async def ytdlp_download_audio(
        self,
        url: str,
        *,
        cookiefile: Path | None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> Path:
        file_name = generate_file_name(url)
        audio_path = self.cfg.cache_dir / f"{file_name}.flac"
        if audio_path.exists():
            return audio_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_name) + ".%(ext)s",
            "format": format or "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                    "preferredquality": "0",
                }
            ],
            "cookiefile": None,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            await to_thread(ydl.download, [url])
        return audio_path
