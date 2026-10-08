"""各平台解析器：**逐个容错 import**。

为什么不能老老实实一行一个 `from .xhs import XHSParser`：Windows 上"某个平台的依赖
装漏/没有轮子/被安全软件拦"是常态，而这里任何一个 import 失败都会让**整包** ImportError
—— 现象是"发链接毫无反应、日志里只留一行 error"，极难排查。

改法：逐个 import，失败的记进 `FAILED_IMPORTS` 并跳过，其余平台照常可用，启动时打印。
消费方（`main.py`）只需要 `BaseParser`：平台是靠 `BaseParser.get_all_subclass()` 自动
发现的 —— 只要模块 import 过、类就注册上了，所以"降级"不会破坏契约。
"""

from .base import BaseParser

# 模块名 → 类名（原来的 19 行 import 就是这些）
_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("acfun", "AcfunParser"),
    ("bilibili", "BilibiliParser"),
    ("douyin", "DouyinParser"),
    ("instagram", "InstagramParser"),
    ("iwara", "IwaraParser"),
    ("kuaishou", "KuaiShouParser"),
    ("ncm", "NCMParser"),
    ("nga", "NGAParser"),
    ("qzone", "QZoneParser"),
    ("shipinhao", "ShipinhaoParser"),
    ("tiktok", "TikTokParser"),
    ("twitter", "TwitterParser"),
    ("weibo", "WeiBoParser"),
    ("xhs", "XHSParser"),
    ("xiaoheihe", "XiaoheiheParser"),
    ("youtube", "YouTubeParser"),
    ("zhihu", "ZhihuParser"),
    ("pixiv", "PixivParser"),
)

#: {平台模块名: 失败原因}，正常时为空；`main.py` 启动时会打印出来
FAILED_IMPORTS: dict[str, str] = {}

for _mod, _cls in _PLATFORMS:
    try:
        _m = __import__(f"{__name__}.{_mod}", fromlist=[_cls])
        globals()[_cls] = getattr(_m, _cls)          # 保持原有名字可用（向后兼容）
    except Exception as _e:                           # noqa: BLE001 — 缺一个平台不该拖垮全部
        FAILED_IMPORTS[_mod] = f"{type(_e).__name__}: {_e}"

__all__ = ["BaseParser"] + [c for _, c in _PLATFORMS if c in globals()]
