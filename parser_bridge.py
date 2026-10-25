# parser_bridge.py — 新解析核心接入层
# 供 video_parser.py 的 handle() 调用：卡片提取 → astrbot_plugin_parser 核心解析 → CQ 段发送

import asyncio
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from video_parser_core.sender import _setup_sender, segs_to_cq
from video_parser_core.utils import extract_json_url
import video_parser_core.main as vpm

# NapCat 共享目录映射（宿主机路径 → 容器路径），由 video_parser.py 注入
_container_fn = None
_send_fn = None


def configure(send_fn, container_path_fn, react_fn=None):
    """注入 WS 发送函数、容器路径映射（可选：解析开始回调）"""
    global _send_fn, _container_fn
    _send_fn = send_fn
    _container_fn = container_path_fn
    _setup_sender(send_fn, container_path_fn)
    if react_fn is not None:
        configure_react(react_fn)


def configure_timeout(seconds):
    """把宿主配置里的"解析总时长上限"转给核心（慢速 CDN 上必须放宽）"""
    try:
        vpm.set_parse_timeout(seconds)
    except Exception:
        pass


def configure_parallel(connections):
    """把宿主配置里的"分段下载路数"转给核心（1 = 关闭）"""
    try:
        vpm.set_parallel_connections(connections)
    except Exception:
        pass


def configure_react(react_fn):
    """注入"解析开始"回调：核心确认命中受支持链接后立即调用（早于下载）"""
    vpm._setup_react(react_fn)


def _is_windows() -> bool:
    """本机是否 Windows（独立函数便于测试替换）"""
    return os.name == "nt"


def _napcat_cache_root() -> str:
    """NapCat 图片缓存的「宿主机目录」。

    解析顺序（既保证 Linux 线上行为一字不变，也保证 Windows 落在对的地方）：
      1. 账号 config.json 的 NAPCAT_CACHE_HOST —— 显式配置，也是兼容层
         astercore/compat/paths.py 用的同一个键，单一事实来源
      2. Linux + Docker 时代的老规则（/root/napcat/cache/images 等），
         但**仅在不跑 Windows 且该目录真实存在时**才采用（线上现状）
      3. ~/napcat/cache/images —— Windows 原生 NapCat 的默认目录，
         也是框架启动时会自动建好的目录

    为什么必须区分：Windows 上 "/root/napcat/..." 会被当成本盘根目录，
    os.makedirs 照样成功，文件实际落在 C:\\root\\napcat\\...；而发送时
    host_to_container() 会把它映射成 /app/cache/images/...，兼容层再映射成
    %USERPROFILE%\\napcat\\cache\\images\\... —— 两边不是同一个目录，
    于是「解析成功、下载成功、就是发不出去」。
    """
    from utils.config import get_config
    explicit = str(get_config("NAPCAT_CACHE_HOST", "") or "").strip()
    if explicit:
        return explicit
    qq = str(get_config("BOT_QQ", 0))
    legacy = "/root/napcat/cache/images" if qq == "740979632" else (
        "/root/napcat2/cache/images" if qq == "2551736206" else "")
    if legacy and not _is_windows() and os.path.isdir(legacy):
        return legacy
    return os.path.join(os.path.expanduser("~"), "napcat", "cache", "images")


def get_cache_dir():
    """返回核心缓存目录（NapCat 共享目录下）"""
    d = os.path.join(_napcat_cache_root(), "parser_cache")
    os.makedirs(d, exist_ok=True)
    return d


def host_to_container(path: str) -> str:
    """宿主机路径 → NapCat 容器内路径

    先按**本实例解析出的**缓存目录映射一次（NAPCAT_CACHE_HOST 优先）。
    老实例映射结果与下面写死的两条完全一致（依星 /root/napcat、羽笙 /root/napcat2），
    但新实例（如 /root/napcat3）**不能**再加一条写死分支 —— 否则解析核心写盘的
    宿主机目录与发给 NapCat 的容器路径对不上，表现就是「解析成功、下载成功、
    就是发不出去」（与 Windows 那次同一个坑）。Windows 原生 NapCat 无容器，
    仍走老逻辑（返回本机路径）。
    """
    if not _is_windows():
        try:
            root = _napcat_cache_root().rstrip("/")
            if root and (path == root or path.startswith(root + "/")):
                return "/app/cache/images" + path[len(root):]
        except Exception:
            pass
    for host_root, container_root in (
        ("/root/napcat2/cache/images", "/app/cache/images"),
        ("/root/napcat/cache/images", "/app/cache/images"),
    ):
        if path.startswith(host_root):
            return path.replace(host_root, container_root, 1)
    return path


def extract_text_from_event(event) -> str:
    """从事件提取可解析文本（raw + JSON 卡片）"""
    text = event.get("raw_message", "") or ""
    for seg in event.get("message", []) or []:
        if seg.get("type") == "json":
            url = extract_json_url(seg.get("data", {}))
            if url:
                return url
    return text


def handle_parse(event: dict):
    """同步入口：解析并发送。
    返回：
      2 = 新核心成功处理（已发送）
      1 = 新核心未匹配链接（继续走旧逻辑，不提示）
      0 = 新核心解析失败（需提示后转旧逻辑）
    """
    text = extract_text_from_event(event)
    if not text:
        return 1
    try:
        result = vpm.handle_event_sync(event, send_fn=_send_fn, container_path_fn=_container_fn,
                                       cache_dir=get_cache_dir())
        if result == "fallback":
            return 0  # 新核心解析失败 → 转旧逻辑
        return 2 if result else 1
    except Exception as e:
        import traceback
        print(f"[解析器] 新核心解析失败: {e}")
        traceback.print_exc()
        return 0
