"""
视频解析插件 - 检测群内分享的视频链接，解析并发送无水印版本
支持平台：B站、抖音、微博、小红书、快手、知乎、微信视频号、acfun、
YouTube、TikTok、Instagram、Twitter、Iwara、Pixiv、网易云、QQ空间

解析核心：astrbot_plugin_parser 复刻版（video_parser_core）
来源仓库：https://github.com/Zhalslar/astrbot_plugin_parser
本文件为适配 NapCat-WordLibBot 的魔改版（去旧版解析逻辑，仅保留新核心），
video_parser_core/ 目录为该仓库核心代码的移植（含 astrbot 兼容层）。
"""

# ========== 插件元数据（SDK 规范）==========
__plugin_name_cn__ = "视频解析"
__plugin_name_en__ = "video_parser"
__plugin_version__ = "1.0.0"
__plugin_desc__ = "多平台视频/图文解析去水印（B站/抖音/微博/小红书/快手/YouTube等16平台）"
__plugin_author__ = "NapCat-WordLibBot"
# ===========================================

import json
import os
import re
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from utils.api import send_message
from utils.config import get_bot_name, get_bot_qq, get_config
from utils.plugin_toggle import is_enabled as _pt_enabled, set_enabled as _pt_set
from utils.command_registry import CommandRegistry

# ========== 配置 ==========
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "video_parser_config.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "auto_send_video": True,
    "auto_send_images": True,
    "max_images": 50,
    "@_reply": True,
    "show_source": True,
    # 新解析核心（astrbot_plugin_parser 复刻版）参数
    "browser_timeout": 60,            # 下载超时（秒）
    "download_retry_times": 3,        # 下载重试次数
    "download_max_size": 300,         # 下载大小上限（MB）
    "download_max_duration": 3600,    # 视频时长上限（秒）
    "forward_threshold": 4,           # 消息段达到该数量强制合并转发
    "debounce_seconds": 30,           # 解析防抖间隔（秒）
    "show_download_fail_tip": True,   # 下载失败时提示
    "audio_to_file": False,           # 音频以文件形式发送（默认语音）
    "proxy": "",                      # 代理地址（如 http://127.0.0.1:7890）
    "bili_video_quality": "P_720P",   # B站画质：P_360P/P_480P/P_720P/P_1080P
    "bili_video_codec": "AVC",        # B站编码：AVC/HEVC/AV1
}

_CONFIG = {}


def vlog(level, msg):
    """视频解析 分级日志"""
    try:
        from utils.log import plugin_log
        plugin_log("视频解析", level, msg)
    except Exception:
        print(f"[视频解析] {msg}")


def _load_config():
    global _CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _CONFIG = json.load(f)
        except Exception:
            pass  # 解析失败保留旧配置
    else:
        _CONFIG = {}
    for k, v in DEFAULT_CONFIG.items():
        _CONFIG.setdefault(k, v)
    _save_config()


def _save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cfg(key, default=None):
    """读取配置（内存值，SIGUSR1 重载）"""
    return _CONFIG.get(key, default)


def reload_config():
    """Web 端保存配置后由主程序（SIGUSR1）通知调用"""
    _load_config()


_load_config()


# ============ 指令注册表（集中定义，一眼可读）============
registry = CommandRegistry("视频解析")


def _cmd_enable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "video_parser", True)
        send_message(event, "视频解析已在本群开启")
    else:
        _CONFIG["enabled"] = True
        _save_config()
        send_message(event, "视频解析已开启")
    return True


def _cmd_disable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "video_parser", False)
        send_message(event, "视频解析已在本群关闭")
    else:
        _CONFIG["enabled"] = False
        _save_config()
        send_message(event, "视频解析已关闭")
    return True


# 注册指令：名称 / 触发词 / 描述 / 处理函数 / 权限 / 匹配方式
registry.register("开启视频解析", ["开启视频解析"], "开启视频解析（群内=本群，私聊=全局）", _cmd_enable, master_only=True, kind="suffix")
registry.register("关闭视频解析", ["关闭视频解析"], "关闭视频解析（群内=本群，私聊=全局）", _cmd_disable, master_only=True, kind="suffix")

# 同步指令中文名到配置（Web 面板展示可读指令名）
_CONFIG.setdefault("command_labels", {}).update(registry.labels())
_save_config()


# ========== 新解析核心接入（astrbot_plugin_parser 复刻版，16 平台）==========
try:
    from plugins.parser_bridge import configure as _pb_configure, host_to_container, get_cache_dir
    import video_parser_core.main as _vpm

    def _pb_send(event, segs):
        """核心解析结果 → CQ 段 → WS 发送（event 由核心传入）"""
        from plugins.parser_bridge import segs_to_cq
        import utils.api as _api
        try:
            _api.send_message(event, segs_to_cq(segs))
            return True
        except Exception as e:
            vlog("error", f"新核心发送失败: {e}")
            return False

    _pb_configure(_pb_send, host_to_container)

    # 预初始化解析核心（后台线程，避免首次消息卡顿）
    def _preinit_core():
        try:
            _vcfg = os.path.join(DATA_DIR, "video_parser_config.json")
            _vpm.get_plugin(send_fn=_pb_send, container_path_fn=host_to_container,
                            cache_dir=get_cache_dir(), video_config_path=_vcfg)
            vlog("info", "新解析核心已预初始化")
        except Exception as e:
            vlog("error", f"解析核心预初始化失败: {e}")
    threading.Thread(target=_preinit_core, daemon=True).start()
except Exception as e:
    vlog("error", f"新解析核心接入失败: {e}")


def is_master(user_id):
    ml = get_config("MASTER_QQ", [])
    if not isinstance(ml, list):
        ml = [ml]
    return str(user_id) in [str(m) for m in ml]


def handle(event):
    if event.get("post_type") != "message":
        return False

    raw = event.get("raw_message", "").strip()
    uid = event.get("user_id", 0)

    # 主人开关指令（注册表统一分发）
    if registry.dispatch(event, raw, is_master(uid), master_cmds_only=True):
        return True

    if not cfg("enabled", True):
        return False
    if event.get("message_type") != "group":
        return False
    # 分群检查
    if not _pt_enabled(event.get("group_id"), "video_parser"):
        return False
    if not raw:
        return False

    # 新解析核心：匹配链接并解析发送（16 平台，含 B站卡片/动态/专栏）
    try:
        from plugins.parser_bridge import handle_parse
        result = handle_parse(event)
        # 2=成功(已发送) 1=未匹配链接 0=解析失败
        if result == 2:
            return True
        if result == 0:
            vlog("warn", "解析失败")
            send_message(event, "⚠️ 视频解析失败，请稍后再试")
            return True
    except Exception as _e:
        vlog("error", f"解析核心异常: {_e}")

    # 未匹配到支持的链接
    return False
