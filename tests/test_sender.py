import base64
import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web


class SenderContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec = importlib.util.spec_from_file_location(
            "sender_under_test",
            Path(__file__).resolve().parents[1] / "sender.py",
        )
        self.sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sender)
        self.requests = []
        self.responses = []
        app = web.Application()
        app.router.add_post("/{action}", self.handle_request)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.api = SimpleNamespace(
            host="127.0.0.1",
            port=self.runner.addresses[0][1],
            token="diagnostic-token",
            bot_uin="10000",
        )
        self.message = {
            "message_info": {
                "user_info": {"user_id": "10001"},
                "group_info": None,
            }
        }
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "image with spaces 图.jpg"
        self.image_bytes = b"image bytes\x00\xff\x10"
        self.path.write_bytes(self.image_bytes)

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def handle_request(self, request):
        self.requests.append(
            {
                "action": request.match_info["action"],
                "payload": await request.json(),
                "query": dict(request.query),
            }
        )
        if self.responses:
            status, body = self.responses.pop(0)
        else:
            status, body = (
                200,
                {"status": "ok", "retcode": 0, "data": {"message_id": 1}},
            )
        return web.json_response(body, status=status)

    async def test_single_image_embeds_original_bytes(self):
        self.assertTrue(await self.sender.send_image(self.message, self.path, self.api))
        request = self.requests[0]
        self.assertEqual(request["action"], "send_private_msg")
        file = request["payload"]["message"][0]["data"]["file"]
        self.assertTrue(file.startswith("base64://"))
        self.assertEqual(
            base64.b64decode(file.removeprefix("base64://")), self.image_bytes
        )

    async def test_forward_embeds_local_image_without_mutating_nodes(self):
        nodes = [
            [self.sender.text_segment("Description")],
            [self.sender.image_segment(self.path)],
        ]
        before = copy.deepcopy(nodes)
        self.assertTrue(
            await self.sender.send_group_forward(self.message, nodes, self.api)
        )
        request = self.requests[0]
        self.assertEqual(request["action"], "send_private_forward_msg")
        self.assertEqual(len(request["payload"]["messages"]), 2)
        file = request["payload"]["messages"][1]["data"]["content"][0]["data"]["file"]
        self.assertTrue(file.startswith("base64://"))
        self.assertEqual(
            base64.b64decode(file.removeprefix("base64://")), self.image_bytes
        )
        self.assertEqual(nodes, before)

    async def test_group_forward_preserves_remote_and_embedded_images(self):
        self.message["message_info"]["group_info"] = {"group_id": "10002"}
        nodes = [
            [
                {"type": "image", "data": {"file": "https://example.test/image.jpg"}},
                {"type": "image", "data": {"file": "base64://YWJj"}},
            ]
        ]
        before = copy.deepcopy(nodes)
        self.assertTrue(
            await self.sender.send_group_forward(self.message, nodes, self.api)
        )
        request = self.requests[0]
        self.assertEqual(request["action"], "send_group_forward_msg")
        self.assertEqual(
            request["payload"]["messages"][0]["data"]["content"], before[0]
        )
        self.assertEqual(nodes, before)

    async def test_group_image_and_forward_embed_local_bytes(self):
        self.message["message_info"]["group_info"] = {"group_id": "10002"}
        self.assertTrue(await self.sender.send_image(self.message, self.path, self.api))
        self.assertEqual(self.requests[0]["action"], "send_group_msg")
        self.assertTrue(
            await self.sender.send_group_forward(
                self.message,
                [[self.sender.image_segment(self.path)]],
                self.api,
            )
        )
        request = self.requests[1]
        self.assertEqual(request["action"], "send_group_forward_msg")
        file = request["payload"]["messages"][0]["data"]["content"][0]["data"]["file"]
        self.assertEqual(
            base64.b64decode(file.removeprefix("base64://")), self.image_bytes
        )

    async def test_business_failure_does_not_retry_after_authentication(self):
        self.responses = [
            (403, {"error": "query token required"}),
            (200, {"status": "failed", "retcode": 1200, "wording": "cannot send"}),
        ]
        self.assertFalse(await self.sender.send_text(self.message, "Test", self.api))
        self.assertEqual(len(self.requests), 2)

    async def test_http_200_with_onebot_failure_is_not_success(self):
        self.responses = [
            (200, {"status": "failed", "retcode": 1200, "message": "test failure"})
        ]
        self.assertFalse(await self.sender.send_text(self.message, "Test", self.api))
        self.assertEqual(len(self.requests), 1)

    async def test_http_200_with_invalid_body_is_not_success(self):
        self.responses = [(200, [])]
        self.assertFalse(await self.sender.send_text(self.message, "Test", self.api))
        self.assertEqual(len(self.requests), 1)

    async def test_authentication_retry_keeps_existing_behavior(self):
        self.responses = [(401, {"error": "query token required"})]
        self.assertTrue(await self.sender.send_text(self.message, "Test", self.api))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1]["query"]["access_token"], self.api.token)

    async def test_private_and_group_video_embed_file_bytes(self):
        for group in (None, {"group_id": "10002"}):
            self.message["message_info"]["group_info"] = group
            self.assertTrue(
                await self.sender.send_video(self.message, self.path, self.api)
            )
            request = self.requests[-1]
            self.assertEqual(
                request["action"], "send_group_msg" if group else "send_private_msg"
            )
            segment = request["payload"]["message"][0]
            self.assertEqual(segment["type"], "video")
            self.assertEqual(
                base64.b64decode(segment["data"]["file"].removeprefix("base64://")),
                self.image_bytes,
            )

    async def test_forward_video_embeds_bytes_and_preserves_input(self):
        nodes = [
            [self.sender.text_segment("Author\nOriginal\nTranslation")],
            [self.sender.video_segment(self.path)],
        ]
        before = copy.deepcopy(nodes)
        self.assertTrue(
            await self.sender.send_group_forward(self.message, nodes, self.api)
        )
        segment = self.requests[0]["payload"]["messages"][1]["data"]["content"][0]
        self.assertEqual(segment["type"], "video")
        self.assertEqual(
            base64.b64decode(segment["data"]["file"].removeprefix("base64://")),
            self.image_bytes,
        )
        self.assertEqual(nodes, before)

    async def test_http_failure_is_not_success(self):
        self.responses = [(500, {"error": "test failure"})]
        self.assertFalse(await self.sender.send_text(self.message, "Test", self.api))
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
