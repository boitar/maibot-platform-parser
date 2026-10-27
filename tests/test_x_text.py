import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from core.config import PluginConfig
from core.data import Author, ParseResult, Platform
from core.exception import ParseException
from core.parsers.twitter import TwitterParser
from core.x_text import Translation, XTranslator, post_text_nodes, split_text


def translation_settings(**updates):
    values = dict(
        enabled=True,
        target_language="zh-CN",
        task_name="utils",
        timeout_seconds=1,
        chunk_chars=1600,
        cache_ttl_seconds=60,
        cache_max_entries=2,
    )
    return SimpleNamespace(**(values | updates))


class TwitterDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = PluginConfig(data_dir=Path(self.temp.name), enabled_platforms=["twitter"])
        downloader = SimpleNamespace(
            download_img=lambda *a, **k: Path("image.jpg"),
            download_video=lambda *a, **k: Path("video.mp4"),
        )
        self.parser = TwitterParser(cfg, downloader)

    async def asyncTearDown(self):
        await self.parser.close_session()
        self.temp.cleanup()

    def payload(self, **values):
        post = dict(
            id="12345",
            author={"name": "示例作者", "screen_name": "@sample", "id": "99"},
            text="line one\n日本語 #tag @friend",
            lang="ja",
            url="https://x.com/sample/status/12345",
            media={},
        )
        return {"code": 200, "status": post | values}

    def test_identity_and_original_are_structured(self):
        result = self.parser.parse_fxtwitter_json(
            self.payload(), "12345", "https://x.com/wrong/status/12345"
        )
        self.assertEqual(result.author.name, "示例作者")
        self.assertEqual(result.extra["author_handle"], "sample")
        self.assertEqual(result.extra["author_id"], "99")
        self.assertEqual(result.text, "line one\n日本語 #tag @friend")
        self.assertEqual(result.url, "https://x.com/sample/status/12345")

    def test_old_envelope_missing_name_and_raw_text(self):
        payload = self.payload(
            author={"screen_name": "sample"}, text=None, raw_text={"text": "原文\n"}
        )
        payload["tweet"] = payload.pop("status")
        result = self.parser.parse_fxtwitter_json(
            payload, "12345", "https://x.com/sample/status/12345"
        )
        self.assertIsNone(result.author)
        self.assertEqual(result.extra["author_handle"], "sample")
        self.assertEqual(result.text, "原文\n")

    def test_wrong_post_and_tombstones_are_rejected(self):
        for payload in (
            self.payload(id="987"),
            {"status": {"type": "tombstone"}},
            {"status": None},
        ):
            with self.subTest(payload=payload), self.assertRaises(ParseException):
                self.parser.parse_fxtwitter_json(
                    payload, "12345", "https://x.com/sample/status/12345"
                )

    def test_media_is_deduplicated_and_keeps_primary_order(self):
        photo = {"type": "photo", "url": "https://media.example/a.png"}
        video = {"type": "video", "url": "https://media.example/b.mp4"}
        result = self.parser.parse_fxtwitter_json(
            self.payload(
                media={"all": [video, photo], "photos": [photo], "videos": [video]}
            ),
            "12345",
            "https://x.com/sample/status/12345",
        )
        self.assertEqual(
            [type(item).__name__ for item in result.contents],
            ["VideoContent", "ImageContent"],
        )

    async def test_text_only_post_does_not_require_media_fallback(self):
        self.parser._req_fxtwitter_api = AsyncMock(return_value=self.payload())
        self.parser._req_xdown_api = AsyncMock()
        keyword, match = self.parser.search_url("https://x.com/i/web/status/12345")
        result = await self.parser.parse(keyword, match)
        self.assertTrue(result.text)
        self.assertEqual(result.contents, [])
        self.parser._req_xdown_api.assert_not_awaited()

    async def test_media_fallback_does_not_invent_author_or_original(self):
        self.parser._req_fxtwitter_api = AsyncMock(
            side_effect=ParseException("offline")
        )
        self.parser._req_xdown_api = AsyncMock(
            return_value={
                "status": "ok",
                "data": '<h3>Download page</h3><a class="tw-button-dl" href="https://media.example/a.jpg">下载图片</a>',
            }
        )
        keyword, match = self.parser.search_url(
            "https://twitter.com/sample/status/12345"
        )
        result = await self.parser.parse(keyword, match)
        self.assertTrue(result.extra["details_unavailable"])
        self.assertIsNone(result.author)
        self.assertIsNone(result.text)
        self.assertIsNone(result.title)
        self.assertEqual(len(result.contents), 1)


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_and_changed_text_or_task(self):
        translator = XTranslator()
        model = AsyncMock(
            return_value={"success": True, "response": '{"translation":"你好"}'}
        )
        settings = translation_settings()
        for _ in range(2):
            self.assertEqual(
                (
                    await translator.translate(
                        "hello", "en", settings, model, post_id="1"
                    )
                ).text,
                "你好",
            )
        self.assertEqual(model.await_count, 1)
        prompt = model.call_args.kwargs["prompt"]
        self.assertEqual([item["role"] for item in prompt], ["system", "user"])
        self.assertEqual(prompt[1]["content"], "hello")
        await translator.translate("hello again", "en", settings, model, post_id="1")
        await translator.translate(
            "hello",
            "en",
            translation_settings(task_name="fast_model"),
            model,
            post_id="1",
        )
        self.assertEqual(model.await_count, 3)
        translator.clear()
        await translator.translate("hello", "en", settings, model, post_id="1")
        self.assertEqual(model.await_count, 4)

    async def test_empty_chinese_and_disabled_do_not_call_model(self):
        translator = XTranslator()
        model = AsyncMock()
        for text, language, settings in (
            ("", "", translation_settings()),
            ("中文", "zh", translation_settings()),
            ("Hello", "en", translation_settings(enabled=False)),
        ):
            result = await translator.translate(text, language, settings, model)
            self.assertNotEqual(result.status, "ok")
        model.assert_not_awaited()

    async def test_timeout_and_partial_failures_are_not_cached(self):
        translator = XTranslator()

        async def slow(**kwargs):
            await asyncio.sleep(10)

        result = await translator.translate(
            "hello", "en", translation_settings(timeout_seconds=0.001), slow
        )
        self.assertIn("翻译暂不可用", result.text)
        model = AsyncMock(
            side_effect=[
                {"success": True, "response": '{"translation":"第一段"}'},
                {"success": False},
            ]
            * 2
        )
        for _ in range(2):
            result = await translator.translate(
                "a" * 250, "en", translation_settings(chunk_chars=200), model
            )
            self.assertEqual(result.status, "partial")
            self.assertIn("第一段", result.text)
            self.assertIn("第 2 段", result.text)
        self.assertEqual(model.await_count, 4)

    async def test_only_translation_field_is_displayed(self):
        model = AsyncMock(
            return_value={
                "success": True,
                "reasoning": "private reasoning",
                "response": '```json\n{"translation":"译文","reasoning":"hidden"}\n```',
            }
        )
        result = await XTranslator().translate(
            "original", "en", translation_settings(), model
        )
        self.assertEqual(result.text, "译文")
        model.return_value = {"success": True, "response": "explanation without JSON"}
        result = await XTranslator().translate(
            "original", "en", translation_settings(), model
        )
        self.assertIn("暂不可用", result.text)

    async def test_cancellation_propagates(self):
        ready = asyncio.Event()

        async def waiting(**kwargs):
            ready.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            XTranslator().translate("original", "en", translation_settings(), waiting)
        )
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_long_original_is_complete_in_forward_nodes(self):
        text = ("日本語 #tag @sample\n\n" * 220) + "尾部"
        self.assertEqual("".join(split_text(text)), text)
        result = ParseResult(
            Platform("twitter", "X"),
            author=Author("昵称"),
            text=text,
            extra={"author_handle": "sample"},
        )
        nodes = post_text_nodes(result, Translation("翻译" * 1600, "ok"))
        self.assertEqual("".join(nodes), f"昵称(@sample)\n{text}\n" + "翻译" * 1600)
        self.assertTrue(all(len(node) <= 1600 for node in nodes))
        self.assertIn("昵称(@sample)", nodes[0])

    def test_compact_layout_has_no_headings_or_source_footer(self):
        result = ParseResult(
            Platform("twitter", "X"),
            author=Author("昵称"),
            text="Original\nline two",
            url="https://x.com/sample/status/12345",
            extra={"author_handle": "sample"},
        )
        self.assertEqual(
            post_text_nodes(result, Translation("中文译文", "ok")),
            ["昵称(@sample)\nOriginal\nline two\n中文译文"],
        )
        self.assertEqual(
            post_text_nodes(
                result, Translation("原文为中文，无需翻译", "same_language")
            ),
            ["昵称(@sample)\nOriginal\nline two"],
        )


if __name__ == "__main__":
    unittest.main()
