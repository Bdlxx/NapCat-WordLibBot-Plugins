"""
JM 本子下载插件
基于 jmcomic (https://github.com/hect0x7/JMComic-Crawler-Python) 实现：
- 群内发送「jm <本子id>」下载禁漫本子，自动合并为 PDF 分享
  （支持多本: jm 123 456；单章: jm 123 p456）
- 「jm详情 <id>」仅查看本子元信息，不下载
- 自动更新 jmcomic 库（禁漫反爬频繁变化，需保持库最新）
- 下载在独立子进程（jm_worker.py）中执行，库更新即时生效、异常不拖垮主程序

开关：
- 「开启jm下载」/「关闭jm下载」仅主人可用
  群内发送 = 本群开关；私聊发送 = 全局总开关（settings.enabled）
"""

# ========== 插件元数据（SDK 规范）==========
__plugin_name_cn__ = "JM下载"
__plugin_name_en__ = "jm_downloader"
__plugin_version__ = "1.0.0"
__plugin_desc__ = "jm命令下载禁漫本子并转PDF分享，自动更新jmcomic库"
__plugin_author__ = "NapCat-WordLibBot"

# ============================================================================
# 可配置项声明（面板据此**自动生成表单**，见 web/config_schema.py）
#   · 字段名 → 控件类型；path 是值在配置文件里的位置（settings.foo / commands.bar）
#   · label/help/group 是面板上直接显示的中文；advanced 折叠进「高级选项」
#   · 纯元数据，不改变插件任何行为；未声明的插件面板会按当前值自动推断
# ============================================================================

__config_schema__ = {
    # ---------------- 指令关键词（commands.*）----------------
    "cmd_download": {
        "type": "text", "label": "下载本子", "group": "指令",
        "path": "commands.download", "default": "jm",
        "help": "触发词：前缀匹配，后面必须跟数字/本子 ID（如「jm 123456」「jm 123 p456」）",
    },
    "cmd_detail": {
        "type": "text", "label": "查看本子详情", "group": "指令",
        "path": "commands.detail", "default": "jm详情",
        "help": "触发词：前缀匹配，仅查看元信息不下载（如「jm详情 123456」）",
    },
    "cmd_enable": {
        "type": "text", "label": "开启JM下载", "group": "指令",
        "path": "commands.enable", "default": "开启jm下载",
        "help": "触发词：后缀匹配，仅主人可用；群内=开本群，私聊=开全局",
    },
    "cmd_disable": {
        "type": "text", "label": "关闭JM下载", "group": "指令",
        "path": "commands.disable", "default": "关闭jm下载",
        "help": "触发词：后缀匹配，仅主人可用；群内=关本群，私聊=关全局",
    },

    # ---------------- 参数（settings.*）----------------
    "setting_enabled": {
        "type": "bool", "label": "插件总开关", "group": "参数",
        "path": "settings.enabled",
        "help": "全局总开关，关闭后所有群都不再响应（分群开关见「群组管理」）",
        "default": True,
    },
    "setting_send_pdf": {
        "type": "bool", "label": "发送 PDF", "group": "参数",
        "path": "settings.send_pdf",
        "help": "下载完成后把本子转成 PDF 发给群",
        "default": True,
    },
    "setting_delete_after_send": {
        "type": "bool", "label": "发送后清理文件", "group": "参数",
        "path": "settings.delete_after_send",
        "help": "发送成功后删除本地下载的图片/PDF",
        "default": True,
    },
    "setting_auto_update": {
        "type": "bool", "label": "自动更新 jmcomic", "group": "参数",
        "path": "settings.auto_update",
        "help": "后台定时检查并升级实例 venv 里的 jmcomic 库",
        "default": True,
    },
    "setting_recall_enabled": {
        "type": "bool", "label": "消息自动撤回", "group": "参数",
        "path": "settings.recall_enabled",
        "help": "开启后本插件发出的消息按下面的时间自动撤回（需 NapCat HTTP 可用）",
        "default": True,
    },
    "setting_recall_notice": {
        "type": "bool", "label": "撤回前提醒", "group": "参数",
        "path": "settings.recall_notice",
        "help": "是否先发一条「将在 N 秒后撤回」的前置提醒",
        "default": True,
    },
    "setting_recall_prefix_seconds": {
        "type": "number", "label": "前置消息撤回时间", "group": "参数",
        "path": "settings.recall_prefix_seconds", "min": 5, "max": 3600, "step": 1, "unit": "秒",
        "help": "查询/进度等前置消息的撤回延时",
        "default": 30,
    },
    "setting_recall_pdf_seconds": {
        "type": "number", "label": "PDF 提醒撤回时间", "group": "参数",
        "path": "settings.recall_pdf_seconds", "min": 5, "max": 3600, "step": 1, "unit": "秒",
        "help": "「PDF 已发送」这类提醒消息的撤回延时",
        "default": 60,
    },
    "setting_recall_file_seconds": {
        "type": "number", "label": "PDF 文件撤回时间", "group": "参数",
        "path": "settings.recall_file_seconds", "min": 5, "max": 86400, "step": 5, "unit": "秒",
        "help": "PDF 文件消息的撤回延时（上传耗时较长，通常比提醒宽松）",
        "default": 300,
    },
    "setting_debounce_seconds": {
        "type": "number", "label": "下载防抖间隔", "group": "参数",
        "path": "settings.debounce_seconds", "min": 0, "max": 3600, "step": 1, "unit": "秒",
        "help": "同会话相同链接/本子 ID 在该时间内不重复响应，0=关闭防抖",
        "default": 30,
    },
    "setting_update_interval_hours": {
        "type": "number", "label": "更新检查间隔", "group": "参数", "advanced": True,
        "path": "settings.update_interval_hours", "min": 1, "max": 720, "step": 1, "unit": "小时",
        "help": "自动更新 jmcomic 的检查周期",
        "default": 24,
    },
    "setting_task_timeout_seconds": {
        "type": "number", "label": "任务超时", "group": "参数", "advanced": True,
        "path": "settings.task_timeout_seconds", "min": 60, "max": 86400, "step": 60, "unit": "秒",
        "help": "单次下载子进程的最长存活时间，超时会被杀掉",
        "default": 1800,
    },
    "setting_cleanup_delay_seconds": {
        "type": "number", "label": "清理延时", "group": "参数", "advanced": True,
        "path": "settings.cleanup_delay_seconds", "min": 0, "max": 7200, "step": 10, "unit": "秒",
        "help": "发送文件后延迟多久再删除本地文件（NapCat 是异步读文件，不能立即删；Windows 建议加大）",
        "default": 600,
    },
    "setting_concurrent_image": {
        "type": "number", "label": "并发下载图片数", "group": "参数", "advanced": True,
        "path": "settings.concurrent_image", "min": 1, "max": 100, "step": 1, "unit": "线程",
        "help": "传给 jmcomic 的单章节内图片并发数，过大易被站点限流",
        "default": 20,
    },
    "setting_concurrent_photo": {
        "type": "number", "label": "并发下载章节数", "group": "参数", "advanced": True,
        "path": "settings.concurrent_photo", "min": 1, "max": 100, "step": 1, "unit": "线程",
        "help": "同时下载的章节数",
        "default": 4,
    },
    "setting_download_dir": {
        "type": "text", "label": "下载目录", "group": "参数", "advanced": True,
        "path": "settings.download_dir",
        "help": "相对实例 data/ 的图片下载目录名（PDF 生成后临时放在 data/jm_pdf/）",
        "default": "jm_downloads",
    },
    "setting_recall_seconds": {
        "type": "number", "label": "前置消息撤回时间（兜底）", "group": "参数", "advanced": True,
        "path": "settings.recall_seconds", "min": 5, "max": 3600, "step": 1, "unit": "秒",
        "help": "仅在 recall_prefix_seconds 缺失时作为兜底；正常配置下不生效",
        "default": 30,
    },

    # ---------------- 回复模板（messages.*）----------------
    "msg_usage": {
        "type": "textarea", "label": "用法提示", "group": "回复模板",
        "path": "messages.usage", "rows": 5,
        "help": "只发「jm」不带 ID 时显示；无占位符，支持换行",
        "default": "📖 用法：\njm <本子id> 下载整个本子，例：jm 123456\njm 123 456 多本下载\njm 123 p456 只下载某章节\njm详情 123456 查看本子信息（不下载）",
    },
    "msg_busy": {
        "type": "text", "label": "任务进行中", "group": "回复模板",
        "path": "messages.busy", "help": "同一群已有任务时的提示；无占位符",
        "default": "⏳ 本群已有 JM 下载任务进行中，请稍后再试",
    },
    "msg_querying": {
        "type": "text", "label": "查询中", "group": "回复模板",
        "path": "messages.querying",
        "help": "占位符：{ids} 本次要处理的 ID 列表（花括号写法，勿改成方括号）",
        "default": "⏳ 正在查询 JM {ids} 的详情...",
    },
    "msg_no_pdf": {
        "type": "text", "label": "未生成 PDF", "group": "回复模板",
        "path": "messages.no_pdf", "help": "无占位符",
        "default": "⚠️ 下载完成但未生成 PDF 文件",
    },
    "msg_send_fail": {
        "type": "text", "label": "PDF 发送失败", "group": "回复模板",
        "path": "messages.send_fail",
        "help": "无占位符（插件会在其后自动追加一行本地文件路径）",
        "default": "⚠️ PDF 发送失败：无法复制到发送缓存目录",
    },
    "msg_fail": {
        "type": "text", "label": "下载失败", "group": "回复模板",
        "path": "messages.fail", "help": "占位符：{err} 失败原因",
        "default": "❌ JM 下载失败：{err}",
    },
    "msg_timeout": {
        "type": "text", "label": "下载超时", "group": "回复模板",
        "path": "messages.timeout", "help": "无占位符",
        "default": "⏰ 下载超时已终止，请稍后重试",
    },
    "msg_pdf_header": {
        "type": "textarea", "label": "PDF 分享说明", "group": "回复模板",
        "path": "messages.pdf_header", "rows": 3,
        "help": "占位符：{titles} 本子标题（顿号连接）；支持换行",
        "default": "📚 《{titles}》PDF 分享：\n文件较大，发送需要一点时间，请稍候…",
    },
    "msg_detail_header": {
        "type": "textarea", "label": "本子详情模板", "group": "回复模板",
        "path": "messages.detail_header", "rows": 7,
        "help": "占位符：{title} {album_id} {authors} {episodes} {pages} {tags} {update_date}；支持换行",
        "default": "📖 《{title}》\n🆔 JM{album_id}\n✍️ 作者：{authors}\n📄 章节数：{episodes}{pages}\n🏷️ 标签：{tags}\n🕒 更新：{update_date}",
    },
    "msg_recall_notice": {
        "type": "text", "label": "撤回提醒", "group": "回复模板",
        "path": "messages.recall_notice",
        "help": "占位符：{prefix} 前置消息撤回秒数、{pdf} PDF 提醒撤回秒数",
        "default": "🔇 本次 JM 下载的前置消息将在 {prefix} 秒后自动撤回，PDF 文件将在 {pdf} 秒后撤回",
    },
    "msg_recall_pdf_notice": {
        "type": "text", "label": "PDF 撤回提醒", "group": "回复模板",
        "path": "messages.recall_pdf_notice", "help": "占位符：{seconds} 秒后撤回",
        "default": "📄 PDF 已发送完成，将在 {seconds} 秒后自动撤回",
    },

    # ---------------- 元数据（自动生成/只读）----------------
    "command_labels": {
        "type": "json", "label": "指令标签映射", "group": "其它",
        "path": "command_labels", "advanced": True, "readonly": True, "rows": 6,
        "help": "插件启动时自动写入的「触发词 → 指令中文名」映射（含未暴露为配置的「jm更新」），只读",
        "default": {"jm": "下载本子", "开启jm下载": "开启JM下载", "关闭jm下载": "关闭JM下载", "jm更新": "手动更新jmcomic", "jm详情": "查看本子详情"},
    },
}

# ===========================================

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import requests

from utils.api import send_message
from utils.config import get_config
from utils.plugin_toggle import is_enabled as _pt_enabled, set_enabled as _pt_set
from utils.command_registry import CommandRegistry

# ========== 路径 ==========
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PLUGIN_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "jm_downloader_config.json")

# ========== 默认配置 ==========
DEFAULT_CONFIG = {
    "commands": {
        "download": "jm",          # 下载指令: jm 123456
        "detail": "jm详情",         # 详情指令: jm详情 123456
        "enable": "开启jm下载",      # 开启指令
        "disable": "关闭jm下载",     # 关闭指令
    },
    "settings": {
        "enabled": True,            # 全局总开关（主人私聊控制 / Web面板）
        "auto_update": True,        # 自动更新 jmcomic 库
        "update_interval_hours": 24,  # 自动更新检查间隔（小时）
        "send_pdf": True,           # 下载完成后发送 PDF
        "delete_after_send": True,  # 发送后清理本地下载文件
        "concurrent_image": 20,     # 同时下载图片数
        "concurrent_photo": 4,      # 同时下载章节数
        "download_dir": "jm_downloads",  # 图片下载目录（相对 data/）
        "task_timeout_seconds": 1800,    # 单次下载超时（秒）
        "cleanup_delay_seconds": 600,    # 发送文件后延迟清理的时间（秒）。NapCat 是异步读取文件，不能立即删除
        "recall_enabled": False,         # 发送的消息自动撤回（后台开关）
        "recall_seconds": 30,            # 前置消息撤回时间（秒）
        "recall_prefix_seconds": 30,     # 前置消息（查询/详情/进度等）撤回时间（秒）
        "recall_pdf_seconds": 60,        # PDF 完成提醒撤回时间（秒）
        "recall_file_seconds": 300,      # PDF 文件消息撤回时间（秒）：文件发送/上传耗时较长，撤回窗口放宽到 5 分钟
        "recall_notice": False,          # 前置消息撤回提醒（默认关闭，仅PDF完成后提醒）
        "debounce_seconds": 30,          # 同会话相同链接/本子ID 防抖间隔（秒），0=关闭
    },
    "messages": {
        "usage": "📖 用法：\njm <本子id> 下载整个本子，例：jm 123456\njm 123 456 多本下载\njm 123 p456 只下载某章节\njm详情 123456 查看本子信息（不下载）",
        "busy": "⏳ 本群已有 JM 下载任务进行中，请稍后再试",
        "querying": "⏳ 正在查询 JM {ids} 的详情...",
        "no_pdf": "⚠️ 下载完成但未生成 PDF 文件",
        "send_fail": "⚠️ PDF 发送失败：无法复制到发送缓存目录",
        "fail": "❌ JM 下载失败：{err}",
        "timeout": "⏰ 下载超时已终止，请稍后重试",
        "pdf_header": "📚 《{titles}》PDF 分享：\n文件较大，发送需要一点时间，请稍候…",
        "detail_header": "📖 《{title}》\n🆔 JM{album_id}\n✍️ 作者：{authors}\n📄 章节数：{episodes}{pages}\n🏷️ 标签：{tags}\n🕒 更新：{update_date}",
        "recall_notice": "🔇 本次 JM 下载的前置消息将在 {prefix} 秒后自动撤回，PDF 文件将在 {pdf} 秒后撤回",
        "recall_pdf_notice": "📄 PDF 已发送完成，将在 {seconds} 秒后自动撤回",
    },
}

_CONFIG = {}
_active_tasks = {}   # group_id -> {ts, detail_only}
_task_lock = threading.Lock()


# ========== 配置读写（热更新）==========
def _load_config():
    global _CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _CONFIG = json.load(f)
        except Exception:
            pass  # 解析失败（如写文件竞态）保留旧配置，下轮重试
    else:
        _CONFIG = {}
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            _CONFIG.setdefault(k, {})
            for kk, vv in v.items():
                _CONFIG[k].setdefault(kk, vv)
        else:
            _CONFIG.setdefault(k, v)
    _save_config()


def _save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[JM下载] 配置保存失败: {e}")


_CONFIG_MTIME = 0


def _ensure_fresh():
    """配置热更新：配置文件变化时自动重新加载（无需重启 Bot）"""
    global _CONFIG, _CONFIG_MTIME
    try:
        mtime = os.stat(CONFIG_FILE).st_mtime_ns
        if mtime != _CONFIG_MTIME:
            _CONFIG_MTIME = mtime
            _load_config()
    except Exception:
        pass


def cfg(key, default=None):
    """读取设置：直接用内存值（后台定时器负责热更新，处理消息零开销）"""
    if "settings" in _CONFIG and key in _CONFIG["settings"]:
        return _CONFIG["settings"].get(key, default)
    return _CONFIG.get(key, default)


def cmd(key, default=None):
    return _CONFIG.get("commands", {}).get(key, default) or default


def _l(key, default=""):
    return _CONFIG.get("messages", {}).get(key, default) or default


def is_master(user_id):
    ml = get_config("MASTER_QQ", [])
    if not isinstance(ml, list):
        ml = [ml]
    return str(user_id) in [str(m) for m in ml]


def _admin_qqs():
    """全插件通用的管理员（面板「设置」→ 管理员 QQ → config.json 的 ADMIN_QQ）"""
    try:
        raw = get_config("ADMIN_QQ", []) or []
    except Exception:
        raw = []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    return {str(x).strip() for x in raw if str(x).strip()}


def is_admin(user_id):
    return str(user_id) in _admin_qqs()


def is_privileged(user_id):
    """主人或管理员（`jm更新` 仍为主人专属）"""
    return is_master(user_id) or is_admin(user_id)


# ========== 子进程环境 ==========
def _venv_python():
    """JM 子进程要用的解释器（jmcomic 装在实例 venv 里）。

    探测顺序（两个平台都能命中，Linux 行为一字不变）：
      1. <实例>/venv/Scripts/python.exe  —— Windows venv 的真实位置
      2. <实例>/venv/bin/python          —— Linux venv；也是兼容层为 Windows 放的副本
      3. <实例>/venv/bin/python.exe      —— 兼容层放的 Windows 副本
    都没有才退回 sys.executable。但**冻结运行（PyInstaller）时禁止退回**：
    那时 sys.executable 是主程序自己，拿它跑 `-c` / `-m pip` 等于用本程序的参数集
    再启动一份自己（会莫名多开一个启动器/面板）。宁可不下载，也不能自我重启。
    """
    for rel in (("venv", "Scripts", "python.exe"),
                ("venv", "bin", "python"),
                ("venv", "bin", "python.exe")):
        cand = os.path.join(BASE_DIR, *rel)
        if os.path.exists(cand):
            return cand
    if getattr(sys, "frozen", False):
        return None
    return sys.executable


def _venv_python_or_raise():
    """拿到解释器，拿不到就给一句人能看懂的话（而不是莫名其妙的多开一个窗口）"""
    py = _venv_python()
    if not py:
        raise RuntimeError(
            "找不到可用的 Python 解释器：请在 <实例目录>/venv 里装好 jmcomic"
            "（冻结版不会拿主程序自身当解释器）")
    return py


def _worker_path():
    return os.path.join(PLUGIN_DIR, "jm_worker.py")


# ========== option 配置生成 ==========
def _write_option(yml_path, dl_dir, pdf_dir, mode):
    """生成 jmcomic option 配置文件
    mode: album → after_album 整本合成一个PDF; photo → after_photo 每章一个PDF
    """
    plugin_hook = "after_photo" if mode == "photo" else "after_album"
    filename_rule = "Pid" if mode == "photo" else "Aid"
    content = f"""log: false
client:
  impl: api
dir_rule:
  base_dir: {dl_dir}
  rule: Bd/Aid/Pindex
download:
  threading:
    image: {int(cfg('concurrent_image', 20))}
    photo: {int(cfg('concurrent_photo', 4))}
plugins:
  {plugin_hook}:
    - plugin: img2pdf
      kwargs:
        pdf_dir: {pdf_dir}
        filename_rule: {filename_rule}
        delete_original_file: true
"""
    with open(yml_path, "w", encoding="utf-8") as f:
        f.write(content)


# ========== 消息撤回 ==========
def _http_send_msg(event, message, timeout=30):
    """通过 NapCat HTTP 发送消息并返回 message_id（用于撤回/确认真实发送成功）。
    timeout 默认 30s：发送文件等大消息时 NapCat 需读取文件并上传，10s 可能不够。
    access_token 放 query 参数（NapCat HTTP 认证在 query/header，body 不识别）；
    消息体用 POST JSON（文件消息段用 GET 传参会报「未知的消息类型：undefined」）"""
    try:
        import requests as _req
        url = str(get_config("NAPCAT_HTTP", "http://127.0.0.1:3000")).rstrip("/")
        token = get_config("ACCESS_TOKEN", "")
        msg_type = event.get("message_type")
        if msg_type == "group":
            action, params = "send_group_msg", {"group_id": event.get("group_id")}
        else:
            action, params = "send_private_msg", {"user_id": event.get("user_id")}
        params["message"] = message
        q = f"?access_token={token}" if token else ""
        r = _req.post(f"{url}/{action}{q}", json=params, timeout=timeout)
        data = r.json()
        if data.get("status") == "ok" and data.get("data"):
            return data["data"].get("message_id")
        print(f"[JM下载] HTTP发送失败: {data}")
    except Exception as e:
        print(f"[JM下载] HTTP发送异常: {e}")
    return None


def _schedule_recall(message_id, seconds):
    """定时撤回单条消息（HTTP delete_msg，access_token 走 query 参数）"""
    def _do_recall():
        try:
            import requests as _req
            url = str(get_config("NAPCAT_HTTP", "http://127.0.0.1:3000")).rstrip("/")
            token = get_config("ACCESS_TOKEN", "")
            r = _req.get(f"{url}/delete_msg",
                         params={"message_id": message_id, "access_token": token}, timeout=15)
            data = r.json()
            if data.get("status") == "ok":
                print(f"[JM下载] 已撤回消息 {message_id}")
            else:
                print(f"[JM下载] 撤回失败 {message_id}: {data.get('message','')[:80]}")
        except Exception as e:
            print(f"[JM下载] 撤回失败 {message_id}: {e}")
    t = threading.Timer(max(1, int(seconds)), _do_recall)
    t.daemon = True
    t.start()


def _recall_send(event, message, recall_list=None, seconds=None):
    """统一发送入口：
    - 开启撤回：HTTP 发送拿 message_id 定时撤回；HTTP 失败退回 WS（不撤回）
    - 未开启：走 WS 发送
    - seconds: 指定撤回时间；None 时用前置消息时间（recall_prefix_seconds）
    """
    if cfg("recall_enabled", False):
        mid = _http_send_msg(event, message)
        if mid is not None:
            if recall_list is not None:
                recall_list.append(mid)
            secs = seconds if seconds is not None else cfg("recall_prefix_seconds", cfg("recall_seconds", 30))
            _schedule_recall(mid, secs)
            return mid
        # HTTP 失败退回 WS
        send_message(event, message)
        return None
    else:
        send_message(event, message)
        return None


# ========== 防抖（同会话相同链接/本子ID 间隔内不重复响应）==========
class _Debouncer:
    """会话级防抖器：link 防抖 + 资源（本子ID）防抖"""

    def __init__(self):
        self._cache: dict[str, dict[str, float]] = {}  # {session: {key: ts}}
        self._lock = threading.Lock()

    def _interval(self) -> float:
        try:
            return float(cfg("debounce_seconds", 30))
        except (TypeError, ValueError):
            return 30

    def _hit(self, session: str, key: str) -> bool:
        interval = self._interval()
        if interval <= 0:
            return False
        now = time.time()
        with self._lock:
            bucket = self._cache.setdefault(session, {})
            # 清理过期
            expire = now - interval
            for k, ts in list(bucket.items()):
                if ts < expire:
                    bucket.pop(k, None)
            # 命中判断
            if key in bucket:
                return True
            bucket[key] = now
            return False

    def hit_link(self, session: str, link: str) -> bool:
        """基于完整下载指令的防抖（jm <ids> 相同请求）"""
        return self._hit(session, f"link:{link}")

    def hit_user_link(self, user: str, link: str) -> bool:
        """同一用户相同下载指令防抖：用户重复发送相同内容时仅解析一次，
        不影响其他用户发送相同请求"""
        return self._hit(f"user:{user}", f"link:{link}")

    def hit_resource(self, session: str, resource_id: str) -> bool:
        """基于本子 ID 的防抖（同一本子不重复下载）"""
        return self._hit(session, f"res:{resource_id}")


_debouncer = _Debouncer()


def _session_key(event) -> str:
    """会话标识：群=群号，私聊=用户ID"""
    return str(event.get("group_id") or event.get("user_id") or 0)


# ========== 任务调度 ==========
def _start_task(event, ids, detail_only=False):
    group_id = str(event.get("group_id") or event.get("user_id") or 0)
    session = _session_key(event)

    # 同用户相同指令防抖：同一用户连续发送相同 jm 请求时仅解析一次
    # （不同用户发相同内容不受影响，各自正常解析）
    link = f"{'详情' if detail_only else '下载'} {','.join(sorted(ids))}"
    user = str(event.get("user_id") or 0)
    if _debouncer.hit_user_link(user, link):
        print(f"[JM下载] 同用户重复指令防抖: {link}")
        return

    # 资源防抖：任一本子 ID 在会话内已请求过，跳过本次（避免同会话重复下载）
    if not detail_only:
        for i in ids:
            if _debouncer.hit_resource(session, i):
                print(f"[JM下载] 本子 {i} 防抖中，跳过重复下载")
                return

    with _task_lock:
        if group_id in _active_tasks:
            send_message(event, _l("busy"))
            return
        _active_tasks[group_id] = {"ts": time.time(), "detail_only": detail_only}
    t = threading.Thread(target=_run_task, args=(event, list(ids), detail_only), daemon=True)
    t.start()


def _run_task(event, ids, detail_only):
    group_id = str(event.get("group_id") or event.get("user_id") or 0)
    proc = None
    yml_path = result_path = None
    recall_list = []  # 本任务发送的消息 id（用于整套撤回）
    try:
        # 前置消息撤回提醒（默认关闭；开启时仅发一次，PDF 完成提醒始终保留）
        if cfg("recall_enabled", False) and cfg("recall_notice", False):
            _recall_send(event, _l("recall_notice").format(
                prefix=int(cfg("recall_prefix_seconds", 30)),
                pdf=int(cfg("recall_pdf_seconds", 60))), recall_list)
        _recall_send(event, _l("querying").format(ids="、".join(ids)), recall_list)
        print(f"[JM下载] 任务开始 group={group_id} ids={ids} detail_only={detail_only}")

        task_id = f"{int(time.time())}_{os.getpid()}"
        yml_path = os.path.join(DATA_DIR, f"jm_option_{task_id}.yml")
        result_path = os.path.join(DATA_DIR, f"jm_result_{task_id}.json")

        # 每个任务独立的下载/PDF目录，避免多群并发时互相删除文件
        dl_name = str(cfg("download_dir", "jm_downloads")).strip() or "jm_downloads"
        dl_dir = os.path.join(DATA_DIR, dl_name, task_id)
        pdf_dir = os.path.join(DATA_DIR, "jm_pdf", task_id)
        os.makedirs(dl_dir, exist_ok=True)
        os.makedirs(pdf_dir, exist_ok=True)

        mode = "photo" if any("p" in i.lower() for i in ids) else "album"
        _write_option(yml_path, dl_dir, pdf_dir, mode)

        cmd_list = [_venv_python_or_raise(), _worker_path(),
                    "--option", yml_path, "--result", result_path,
                    "--ids"] + ids
        if detail_only:
            cmd_list.append("--detail-only")

        proc = subprocess.Popen(cmd_list,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)

        # 读取子进程输出，转发进度
        stop_read = {"flag": False}

        def _read_output():
            for line in proc.stdout:
                if stop_read["flag"]:
                    break
                line = line.strip()
                if not line.startswith("[JM_PROGRESS]"):
                    continue
                try:
                    data = json.loads(line[len("[JM_PROGRESS]"):].strip())
                except Exception:
                    continue
                msg = _progress_to_text(data)
                if msg:
                    _recall_send(event, msg, recall_list)

        rt = threading.Thread(target=_read_output, daemon=True)
        rt.start()

        deadline = time.time() + int(cfg("task_timeout_seconds", 1800))
        while proc.poll() is None:
            if time.time() > deadline:
                proc.kill()
                _recall_send(event, _l("timeout"), recall_list)
                return
            time.sleep(1)
        stop_read["flag"] = True
        rt.join(timeout=3)
        rc = proc.returncode
        print(f"[JM下载] 子进程退出码 {rc}")

        result = None
        if result_path and os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    result = json.load(f)
            except Exception:
                result = None

        if not result or not result.get("ok"):
            err = (result or {}).get("error", f"子进程退出码 {rc}")
            print(f"[JM下载] 任务失败: {err}")
            _recall_send(event, _l("fail").format(err=err), recall_list)
            return

        if detail_only:
            print(f"[JM下载] 详情查询完成 group={group_id}")
            return

        if cfg("send_pdf", True):
            _send_pdfs(event, result, task_id, recall_list)
        else:
            _recall_send(event, _l("no_pdf"), recall_list)
    except Exception as e:
        traceback.print_exc()
        try:
            _recall_send(event, _l("fail").format(err=e), recall_list)
        except Exception:
            pass
    finally:
        with _task_lock:
            _active_tasks.pop(group_id, None)
        # 清理临时 option / result 文件
        for p in (yml_path, result_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def _progress_to_text(data):
    """把 worker 的进度行转成发送给用户的文本"""
    t = data.get("type")
    if t == "detail":
        title = data.get("title") or "未知"
        authors = "、".join(data.get("authors") or []) or "未知"
        episodes = data.get("episode_count") or 0
        pages = data.get("page_count") or 0
        pages_txt = f"\n📄 总页数：{pages}" if pages else ""
        update_date = data.get("update_date")
        if update_date in ("0", "", "None", None):
            update_date = "未知"
        tags = "、".join((data.get("tags") or [])[:8]) or "无"
        return _l("detail_header").format(
            title=title, album_id=data.get("album_id", "?"),
            authors=authors, episodes=episodes, pages=pages_txt,
            tags=tags, update_date=update_date)
    if t == "download_start":
        mode = data.get("mode")
        if mode == "photo":
            return f"⏳ 正在下载章节 {data.get('photo_id')}，并转换为PDF..."
        return f"⏳ 开始下载（共 {data.get('episode_count')} 个章节），完成后合并为PDF..."
    if t == "done":
        return "✅ 下载完成，正在发送 PDF..."
    return None


# ========== PDF 发送 ==========
def _is_windows() -> bool:
    """本机是否 Windows（独立函数便于测试替换）"""
    return os.name == "nt"


def _send_path(*parts):
    """拼接「发给 NapCat 的路径」。

    Linux+Docker：发的是**容器内路径**（固定用 '/' 拼，不能用 os.path.join ——
    在 Windows 上会拼出 "/app/cache/images\\jm_pdf\\x.pdf" 这种混合分隔符）。
    Windows 原生 NapCat：没有容器，发的是**本机路径**，用 os.path.join 得到原生分隔符。
    """
    if _is_windows():
        return os.path.join(*parts)
    return "/".join(str(p).rstrip("/") for p in parts)


def _napcat_shared_candidates():
    """候选的 NapCat 共享目录：[(宿主机写入目录, 发给 NapCat 的路径前缀), ...]

    两个元素的含义在两个平台上是不同的：
      · Linux + Docker：NapCat 在容器里，插件写宿主机目录、发 /app/cache/images/…
        —— 两种路径**不一样**，靠映射对上。
      · Windows 原生 NapCat：没有容器，NapCat 读的就是本机路径 —— 两种路径
        **必须相同**。发 /app/cache/images/… 给它等于给它一个不存在的路径。
    """
    qq = str(get_config("BOT_QQ", 0))
    local = os.path.expanduser("~/napcat/cache/images")
    # 显式配置优先（与 astercore/compat/paths.py 用的是同一个键）
    explicit = str(get_config("NAPCAT_CACHE_HOST", "") or "").strip()
    # Windows：本机路径即发送路径（PDF 走 HTTP 直发，不经过框架的出口映射，
    # 所以这里必须一步到位给出 NapCat 真正能读到的路径）
    if _is_windows():
        root = explicit or local
        return [(root, root)]
    cands = []
    if explicit:
        cands.append((explicit, "/app/cache/images"))
    # Linux+Docker 时代的老路径：**只在非 Windows 上尝试**。
    # 原因：Windows 上 "/root/napcat/..." 会被当成本盘根目录，os.makedirs 与写
    # 探针文件都会"成功"（普通用户可以在 C:\ 下建目录），于是"写不进去就换下一个"
    # 的探测被绕过 → 选中 C:\root\ 下的错误目录。
    if qq == "740979632":       # 依星
        cands.append(("/root/napcat/cache/images", "/app/cache/images"))
        cands.append(("/root/napcat2/cache/images", "/app/cache/images"))
    elif qq == "2551736206":    # 羽笙
        cands.append(("/root/napcat2/cache/images", "/app/cache/images"))
        cands.append(("/root/napcat/cache/images", "/app/cache/images"))
    else:
        cands.append((local, "/app/cache/images"))
    cands.append((local, "/app/cache/images"))
    # 去重
    seen = set()
    uniq = []
    for h, c in cands:
        if (h, c) not in seen:
            seen.add((h, c))
            uniq.append((h, c))
    return uniq


def _send_pdfs(event, result, task_id=None, recall_list=None):
    pdfs = []
    for r in result.get("results", []):
        pdfs += r.get("pdfs", []) or []
    if not pdfs:
        _recall_send(event, _l("no_pdf"), recall_list)
        return

    sent = []  # (host_path, container_path)
    cache_dir_used = None
    for host_root, container_root in _napcat_shared_candidates():
        try:
            pdf_cache_dir = os.path.join(host_root, "jm_pdf")
            os.makedirs(pdf_cache_dir, exist_ok=True)
            # 验证可写
            probe = os.path.join(pdf_cache_dir, ".jm_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            cache_dir_used = (host_root, container_root, pdf_cache_dir)
            print(f"[JM下载] 使用NapCat共享目录: {host_root}")
            break
        except Exception as e:
            print(f"[JM下载] 共享目录不可写 {host_root}: {e}")
            continue
    if cache_dir_used is None:
        _recall_send(event, _l("send_fail") + f"\n本地文件保留在: {os.path.dirname(pdfs[0])}", recall_list)
        return

    _, container_root, pdf_cache_dir = cache_dir_used
    for p in pdfs:
        if not os.path.exists(p):
            print(f"[JM下载] PDF不存在: {p}")
            continue
        fname = os.path.basename(p)
        dest = os.path.join(pdf_cache_dir, fname)
        try:
            shutil.copy2(p, dest)
        except Exception as e:
            print(f"[JM下载] 复制PDF失败 {p} -> {dest}: {e}")
            continue
        # 拼接「发给 NapCat 的路径」：容器路径固定用 '/'，本机路径用 os.path.join
        container_path = _send_path(container_root, "jm_pdf", fname)
        sent.append((dest, container_path))
        print(f"[JM下载] PDF已就绪: {p} -> {container_path}")

    if not sent:
        _recall_send(event, _l("send_fail") + f"\n本地文件保留在: {os.path.dirname(pdfs[0])}", recall_list)
        return

    titles = []
    for r in result.get("results", []):
        titles.append(r.get("title") or r.get("album_id") or "?")
    pdf_secs = int(cfg("recall_pdf_seconds", 60))
    _recall_send(event, _l("pdf_header").format(titles="、".join(titles)), recall_list, seconds=pdf_secs)
    time.sleep(0.5)

    # PDF 文件：走 HTTP 发送拿 message_id（NapCat 真实发送成功）才算发出，
    # 完成提醒与撤回计时都以该时刻为基准；HTTP 失败回退 WS 尽力发送
    file_secs = int(cfg("recall_file_seconds", 300))  # 文件撤回窗口较长（默认 5 分钟）
    sent_ok = 0
    for dest, container_path in sent:
        try:
            # 文件消息上传耗时较长，超时放宽到 60s
            mid = _http_send_msg(event, [{"type": "file", "data": {"file": container_path}}], timeout=60)
            if mid is not None:
                sent_ok += 1
                print(f"[JM下载] PDF发送成功: {container_path} (mid={mid})")
                if cfg("recall_enabled", False):
                    if recall_list is not None:
                        recall_list.append(mid)
                    # 文件消息撤回：从真实发送成功起，等待较长窗口（file_secs）
                    _schedule_recall(mid, file_secs)
            else:
                print(f"[JM下载] PDF发送失败(HTTP无返回): {container_path}")
                # 回退 WS 尽力发送（无法确认真实发出，不参与完成提醒判定）
                send_message(event, [{"type": "file", "data": {"file": container_path}}])
        except Exception as e:
            print(f"[JM下载] 发送PDF异常 {container_path}: {e}")
        time.sleep(1.5)

    # PDF 发送完成提醒：仅在确认 NapCat 真实发送成功（拿到 message_id）后发送
    if sent_ok > 0:
        if cfg("recall_enabled", False):
            _recall_send(event, _l("recall_pdf_notice").format(seconds=pdf_secs), recall_list, seconds=pdf_secs)
        else:
            send_message(event, "📄 PDF 已发送完成")

    # 清理：延迟执行（NapCat 收到 sendMsg 后异步读取文件上传，立即删除会导致发送失败）
    if cfg("delete_after_send", True):
        try:
            delay = max(30, int(cfg("cleanup_delay_seconds", 600)))
        except (TypeError, ValueError):
            delay = 600
        print(f"[JM下载] 将在 {delay}s 后清理下载与PDF文件")
        threading.Timer(delay, _cleanup_task_files, args=(pdfs, sent, task_id)).start()


def _cleanup_task_files(pdfs, sent, task_id):
    """延迟清理：只删除本任务的文件，避免影响其他并发任务"""
    if task_id:
        # 只删本任务的下载子目录
        dl_name = str(cfg("download_dir", "jm_downloads")).strip() or "jm_downloads"
        dl_dir = os.path.join(DATA_DIR, dl_name, task_id)
        if os.path.isdir(dl_dir):
            shutil.rmtree(dl_dir, ignore_errors=True)
    for p in pdfs:
        try:
            os.remove(p)
        except Exception:
            pass
    for dest, _ in sent:
        try:
            os.remove(dest)
        except Exception:
            pass
    if task_id:
        pdf_task_dir = os.path.join(DATA_DIR, "jm_pdf", task_id)
        if os.path.isdir(pdf_task_dir):
            try:
                os.rmdir(pdf_task_dir)  # 仅删除空目录
            except Exception:
                pass
    print("[JM下载] 已清理下载与PDF文件")


# ========== 自动更新 jmcomic 库 ==========
def _installed_version():
    try:
        p = subprocess.run([_venv_python_or_raise(), "-c",
                            "import jmcomic; print(jmcomic.__version__)"],
                           capture_output=True, text=True, timeout=30)
        v = (p.stdout or "").strip()
        return v or None
    except Exception:
        return None


def check_and_update():
    """检查 PyPI 上 jmcomic 最新版本，有新版则自动升级（子进程隔离，下次下载即生效）"""
    try:
        r = requests.get("https://pypi.org/pypi/jmcomic/json", timeout=15)
        latest = r.json()["info"]["version"]
    except Exception as e:
        print(f"[JM下载] 获取 jmcomic 最新版本失败: {e}")
        return None
    installed = _installed_version()
    print(f"[JM下载] jmcomic 版本检查：已装 {installed} / 最新 {latest}")
    if installed == latest:
        return latest
    print(f"[JM下载] 发现新版本 {latest}（当前 {installed}），开始自动更新...")
    try:
        p = subprocess.run([_venv_python_or_raise(), "-m", "pip", "install", "-U", "jmcomic"],
                           capture_output=True, text=True, timeout=600)
        if p.returncode == 0:
            new_v = _installed_version()
            print(f"[JM下载] jmcomic 自动更新成功 → {new_v}")
            return new_v
        print(f"[JM下载] jmcomic 自动更新失败: {(p.stderr or p.stdout)[-500:]}")
        return None
    except Exception as e:
        print(f"[JM下载] jmcomic 自动更新异常: {e}")
        return None


def _auto_update_loop():
    """后台循环：按配置间隔检查更新"""
    time.sleep(8)  # 启动后稍等，避免与开机初始化抢资源
    while True:
        try:
            check_and_update()
        except Exception as e:
            print(f"[JM下载] 自动更新检查异常: {e}")
        try:
            hours = max(1, int(cfg("update_interval_hours", 24)))
        except (TypeError, ValueError):
            hours = 24
        time.sleep(hours * 3600)


# ============ 指令注册表（集中定义，一眼可读）============
registry = CommandRegistry("JM下载")


def _cmd_enable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "jm_downloader", True)
        send_message(event, "JM下载已在本群开启")
    else:
        _CONFIG.setdefault("settings", {})["enabled"] = True
        _save_config()
        send_message(event, "JM下载已开启（全局）")
    return True


def _cmd_disable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "jm_downloader", False)
        send_message(event, "JM下载已在本群关闭")
    else:
        _CONFIG.setdefault("settings", {})["enabled"] = False
        _save_config()
        send_message(event, "JM下载已关闭（全局）")
    return True


def _cmd_update(event, raw, kw):
    # 会真的 pip 安装依赖包 → 主人专属（管理员也不行）
    if not is_master(event.get("user_id")):
        send_message(event, "只有主人才能更新 jmcomic（会安装依赖包）")
        return True

    def _manual_update():
        send_message(event, "⏳ 正在检查 jmcomic 更新...")
        new_v = check_and_update()
        if new_v:
            send_message(event, f"✅ jmcomic 已更新至 {new_v}")
        else:
            send_message(event, "jmcomic 已是最新版本（或更新失败，详见日志）")
    threading.Thread(target=_manual_update, daemon=True).start()
    return True


def _cmd_detail(event, raw, kw):
    """jm详情 <id>：只查看本子元信息"""
    m = re.match(rf'^{re.escape(cmd("detail", "jm详情"))}\s*(\d{{3,10}})$', raw, re.I)
    if not m:
        return False
    _start_task(event, [m.group(1)], detail_only=True)
    return True


def _cmd_download(event, raw, kw):
    """jm <id> [id...] [p<章节>]：下载本子并转 PDF"""
    dl = cmd("download", "jm")
    # 仅当 jm 后跟数字 / p数字 时才接管
    if not (raw.lower() == dl.lower()
            or raw.lower().startswith(dl.lower() + " ")
            or re.match(rf'^{re.escape(dl)}\s*\d', raw, re.I)):
        return False
    body = raw[len(dl):].strip()
    if not body:
        send_message(event, _l("usage"))
        return True
    ids = []
    for t in body.split():
        tl = t.lower()
        if re.fullmatch(r'\d{3,10}', tl):
            ids.append(tl)
        elif re.fullmatch(r'p\d{3,10}', tl):
            ids.append(tl)
        else:
            return False  # 含无关内容，不作为 jm 指令处理
    if not ids:
        send_message(event, _l("usage"))
        return True
    _start_task(event, ids)
    return True


# 注册指令：名称 / 触发词 / 描述 / 处理函数 / 权限 / 匹配方式
registry.register("开启JM下载", [cmd("enable", "开启jm下载")], "开启 JM 下载（群内=本群，私聊=全局）", _cmd_enable, master_only=True, kind="suffix")
registry.register("关闭JM下载", [cmd("disable", "关闭jm下载")], "关闭 JM 下载（群内=本群，私聊=全局）", _cmd_disable, master_only=True, kind="suffix")
registry.register("手动更新jmcomic", ["jm更新"], "手动检查并更新 jmcomic 库", _cmd_update, master_only=True)
registry.register("查看本子详情", [cmd("detail", "jm详情")], "仅查看本子元信息（不下载），如 jm详情 123456", _cmd_detail, kind="prefix")
registry.register("下载本子", [cmd("download", "jm")], "下载本子并转 PDF 分享，如 jm 123456 / jm 123 456 / jm 123 p456", _cmd_download, kind="prefix")


# ========== 主入口 ==========
def handle(event):
    if event.get("post_type") != "message":
        return False

    raw = event.get("raw_message", "").strip()
    uid = event.get("user_id", 0)
    msg_type = event.get("message_type")

    # ---- 特权指令（开关：主人 + 管理员；jm更新在 handler 里另外限主人） ----
    if registry.dispatch(event, raw, is_privileged(uid), master_cmds_only=True):
        return True

    # ---- 全局开关 ----
    if not cfg("enabled", True):
        return False
    if msg_type == "group" and not _pt_enabled(event.get("group_id"), "jm_downloader"):
        return False
    if not raw:
        return False

    # ---- 下载/详情指令（注册表统一分发） ----
    if registry.dispatch(event, raw, is_master(uid)):
        return True
    return False


# ========== 模块加载 ==========
_load_config()


def _sync_command_labels():
    """同步指令中文名到配置（Web 面板展示可读指令名）。
    必须在 _load_config() 之后调用，否则会覆盖磁盘上已保存的配置。"""
    _CONFIG.setdefault("command_labels", {}).update(registry.labels())
    _save_config()


_sync_command_labels()


def reload_config():
    """Web 端保存配置后由主程序（SIGUSR1）通知调用，立即重新加载配置"""
    _load_config()
    _sync_command_labels()

# 启动自动更新后台线程
if cfg("auto_update", True):
    _auto_thread = threading.Thread(target=_auto_update_loop, daemon=True)
    _auto_thread.start()
    print(f"[JM下载] 自动更新线程已启动（间隔 {cfg('update_interval_hours', 24)}h）")
