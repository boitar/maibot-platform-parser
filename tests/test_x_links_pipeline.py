import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_x_pipeline as harness
from test_links import FULL, DISPLAY, card_segments


class CardHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = harness.XPipelineTests()
        await self.fixture.asyncSetUp()
        self.plugin = self.fixture.plugin
        config = self.plugin.get_plugin_config_data()
        config["parser"].update(debounce_seconds=0, enable_reply_parse=True)
        self.plugin.set_plugin_config(config)
        parser_type = importlib.import_module(
            "x_pipeline_plugin.core.parsers.twitter"
        ).TwitterParser
        cfg = harness.plugin_module.CorePluginConfig(
            data_dir=self.fixture.root / "parser", enabled_platforms=["twitter"]
        )
        self.parser = parser_type(cfg, SimpleNamespace())
        self.parser._req_fxtwitter_api = AsyncMock()
        self.plugin._parsers = [self.parser]
        self.plugin._process_task = AsyncMock()

    async def asyncTearDown(self):
        await asyncio.gather(*tuple(self.plugin._tasks), return_exceptions=True)
        await self.parser.close_session()
        await self.fixture.asyncTearDown()

    async def assert_full_id_selected(self, message):
        result = await self.plugin.handle_link(message=message, stream_id="test-stream")
        await asyncio.sleep(0)
        self.assertEqual(result, {"action": "abort"})
        self.plugin._process_task.assert_awaited_once()
        args = self.plugin._process_task.call_args.args
        self.assertTrue(args[2].group(0).endswith("2114720901698847166"))
        self.assertEqual(args[3], FULL)
        self.assertEqual(self.fixture.requests, [])

    async def test_current_adapter_card_text_selects_full_id(self):
        message = self.fixture.message | {
            "processed_plain_text": DISPLAY + " 链接: " + FULL
        }
        await self.assert_full_id_selected(message)

    async def test_raw_json_card_selects_full_id(self):
        message = self.fixture.message | {
            "processed_plain_text": DISPLAY,
            "raw_message": card_segments(),
        }
        await self.assert_full_id_selected(message)

    async def test_reply_to_card_selects_full_id(self):
        message = self.fixture.message | {
            "raw_message": [{"type": "reply", "data": {"id": "77"}}]
        }
        with patch.object(
            harness.plugin_module,
            "get_message",
            AsyncMock(return_value=card_segments()),
        ):
            await self.assert_full_id_selected(message)

    async def test_truncated_only_card_requests_complete_link_without_fetching_post(
        self,
    ):
        message = self.fixture.message | {"processed_plain_text": DISPLAY}
        result = await self.plugin.handle_link(message=message, stream_id="")
        self.assertEqual(result, {"action": "abort"})
        self.plugin._process_task.assert_not_awaited()
        self.parser._req_fxtwitter_api.assert_not_awaited()
        self.assertEqual(len(self.fixture.requests), 1)
        action, payload = self.fixture.requests[0]
        self.assertEqual(action, "send_private_msg")
        self.assertIn("完整链接", payload["message"][0]["data"]["text"])


if __name__ == "__main__":
    unittest.main()
