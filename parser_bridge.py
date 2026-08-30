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


def configure(send_fn, container_path_fn):
    """注入 WS 发送函数和容器路径映射"""
    global _send_fn, _container_fn
    _send_fn = send_fn
    _container_fn = container_path_fn
    _setup_sender(send_fn, container_path_fn)


def get_cache_dir():
    """返回核心缓存目录（NapCat 共享目录下）"""
    from utils.config import get_config
    qq = str(get_config("BOT_QQ", 0))
    if qq == "740979632":
        host = "/root/napcat/cache/images"
    elif qq == "2551736206":
        host = "/root/napcat2/cache/images"
    else:
        host = os.path.expanduser("~/napcat/cache/images")
    d = os.path.join(host, "parser_cache")
    os.makedirs(d, exist_ok=True)
    return d


def host_to_container(path: str) -> str:
    """宿主机路径 → NapCat 容器内路径"""
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
        result = vpm.handle_event_sync(event, send_fn=_send_fn, container_path_fn=_container_fn)
        if result == "fallback":
            return 0  # 新核心解析失败 → 转旧逻辑
        return 2 if result else 1
    except Exception as e:
        import traceback
        print(f"[解析器] 新核心解析失败: {e}")
        traceback.print_exc()
        return 0
