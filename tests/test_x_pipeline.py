import asyncio
import base64
import importlib.util
from io import BytesIO
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "x_pipeline_plugin", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
)
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)
data = sys.modules["x_pipeline_plugin.core.data"]


class XPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "cached.jpg"
        Image.new("RGBA", (96, 64), (100, 80, 150, 120)).save(self.source, format="PNG")
        self.original = self.source.read_bytes()
        self.requests = []
        self.forward_ok = True
        app = web.Application()
        app.router.add_post("/{action}", self.onebot)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.plugin = plugin_module.MultiPlatformParserPlugin()
        self.plugin.set_plugin_config(
            {
                "plugin": {"config_version": "1.7.4"},
                "api": {
                    "host": "127.0.0.1",
                    "port": self.runner.addresses[0][1],
                    "bot_uin": "10000",
                },
                "media_randomizer": {
                    "weight_reencode": 0,
                    "weight_border": 1,
                    "weight_crop": 0,
                    "weight_canvas": 0,
                    "weight_edge_noise": 0,
                    "weight_pixel_noise": 0,
                    "border_min_px": 1,
                    "border_max_px": 1,
                },
            }
        )
        self.send = SimpleNamespace(
            forward=AsyncMock(return_value={"sent": False}),
            text=AsyncMock(return_value=False),
            image=AsyncMock(return_value=False),
        )
        self.llm = SimpleNamespace(
            generate=AsyncMock(
                return_value={
                    "success": True,
                    "response": '{"translation":"新插画完成啦！"}',
                }
            )
        )
        self.plugin._set_context(
            SimpleNamespace(
                send=self.send,
                llm=self.llm,
                logger=logging.getLogger("x_pipeline_test"),
                paths=SimpleNamespace(data_dir=self.root / "plugin-data"),
            )
        )
        self.message = {
            "message_info": {"user_info": {"user_id": "10001"}, "group_info": None}
        }

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def onebot(self, request):
        payload = await request.json()
        action = request.match_info["action"]
        self.requests.append((action, payload))
        ok = self.forward_ok or "forward" not in action
        return web.json_response(
            {
                "status": "ok" if ok else "failed",
                "retcode": 0 if ok else 1200,
                "data": {"message_id": 123} if ok else None,
            }
        )

    def result(self, text="New illustration!", images=2):
        return data.ParseResult(
            data.Platform("twitter", "X"),
            author=data.Author("示例作者"),
            text=text,
            url="https://x.com/sample/status/12345",
            extra={
                "tweet_id": "12345",
                "author_handle": "sample",
                "source_language": "en",
            },
            contents=[data.ImageContent(self.source) for _ in range(images)],
        )

    def assert_prepared_png(self, file):
        self.assertTrue(file.startswith("base64://"))
        value = base64.b64decode(file.removeprefix("base64://"))
        self.assertNotEqual(value, self.original)
        with Image.open(BytesIO(value)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (98, 66))

    def assert_cleanup(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        root = self.root / "plugin-data/temp/media_randomizer"
        self.assertEqual(list(root.glob("*")), [])

    async def test_sdk_failure_http_forward_uses_processed_bytes_once(self):
        await self.plugin._send_result(self.message, self.result(), "stream")
        sdk_nodes = self.send.forward.call_args.args[0]
        action, payload = self.requests[0]
        self.assertEqual(action, "send_private_forward_msg")
        nodes = payload["messages"]
        header = nodes[0]["data"]["content"][0]["data"]["text"]
        self.assertIn("示例作者(@sample)", header)
        self.assertIn("New illustration!", header)
        self.assertIn("新插画完成啦！", header)
        images = [node["data"]["content"][0]["data"]["file"] for node in nodes[1:]]
        self.assertEqual(images[0], images[1])
        self.assert_prepared_png(images[0])
        self.assertEqual(
            images[0], "base64://" + sdk_nodes[1]["segments"][0]["binary_data_base64"]
        )
        self.assert_cleanup()

    async def test_both_forward_failures_preserve_long_text_and_prepared_image(self):
        self.forward_ok = False
        original = "Line\n" * 600 + "Tail"
        await self.plugin._send_result(
            self.message, self.result(original, images=1), "stream"
        )
        ordinary = [
            payload["message"][0]
            for action, payload in self.requests
            if action == "send_private_msg"
        ]
        text = "\n".join(
            seg["data"]["text"] for seg in ordinary if seg["type"] == "text"
        )
        self.assertIn("Tail", text)
        self.assertIn("新插画完成啦！", text)
        self.assertEqual(text.count("Line"), 600)
        self.assert_prepared_png(
            [seg for seg in ordinary if seg["type"] == "image"][0]["data"]["file"]
        )
        self.assert_cleanup()

    async def test_sdk_success_still_uses_processed_image_and_cleans_copy(self):
        self.send.forward.return_value = {"sent": True}
        await self.plugin._send_result(self.message, self.result(images=1), "stream")
        self.assertEqual(self.requests, [])
        nodes = self.send.forward.call_args.args[0]
        self.assert_prepared_png(
            "base64://" + nodes[1]["segments"][0]["binary_data_base64"]
        )
        self.assert_cleanup()

    async def test_translation_failure_does_not_block_original_or_image(self):
        self.llm.generate.side_effect = RuntimeError("model offline")
        await self.plugin._send_result(self.message, self.result(images=1), "")
        header = self.requests[0][1]["messages"][0]["data"]["content"][0]["data"][
            "text"
        ]
        self.assertIn("New illustration!", header)
        self.assertIn("翻译暂不可用", header)
        self.assert_prepared_png(
            self.requests[0][1]["messages"][1]["data"]["content"][0]["data"]["file"]
        )
        self.assert_cleanup()

    async def test_missing_image_does_not_block_original_and_translation(self):
        result = self.result(images=1)
        result.contents[0].path_task = self.root / "missing.jpg"
        await self.plugin._send_result(self.message, result, "")
        text = self.requests[0][1]["messages"][0]["data"]["content"][0]["data"]["text"]
        self.assertIn("New illustration!", text)
        self.assertIn("新插画完成啦！", text)
        self.assertIn(
            "媒体未能下载",
            self.requests[0][1]["messages"][1]["data"]["content"][0]["data"]["text"],
        )
        self.assert_cleanup()

    async def test_text_only_and_unavailable_original_do_not_disappear(self):
        result = self.result(images=0)
        result.extra["details_unavailable"] = True
        result.text = None
        result.author = None
        result.extra["author_handle"] = ""
        await self.plugin._send_result(self.message, result, "")
        text = self.requests[0][1]["message"][0]["data"]["text"]
        self.assertIn("正文暂未获取", text)
        self.assertIn("作者信息未获取", text)
        self.llm.generate.assert_not_awaited()
        self.assert_cleanup()

    async def test_send_error_and_unload_cancel_cleanup_before_exit(self):
        started = asyncio.Event()

        async def waiting(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.send.forward.side_effect = waiting
        task = asyncio.create_task(
            self.plugin._send_result(self.message, self.result(images=1), "stream")
        )
        self.plugin._tasks.add(task)
        await started.wait()
        await self.plugin.on_unload()
        self.assertTrue(task.cancelled())
        self.assert_cleanup()

    def video(self, name="video with spaces.mp4"):
        path = self.root / name
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00video bytes\xff")
        return path

    async def test_video_is_wrapped_even_with_generic_forward_switch_off(self):
        cfg = self.plugin.get_plugin_config_data()
        cfg["parser"]["use_forward_for_multi"] = False
        self.plugin.set_plugin_config(cfg)
        video = self.video()
        result = self.result(images=0)
        result.contents = [data.VideoContent(video)]
        await self.plugin._send_result(self.message, result, "stream")
        self.assertEqual(
            [action for action, _ in self.requests], ["send_private_forward_msg"]
        )
        nodes = self.requests[0][1]["messages"]
        header = nodes[0]["data"]["content"][0]["data"]["text"]
        self.assertEqual(header, "示例作者(@sample)\nNew illustration!\n新插画完成啦！")
        segment = nodes[1]["data"]["content"][0]
        self.assertEqual(segment["type"], "video")
        self.assertEqual(
            base64.b64decode(segment["data"]["file"].removeprefix("base64://")),
            video.read_bytes(),
        )
        sdk = self.send.forward.call_args.args[0]
        self.assertEqual(sdk[1]["segments"][0], {"type": "dict", "data": segment})
        self.assertIn("user_id", sdk[1])
        self.assert_cleanup()

    async def test_mixed_images_and_multiple_videos_keep_order(self):
        first, second = self.video("one.mp4"), self.video("two.mp4")
        result = self.result(images=1)
        result.contents.extend([data.VideoContent(first), data.DynamicContent(second)])
        await self.plugin._send_result(self.message, result, "")
        self.assertEqual(
            [action for action, _ in self.requests], ["send_private_forward_msg"]
        )
        nodes = self.requests[0][1]["messages"]
        self.assertEqual(
            [node["data"]["content"][0]["type"] for node in nodes],
            ["text", "image", "video", "video"],
        )
        self.assert_cleanup()

    async def test_video_forward_failure_uses_embedded_standalone_fallback(self):
        self.forward_ok = False
        video = self.video()
        result = self.result(images=0)
        result.contents.append(data.VideoContent(video))
        await self.plugin._send_result(self.message, result, "stream")
        self.assertEqual(
            [action for action, _ in self.requests],
            ["send_private_forward_msg", "send_private_msg", "send_private_msg"],
        )
        segment = self.requests[-1][1]["message"][0]
        self.assertEqual(segment["type"], "video")
        self.assertEqual(
            base64.b64decode(segment["data"]["file"].removeprefix("base64://")),
            video.read_bytes(),
        )
        self.assert_cleanup()

    async def test_video_sdk_success_uses_dict_component(self):
        self.send.forward.return_value = {"sent": True}
        result = self.result(images=0)
        result.contents.append(data.VideoContent(self.video()))
        await self.plugin._send_result(self.message, result, "stream")
        self.assertEqual(self.requests, [])
        sdk = self.send.forward.call_args.args[0]
        self.assertEqual(sdk[1]["segments"][0]["data"]["type"], "video")
        self.assertTrue(
            sdk[1]["segments"][0]["data"]["data"]["file"].startswith("base64://")
        )
        self.assert_cleanup()

    async def test_non_x_forward_fallback_keeps_independent_text_nodes(self):
        self.forward_ok = False
        result = data.ParseResult(
            data.Platform("other", "Other"),
            title="Header",
            contents=[data.TextContent("extra text must survive")],
        )
        await self.plugin._send_result(self.message, result, "")
        text = "\n".join(
            payload["message"][0]["data"]["text"]
            for action, payload in self.requests
            if action == "send_private_msg"
        )
        self.assertIn("Header", text)
        self.assertIn("extra text must survive", text)


if __name__ == "__main__":
    unittest.main()
