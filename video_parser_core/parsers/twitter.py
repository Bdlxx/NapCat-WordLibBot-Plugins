import re
from itertools import chain
from typing import Any, ClassVar

from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="推特")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.twitter
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://xdown.app",
                "Referer": "https://xdown.app/",
            }
        )
        self.xdown_url = "https://xdown.app/api/ajaxSearch"
        self.cookiejar = CookieJar(config, self.mycfg, domain="xdown.app")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    async def _req_xdown_api(self, url: str) -> dict[str, Any]:
        async with self.session.post(
            url=self.xdown_url,
            data={"q": url, "lang": "zh-cn"},
            headers=self.headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"xdown API {resp.status} {resp.reason}")
            return await resp.json()

    @handle(
        "twitter.com",
        (
            r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    @handle(
        "x.com",
        (
            r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        # 从匹配对象中获取原始URL
        url = f"https://{searched.group(0)}"
        resp = await self._req_xdown_api(url)
        if resp.get("status") != "ok":
            raise ParseException("解析失败")

        html_content = resp.get("data")

        if html_content is None:
            raise ParseException("解析失败, 数据为空")

        # 补充作者/标题：xdown 返回的 HTML 只含媒体下载链接，不含作者，
        # 用 fxtwitter（优先）/ oEmbed（兜底）获取推文作者与正文
        tweet_id = None
        if m := re.search(r"status/(\d+)", url):
            tweet_id = m.group(1)
        author_name = None
        title = None
        if tweet_id:
            try:
                info = await self._fetch_tweet_info(tweet_id)
                if info:
                    author_name = info.get("author")
                    title = info.get("text")
            except Exception:
                pass

        return self.parse_twitter_html(
            html_content, author_name=author_name, title=title
        )

    async def _fetch_tweet_info(self, tweet_id: str) -> dict[str, Any] | None:
        """获取推文作者（screen_name）与正文：fxtwitter 优先，oEmbed 兜底"""
        # 1. fxtwitter（信息全：作者 + 正文）
        try:
            async with self.session.get(
                f"https://api.fxtwitter.com/status/{tweet_id}",
                headers={"User-Agent": self.headers.get("User-Agent", "Mozilla/5.0")},
                timeout=15,
            ) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    tw = j.get("tweet") or {}
                    author = (tw.get("author") or {})
                    if author.get("screen_name"):
                        return {
                            "author": author.get("screen_name"),
                            "text": (tw.get("text") or "")[:300],
                        }
        except Exception:
            pass
        # 2. oEmbed 兜底（author_url 含 handle，如 https://x.com/CandyXQwQ）
        try:
            async with self.session.get(
                "https://publish.twitter.com/oembed",
                params={"url": f"https://x.com/i/status/{tweet_id}"},
                headers={"User-Agent": self.headers.get("User-Agent", "Mozilla/5.0")},
                timeout=15,
            ) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    author_url = j.get("author_url") or ""
                    handle = author_url.rstrip("/").split("/")[-1]
                    if handle:
                        return {"author": handle, "text": ""}
        except Exception:
            pass
        return None

    def parse_twitter_html(
        self,
        html_content: str,
        author_name: str | None = None,
        title: str | None = None,
    ) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容（xdown 返回，含媒体下载链接）
            author_name (str | None): 作者 screen_name（fxtwitter/oEmbed 补充）
            title (str | None): 推文正文（fxtwitter 补充）

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # 初始化数据
        title = title or None
        cover_url = None
        video_url = None
        images_urls = []
        dynamic_urls = []

        # 1. 提取缩略图链接
        thumb_tag = soup.find("img")
        if isinstance(thumb_tag, Tag):
            if cover := thumb_tag.get("src"):
                cover_url = str(cover)

        # 2. 提取下载链接
        tw_button_tags = soup.find_all("a", class_="tw-button-dl")
        abutton_tags = soup.find_all("a", class_="abutton")
        for tag in chain(tw_button_tags, abutton_tags):
            if not isinstance(tag, Tag):
                continue
            href = tag.get("href")
            if href is None:
                continue

            href = str(href)
            text = tag.get_text(strip=True)
            if "下载 MP4" in text:
                video_url = href
                break
            elif "下载图片" in text:
                images_urls.append(href)
            elif "下载 gif" in text:
                dynamic_urls.append(href)

        # 3. 提取标题
        title_tag = soup.find("h3")
        if title_tag:
            title = title_tag.get_text(strip=True)

        # 简洁的构建方式
        contents = []

        # 添加视频内容
        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        if images_urls:
            contents.extend(self.create_image_contents(images_urls))

        # 添加动态内容
        if dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        return self.result(
            title=title,
            author=self.create_author(author_name or "无用户名"),
            contents=contents,
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):
