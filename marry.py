import hashlib
import json
import os
import random
import time
import threading
import requests
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from utils.api import send_message
from utils.config import get_config, get_master_qq, get_napcat_http, get_access_token
from utils.plugin_toggle import is_enabled as _pt_enabled, set_enabled as _pt_set
from utils.command_registry import CommandRegistry

# ========== 头像缓存（规避 NapCat 下载 qlogo 超时）==========
_CACHE_DIR = None


def plog(level, msg):
    """分级日志（plugin_log 失败时兜底 print；别递归调用自己）"""
    try:
        from utils.log import plugin_log
        plugin_log("结婚", level, msg)
    except Exception:
        print(f"[结婚] {msg}")


def _is_windows() -> bool:
    """本机是否 Windows（独立函数便于测试替换）"""
    return os.name == "nt"


def _get_cache_dir():
    global _CACHE_DIR
    if _CACHE_DIR:
        return _CACHE_DIR
    bot_qq = str(get_config("BOT_QQ", 0))
    # 显式配置优先（与 astercore/compat/paths.py 用的是同一个键）
    explicit = str(get_config("NAPCAT_CACHE_HOST", "") or "").strip()
    if explicit:
        _CACHE_DIR = explicit
    # Linux+Docker 时代的老路径：**只在非 Windows 上采用**。
    # Windows 上 "/root/napcat/..." 会被当成本盘根目录，makedirs 与写文件都会
    # "成功"，头像实际落在 C:\root\...，而发送用的 /app/cache/images/... 会被
    # 映射到 %USERPROFILE%\napcat\cache\images\... → 头像永远发不出去。
    elif _is_windows() or bot_qq not in ("740979632", "2551736206"):
        _CACHE_DIR = os.path.expanduser("~/napcat/cache/images")
    elif bot_qq == "740979632":
        _CACHE_DIR = "/root/napcat/cache/images"
    else:
        _CACHE_DIR = "/root/napcat2/cache/images"
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return _CACHE_DIR

def cache_avatar(avatar_url, qq):
    """下载头像到 NapCat 缓存目录，返回容器内路径；失败则返回原 URL"""
    try:
        cache_dir = _get_cache_dir()
        url_hash = hashlib.md5(avatar_url.encode()).hexdigest()
        filename = url_hash + ".jpg"
        filepath = os.path.join(cache_dir, filename)
        container_path = f"/app/cache/images/{filename}"

        # 已有缓存直接用
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            return container_path

        # 下载头像
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(avatar_url, headers=headers, timeout=8)
        if r.status_code == 200 and len(r.content) > 1024:
            with open(filepath, 'wb') as f:
                f.write(r.content)
            return container_path
        return avatar_url
    except Exception as e:
        print(f"[结婚] 头像缓存失败: {e}")
        return avatar_url
# ========== 插件元数据（SDK 规范）==========
__plugin_name_cn__ = "结婚插件"
__plugin_name_en__ = "marry"
__plugin_version__ = "1.0.0"
__plugin_desc__ = "群内每日结婚/离婚系统"
__plugin_author__ = "NapCat-WordLibBot"

# ============================================================================
# 可配置项声明（面板据此**自动生成表单**，见 web/config_schema.py）
#   · 字段名 → 控件类型；path 是值在配置文件里的位置（settings.foo / commands.bar）
#   · label/help/group 是面板上直接显示的中文；advanced 折叠进「高级选项」
#   · 纯元数据，不改变插件任何行为；未声明的插件面板会按当前值自动推断
# ============================================================================

__config_schema__ = {
    # ---------------- 指令关键词（commands.*）----------------
    "cmd_enable": {
        "type": "text", "label": "开启结婚", "group": "指令",
        "path": "commands.enable", "default": "开启结婚",
        "help": "触发词：后缀匹配，仅主人可用；群内=开本群，私聊=开全局",
    },
    "cmd_disable": {
        "type": "text", "label": "关闭结婚", "group": "指令",
        "path": "commands.disable", "default": "关闭结婚",
        "help": "触发词：后缀匹配，仅主人可用；群内=关本群，私聊=关全局",
    },
    "cmd_set_prob": {
        "type": "text", "label": "设置结婚概率", "group": "指令",
        "path": "commands.set_prob", "default": "设置结婚概率",
        "help": "触发词：前缀匹配，仅主人可用，用法「设置结婚概率 80」",
    },
    "cmd_set_cd": {
        "type": "text", "label": "设置离婚CD", "group": "指令",
        "path": "commands.set_cd", "default": "设置离婚CD",
        "help": "触发词：前缀匹配，仅主人可用，用法「设置离婚CD 24」",
    },
    "cmd_marry": {
        "type": "text", "label": "娶群友", "group": "指令",
        "path": "commands.marry", "default": "娶群友",
        "help": "触发词：整条消息全等，随机娶一位群友",
    },
    "cmd_marry_f": {
        "type": "text", "label": "嫁群友", "group": "指令",
        "path": "commands.marry_f", "default": "嫁群友",
        "help": "触发词：整条消息全等，随机嫁给一位群友",
    },
    "cmd_marry_t": {
        "type": "text", "label": "指定娶", "group": "指令",
        "path": "commands.marry_t", "default": "娶",
        "help": "触发词：前缀匹配，后面必须 @ 一个人（如「娶 @某人」）",
    },
    "cmd_marry_ft": {
        "type": "text", "label": "指定嫁", "group": "指令",
        "path": "commands.marry_ft", "default": "嫁",
        "help": "触发词：前缀匹配，后面必须 @ 一个人（如「嫁 @某人」）",
    },
    "cmd_divorce": {
        "type": "text", "label": "闹离婚", "group": "指令",
        "path": "commands.divorce", "default": "闹离婚",
        "help": "触发词：整条消息全等",
    },
    "cmd_my_object": {
        "type": "text", "label": "我的对象", "group": "指令",
        "path": "commands.my_object", "default": "我的对象",
        "help": "触发词：整条消息全等，查看今天的对象（带头像）",
    },

    # ---------------- 参数（settings.*）----------------
    "setting_enabled": {
        "type": "bool", "label": "插件总开关", "group": "参数",
        "path": "settings.enabled",
        "help": "全局总开关，关闭后所有群都不再响应（分群开关见「群组管理」）",
        "default": True,
    },
    "setting_success_rate": {
        "type": "number", "label": "结婚成功率", "group": "参数",
        "path": "settings.success_rate", "default": 80,
        "min": 0, "max": 100, "step": 1, "unit": "%",
        "help": "0=永远失败，100=必定成功；当前配置文件里没有这一项，属默认值",
    },
    "setting_divorce_cd_hours": {
        "type": "number", "label": "离婚冷却", "group": "参数",
        "path": "settings.divorce_cd_hours", "min": 0, "max": 720, "unit": "小时",
        "help": "离婚后要等多久才能再次离婚；允许小数（如 0.25 = 15 分钟），故未设置步长",
        "default": 0.25,
    },

    # ---------------- 回复模板（replies.*）----------------
    "reply_only_group": {
        "type": "text", "label": "仅群内可用", "group": "回复模板",
        "path": "replies.only_group", "default": "该功能只能在群内使用。", "help": "无占位符",
    },
    "reply_no_members": {
        "type": "text", "label": "取不到群成员", "group": "回复模板",
        "path": "replies.no_members", "default": "无法获取群成员列表，请稍后再试。", "help": "无占位符",
    },
    "reply_already_married": {
        "type": "text", "label": "今天已有对象", "group": "回复模板",
        "path": "replies.already_married", "default": "你今天已经有对象了，不能再次结婚。", "help": "无占位符",
    },
    "reply_no_one_left": {
        "type": "text", "label": "群内无人可选", "group": "回复模板",
        "path": "replies.no_one_left", "default": "群内所有其他成员今天都有对象了，明天再试吧。", "help": "无占位符",
    },
    "reply_self_marry": {
        "type": "text", "label": "不能娶嫁自己", "group": "回复模板",
        "path": "replies.self_marry", "default": "不能娶/嫁自己哦。", "help": "无占位符",
    },
    "reply_not_in_group": {
        "type": "text", "label": "对方不在本群", "group": "回复模板",
        "path": "replies.not_in_group", "default": "对方不在本群，无法结婚。", "help": "无占位符",
    },
    "reply_target_married": {
        "type": "text", "label": "对方已有对象", "group": "回复模板",
        "path": "replies.target_married", "default": "对方今天已经有对象了，不能娶/嫁。", "help": "无占位符",
    },
    "reply_failed": {
        "type": "text", "label": "表白失败", "group": "回复模板",
        "path": "replies.failed", "default": "表白失败，对方拒绝了你的求婚。", "help": "无占位符",
    },
    "reply_just_married": {
        "type": "text", "label": "你刚结过婚", "group": "回复模板",
        "path": "replies.just_married", "default": "你刚刚已经结婚了，请稍后再试。", "help": "无占位符",
    },
    "reply_target_just_married": {
        "type": "text", "label": "对方刚结过婚", "group": "回复模板",
        "path": "replies.target_just_married", "default": "对方刚刚有了对象，请稍后再试。", "help": "无占位符",
    },
    "reply_divorce_cooldown": {
        "type": "text", "label": "离婚冷却中", "group": "回复模板",
        "path": "replies.divorce_cooldown", "default": "你刚刚离过婚，请等待 {hours} 小时 {minutes} 分钟后再离婚。",
        "help": "占位符：{hours} 剩余小时、{minutes} 剩余分钟（花括号写法，勿改成方括号）",
    },
    "reply_no_divorce": {
        "type": "text", "label": "今天没有对象", "group": "回复模板",
        "path": "replies.no_divorce", "default": "你今天都没有对象，离什么婚？", "help": "无占位符",
    },
    "reply_divorce_success": {
        "type": "text", "label": "离婚成功", "group": "回复模板",
        "path": "replies.divorce_success", "default": "离婚成功，你可以重新开始新的恋情了。", "help": "无占位符",
    },
    "reply_no_object": {
        "type": "text", "label": "今天还没有对象", "group": "回复模板",
        "path": "replies.no_object", "default": "你今天还没有对象哦。", "help": "无占位符",
    },
    "reply_master_only": {
        "type": "text", "label": "仅主人（概率）", "group": "回复模板",
        "path": "replies.master_only", "default": "只有主人或管理员才能设置概率。", "help": "无占位符",
    },
    "reply_master_only_cd": {
        "type": "text", "label": "仅主人（CD）", "group": "回复模板",
        "path": "replies.master_only_cd", "default": "只有主人或管理员才能设置CD。", "help": "无占位符",
    },
    "reply_format_prob": {
        "type": "text", "label": "概率格式错误", "group": "回复模板",
        "path": "replies.format_prob", "default": "格式错误：设置结婚概率 数字", "help": "无占位符",
    },
    "reply_prob_range": {
        "type": "text", "label": "概率超出范围", "group": "回复模板",
        "path": "replies.prob_range", "default": "概率必须在0-100之间。", "help": "无占位符",
    },
    "reply_prob_set": {
        "type": "text", "label": "概率设置成功", "group": "回复模板",
        "path": "replies.prob_set", "default": "结婚成功率已设置为 {rate}%",
        "help": "占位符：{rate} 新概率（花括号写法）",
    },
    "reply_format_cd": {
        "type": "text", "label": "CD格式错误", "group": "回复模板",
        "path": "replies.format_cd", "default": "格式错误：设置离婚CD 小时", "help": "无占位符",
    },
    "reply_cd_non_neg": {
        "type": "text", "label": "CD不能为负", "group": "回复模板",
        "path": "replies.cd_non_neg", "default": "CD必须是非负数。", "help": "无占位符",
    },
    "reply_cd_set": {
        "type": "text", "label": "CD设置成功", "group": "回复模板",
        "path": "replies.cd_set", "default": "离婚CD已设置为 {hours} 小时",
        "help": "占位符：{hours} 新的冷却小时数（花括号写法）",
    },
    "reply_not_number": {
        "type": "text", "label": "请输入数字", "group": "回复模板",
        "path": "replies.not_number", "default": "请输入数字。", "help": "设置概率/CD 时输入非法共用一个模板",
    },

    # ---------------- 元数据（自动生成/只读）----------------
    "command_labels": {
        "type": "json", "label": "指令标签映射", "group": "其它",
        "path": "command_labels", "advanced": True, "readonly": True, "rows": 6,
        "help": "插件启动时自动写入的「触发词 → 指令中文名」映射，供面板显示可读名称；只读",
        "default": {"开启结婚": "开启结婚", "关闭结婚": "关闭结婚", "设置结婚概率": "设置结婚概率", "设置离婚CD": "设置离婚CD", "娶群友": "娶群友", "嫁群友": "嫁群友", "娶": "指定娶", "嫁": "指定嫁", "闹离婚": "闹离婚", "我的对象": "我的对象"},
    },
}

# ===========================================


# ========== 可配置命令和回复 ==========
_CFG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "marry_config.json")
_CFG = {}


def _load_cfg():
    """从配置文件加载配置到内存"""
    global _CFG
    try:
        if os.path.exists(_CFG_FILE):
            with open(_CFG_FILE, 'r', encoding='utf-8') as fh:
                _CFG = json.load(fh)
        else:
            _CFG = {}
    except Exception:
        pass  # 解析失败保留旧配置


def _l():
    """读取内存中的配置（热更新由后台定时器完成，处理消息零开销）"""
    return _CFG


# 配置热更新：每 5 秒检查配置文件，变化自动重载（保存后自动生效）
_CFG_MTIME = 0


def _ensure_fresh():
    global _CFG_MTIME
    try:
        mtime = os.stat(_CFG_FILE).st_mtime_ns
        if mtime != _CFG_MTIME:
            _CFG_MTIME = mtime
            _load_cfg()
    except Exception:
        pass


def reload_config():
    """Web 端保存配置后由主程序（SIGUSR1）通知调用，立即重新加载配置"""
    _load_cfg()

_load_cfg()

def cmd(k, d=None):
    return _CFG.get('commands', {}).get(k) or d

def rep(k, d='', **kw):
    r = _CFG.get('replies', {}).get(k) or d
    if r and kw:
        try:
            return r.format(**kw)
        except:
            return r
    return r or d

def setting(k, d=None):
    v = _CFG.get('settings', {}).get(k)
    return v if v is not None else d

def _save():
    with open(_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(_CFG, f, ensure_ascii=False, indent=2)


MASTER_QQ = get_master_qq()
NAPCAT_HTTP = get_napcat_http()
ACCESS_TOKEN = get_access_token()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)

MARRIAGE_FILE = os.path.join(DATA_DIR, "marriage.json")
CD_FILE = os.path.join(DATA_DIR, "marriage_cd.json")


def _write_json(path, data):
    """写入 JSON，保留文件中原有的 _note 备注"""
    note = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                note = json.load(f).get("_note")
        except:
            pass
    if note:
        data["_note"] = note
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _purge_old_days(data):
    today = get_today_key()
    for group_key in list(data.keys()):
        if group_key == "_note":
            continue
        days = data.get(group_key, {})
        for date_key in list(days.keys()):
            if date_key != today:
                del days[date_key]
        if not days:
            del data[group_key]
        else:
            data[group_key] = days
    return data

def load_marriage():
    if not os.path.exists(MARRIAGE_FILE):
        return {}
    try:
        with open(MARRIAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.pop("_note", None)
        _purge_old_days(data)
        return data
    except Exception as e:
        plog("error", f"加载婚姻数据失败: {e}")
        return {}

def save_marriage(data):
    _purge_old_days(data)
    _write_json(MARRIAGE_FILE, data)

def load_cd():
    if os.path.exists(CD_FILE):
        try:
            with open(CD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.pop("_note", None)
                return data
        except:
            return {}
    return {}

def save_cd(data):
    _write_json(CD_FILE, data)

def get_today_key():
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

def get_group_members(group_id):
    try:
        url = f"{NAPCAT_HTTP}/get_group_member_list"
        params = {"group_id": group_id}
        if ACCESS_TOKEN:
            params["access_token"] = ACCESS_TOKEN
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok" and data.get("retcode") == 0:
                return data.get("data", [])
        return []
    except Exception as e:
        plog("error", f"获取群成员失败: {e}")
        return []

def is_master(user_id):
    """主人（每次实时读 config.json）"""
    try:
        ml = get_master_qq() or []
    except Exception:
        ml = []
    if not isinstance(ml, (list, tuple, set)):
        ml = [ml]
    return str(user_id) in {str(m).strip() for m in ml}


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
    """主人或管理员"""
    return is_master(user_id) or is_admin(user_id)

def extract_at(event):
    for seg in event.get("message", []):
        if seg.get("type") == "at":
            qq = seg.get("data", {}).get("qq")
            if qq:
                if qq == "all":
                    continue
                return int(qq)
    return None

def handle_marriage(event, target_qq=None, mode="娶"):
    if event.get("message_type") != "group":
        send_message(event, rep("only_group", "该功能只能在群内使用。"))
        return True

    user_id = event.get("user_id")
    group_id = event.get("group_id")

    members = get_group_members(group_id)
    if not members:
        send_message(event, rep("no_members", "无法获取群成员列表，请稍后再试。"))
        return True

    marriage_data = load_marriage()
    today = get_today_key()
    group_key = str(group_id)
    if group_key not in marriage_data:
        marriage_data[group_key] = {}
    if today not in marriage_data[group_key]:
        marriage_data[group_key][today] = {}

    if str(user_id) in marriage_data[group_key][today]:
        send_message(event, rep("already_married", "你今天已经有对象了，不能再次结婚。"))
        return True

    if target_qq is None:
        occupied = set(marriage_data[group_key][today].keys())
        available = [m for m in members if m.get("user_id") != user_id and str(m.get("user_id")) not in occupied]
        if not available:
            send_message(event, rep("no_one_left", "群内所有其他成员今天都有对象了，明天再试吧。"))
            return True
        target = random.choice(available)
        target_qq = target.get("user_id")
        target_nick = target.get("nickname", "")
    else:
        if target_qq == user_id:
            send_message(event, rep("self_marry", "不能娶/嫁自己哦。"))
            return True
        target_info = next((m for m in members if m.get("user_id") == target_qq), None)
        if not target_info:
            send_message(event, rep("not_in_group", "对方不在本群，无法结婚。"))
            return True
        target_nick = target_info.get("nickname", "")
        if str(target_qq) in marriage_data[group_key][today]:
            send_message(event, rep("target_married", "对方今天已经有对象了，不能娶/嫁。"))
            return True

    success_rate = setting("success_rate", 80)
    if random.randint(1, 100) > success_rate:
        send_message(event, rep("failed", "表白失败，对方拒绝了你的求婚。"))
        return True

    marriage_data = load_marriage()
    marriage_data.setdefault(group_key, {}).setdefault(today, {})

    if str(user_id) in marriage_data[group_key][today]:
        send_message(event, rep("just_married", "你刚刚已经结婚了，请稍后再试。"))
        return True
    if str(target_qq) in marriage_data[group_key][today]:
        send_message(event, rep("target_just_married", "对方刚刚有了对象，请稍后再试。"))
        return True

    marriage_data[group_key][today][str(user_id)] = {"qq": target_qq, "mode": mode}
    inverse_mode = "嫁" if mode == "娶" else "娶"
    marriage_data[group_key][today][str(target_qq)] = {"qq": user_id, "mode": inverse_mode}
    save_marriage(marriage_data)

    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={target_qq}&s=640"
    avatar_file = cache_avatar(avatar_url, target_qq)
    if mode == "娶":
        msg_segments = [
            {"type": "at", "data": {"qq": str(user_id)}},
            {"type": "text", "data": {"text": f" 恭喜你！今天你娶了老婆 {target_nick}({target_qq}) "}},
            {"type": "image", "data": {"file": avatar_file}},
            {"type": "text", "data": {"text": " 祝你们幸福！"}}
        ]
    else:
        msg_segments = [
            {"type": "at", "data": {"qq": str(user_id)}},
            {"type": "text", "data": {"text": f" 恭喜你！今天你嫁给了老公 {target_nick}({target_qq}) "}},
            {"type": "image", "data": {"file": avatar_file}},
            {"type": "text", "data": {"text": " 祝你们幸福！"}}
        ]
    send_message(event, msg_segments)
    return True

def handle_divorce(event):
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    if not group_id:
        send_message(event, rep("only_group", "该功能只能在群内使用。"))
        return True

    cd_data = load_cd()
    user_cd = cd_data.get(str(user_id))
    if user_cd and user_cd > time.time():
        remaining = user_cd - time.time()
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        send_message(event, rep("divorce_cooldown", "你刚刚离过婚，请等待 {hours} 小时 {minutes} 分钟后再离婚。", hours=hours, minutes=minutes))
        return True

    marriage_data = load_marriage()
    today = get_today_key()
    group_key = str(group_id)
    if group_key not in marriage_data or today not in marriage_data[group_key]:
        send_message(event, rep("no_divorce", "你今天都没有对象，离什么婚？"))
        return True

    if str(user_id) not in marriage_data[group_key][today]:
        send_message(event, rep("no_divorce", "你今天都没有对象，离什么婚？"))
        return True

    partner = marriage_data[group_key][today][str(user_id)]
    partner_qq = partner.get("qq") if isinstance(partner, dict) else partner
    del marriage_data[group_key][today][str(user_id)]
    if str(partner_qq) in marriage_data[group_key][today]:
        del marriage_data[group_key][today][str(partner_qq)]
    save_marriage(marriage_data)

    cd_data[str(user_id)] = time.time() + setting("divorce_cd_hours", 24) * 3600
    save_cd(cd_data)

    send_message(event, rep("divorce_success", "离婚成功，你可以重新开始新的恋情了。"))
    return True

def handle_my_object(event):
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    if not group_id:
        send_message(event, rep("only_group", "该功能只能在群内使用。"))
        return True

    marriage_data = load_marriage()
    today = get_today_key()
    group_key = str(group_id)
    if group_key not in marriage_data or today not in marriage_data[group_key] or str(user_id) not in marriage_data[group_key][today]:
        send_message(event, rep("no_object", "你今天还没有对象哦。"))
        return True

    target_qq = marriage_data[group_key][today][str(user_id)]
    if isinstance(target_qq, dict):
        mode = target_qq.get("mode", "娶")
        target_qq = target_qq["qq"]
    else:
        mode = "娶"
    members = get_group_members(group_id)
    target_info = next((m for m in members if m.get("user_id") == int(target_qq)), None)
    target_nick = target_info.get("nickname", "") if target_info else "未知"

    caller_role = "老公" if mode == "娶" else "老婆"
    partner_role = "老婆" if mode == "娶" else "老公"

    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={target_qq}&s=640"
    avatar_file = cache_avatar(avatar_url, target_qq)
    msg_segments = [
        {"type": "at", "data": {"qq": str(user_id)}},
        {"type": "text", "data": {"text": f" 今天你的{partner_role}是 {target_nick}({target_qq}) "}},
        {"type": "image", "data": {"file": avatar_file}}
    ]
    send_message(event, msg_segments)
    return True

def handle_set_prob(event):
    if not is_privileged(event.get("user_id")):
        send_message(event, rep("master_only", "只有主人或管理员才能设置概率。"))
        return True
    parts = event.get("raw_message", "").split()
    if len(parts) != 2:
        send_message(event, rep("format_prob", "格式错误：设置结婚概率 数字"))
        return True
    try:
        rate = int(parts[1])
        if 0 <= rate <= 100:
            _l()
            _CFG.setdefault("settings", {})["success_rate"] = rate
            _save()
            send_message(event, rep("prob_set", "结婚成功率已设置为 {rate}%", rate=rate))
        else:
            send_message(event, rep("prob_range", "概率必须在0-100之间。"))
    except:
        send_message(event, rep("not_number", "请输入数字。"))
    return True

def handle_set_cd(event):
    if not is_privileged(event.get("user_id")):
        send_message(event, rep("master_only_cd", "只有主人或管理员才能设置CD。"))
        return True
    parts = event.get("raw_message", "").split()
    if len(parts) != 2:
        send_message(event, rep("format_cd", "格式错误：设置离婚CD 小时"))
        return True
    try:
        hours = float(parts[1])
        if hours >= 0:
            _l()
            _CFG.setdefault("settings", {})["divorce_cd_hours"] = hours
            _save()
            send_message(event, rep("cd_set", "离婚CD已设置为 {hours} 小时", hours=hours))
        else:
            send_message(event, rep("cd_non_neg", "CD必须是非负数。"))
    except:
        send_message(event, rep("not_number", "请输入数字。"))
    return True

# ============ 指令注册表（集中定义，一眼可读）============
registry = CommandRegistry("结婚")


def _cmd_enable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "marry", True)
        send_message(event, "结婚已在本群开启")
    else:
        _CFG.setdefault("settings", {})["enabled"] = True
        _save()
        send_message(event, "结婚已开启")
    return True


def _cmd_disable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "marry", False)
        send_message(event, "结婚已在本群关闭")
    else:
        _CFG.setdefault("settings", {})["enabled"] = False
        _save()
        send_message(event, "结婚已关闭")
    return True


def _cmd_set_prob(event, raw, kw):
    return handle_set_prob(event)


def _cmd_set_cd(event, raw, kw):
    return handle_set_cd(event)


def _cmd_marry(event, raw, kw):
    return handle_marriage(event, mode="娶")


def _cmd_marry_f(event, raw, kw):
    return handle_marriage(event, mode="嫁")


def _cmd_marry_t(event, raw, kw):
    at_qq = extract_at(event)
    if at_qq is None:
        return False
    return handle_marriage(event, at_qq, mode="娶")


def _cmd_marry_ft(event, raw, kw):
    at_qq = extract_at(event)
    if at_qq is None:
        return False
    return handle_marriage(event, at_qq, mode="嫁")


def _cmd_divorce(event, raw, kw):
    return handle_divorce(event)


def _cmd_my_object(event, raw, kw):
    return handle_my_object(event)


# 注册指令：名称 / 触发词 / 描述 / 处理函数 / 权限 / 匹配方式
registry.register("开启结婚", [cmd("enable", "开启结婚")], "开启结婚系统（群内=本群，私聊=全局）", _cmd_enable, master_only=True, kind="suffix")
registry.register("关闭结婚", [cmd("disable", "关闭结婚")], "关闭结婚系统（群内=本群，私聊=全局）", _cmd_disable, master_only=True, kind="suffix")
registry.register("设置结婚概率", [cmd("set_prob", "设置结婚概率")], "设置结婚成功率（如：设置结婚概率 80）", _cmd_set_prob, master_only=True, kind="prefix")
registry.register("设置离婚CD", [cmd("set_cd", "设置离婚CD")], "设置离婚冷却时间（如：设置离婚CD 24）", _cmd_set_cd, master_only=True, kind="prefix")
registry.register("娶群友", [cmd("marry", "娶群友")], "在群里娶群友", _cmd_marry)
registry.register("嫁群友", [cmd("marry_f", "嫁群友")], "在群里嫁群友", _cmd_marry_f)
registry.register("指定娶", [cmd("marry_t", "娶")], "娶指定的人（如：娶 @某人）", _cmd_marry_t, kind="prefix")
registry.register("指定嫁", [cmd("marry_ft", "嫁")], "嫁给指定的人（如：嫁 @某人）", _cmd_marry_ft, kind="prefix")
registry.register("闹离婚", [cmd("divorce", "闹离婚")], "与对象离婚", _cmd_divorce)
registry.register("我的对象", [cmd("my_object", "我的对象")], "查看当前对象", _cmd_my_object)

# 同步指令中文名到配置（Web 面板展示可读指令名）
_CFG.setdefault("command_labels", {}).update(registry.labels())
_save()


def handle(event):
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return False

    raw_msg = event.get("raw_message", "").strip()
    user_id = event.get("user_id")

    _l()
    # 特权指令（开关/设置：主人 + 管理员，注册表统一分发）
    if registry.dispatch(event, raw_msg, is_privileged(user_id), master_cmds_only=True):
        return True

    if not setting("enabled", True):
        return False
    # 分群检查
    if event.get("message_type") == "group" and not _pt_enabled(event.get("group_id"), "marry"):
        return False

    # 玩家指令（娶/嫁/离婚/我的对象，注册表统一分发）
    if registry.dispatch(event, raw_msg, is_privileged(user_id)):
        return True
    return False

