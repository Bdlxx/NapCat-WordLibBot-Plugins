# astrbot.core.message.components 兼容层
# 消息组件：持有数据，由我们自己的 sender 转换为 OneBot CQ 段


class BaseMessageComponent:
    """消息组件基类"""

    type: str = "unknown"

    def __init__(self, *args, **kwargs):
        pass

    def to_cq(self) -> dict:
        """转换为 OneBot 消息段"""
        return {"type": self.type, "data": {}}


class Plain(BaseMessageComponent):
    """文本"""

    type = "text"

    def __init__(self, text: str):
        super().__init__()
        self.text = str(text)

    def to_cq(self) -> dict:
        return {"type": "text", "data": {"text": self.text}}


class Image(BaseMessageComponent):
    """图片"""

    type = "image"

    def __init__(self, file: str = "", path: str = ""):
        super().__init__()
        self.file = file
        self.path = path

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(path=path, file=path)

    @classmethod
    def fromURL(cls, url: str):
        return cls(file=url)

    def to_cq(self) -> dict:
        return {"type": "image", "data": {"file": self.path or self.file}}


class Video(BaseMessageComponent):
    """视频"""

    type = "video"

    def __init__(self, file: str = "", path: str = ""):
        super().__init__()
        self.file = file
        self.path = path

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(path=path, file=path)

    @classmethod
    def fromURL(cls, url: str):
        return cls(file=url)

    def to_cq(self) -> dict:
        return {"type": "video", "data": {"file": self.path or self.file}}


class Record(BaseMessageComponent):
    """语音"""

    type = "record"

    def __init__(self, file: str = "", path: str = ""):
        super().__init__()
        self.file = file
        self.path = path

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(path=path, file=path)

    def to_cq(self) -> dict:
        return {"type": "record", "data": {"file": self.path or self.file}}


class File(BaseMessageComponent):
    """文件"""

    type = "file"

    def __init__(self, name: str = "", file: str = ""):
        super().__init__()
        self.name = name
        self.file = file

    def to_cq(self) -> dict:
        return {"type": "file", "data": {"file": self.file, "name": self.name}}


class At(BaseMessageComponent):
    """@ 某人"""

    type = "at"

    def __init__(self, qq: str | int):
        super().__init__()
        self.qq = str(qq)

    def to_cq(self) -> dict:
        return {"type": "at", "data": {"qq": self.qq}}


class Reply(BaseMessageComponent):
    """引用回复"""

    type = "reply"

    def __init__(self, message_id: str | int, chain=None):
        super().__init__()
        self.message_id = str(message_id)
        self.chain = chain or []

    def to_cq(self) -> dict:
        return {"type": "reply", "data": {"id": self.message_id}}


class Json(BaseMessageComponent):
    """JSON 卡片"""

    type = "json"

    def __init__(self, data: dict | str):
        super().__init__()
        self.data = data

    def to_cq(self) -> dict:
        import json
        if isinstance(self.data, dict):
            return {"type": "json", "data": {"data": json.dumps(self.data, ensure_ascii=False)}}
        return {"type": "json", "data": {"data": self.data}}


class Node(BaseMessageComponent):
    """合并转发节点"""

    type = "node"

    def __init__(self, uin: str = "", name: str = "", content: list | None = None):
        super().__init__()
        self.uin = str(uin)
        self.name = name
        self.content = content or []

    def to_cq(self) -> dict:
        return {"type": "node", "data": {"uin": self.uin, "name": self.name, "content": self.content}}


class Nodes(BaseMessageComponent):
    """合并转发"""

    type = "forward"

    def __init__(self, nodes: list | None = None):
        super().__init__()
        self.nodes = nodes or []

    def to_cq(self) -> dict:
        return {"type": "forward", "data": {}}
