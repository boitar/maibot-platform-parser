import asyncio
import logging
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

logger = logging.getLogger("plugin.multi_platform_parser.twitter")
STATUS_PATH = r"(?:(?:[A-Za-z0-9_]+/)*status|statuses)/[0-9]+(?=$|[/?#\s])"
FX_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "MaiBot-platform-parser/1.8.0-custom",
}
MEDIA_HEADERS = {"User-Agent": FX_HEADERS["User-Agent"], "Referer": "https://x.com/"}


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
        (r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/" + STATUS_PATH),
    )
    @handle(
        "x.com",
        (r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/" + STATUS_PATH),
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        url = f"https://{searched.group(0)}"
        tweet_id = re.search(r"/(?:status|statuses)/(\d+)", url).group(1)
        try:
            payload = await self._req_fxtwitter_api(tweet_id)
            return self.parse_fxtwitter_json(payload, tweet_id, url)
        except (
            ClientError,
            TimeoutError,
            ValueError,
            TypeError,
            ParseException,
        ) as exc:
            logger.warning(
                "FxTwitter details unavailable: post=%s error=%s; trying media fallback",
                tweet_id,
                type(exc).__name__,
            )
        resp = await self._req_xdown_api(url)
        if resp.get("status") != "ok":
            raise ParseException("解析失败")

        html_content = resp.get("data")

        if html_content is None:
            raise ParseException("解析失败, 数据为空")

        result = self.parse_twitter_html(html_content)
        result.url = url
        result.extra.update(tweet_id=tweet_id, details_unavailable=True)
        return result

    async def _req_fxtwitter_api(self, tweet_id: str) -> dict[str, Any]:
        # Keep xdown cookies/headers off the FxTwitter request.
        for attempt in range(2):
            try:
                async with self.session.get(
                    f"https://api.fxtwitter.com/2/status/{tweet_id}",
                    headers=FX_HEADERS,
                ) as response:
                    response.raise_for_status()
                    payload = await response.json()
                if not isinstance(payload, dict) or payload.get("code") != 200:
                    raise ParseException("X 详情接口未返回可用推文")
                return payload
            except (ClientError, TimeoutError):
                if attempt:
                    raise
                await asyncio.sleep(0.5)
        raise ParseException("X 详情接口不可用")

    def parse_fxtwitter_json(
        self, payload: dict[str, Any], tweet_id: str, url: str
    ) -> ParseResult:
        post = payload.get("tweet")
        if not isinstance(post, dict):
            post = payload.get("status")
        if not isinstance(post, dict) or post.get("type") == "tombstone":
            raise ParseException("X 原推文不可用")
        if str(post.get("id", tweet_id)) != tweet_id:
            raise ParseException("X 详情与原链接不匹配")
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        name = str(author.get("name") or "").strip()
        handle = str(author.get("screen_name") or "").strip().lstrip("@")
        raw_text = post.get("raw_text")
        text = post.get("text")
        if not isinstance(text, str) and isinstance(raw_text, dict):
            text = raw_text.get("text")
        text = text if isinstance(text, str) else ""
        media = post.get("media") if isinstance(post.get("media"), dict) else {}
        ordered = media.get("all") or []
        items = [(item, "") for item in ordered if isinstance(item, dict)]
        items.extend(
            (item, "photo")
            for item in media.get("photos", []) or []
            if isinstance(item, dict)
        )
        items.extend(
            (item, "video")
            for item in media.get("videos", []) or []
            if isinstance(item, dict)
        )
        external = media.get("external")
        if isinstance(external, dict):
            items.append((external, "video"))
        seen = set()
        contents = []
        for item, default_type in items:
            media_url = item.get("url")
            if (
                not isinstance(media_url, str)
                or not media_url.startswith(("https://", "http://"))
                or media_url in seen
            ):
                continue
            seen.add(media_url)
            kind = item.get("type") or default_type
            if kind == "photo":
                contents.extend(
                    self.create_image_contents([media_url], headers=MEDIA_HEADERS)
                )
            elif kind in ("video", "gif"):
                contents.append(
                    self.create_video_content(media_url, headers=MEDIA_HEADERS)
                )
        source_url = post.get("url")
        if not isinstance(source_url, str) or not re.match(
            rf"https://(?:www\.)?(?:x|twitter)\.com/[^?#]+/(?:status|statuses)/{tweet_id}(?:[/?#]|$)",
            source_url,
        ):
            source_url = url
        return self.result(
            author=self.create_author(name) if name else None,
            text=text,
            contents=contents,
            url=source_url,
            extra={
                "tweet_id": tweet_id,
                "author_handle": handle,
                "author_id": str(author.get("id") or ""),
                "source_language": str(post.get("lang") or ""),
                "details_unavailable": False,
            },
        )

    def parse_twitter_html(self, html_content: str) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # 初始化数据
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
            # xdown's h3 is a download-page title, not verified original post text.
            author=None,
            contents=contents,
            extra={"details_unavailable": True},
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):
