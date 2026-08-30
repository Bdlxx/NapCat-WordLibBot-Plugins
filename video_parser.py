"""
视频解析插件 - 检测群内分享的视频链接，解析并发送无水印版本
支持平台：哔哩哔哩、抖音、快手、小红书、TikTok

解析策略：
- 哔哩哔哩：公开 API（无需登录）
- 抖音：52api.cn 接口（HMAC-SHA256 签名认证）
- TikTok：web API
- 快手/小红书：Playwright 模拟浏览器提取
"""

# ========== 插件元数据（SDK 规范）==========
__plugin_name_cn__ = "视频解析"
__plugin_name_en__ = "video_parser"
__plugin_version__ = "1.0.0"
__plugin_desc__ = "抖音/B站/快手/小红书/TikTok视频解析去水印"
__plugin_author__ = "NapCat-WordLibBot"
# ===========================================

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
import threading
import requests

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from utils.api import send_message, send_forward_msg
from utils.config import get_bot_name, get_bot_qq, get_config
from utils.http_client import http_get, HttpError
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
_browser_pool = None


def vlog(level, msg):
    """视频解析 分级日志"""
    try:
        from utils.log import plugin_log
        plugin_log("视频解析", level, msg)
    except Exception:
        print(f"[视频解析] {msg}")


# ========== 解析结果缓存池 ==========
# key: 原始分享链接 md5 → {result, ts, video_local}
# 同一链接多次触发时直接复用第一次的解析结果和已下载文件
RESULT_CACHE = {}
RESULT_CACHE_FILE = os.path.join(DATA_DIR, "video_cache.json")
RESULT_CACHE_TTL = 2 * 3600  # 2小时有效（CDN URL 有时效）


def _load_result_cache():
    global RESULT_CACHE
    if os.path.exists(RESULT_CACHE_FILE):
        try:
            with open(RESULT_CACHE_FILE, "r", encoding="utf-8") as f:
                RESULT_CACHE = json.load(f)
        except:
            RESULT_CACHE = {}


def _save_result_cache():
    try:
        # 清理过期条目
        now = time.time()
        expired = [k for k, v in RESULT_CACHE.items() if now - v.get('ts', 0) > RESULT_CACHE_TTL]
        for k in expired:
            RESULT_CACHE.pop(k, None)
        with open(RESULT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(RESULT_CACHE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        vlog("error", f"缓存保存失败: {e}")


def _cache_key(url):
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()


def _cache_get(url):
    """查缓存，命中返回 (result, 是否缓存命中)"""
    k = _cache_key(url)
    entry = RESULT_CACHE.get(k)
    if not entry:
        return None, False
    now = time.time()
    if now - entry.get('ts', 0) > RESULT_CACHE_TTL:
        RESULT_CACHE.pop(k, None)
        return None, False
    vlog("info", f"命中解析缓存: {url[:40]}...")
    return entry.get('result'), True


def _cache_set(url, result):
    """写入缓存"""
    k = _cache_key(url)
    RESULT_CACHE[k] = {'result': result, 'ts': time.time()}
    _save_result_cache()

def _load_config():
    global _CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _CONFIG = json.load(f)
        except:
            pass  # 解析失败保留旧配置
    else:
        _CONFIG = {}
    for k, v in DEFAULT_CONFIG.items():
        _CONFIG.setdefault(k, v)
    _save_config()

def _save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_CONFIG, f, ensure_ascii=False, indent=2)

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
    """直接用内存值（后台定时器负责热更新，处理消息零开销）"""
    return _CONFIG.get(key, default)

_load_config()
_load_result_cache()


def reload_config():
    """Web 端保存配置后由主程序（SIGUSR1）通知调用，立即重新加载配置"""
    _load_config()

# ========== 视频下载到本地缓存（绕过CDN防盗链）==========
BOT_QQ_STR = str(get_config("BOT_QQ", 0))
if BOT_QQ_STR == "740979632":
    CACHE_DIR = "/root/napcat/cache/images"
elif BOT_QQ_STR == "2551736206":
    CACHE_DIR = "/root/napcat2/cache/images"
else:
    CACHE_DIR = os.path.expanduser("~/napcat/cache/images")
CONTAINER_CACHE_PATH = "/app/cache/images"
os.makedirs(CACHE_DIR, exist_ok=True)

def download_video_to_cache(url, referer="https://www.douyin.com/", max_retries=3):
    """下载视频到 NapCat 缓存目录，返回容器内路径。
    支持断点续传（Range）与失败重试：大视频（几十MB+）单次流式下载易被
    B 站 CDN 中断（IncompleteRead），中断后从已下载字节数继续，多次仍失败则
    返回 '' 表示下载失败（调用方不应回退发直链——B 站直链 NapCat 下载会 Forbidden）。"""
    import hashlib
    # 类型安全：确保 url 是字符串
    if not isinstance(url, str):
        vlog("error", f"下载失败: url 不是字符串 ({type(url).__name__})")
        return str(url) if url else ''
    url_hash = hashlib.md5(url.encode()).hexdigest()
    filename = url_hash + ".mp4"
    filepath = os.path.join(CACHE_DIR, filename)
    container_path = f"{CONTAINER_CACHE_PATH}/{filename}"

    if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
        vlog("info", f"使用缓存视频: {filename}")
        return container_path

    vlog("info", f"下载视频到缓存: {filename}")
    base_headers = {
        'User-Agent': _UA,
        'Referer': referer,
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Connection': 'keep-alive',
    }
    # 已下载字节数（断点续传用）
    downloaded = 0
    if os.path.exists(filepath):
        downloaded = os.path.getsize(filepath)

    for attempt in range(1, max_retries + 1):
        try:
            headers = dict(base_headers)
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'
            r = requests.get(url, headers=headers, timeout=120, stream=True)
            if r.status_code == 416:  # Range 越界（已下完）
                size_mb = downloaded / 1024 / 1024
                vlog("info", f"下载完成: {filename} ({size_mb:.1f}MB)")
                return container_path if size_mb >= 0.05 else ''
            if r.status_code not in (200, 206):
                vlog("error", f"下载失败 HTTP {r.status_code} (第{attempt}次)")
                return ''
            mode = 'ab' if r.status_code == 206 and downloaded > 0 else 'wb'
            if mode == 'wb':
                downloaded = 0
            with open(filepath, mode) as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            size_mb = downloaded / 1024 / 1024
            vlog("info", f"下载完成: {filename} ({size_mb:.1f}MB)")
            if size_mb < 0.05:
                vlog("info", f"视频过小(仅{size_mb:.3f}MB)")
                try: os.remove(filepath)
                except Exception: pass
                return ''
            return container_path
        except Exception as e:
            vlog("error", f"下载异常(第{attempt}/{max_retries}次): {e}")
            # 已下载的部分保留，下次从断点继续
            if os.path.exists(filepath):
                downloaded = os.path.getsize(filepath)
            if attempt < max_retries:
                time.sleep(2 * attempt)
    vlog("error", f"下载失败（已重试 {max_retries} 次），放弃")
    return ''


# ========== 平台匹配 ==========
PLATFORM_PATTERNS = [
    (r'https?://v\.douyin\.com/[A-Za-z0-9_-]+/?', '抖音'),
    (r'https?://www\.douyin\.com/video/\d+', '抖音'),
    (r'https?://www\.iesdouyin\.com/\S+', '抖音'),
    (r'https?://www\.douyin\.com/share/video/\d+', '抖音'),
    (r'https?://www\.bilibili\.com/video/BV[\w]+', '哔哩哔哩'),
    (r'https?://b23\.tv/[\w]+', '哔哩哔哩'),
    (r'https?://m\.bilibili\.com/video/BV[\w]+', '哔哩哔哩'),
    (r'https?://v\.kuaishou\.com/[A-Za-z0-9_-]+/?', '快手'),
    (r'https?://www\.kuaishou\.com/\S+', '快手'),
    (r'https?://www\.xiaohongshu\.com/explore/[\w]+', '小红书'),
    (r'https?://www\.xiaohongshu\.com/discovery/item/[\w]+', '小红书'),
    (r'https?://xhslink\.com/[A-Za-z0-9_-]+/?', '小红书'),
    (r'https?://www\.tiktok\.com/@[\w.-]+/video/\d+', 'TikTok'),
    (r'https?://vt\.tiktok\.com/[A-Za-z0-9_-]+/?', 'TikTok'),
    (r'https?://vm\.tiktok\.com/[A-Za-z0-9_-]+/?', 'TikTok'),
]

PLATFORM_NAMES = {
    '抖音': '🎵', '哔哩哔哩': '📺', '快手': '🎬',
    '小红书': '📕', 'TikTok': '🌍',
}

BOT_NAME = get_bot_name()
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def extract_url(text):
    for pattern, platform in PLATFORM_PATTERNS:
        match = re.search(pattern, text)
        if match:
            url = match.group(0)
            url = re.sub(r'[^\w/:-]$', '', url)
            if url.startswith('http://'):
                url = url.replace('http://', 'https://', 1)
            return url, platform
    return None, None


def collect_card_text(event, raw):
    """从事件消息段收集文本/链接，支持 QQ 分享卡片（CQ:share / CQ:json）。
    JSON 卡片中的 URL 带 \\/ 转义或 &#47; 实体，统一还原后再做平台匹配。"""
    import html as _html
    parts = []
    for seg in event.get("message", []) or []:
        st = seg.get("type", "")
        sd = seg.get("data", {}) or {}
        if st == "text":
            parts.append(sd.get("text", ""))
        elif st == "share":
            parts.append(sd.get("url", ""))
        elif st == "json":
            parts.append(sd.get("data", ""))
    parts.append(raw)
    text = "\n".join(x for x in parts if x)
    text = _html.unescape(text)
    # 还原 JSON 转义（\\/）与 HTML 实体斜杠（&#47;）
    text = text.replace('\\/', '/').replace('&#47;', '/')
    return text


def _req_headers(referer=None):
    h = {'User-Agent': _UA}
    if referer:
        h['Referer'] = referer
    return h


# ========== 哔哩哔哩：52api.cn API ==========

BILIBILI_API_URL = 'https://www.52api.cn/api/bilibili'

def _parse_bilibili_52api(url):
    """B站用 52api.cn API 解析"""
    headers, params = _build_52api_sign(url)
    if not headers:
        return None
    vlog("info", f"请求 52api.cn B站解析...")
    try:
        resp = http_get(BILIBILI_API_URL, params=params, headers=headers, timeout=15)
    except HttpError as e:
        vlog("error", f"52api B站请求异常: {e}")
        return None
    code = resp.get('code')
    if code not in (200, 0):
        vlog("error", f"52api B站业务错误: {resp.get('msg','')}")
        return None
    result_data = resp.get('data', {})
    if isinstance(result_data, str):
        try:
            result_data = json.loads(result_data)
        except json.JSONDecodeError:
            vlog("info", f"52api B站 data 不是合法 JSON")
            return None
    if not result_data or not isinstance(result_data, dict):
        return None
    # 提取视频URL（兼容多种返回格式）
    video_url = (
        result_data.get('url') or result_data.get('video_url') or
        result_data.get('video') or result_data.get('play_url') or ''
    )
    if not video_url:
        return None
    title = result_data.get('title') or result_data.get('desc') or ''
    author = (
        result_data.get('author') or result_data.get('owner') or
        result_data.get('up') or result_data.get('name') or ''
    )
    if isinstance(author, dict):
        author = author.get('name', '')
    return {
        'video_list': [video_url],
        'image_list': [],
        'title': title or '',
        'author': author or '',
    }

# ========== 哔哩哔哩：fallback 公开 API ==========
def _parse_bilibili(bvid):
    """B站用公开API取视频地址"""
    headers = _req_headers('https://www.bilibili.com/')
    info = requests.get(f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}',
                        headers=headers, timeout=15)
    if info.status_code != 200 or info.json().get('code') != 0:
        return None
    d = info.json()['data']
    title = d.get('title', '')
    author = d.get('owner', {}).get('name', '')
    cid = d['pages'][0]['cid']
    p = requests.get(
        f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnver=0&fnval=1',
        headers=headers, timeout=15
    )
    if p.status_code != 200 or p.json().get('code') != 0:
        return None
    video_data = p.json()['data']
    video_url = ''
    for item in video_data.get('durl', []):
        video_url = item.get('url', '') or video_url
    if not video_url:
        videos = video_data.get('dash', {}).get('video', [])
        if videos:
            videos.sort(key=lambda x: x.get('id', 0), reverse=True)
            video_url = videos[0].get('base_url', '')
    if not video_url:
        return None
    return {'video_list': [video_url], 'image_list': [], 'title': title, 'author': author}


# ========== 抖音：52api.cn API（HMAC-SHA256 签名认证）==========

DOUYIN_API_URL = 'https://www.52api.cn/api/douyin'


def _build_52api_sign(url):
    """生成 52api.cn HMAC-SHA256 签名请求参数"""
    api_key = get_config("52api_key", "")
    api_secret = get_config("52api_secret", "")
    if not api_key or not api_secret:
        vlog("info", f"未配置 52api_key 或 52api_secret")
        return None, None

    timestamp = int(time.time())
    sign_string = f"key={api_key}&timestamp={timestamp}"
    signature = hmac.new(
        api_secret.encode('utf-8'),
        sign_string.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        'X-Api-Key': api_key,
        'X-Api-Timestamp': str(timestamp),
        'X-Api-Sign': signature,
        'User-Agent': _UA,
    }
    params = {'key': api_key, 'url': url}
    return headers, params


def _parse_douyin(url):
    """抖音：通过 52api.cn API 解析（失败自动重试）"""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            vlog("info", f"抖音解析 第{attempt}次尝试...")
            result = _parse_douyin_api(url)
            if result:
                vlog("info", f"抖音第{attempt}次尝试成功")
                return result
            vlog("info", f"抖音第{attempt}次尝试返回空结果")
        except Exception as e:
            vlog("error", f"抖音第{attempt}次尝试异常: {e}")
            import traceback
            traceback.print_exc()
        if attempt < max_retries:
            time.sleep(2)
    vlog("error", f"抖音解析失败（已重试{max_retries}次）")
    return None


def _parse_douyin_api(url):
    """调用 52api.cn 解析抖音链接（HMAC-SHA256 签名认证）"""
    headers, params = _build_52api_sign(url)
    if not headers:
        return None

    vlog("info", f"请求 52api.cn ...")
    try:
        resp = http_get(DOUYIN_API_URL, params=params, headers=headers, timeout=10)
    except HttpError as e:
        vlog("error", f"52api 请求异常: {e}")
        return None

    code = resp.get('code')
    msg = resp.get('msg', '')
    vlog("info", f"52api 响应: code={code}, msg={msg}")

    if code not in (200, 0):
        vlog("error", f"52api 业务错误: {msg}")
        return None

    # data 可能是 dict 或 JSON 字符串
    result_data = resp.get('data', {})
    if isinstance(result_data, str):
        try:
            result_data = json.loads(result_data)
        except json.JSONDecodeError:
            vlog("info", f"52api data 不是合法 JSON: {result_data[:100]}")
            return None
    if not result_data or not isinstance(result_data, dict):
        vlog("info", f"52api data 为空")
        return None

    video_list = []
    image_list = []

    # 检查 work_type 判断多内容
    work_type = result_data.get('work_type', '')
    work_url_val = result_data.get('work_url')

    if work_type == 'images' and isinstance(work_url_val, list):
        # === 实况/图文：work_url 是列表，每项含 {stream, url, width, height} ===
        vlog("info", f"多内容解析: work_url 列表长度={len(work_url_val)}")
        for item in work_url_val:
            if isinstance(item, dict):
                stream_url = item.get('stream', '')
                img_url_val = item.get('url', '')
                if stream_url:
                    video_list.append(stream_url)
                if img_url_val:
                    image_list.append({'url': img_url_val})
            elif isinstance(item, str):
                video_list.append(item)
    else:
        # === 单视频：work_url 是字符串 ===
        if isinstance(work_url_val, str) and work_url_val:
            video_list = [work_url_val]
        elif isinstance(work_url_val, dict):
            # 单视频但 work_url 是 dict 的情况
            vu = work_url_val.get('stream') or work_url_val.get('url') or ''
            if vu:
                video_list = [vu]

    # 如果上面的 main 路径没提取到，尝试其他备用字段
    if not video_list and not image_list:
        for field in ('video_url', 'video', 'url', 'play_addr', 'play', 'videourl'):
            val = result_data.get(field)
            if isinstance(val, str) and val:
                video_list = [val]
                break
            if isinstance(val, dict):
                vu = val.get('stream') or val.get('url') or ''
                if vu:
                    video_list = [vu]
                    break

    # 提取标题和作者（兼容 52api.cn work_ 前缀格式）
    title = (
        result_data.get('work_title') or result_data.get('title') or
        result_data.get('desc') or ''
    )
    author = (
        result_data.get('work_author') or result_data.get('author') or
        result_data.get('nickname') or result_data.get('user_name') or
        result_data.get('author_name') or ''
    )

    if not video_list and not image_list:
        vlog("info", f"52api 未解析到视频或图片内容")
        return None

    return {
        'video_list': video_list,
        'image_list': image_list,
        'title': title or '',
        'author': author or '',
    }


# ========== TikTok：Playwright 浏览器提取 ==========
def _parse_tiktok(url):
    """TikTok：用 Playwright 打开页面提取视频URL"""
    try:
        result = asyncio.run(_parse_tiktok_async(url))
        return result
    except Exception as e:
        vlog("error", f"TikTok 解析异常: {e}")
        import traceback
        traceback.print_exc()
    return None

async def _parse_tiktok_async(url):
    """异步：用 Playwright 打开 TikTok 页面提取视频"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        vlog("info", f"需要 playwright")
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
        )
        page = await ctx.new_page()

        vlog("info", f"打开 TikTok 页面...")
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        except Exception as e:
            vlog("warn", f"TikTok 加载超时: {e}")
        await page.wait_for_timeout(5000)

        # 从页面提取视频URL
        video_url = await page.evaluate('''() => {
            // 方法1: video 标签
            const v = document.querySelector('video');
            if (v && v.getAttribute('src') && v.getAttribute('src').length > 10) {
                return v.getAttribute('src');
            }
            // 方法2: video source 标签
            const vs = document.querySelector('video source[src]');
            if (vs && vs.getAttribute('src')) return vs.getAttribute('src');
            // 方法3: 从 script 数据中提取
            const scripts = document.querySelectorAll('script[id*="__NEXT_DATA__"], script[id*="__UNIVERSAL_DATA"]');
            for (const s of scripts) {
                try {
                    const d = JSON.parse(s.textContent);
                    const video = d?.props?.pageProps?.videoData?.video?.playAddr?.UrlList?.[0]
                              || d?.props?.pageProps?.itemInfo?.itemStruct?.video?.playAddr?.UrlList?.[0]
                              || d?.props?.pageProps?.videoInfo?.itemStruct?.video?.playAddr?.[0];
                    if (video) return video;
                } catch(e) {}
            }
            // 方法4: 所有 video 类型链接
            const allVideo = document.querySelectorAll('[src*=".mp4"], [data-src*=".mp4"]');
            for (const el of allVideo) {
                const s = el.getAttribute('src') || el.getAttribute('data-src') || '';
                if (s) return s;
            }
            return '';
        }''')

        # 提取标题和作者
        info = await page.evaluate('''() => {
            const meta = document.querySelector('meta[property="og:title"]');
            const desc = document.querySelector('meta[name="description"]');
            const title = meta ? meta.getAttribute('content') : (desc ? desc.getAttribute('content') : '');
            return {
                title: (title || '').split('|')[0].trim() || '',
                author: document.querySelector('meta[property="og:video:tag"]')?.getAttribute('content')?.split(',')[0] || '',
            };
        }''')

        await browser.close()

        if video_url:
            vlog("info", f"TikTok 提取成功")
            return {
                'video_list': [video_url],
                'image_list': [],
                'title': info.get('title', '') or '',
                'author': info.get('author', '') or '',
            }
        vlog("info", f"TikTok 未提取到视频URL")
        return None


# ========== Playwright 模拟浏览器（快手、小红书） ==========
async def _parse_with_browser(url, platform):
    """用 Playwright 打开页面提取视频/图片"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        vlog("info", f"需要 playwright: pip3 install playwright && playwright install chromium")
        return None

    global _browser_pool
    if _browser_pool is None:
        p = await async_playwright().start()
        _browser_pool = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )

    ctx = await _browser_pool.new_context(
        user_agent=_UA,
        viewport={'width': 1920, 'height': 1080},
        locale='zh-CN',
    )
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(3000)

        if platform == '快手':
            # 尝试提取 video 标签
            video = await page.eval_on_selector_all(
                'video source[src]',
                'els => els.map(e => e.getAttribute("src")).filter(Boolean)'
            ) or await page.evaluate('''() => {
                const v = document.querySelector('video');
                return v ? v.getAttribute('src') || (v.querySelector("source")||{}).getAttribute("src") : null;
            }''')
            if video:
                v = video[0] if isinstance(video, list) else video
                return {'video_list': [v] if v else [], 'image_list': [], 'title': '', 'author': ''}
            # og:video
            og = await page.evaluate(
                "document.querySelector('meta[property=\"og:video\"]')?.getAttribute('content')")
            if og:
                return {'video_list': [og] if og else [], 'image_list': [], 'title': '', 'author': ''}

        elif platform == '小红书':
            await page.wait_for_timeout(3000)
            imgs = await page.evaluate(r'''() => {
                const urls = new Set();
                document.querySelectorAll('img').forEach(img => {
                    const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
                    if (src.includes('xhscdn.com') && !src.includes('avatar') && !src.includes('icon')) {
                        // 取最大图
                        urls.add(src.replace(/!thumbnail|!webp|!w\d+_\d+/g, '').replace('http://', 'https://'));
                    }
                });
                return [...urls];
            }''')
            if imgs:
                return {'video_list': [], 'image_list': [{'url': u} for u in imgs],
                        'title': '', 'author': ''}

        return None
    finally:
        await page.close()
        await ctx.close()


# ========== 入口调度 ==========
def parse_video(url, platform):
    """同步入口，根据平台选解析策略（带缓存池）"""
    # 先查缓存
    cached, hit = _cache_get(url)
    if hit and cached:
        return cached
    try:
        if platform == '哔哩哔哩':
            # 先用公开 API 解析（稳定可靠）
            bv_url = url
            if 'b23.tv' in url:
                try:
                    r = requests.get(url, headers=_req_headers('https://www.bilibili.com/'), allow_redirects=True, timeout=10)
                    bv_url = r.url
                except:
                    pass
            m = re.search(r'BV[\w]+', bv_url)
            if m:
                result = _parse_bilibili(m.group(0))
                if result:
                    _cache_set(url, result)
                    return result
            # fallback：52api.cn
            vlog("error", f"B站公开API失败，尝试 52api.cn")
            result = _parse_bilibili_52api(url)
            if result:
                _cache_set(url, result)
            return result
        elif platform == '抖音':
            result = _parse_douyin(url)
            if result:
                _cache_set(url, result)
            return result
        elif platform == 'TikTok':
            result = _parse_tiktok(url)
            if result:
                _cache_set(url, result)
            return result
        elif platform in ('快手', '小红书'):
            result = asyncio.run(_parse_with_browser(url, platform))
            if result:
                _cache_set(url, result)
            return result
    except Exception as e:
        vlog("error", f"解析 {platform} 失败: {e}")
        import traceback
        traceback.print_exc()
    return None


# ========== 消息发送 ==========
def send_video(event, video_url, platform, title, author):
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    # 类型安全：确保 video_url 是字符串
    if isinstance(video_url, (list, tuple)):
        video_url = video_url[0] if video_url else ''
    if not isinstance(video_url, str):
        video_url = str(video_url)
    msg = []
    if cfg("@_reply"):
        msg.append({"type": "at", "data": {"qq": user_id}})
    txt = f"\n📹 视频解析结果"
    if cfg("show_source") and platform:
        txt += f" ({PLATFORM_NAMES.get(platform, '')}{platform})"
    if title:
        txt += f"\n📌 {title[:50]}"
    if author:
        txt += f"\n👤 {author[:20]}"
    msg.append({"type": "text", "data": {"text": txt + "\n"}})
    msg.append({"type": "video", "data": {"file": video_url}})
    vlog("info", f"发送视频到群 {group_id}")
    send_message(event, msg)


def send_merge_forward(event, video_list, image_urls, platform, title, author):
    """抖音实况图/图文：合并转发（视频直链+图片URL）
    图片超 50 张时分批发送（QQ 单次合并转发上限约 50 条 node）
    """
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    max_img = cfg("max_images", 50)
    imgs = image_urls[:max_img]
    bot_qq = get_config("BOT_QQ", 0)
    bot_name = get_bot_name()

    txt = f"📦 视频解析结果"
    if cfg("show_source") and platform:
        txt += f" ({PLATFORM_NAMES.get(platform, '')}{platform})"
    if title:
        txt += f"\n📌 {title[:50]}"
    if author:
        txt += f"\n👤 {author[:20]}"
    if video_list:
        txt += f"\n🎬 {len(video_list)} 个视频"
    if imgs:
        txt += f"\n🖼️ {len(imgs)} 张图片"

    # 构建完整 nodes（含说明文字 + 视频 + 图片）
    def _build_nodes(start_img_idx, img_batch, with_header):
        nodes = []
        if with_header:
            nodes.append({
                "name": bot_name,
                "uin": str(bot_qq),
                "content": [{"type": "text", "data": {"text": txt}}]
            })
        for vu in video_list:
            if isinstance(vu, dict):
                vu = vu.get('stream') or vu.get('url') or ''
            if vu:
                nodes.append({
                    "name": bot_name,
                    "uin": str(bot_qq),
                    "content": [{"type": "video", "data": {"file": str(vu)}}]
                })
        for img_url in img_batch:
            nodes.append({
                "name": bot_name,
                "uin": str(bot_qq),
                "content": [{"type": "image", "data": {"file": img_url}}]
            })
        return nodes

    # 每批最多 45 个媒体 node（留余量避免超限），超 50 张图分批
    BATCH_MAX = 45
    total_media = len(video_list) + len(imgs)
    if total_media > BATCH_MAX:
        # 分批：第一批含说明文字，后续批次仅媒体
        batches = []
        # 视频放第一批，图片分摊
        vid_count = len(video_list)
        first_capacity = max(1, BATCH_MAX - vid_count)
        first_imgs = imgs[:first_capacity]
        batches.append(_build_nodes(0, first_imgs, True))
        remaining = imgs[first_capacity:]
        while remaining:
            chunk = remaining[:BATCH_MAX]
            batches.append(_build_nodes(len(imgs)-len(remaining), chunk, False))
            remaining = remaining[len(chunk):]
        for nodes in batches:
            if len(nodes) > 1:
                send_forward_msg(event, nodes)
                time.sleep(0.8)
        vlog("info", f"分批发送 {len(batches)} 组合并转发，共 {len(video_list)}视频{len(imgs)}图 到群 {group_id}")
    else:
        nodes = _build_nodes(0, imgs, True)
        if len(nodes) > 1:
            send_forward_msg(event, nodes)
            vlog("info", f"发送合并转发 {len(nodes)} 条到群 {group_id}")

    # 发送提示
    at_msg = []
    if cfg("@_reply"):
        at_msg.append({"type": "at", "data": {"qq": user_id}})
    at_msg.append({"type": "text", "data": {"text": f"\n📦 已发送 ({len(video_list)}视频{',' + str(len(imgs)) + '图' if imgs else ''})"}})
    send_message(event, at_msg)


def send_images(event, image_urls, platform, title, author):
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    max_img = cfg("max_images", 50)
    imgs = image_urls[:max_img]
    msg = []
    if cfg("@_reply"):
        msg.append({"type": "at", "data": {"qq": user_id}})
    txt = f"\n📸 图片解析结果"
    if cfg("show_source") and platform:
        txt += f" ({PLATFORM_NAMES.get(platform, '')}{platform})"
    if title:
        txt += f"\n📌 {title[:50]}"
    txt += f"\n共 {len(imgs)} 张图片"
    msg.append({"type": "text", "data": {"text": txt}})
    send_message(event, msg)
    for u in imgs:
        time.sleep(0.3)
        send_message(event, [{"type": "image", "data": {"file": u}}])
        vlog("info", f"发送图片 {group_id}")


def is_master(user_id):
    ml = get_config("MASTER_QQ", [])
    if not isinstance(ml, list):
        ml = [ml]
    return str(user_id) in [str(m) for m in ml]


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

# ========== 新解析核心接入（astrbot_plugin_parser 复刻版）==========
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
            vlog("info", f"新解析核心已预初始化")
        except Exception as e:
            vlog("error", f"解析核心预初始化失败: {e}")
    threading.Thread(target=_preinit_core, daemon=True).start()
except Exception as e:
    vlog("error", f"新解析核心接入失败: {e}")


def _send_parsed(event, result, platform):
    """发送旧逻辑解析结果：单视频直发，多视频/图文合集合并转发"""
    return _send_parsed(event, result, platform)


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

    # 抖音：优先旧逻辑（52api 对实况图/图文/合集支持完整：视频+图片合并转发，
    # 新核心 slides 的 image.video 常为空或带水印，实况图支持不完整）
    extracted_text = collect_card_text(event, raw)
    url, platform = extract_url(extracted_text)
    if not url or not platform:
        return False

    if platform == '抖音':
        vlog("info", f"{platform}: {url[:50]}...")
        send_message(event, f"⏳ 正在解析{platform}视频，请稍候...")
        result = parse_video(url, platform)
        if not result:
            # 旧逻辑失败 → 尝试新核心
            vlog("warn", "旧解析器失败，尝试新解析核心")
            try:
                from plugins.parser_bridge import handle_parse
                if handle_parse(event) == 2:
                    return True
            except Exception as _e2:
                vlog("error", f"新解析核心异常: {_e2}")
            vlog("error", f"{platform} 解析失败")
            return True
        return _send_parsed(event, result, platform)

    # 尝试新解析核心（astrbot_plugin_parser 复刻版：16 平台，含 B站卡片/动态/专栏等）
    # 返回 2=成功 1=未匹配 0=解析失败（提示后转旧逻辑）
    try:
        from plugins.parser_bridge import handle_parse
        result = handle_parse(event)
        if result == 2:
            return True
        if result == 0:
            # 新核心解析失败：提示切换解析器，再转旧逻辑
            vlog("warn", "新核心解析失败，切换回旧解析器")
            send_message(event, "⚠️ 新解析器解析失败，切换回旧解析器重试...")
    except Exception as _e:
        vlog("error", f"新解析核心异常，回退旧逻辑: {_e}")

    # 从 message 段提取 URL（支持 QQ 分享卡片：CQ:share / CQ:json）
    extracted_text = collect_card_text(event, raw)
    url, platform = extract_url(extracted_text)
    if not url or not platform:
        return False

    vlog("info", f"{platform}: {url[:50]}...")
    send_message(event, f"⏳ 正在解析{platform}视频，请稍候...")
    result = parse_video(url, platform)
    if not result:
        vlog("error", f"{platform} 解析失败")
        return True

    return _send_parsed(event, result, platform)
