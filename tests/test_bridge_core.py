import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from astrbot_plugin_qwenpaw_bridge.bridge_core import (
    AssistantCollector, BridgeError, BridgeStore, QwenPawClient, decode_sse,
)


def message(text="完成", **kwargs):
    return {"object": "message", "type": "message", "status": "completed",
            "role": "assistant", "id": "msg-1",
            "content": [{"type": "text", "text": text}], **kwargs}


def frame(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bridge.db"
        self.store = BridgeStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_routes_survive_restart_and_separate_platform_group_and_sender(self):
        inputs = [("wx1:Friend:123", "123", "wx1"),
                  ("qq1:Friend:123", "123", "qq1"),
                  ("qq1:Group:g", "123", "qq1"),
                  ("qq1:Group:g", "456", "qq1")]
        routes = [self.store.get_or_create_session(*x) for x in inputs]
        self.assertEqual(len({x["session_id"] for x in routes}), 4)
        self.assertNotIn("123", routes[0]["session_id"])
        self.store.close()
        self.store = BridgeStore(self.path)
        for source, route in zip(inputs, routes):
            self.assertEqual(self.store.get_or_create_session(*source), route)
            self.assertEqual(self.store.get_session(route["session_id"]), route)
        self.assertIsNone(self.store.get_session("invented-session"))

    def test_persistent_receipts_suppress_duplicate_side_effects(self):
        self.assertTrue(self.store.claim("tool", "call-1"))
        self.assertFalse(self.store.claim("tool", "call-1"))
        self.store.finish("tool", "call-1", {"ok": True})
        self.assertEqual(self.store.receipt("tool", "call-1"),
                         {"state": "done", "result": {"ok": True}})
        self.store.fail("tool", "call-1")
        self.assertFalse(self.store.claim("tool", "call-1"))
        self.assertTrue(self.store.claim("delivery", "call-1"))


class CollectorTest(unittest.TestCase):
    @staticmethod
    def sent_file(tool="send_file_to_user", path="file:///bridge-files/ab_" + "a" * 32 + "/outbound/report.pdf", **changes):
        return {
            "object": "message", "type": "plugin_call_output", "role": "tool", "status": "completed",
            "content": [{"type": "data", "data": {"name": tool, "output": json.dumps([
                {"type": "data", "source": {"type": "url", "url": path, "media_type": "application/pdf"}},
                {"type": "text", "text": "private tool diagnostic"},
            ])}}], **changes,
        }

    def test_native_send_file_is_retained_when_final_snapshot_contains_only_text(self):
        collector = AssistantCollector()
        event = self.sent_file()
        collector.feed(event)
        collector.feed(event)  # Reconnected stream copy does not duplicate files.
        collector.feed(message())
        collector.feed({"object": "response", "status": "completed", "output": [message(id=None)]})
        blocks = collector.result()
        self.assertEqual([block["type"] for block in blocks], ["text", "file"])
        self.assertTrue(blocks[1]["file_url"].endswith("/outbound/report.pdf"))
        self.assertNotIn("private tool diagnostic", json.dumps(blocks))

    def test_only_explicit_completed_file_send_tool_can_release_media(self):
        collector = AssistantCollector()
        for event in [self.sent_file(tool="browser_screenshot"), self.sent_file(status="in_progress"),
                      self.sent_file(role="assistant"), self.sent_file(type="reasoning")]:
            collector.feed(event)
        collector.feed({"object": "response", "status": "completed", "output": [message()]})
        self.assertEqual(collector.result(), [{"type": "text", "text": "完成"}])

    def test_native_media_in_final_snapshot_and_stream_are_deduplicated(self):
        collector = AssistantCollector()
        event = self.sent_file()
        path = json.loads(event["content"][0]["data"]["output"])[0]["source"]["url"]
        collector.feed(event)
        collector.feed({"object": "response", "status": "completed", "output": [
            message(content=[{"type": "file", "file_url": path}]), event,
        ]})
        self.assertEqual(collector.result(), [{"type": "file", "file_url": path}])

    def test_native_send_media_count_is_bounded(self):
        collector = AssistantCollector()
        for index in range(4):
            collector.feed(self.sent_file(path=f"/outbound/{index}.pdf"))
        with self.assertRaisesRegex(BridgeError, "four attachments"):
            collector.feed(self.sent_file(path="/outbound/5.pdf"))

    def test_native_data_blocks_are_classified_by_mime_without_releasing_inline_data(self):
        for mime, kind, field in [("image/png", "image", "image_url"), ("audio/mpeg", "audio", "data"),
                                  ("video/mp4", "video", "video_url"), ("text/plain", "file", "file_url")]:
            with self.subTest(mime=mime):
                event = self.sent_file()
                output = json.loads(event["content"][0]["data"]["output"])
                output[0]["source"]["media_type"] = mime
                output.append({"type": "data", "source": {"type": "base64", "data": "private", "media_type": mime}})
                event["content"][0]["data"]["output"] = json.dumps(output)
                collector = AssistantCollector()
                collector.feed(event)
                collector.feed({"object": "response", "status": "completed", "output": []})
                self.assertEqual(collector.result(), [{"type": kind, field: output[0]["source"]["url"]}])

    def test_ignores_reasoning_and_tool_outputs_and_deduplicates_snapshot(self):
        collector = AssistantCollector()
        collector.feed(message("private reasoning", type="reasoning"))
        collector.feed(message("secret tool result", role="tool"))
        collector.feed(message("partial", status="in_progress"))
        collector.feed(message())
        collector.feed(message())
        collector.feed({"object": "response", "status": "completed",
                        "output": [message(id=None)]})
        self.assertEqual(collector.result(), [{"type": "text", "text": "完成"}])

    def test_failure_and_premature_disconnect_are_not_success(self):
        collector = AssistantCollector()
        collector.feed(message())
        with self.assertRaises(BridgeError):
            collector.result()
        with self.assertRaises(BridgeError):
            collector.feed({"object": "response", "status": "failed"})


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_utf8_split_across_every_byte_and_crlf(self):
        wire = b": comment\r\n\r\n" + frame(message()).replace(b"\n", b"\r\n")
        async def chunks():
            for value in wire:
                yield bytes([value])
        self.assertEqual([x async for x in decode_sse(chunks())], [message()])

    async def test_truncated_and_oversize_events_fail(self):
        async def truncated():
            yield b'data: {"unfinished":'
        with self.assertRaises(BridgeError):
            [x async for x in decode_sse(truncated())]
        async def big():
            yield b"data: " + b"x" * 100
        with self.assertRaises(BridgeError):
            [x async for x in decode_sse(big(), 50)]


class ClientContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = web.Application()
        self.requests = []
        self.mode = "success"
        self.app.router.add_post("/api/agents/default/console/chat", self.chat)
        self.app.router.add_get("/api/approval/list", self.approvals)
        self.app.router.add_post("/api/approval/approve", self.approve)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = QwenPawClient(f"http://127.0.0.1:{port}", "default", "x" * 48)

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def chat(self, request):
        self.assertEqual(request.headers.get("X-QwenPaw-Runtime-Token"), "x" * 48)
        payload = await request.json()
        self.requests.append(payload)
        if self.mode == "busy":
            return web.json_response({}, status=409)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if self.mode == "reconnect" and len(self.requests) == 1:
            await response.write(frame(message()))
        else:
            await response.write(frame(message()))
            await response.write(frame({"object": "response", "status": "completed", "output": [message()]}))
        await response.write_eof()
        return response

    async def approvals(self, request):
        return web.json_response({"pending_approvals": [{"request_id": "req-1",
            "session_id": "child", "root_session_id": "root", "tool_name": "run_shell",
            "exact_target": "pwd", "reasoning": "private reasoning"}]})

    async def approve(self, request):
        self.assertEqual(await request.json(), {"request_id": "req-1", "session_id": "root",
                                               "user_id": "u1", "scope": "exact"})
        return web.json_response({"success": True})

    async def test_real_http_auth_native_payload_and_no_double_answer(self):
        answer = await self.client.chat("s1", "u1", "你好", turn_id="f" * 32)
        self.assertEqual(answer, [{"type": "text", "text": "完成"}])
        self.assertEqual(self.requests, [{"input": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}],
                                          "session_id": "s1", "user_id": "u1", "channel": "astrbot",
                                          "request_context": {"astrbot_bridge_turn_id": "f" * 32,
                                                              "channel_meta": {"astrbot_bridge_turn_id": "f" * 32}}}])

    async def test_reconnect_does_not_resubmit_original_input(self):
        self.mode = "reconnect"
        self.assertEqual(await self.client.chat("s1", "u1", "只执行一次"), [{"type": "text", "text": "完成"}])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[1]["reconnect"])
        self.assertEqual(self.requests[1]["input"], [])
        self.assertEqual(self.requests[0]["request_context"], self.requests[1]["request_context"])

    async def test_busy_task_is_never_automatically_retried(self):
        self.mode = "busy"
        with self.assertRaisesRegex(BridgeError, "already has a running task"):
            await self.client.chat("s1", "u1", "任务")
        self.assertEqual(len(self.requests), 1)

    async def test_approval_is_exact_and_root_scoped_without_reasoning(self):
        pending = await self.client.pending_approvals()
        self.assertEqual(pending[0]["session_id"], "root")
        self.assertEqual(pending[0]["target"], "pwd")
        self.assertNotIn("reasoning", pending[0])
        self.assertEqual(await self.client.approval("req-1", "root", "u1", True), {"success": True})


if __name__ == "__main__":
    unittest.main()
