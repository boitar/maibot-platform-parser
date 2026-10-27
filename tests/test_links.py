import json
import unittest

from core.exception import ParseException
from core.links import extract_urls, has_incomplete_x_post, message_source
from core.parsers.twitter import TwitterParser


FULL = "https://x.com/i/web/status/2114720901698847166"
DISPLAY = "https://x.com/i/web/status/211…：https://x.com/i/web/status/2114720901698…"


def card_segments():
    return [
        {
            "type": "json",
            "data": {
                "data": json.dumps(
                    {
                        "app": "com.tencent.tuwen.lua",
                        "meta": {
                            "news": {
                                "title": "https://x.com/i/web/status/211…",
                                "desc": "https://x.com/i/web/status/2114720901698…",
                                "jumpUrl": FULL,
                                "preview": "https://image.example/thumbnail.jpg",
                            }
                        },
                    }
                )
            },
        }
    ]


class CardLinkTests(unittest.TestCase):
    def test_actual_flattened_card_selects_complete_jump_url(self):
        self.assertEqual(extract_urls(DISPLAY + " 链接: " + FULL), [FULL])

    def test_nested_json_target_precedes_truncated_display(self):
        source = message_source(
            {"processed_plain_text": DISPLAY, "raw_message": card_segments()}
        )
        self.assertTrue(source.startswith("链接: " + FULL))
        self.assertEqual(extract_urls(source), [FULL])

    def test_host_text_segment_and_platform_metadata_are_supported(self):
        for message in (
            {"raw_message": [{"type": "text", "data": DISPLAY + " 链接: " + FULL}]},
            {
                "processed_plain_text": DISPLAY,
                "message_info": {
                    "additional_config": {
                        "platform_card_payloads": [
                            {"payload": {"meta": {"news": {"jumpUrl": FULL}}}}
                        ]
                    }
                },
            },
        ):
            with self.subTest(message=message):
                self.assertEqual(extract_urls(message_source(message)), [FULL])

    def test_card_without_jump_url_can_use_complete_title(self):
        message = {
            "raw_message": [
                {
                    "type": "json",
                    "data": {
                        "data": json.dumps(
                            {
                                "meta": {
                                    "news": {
                                        "title": FULL,
                                        "preview": "https://image.example/preview.jpg",
                                    }
                                }
                            }
                        )
                    },
                }
            ]
        }
        self.assertEqual(extract_urls(message_source(message)), [FULL])

    def test_explicit_target_wins_over_short_but_valid_display_link(self):
        old = "https://x.com/i/web/status/211"
        self.assertEqual(extract_urls(old + " 链接: " + FULL), [FULL, old])
        self.assertEqual(
            extract_urls(
                message_source(
                    {
                        "processed_plain_text": old,
                        "raw_message": [{"type": "card", "data": {"jumpUrl": FULL}}],
                    }
                )
            ),
            [FULL, old],
        )

    def test_truncated_or_malformed_ids_never_become_numeric_prefixes(self):
        for suffix in (
            "211…",
            "211...",
            "211⋯",
            "211abc",
            "211%E2%80%A6",
            "2114720901698…",
        ):
            url = "https://x.com/i/web/status/" + suffix
            with self.subTest(url=url):
                self.assertEqual(extract_urls(url), [])
                self.assertTrue(has_incomplete_x_post(url))
                with self.assertRaises(ParseException):
                    TwitterParser.search_url(url)

    def test_complete_old_and_modern_ids_media_suffixes_and_queries_still_work(self):
        for url in (
            "https://x.com/i/web/status/211",
            FULL,
            FULL + "?s=20",
            FULL + "/photo/1",
            "https://twitter.com/sample/status/20",
            "https://twitter.com/statuses/20",
        ):
            with self.subTest(url=url):
                self.assertEqual(extract_urls(url), [url])
                _, matched = TwitterParser.search_url(url)
                self.assertIn("/status", matched.group(0))

    def test_non_x_urls_keep_their_order_and_punctuation_handling(self):
        text = "https://www.bilibili.com/video/BV1demo，https://example.com/a?x=1."
        self.assertEqual(
            extract_urls(text),
            ["https://www.bilibili.com/video/BV1demo", "https://example.com/a?x=1"],
        )


if __name__ == "__main__":
    unittest.main()
