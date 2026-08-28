# config.py — 简化配置层
# 原版依赖 AstrBotConfig/Context，这里替换为扁平配置对象（属性访问）
# 供解析核心（download/parsers/base）读取，无需改动原逻辑

import json
import os
from pathlib import Path
from types import SimpleNamespace

# 兼容原类型名：各解析器子配置项
ParserItem = SimpleNamespace


class PluginConfig:
    """扁平配置：用属性访问提供解析核心所需的所有配置项"""

    def __init__(self, config_dir: str | None = None, data_dir: str | None = None):
        # 缓存目录：默认放在实例 data/parser_cache
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = Path(data_dir or os.path.join(base, "data"))
        self.cache_dir = Path(config_dir or os.path.join(str(self.data_dir), "parser_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = Path(os.path.join(str(self.data_dir), "parser_cookies"))
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        # 下载
        self.download_retry_times = 3
        self.download_timeout = 60
        self.source_max_size = 300      # MB
        self.max_duration = 60 * 60     # 秒
        self.max_size = 300             # MB（兼容旧字段）

        # 网络
        self.proxy = None
        self.common_timeout = 60

        # 发送
        self.forward_threshold = 4      # 消息段达到该数量强制合并转发
        self.single_heavy_render_card = False  # 单重媒体渲染卡片（暂不启用）
        self.show_download_fail_tip = True
        self.audio_to_file = False

        # 白名单/黑名单（统一会话，简化）
        self.whitelist: list[str] = []
        self.blacklist: list[str] = []
        self.require_at_in_group = False
        self.debounce_interval = 30     # 防抖秒数

        # 解析器子配置（各平台）
        self.parser = SimpleNamespace(
            enabled_platforms=lambda: [
                "bilibili", "douyin", "kuaishou", "xhs", "weibo",
                "tiktok", "youtube", "acfun", "instagram", "twitter",
                "pixiv", "shipinhao", "zhihu", "xiaoheihe", "iwara", "ncm",
            ],
            bilibili=SimpleNamespace(
                video_quality="P_720P",
                video_codec_list=["AVC"],
                use_proxy=False,
                max_page=5,
                name="bilibili",
                cookies="",
            ),
            douyin=SimpleNamespace(use_proxy=False,
                name="douyin",
                cookies=""),
            kuaishou=SimpleNamespace(use_proxy=False,
                name="kuaishou",
                cookies=""),
            xhs=SimpleNamespace(use_proxy=False,
                name="xhs",
                cookies=""),
            weibo=SimpleNamespace(use_proxy=False,
                name="weibo",
                cookies=""),
            tiktok=SimpleNamespace(use_proxy=False,
                name="tiktok",
                cookies=""),
            youtube=SimpleNamespace(use_proxy=False,
                name="youtube",
                cookies=""),
            acfun=SimpleNamespace(use_proxy=False,
                name="acfun",
                cookies=""),
            instagram=SimpleNamespace(use_proxy=False,
                name="instagram",
                cookies=""),
            twitter=SimpleNamespace(use_proxy=False,
                name="twitter",
                cookies=""),
            pixiv=SimpleNamespace(use_proxy=False, nsfw=False,
                name="pixiv",
                cookies=""),
            shipinhao=SimpleNamespace(use_proxy=False,
                name="shipinhao",
                cookies=""),
            zhihu=SimpleNamespace(use_proxy=False,
                name="zhihu",
                cookies=""),
            xiaoheihe=SimpleNamespace(use_proxy=False,
                name="xiaoheihe",
                cookies=""),
            iwara=SimpleNamespace(use_proxy=False,
                name="iwara",
                cookies=""),
            ncm=SimpleNamespace(use_proxy=False,
                name="ncm",
                cookies=""),
            qzone=SimpleNamespace(use_proxy=False, send_blue_links=False,
                name="qzone",
                cookies=""),
        )

        # cookies（可后续从 web 面板配置）
        self.cookies: dict[str, str] = {}
        self.timezone = "Asia/Shanghai"

    # 兼容原 blacklist 会话接口（简化：全平台统一）
    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)

    # 从 JSON 覆盖配置
    def load_from_json(self, path: str):
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for k, v in data.items():
            if hasattr(self, k) and not k.startswith("_"):
                setattr(self, k, v)
