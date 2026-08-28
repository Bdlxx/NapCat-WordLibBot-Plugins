# render.py — 简化渲染器
# 完整版基于 PIL + apilmoji 渲染媒体卡片；这里先提供接口兼容，
# render_card 返回 None 表示不渲染（sender 走文本+直发路径）。
# 后续如需卡片效果，可在此基础上移植原 render.py。

import asyncio
from pathlib import Path

from .data import ParseResult


class Renderer:
    def __init__(self, config):
        self.cfg = config

    @classmethod
    def load_resources(cls):
        """兼容原接口：加载渲染资源（此处无资源需要加载）"""
        return None

    async def render_card(self, result: ParseResult) -> Path | None:
        """兼容原接口：返回渲染图片路径；返回 None 表示不渲染卡片"""
        return None
