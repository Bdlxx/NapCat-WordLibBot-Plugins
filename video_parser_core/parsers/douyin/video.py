from random import choice
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from msgspec import Struct, field

from ..base import ParseException


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    uri: str | None = None
    url_list: list[str] = field(default_factory=list)


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images] if self.images else []

    @property
    def video_url(self) -> str | None:
        if not self.video:
            return None
        # play_addr.uri 是完整 CDN URL 时直接使用（实测可直接下载；
        # 用 play_token 重建 aweme.snssdk.com/aweme/v1/play/ 对该 video_id 返回空）
        if self.video.play_addr.uri and "://" in self.video.play_addr.uri:
            return self.video.play_addr.uri
        token = self.play_token
        if token:
            return f"https://aweme.snssdk.com/aweme/v1/play/?video_id={token}&ratio=720p"
        if self.video.play_addr.url_list:
            return choice(self.video.play_addr.url_list).replace("playwm", "play")
        return None

    @property
    def play_token(self) -> str | None:
        if not self.video:
            return None

        play_addr = self.video.play_addr
        # uri 可能是纯 video_id（如 tos-cn-ve-2774/xxx），也可能是完整 URL
        if play_addr.uri:
            return self._extract_video_id(play_addr.uri)

        for url in play_addr.url_list:
            query = parse_qs(urlparse(url).query)
            if video_id := query.get("video_id"):
                return self._extract_video_id(video_id[0])
        return None

    @staticmethod
    def _extract_video_id(value: str) -> str | None:
        """从 video_id/uri 提取抖音 play API 所需的纯 ID：
        - 完整 URL（https://xxx.com/obj/tos-cn-ve-2774/abc）→ obj 路径
        - obj 路径（tos-cn-ve-2774/abc）→ 原样
        - 其他 URL → 末段"""
        if not value:
            return None
        # 完整 URL 或含 obj 路径：提取 /obj/ 后内容
        m = re.search(r"/obj/(.+?)(?:\?|$)", value)
        if m:
            return m.group(1)
        # 纯 ID（可能带 / 路径）→ 原样
        if "://" not in value:
            return value
        # 其他 URL → 末段
        return value.rstrip("/").split("/")[-1]

    @property
    def cover_url(self) -> str | None:
        return choice(self.video.cover.url_list) if self.video else None

    @property
    def avatar_url(self) -> str | None:
        if avatar := self.author.avatar_thumb:
            return choice(avatar.url_list)
        elif avatar := self.author.avatar_medium:
            return choice(avatar.url_list)
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ParseException("can't find data in videoInfoRes")
        return choice(self.item_list)


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(
        name="videoInfoRes", default_factory=VideoInfoRes
    )


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        if page := self.loader_data.video_page:
            return page.video_info_res.video_data
        elif page := self.loader_data.note_page:
            return page.video_info_res.video_data
        raise ParseException(
            "can't find video_(id)/page or note_(id)/page in router data"
        )
