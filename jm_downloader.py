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
        "recall_seconds": 120,           # 撤回时间（秒，QQ 限制 2 分钟内可撤回）
        "recall_notice": True,           # 撤回前提醒（整套消息只提醒一次）
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
        "recall_notice": "🔇 本次 JM 下载的整套消息将在 {seconds} 秒后自动撤回",
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
            _CONFIG = {}
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


def cfg(key, default=None):
    """读取设置：优先 settings 段（Web面板格式），兼容顶层旧写法"""
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


# ========== 子进程环境 ==========
def _venv_python():
    """优先使用项目 venv 的 python（jmcomic 装在里面）"""
    cand = os.path.join(BASE_DIR, "venv", "bin", "python")
    if os.path.exists(cand):
        return cand
    return sys.executable


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
def _http_send_msg(event, message):
    """通过 NapCat HTTP 发送消息并返回 message_id（用于撤回）"""
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
        params["access_token"] = token
        r = _req.get(f"{url}/{action}", params=params, timeout=10)
        data = r.json()
        if data.get("status") == "ok" and data.get("data"):
            return data["data"].get("message_id")
        print(f"[JM下载] HTTP发送失败: {data}")
    except Exception as e:
        print(f"[JM下载] HTTP发送异常: {e}")
    return None


def _schedule_recall(message_id, seconds):
    """定时撤回单条消息（QQ 限制 2 分钟内可撤回）"""
    def _do_recall():
        try:
            import requests as _req
            url = str(get_config("NAPCAT_HTTP", "http://127.0.0.1:3000")).rstrip("/")
            token = get_config("ACCESS_TOKEN", "")
            _req.get(f"{url}/delete_msg", params={"message_id": message_id, "access_token": token}, timeout=10)
            print(f"[JM下载] 已撤回消息 {message_id}")
        except Exception as e:
            print(f"[JM下载] 撤回失败 {message_id}: {e}")
    t = threading.Timer(max(1, int(seconds)), _do_recall)
    t.daemon = True
    t.start()


def _recall_send(event, message, recall_list=None):
    """统一发送入口：
    - 开启撤回：HTTP 发送并记录 message_id 定时撤回；HTTP 失败退回 WS（不撤回）
    - 未开启：走原有 WS 发送
    """
    if cfg("recall_enabled", False):
        mid = _http_send_msg(event, message)
        if mid is not None:
            if recall_list is not None:
                recall_list.append(mid)
            _schedule_recall(mid, cfg("recall_seconds", 120))
            return
        # HTTP 失败退回 WS
        send_message(event, message)
    else:
        send_message(event, message)


# ========== 任务调度 ==========
def _start_task(event, ids, detail_only=False):
    group_id = str(event.get("group_id") or event.get("user_id") or 0)
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
        # 撤回提醒（整套只提醒一次）
        if cfg("recall_enabled", False) and cfg("recall_notice", True):
            _recall_send(event, _l("recall_notice").format(seconds=int(cfg("recall_seconds", 120))), recall_list)
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

        cmd_list = [_venv_python(), _worker_path(),
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
def _napcat_shared_candidates():
    """候选的 NapCat 宿主机共享目录（逐个尝试，写不进去就换下一个）"""
    qq = str(get_config("BOT_QQ", 0))
    cands = []
    if qq == "740979632":       # 依星
        cands.append(("/root/napcat/cache/images", "/app/cache/images"))
        cands.append(("/root/napcat2/cache/images", "/app/cache/images"))
    elif qq == "2551736206":    # 羽笙
        cands.append(("/root/napcat2/cache/images", "/app/cache/images"))
        cands.append(("/root/napcat/cache/images", "/app/cache/images"))
    else:
        cands.append((os.path.expanduser("~/napcat/cache/images"), "/app/cache/images"))
    cands.append((os.path.expanduser("~/napcat/cache/images"), "/app/cache/images"))
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
        container_path = os.path.join(container_root, "jm_pdf", fname)
        sent.append((dest, container_path))
        print(f"[JM下载] PDF已就绪: {p} -> {container_path}")

    if not sent:
        _recall_send(event, _l("send_fail") + f"\n本地文件保留在: {os.path.dirname(pdfs[0])}", recall_list)
        return

    titles = []
    for r in result.get("results", []):
        titles.append(r.get("title") or r.get("album_id") or "?")
    _recall_send(event, _l("pdf_header").format(titles="、".join(titles)), recall_list)
    time.sleep(0.5)

    for dest, container_path in sent:
        try:
            _recall_send(event, [{"type": "file", "data": {"file": container_path}}], recall_list)
        except Exception as e:
            print(f"[JM下载] 发送PDF失败 {container_path}: {e}")
        time.sleep(1.5)

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
        p = subprocess.run([_venv_python(), "-c",
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
        p = subprocess.run([_venv_python(), "-m", "pip", "install", "-U", "jmcomic"],
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

    # ---- 主人指令（开关/更新，注册表统一分发） ----
    if registry.dispatch(event, raw, is_master(uid), master_cmds_only=True):
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

# 启动自动更新后台线程
if cfg("auto_update", True):
    _auto_thread = threading.Thread(target=_auto_update_loop, daemon=True)
    _auto_thread.start()
    print(f"[JM下载] 自动更新线程已启动（间隔 {cfg('update_interval_hours', 24)}h）")
