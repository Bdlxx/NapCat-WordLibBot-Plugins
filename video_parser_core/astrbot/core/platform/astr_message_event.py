# astrbot.core.platform.astr_message_event 兼容层
# AstrMessageEvent 的轻量替代：包装我们的 OneBot 事件，提供 send 能力


class AstrMessageEvent:
    """消息事件兼容类：包装 NapCat OneBot 事件，提供链式发送"""

    def __init__(self, event: dict, send_fn=None):
        self.event = event
        self._send_fn = send_fn  # fn(segs: list[BaseMessageComponent]) -> bool

    def get_self_id(self) -> str:
        return str(self.event.get("self_id", 0))

    def get_message_type(self):
        return self.event.get("message_type")

    def is_private_chat(self) -> bool:
        return self.event.get("message_type") == "private"

    def get_messages(self):
        return self.event.get("message", [])

    @property
    def message_str(self) -> str:
        return self.event.get("raw_message", "") or ""

    def unified_msg_origin(self) -> str:
        mt = self.event.get("message_type", "")
        if mt == "group":
            return f"group:{self.event.get('group_id', 0)}"
        return f"private:{self.event.get('user_id', 0)}"

    def chain_result(self, segs: list):
        """原接口返回消息链；我们的 sender 直接调用 send()"""
        return segs

    async def send(self, segs: list) -> None:
        if self._send_fn:
            ret = self._send_fn(segs)
            if hasattr(ret, "__await__"):
                await ret

    def __repr__(self):
        return f"AstrMessageEvent({self.event.get('message_type')})"
