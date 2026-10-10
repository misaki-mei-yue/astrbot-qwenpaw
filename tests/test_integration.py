"""Real loopback HTTP across both adapters; platform/runtime APIs remain stubs.

These tests exercise actual authentication, routing, JSON bodies, SQLite
receipts, tool execution adapter and SSE parsing. No account, model, production
server, platform login or official runtime is started.
"""

import asyncio
import base64
from contextlib import ExitStack
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

# Reuse the published narrow API fixtures rather than recreate frameworks.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_astrbot_adapter as astrbot_fixture
from test_qwenpaw_adapter import RuntimeStub
from module_stubs import module_overrides

from astrbot_plugin_qwenpaw_bridge.bridge_core import BridgeStore, QwenPawClient
from qwenpaw_plugin_astrbot_bridge.bridge_client import BridgeClient, BridgeError, BridgeSettings
from qwenpaw_plugin_astrbot_bridge.turn_context import clear_turn_id


class HttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.files_root = Path(self.temp.name) / "shared"
        self.source_root = Path(self.temp.name) / "incoming"
        self.files_root.mkdir()
        self.source_root.mkdir()
        self.files_root = self.files_root.resolve()
        self.source_root = self.source_root.resolve()
        self.media_chat = False
        self.media_kind = "image"
        self.runtime = RuntimeStub()
        self.context = SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            get_llm_tool_manager=lambda: SimpleNamespace(func_list=[astrbot_fixture.Tool()]),
        )
        self.bridge_token = "b" * 48
        self.runtime_token = "r" * 48
        self.executions = []
        self.gateway_requests = []
        self.qwen_requests = []
        self.approval_requests = []
        self.fake_errors = []
        self.hooks = SimpleNamespace(on_tool_start=AsyncMock(), on_tool_end=AsyncMock())
        test = self

        class Executor:
            @staticmethod
            async def execute(tool, wrapper, **arguments):
                test.executions.append((tool, wrapper, arguments))
                yield astrbot_fixture.CallToolResult([
                    {"type": "text", "text": "查到：" + arguments["query"]}
                ])

        plugin = SimpleNamespace(name="on", activated=True, reserved=False)
        session_manager = SimpleNamespace(is_plugin_enabled_for_session=AsyncMock(return_value=True))
        modules = {
            **astrbot_fixture.ASTRBOT_MODULES,
            **self.runtime.modules,
            "astrbot.core.agent.handoff": astrbot_fixture.module("astrbot.core.agent.handoff", HandoffTool=astrbot_fixture.HandoffTool),
            "astrbot.core.agent.mcp_client": astrbot_fixture.module("astrbot.core.agent.mcp_client", MCPTool=astrbot_fixture.MCPTool),
            "astrbot.core.agent.tool": astrbot_fixture.module("astrbot.core.agent.tool", ToolSet=astrbot_fixture.ToolSet),
            "astrbot.core.star.session_plugin_manager": astrbot_fixture.module("astrbot.core.star.session_plugin_manager", SessionPluginManager=session_manager),
            "astrbot.core.star.star": astrbot_fixture.module("astrbot.core.star.star", star_map={"plugin.on": plugin}),
            "mcp.types": astrbot_fixture.module("mcp.types", CallToolResult=astrbot_fixture.CallToolResult),
            "astrbot.core.agent.run_context": astrbot_fixture.module("astrbot.core.agent.run_context", ContextWrapper=lambda **kwargs: SimpleNamespace(**kwargs)),
            "astrbot.core.astr_agent_context": astrbot_fixture.module("astrbot.core.astr_agent_context", AstrAgentContext=lambda **kwargs: SimpleNamespace(**kwargs)),
            "astrbot.core.astr_agent_hooks": astrbot_fixture.module("astrbot.core.astr_agent_hooks", MAIN_AGENT_HOOKS=self.hooks),
            "astrbot.core.astr_agent_tool_exec": astrbot_fixture.module("astrbot.core.astr_agent_tool_exec", FunctionToolExecutor=Executor),
        }
        self.modules_patch = ExitStack()
        self.modules_patch.enter_context(module_overrides(modules))
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.bridge = astrbot_fixture.adapter.QwenPawBridge(self.context, {
            "owner_user_ids": ["owner"], "tool_allowlist": ["lookup"],
            "files_root": str(self.files_root), "source_roots": [str(self.source_root)],
        })
        self.bridge.token = self.bridge_token
        self.bridge._store = BridgeStore(Path(self.temp.name) / "bridge.sqlite3")
        self.route = self.bridge._store.get_or_create_session("wx:FriendMessage:owner", "owner", "weixin_oc")
        self.sid = self.route["session_id"]

        @web.middleware
        async def record_gateway(request, handler):
            self.gateway_requests.append((request.path, await request.json()))
            return await handler(request)

        gateway = web.Application(middlewares=[self.bridge._auth, record_gateway])
        gateway.router.add_post("/v1/tools/list", self.bridge._tools_list)
        gateway.router.add_post("/v1/tools/call", self.bridge._tools_call)
        gateway.router.add_post("/v1/deliver", self.bridge._deliver)
        self.gateway_runner, self.gateway_url = await self._listen(gateway)
        self.runtime.profile.channels.astrbot.callback_url = self.gateway_url
        self.runtime.profile.channels.astrbot.callback_token = self.bridge_token
        self.runtime.profile.channels.astrbot.files_dir = str(self.files_root)
        self.tools = importlib.import_module("qwenpaw_plugin_astrbot_bridge.tools")
        self.tools.configure_tools({})
        self.channel_module = importlib.import_module("qwenpaw_plugin_astrbot_bridge.channel")
        self.runtime_hooks = importlib.import_module("qwenpaw_plugin_astrbot_bridge.runtime_hooks")
        self.callback = BridgeClient(BridgeSettings(self.gateway_url, self.bridge_token, timeout=3, attempts=1))

        fake_qwen = web.Application()
        fake_qwen.router.add_post("/api/agents/default/console/chat", self._fake_chat)
        fake_qwen.router.add_get("/api/approval/list", self._fake_pending)
        fake_qwen.router.add_post("/api/approval/approve", self._fake_approval)
        fake_qwen.router.add_post("/api/approval/deny", self._fake_approval)
        self.qwen_runner, qwen_url = await self._listen(fake_qwen)
        self.bridge._client = QwenPawClient(qwen_url, "default", self.runtime_token, timeout=5)

    async def asyncTearDown(self):
        clear_turn_id()
        await self.bridge._client.close()
        await self.qwen_runner.cleanup()
        await self.gateway_runner.cleanup()
        self.bridge._store.close()
        self.env_patch.stop()
        self.modules_patch.close()
        self.temp.cleanup()

    @staticmethod
    async def _listen(app):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        address = runner.addresses[0]
        return runner, f"http://127.0.0.1:{address[1]}"

    async def _bind_runtime(self, body):
        self.runtime.context.root_session_id = body["session_id"]
        # A child task's own session cannot redirect a callback away from root.
        self.runtime.context.session_id = "child-task-session"
        self.runtime.context.user_id = body["user_id"]
        self.runtime.context.channel = body["channel"]
        self.runtime.context.agent_id = "default"
        self.runtime.context.approval_route = {
            "root_session_id": body["session_id"], "user_id": body["user_id"],
            "channel": body["channel"], "channel_meta": {},
        }
        await self.runtime_hooks.BridgeTurnContextHook().run(SimpleNamespace(request=SimpleNamespace(**body)))

    def _active_turn(self, nonce="c" * 32):
        event = astrbot_fixture.Event()
        event.plugins_name = ["on"]
        self.bridge._active[self.sid] = astrbot_fixture.adapter.ActiveTurn(
            event, "owner", time.monotonic() + 30, turn_id=nonce
        )
        return event

    async def _fake_chat(self, request):
        if request.headers.get("X-QwenPaw-Runtime-Token") != self.runtime_token:
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await request.json()
        self.qwen_requests.append(body)
        try:
            await self._bind_runtime(body)
            if self.media_chat:
                return await self._fake_media_chat(request, body)
            listing = await self.tools.astrbot_list_tools()
            if [tool["name"] for tool in listing["tools"]] != ["lookup"]:
                raise AssertionError("Live event tool listing did not reach AstrBot")
            result = await self.tools.astrbot_call_tool("lookup", {"query": body["input"][0]["content"][0]["text"]})
            message = {
                "object": "message", "status": "completed", "type": "message", "role": "assistant",
                "id": "stream-message", "content": result["content"],
            }
            # The final snapshot has a different/missing ID, as official SSE may.
            final_message = {key: value for key, value in message.items() if key != "id"}
            events = [
                {**message, "type": "reasoning", "content": [{"type": "text", "text": "private reasoning"}]},
                {**message, "type": "function_call_output", "content": [{"type": "text", "text": "private tool output"}]},
                {"object": "content", "status": "in_progress", "delta": "partial output"},
                message,
                {"object": "response", "status": "completed", "output": [final_message]},
            ]
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            data = b"".join(("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode("utf-8") for event in events)
            # Actual TCP HTTP body writes split JSON lines and Chinese UTF-8.
            for offset in range(0, len(data), 7):
                await response.write(data[offset:offset + 7])
                await asyncio.sleep(0)
            await response.write_eof()
            return response
        except Exception as exc:
            self.fake_errors.append(exc)
            return web.json_response({"error": "fake_runtime_contract_failed"}, status=500)
        finally:
            await self.runtime_hooks.BridgeTurnCleanupHook().run(SimpleNamespace())

    async def _fake_media_chat(self, request, body):
        # Read the actual incoming file, generate an output in the trusted
        # runtime's own workspace, then use the official send_file envelope.
        media_block = next(part for part in body["input"][0]["content"] if part["type"] == self.media_kind)
        source = Path(media_block["image_url" if self.media_kind == "image" else "file_url"])
        self.assertTrue(source.is_relative_to(self.files_root / self.sid / "inbound"))
        workspace = await self.tools.astrbot_media_workspace()
        target = Path(workspace["outbound_dir"]) / ("result.png" if self.media_kind == "image" else "result.txt")
        target.write_bytes(source.read_bytes())
        sent = {
            "object": "message", "status": "completed", "type": "plugin_call_output", "role": "tool",
            "content": [{"type": "data", "data": {"name": "send_file_to_user", "output": json.dumps([
                {"type": "image" if self.media_kind == "image" else "data", "source": {
                    "type": "url", "url": target.as_uri(),
                    "media_type": "image/png" if self.media_kind == "image" else "text/plain",
                }},
                {"type": "text", "text": "internal diagnostic must stay private"},
            ])}}],
        }
        final = {"object": "message", "status": "completed", "type": "message", "role": "assistant",
                 "content": [{"type": "text", "text": "已生成附件"}]}
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        # Tool media is absent from the final snapshot, as in QwenPaw 2.2.1.
        for event in [sent, final, {"object": "response", "status": "completed", "output": [final]}]:
            await response.write(("data: " + json.dumps(event) + "\n\n").encode())
        await response.write_eof()
        return response

    async def test_image_bytes_cross_chat_http_and_native_tool_sse_without_getting_lost(self):
        self.media_chat = True
        content = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB3cAAAAASUVORK5CYII=")
        source = self.source_root / "input.png"
        source.write_bytes(content)
        event = astrbot_fixture.Event(text="处理这个图片", message_id="image-round-trip")
        event.segments = [astrbot_fixture.Image(file=str(source))]
        replies = [reply async for reply in self.bridge.forward_chat(event)]
        self.assertEqual(self.fake_errors, [])
        self.assertEqual(len(self.qwen_requests), 1)
        self.assertEqual(len(replies), 1)
        image_parts = [part for part in replies[0].chain if isinstance(part, astrbot_fixture.Image)]
        self.assertEqual(len(image_parts), 1)
        snapshot = Path(image_parts[0].file)
        self.assertTrue(snapshot.is_relative_to(self.files_root / self.sid / "delivery"))
        self.assertEqual(snapshot.read_bytes(), content)
        text = "".join(part.text for part in replies[0].chain if isinstance(part, astrbot_fixture.Plain))
        self.assertEqual(text, "已生成附件")
        self.context.send_message.assert_not_awaited()

    async def test_file_only_input_and_generic_data_tool_output_preserve_bytes(self):
        self.media_chat = True
        self.media_kind = "file"
        content = "没有附带文字的普通文件".encode("utf-8")
        source = self.source_root / "input.txt"
        source.write_bytes(content)
        event = astrbot_fixture.Event(text="", message_id="file-only-round-trip")
        event.segments = [astrbot_fixture.File(name="input.txt", file=str(source))]
        replies = [reply async for reply in self.bridge.forward_chat(event)]
        self.assertEqual(self.fake_errors, [])
        self.assertEqual(len(self.qwen_requests), 1)
        self.assertEqual(len(replies), 1)
        file_parts = [part for part in replies[0].chain if isinstance(part, astrbot_fixture.File)]
        self.assertEqual(len(file_parts), 1)
        snapshot = Path(file_parts[0].file)
        self.assertTrue(snapshot.is_relative_to(self.files_root / self.sid / "delivery"))
        self.assertEqual(snapshot.read_bytes(), content)
        self.context.send_message.assert_not_awaited()

    async def test_proactive_media_tool_replays_once_even_after_original_file_is_removed(self):
        await self._bind_runtime({"session_id": self.sid, "user_id": "owner", "channel": "astrbot", "request_context": {}})
        workspace = await self.tools.astrbot_media_workspace()
        target = Path(workspace["outbound_dir"]) / "report.txt"
        content = "主动生成的文件".encode("utf-8")
        target.write_bytes(content)
        accepted = await self.tools.astrbot_send_file(str(target), "file")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.executions, [])
        request_path, payload = self.gateway_requests[-1]
        self.assertEqual(request_path, "/v1/deliver")
        self.assertNotIn("turn_id", payload)
        target.unlink()
        repeated = await self.callback.post(request_path, payload)
        self.assertEqual(repeated, accepted)
        self.context.send_message.assert_awaited_once()
        origin, chain = self.context.send_message.await_args.args
        self.assertEqual(origin, self.route["origin"])
        files = [part for part in chain.chain if isinstance(part, astrbot_fixture.File)]
        self.assertEqual(len(files), 1)
        snapshot = Path(files[0].file)
        self.assertEqual(snapshot.read_bytes(), content)
        self.assertTrue(snapshot.is_relative_to(self.files_root / self.sid / "delivery"))

    async def test_media_callback_rejects_tampering_and_cross_session_before_platform_send(self):
        from qwenpaw_plugin_astrbot_bridge.media import media_descriptor, media_workspace
        workspace = media_workspace(str(self.files_root), self.sid)
        target = Path(workspace["outbound_dir"]) / "safe.txt"
        target.write_text("original", encoding="utf-8")
        descriptor = media_descriptor(str(self.files_root), self.sid, str(target), "file")
        payload = {"session_id": self.sid, "user_id": "owner", "delivery_id": "tampered", "content": [descriptor]}
        target.write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "HTTP 400"):
            await self.callback.post("/v1/deliver", payload)
        target.write_text("original", encoding="utf-8")
        foreign = self.bridge._store.get_or_create_session("qq:Group:other", "owner", "aiocqhttp")
        with self.assertRaisesRegex(BridgeError, "HTTP 400"):
            await self.callback.post("/v1/deliver", {**payload, "session_id": foreign["session_id"], "delivery_id": "foreign"})
        self.context.send_message.assert_not_awaited()

    async def _fake_pending(self, request):
        if request.headers.get("X-QwenPaw-Runtime-Token") != self.runtime_token:
            return web.json_response({"error": "unauthorized"}, status=401)
        self.approval_requests.append((request.method, request.path, None))
        return web.json_response({"pending_approvals": [
            {"request_id": "approve-this", "root_session_id": self.sid, "session_id": "child-task-session",
             "owner_agent_id": "default", "tool_name": "browser", "tool_display_name": "打开网页",
             "exact_target": "https://example.invalid/task", "reasoning": "private reasoning"},
            {"request_id": "another-agent", "root_session_id": self.sid, "owner_agent_id": "other", "tool_name": "other tool"},
            {"request_id": "another-session", "root_session_id": "ab_" + "f" * 32, "owner_agent_id": "default", "tool_name": "foreign tool"},
        ]})

    async def _fake_approval(self, request):
        if request.headers.get("X-QwenPaw-Runtime-Token") != self.runtime_token:
            return web.json_response({"error": "unauthorized"}, status=401)
        self.approval_requests.append((request.method, request.path, await request.json()))
        return web.json_response({"success": True})

    async def test_chat_sse_tools_and_original_event_form_one_round_trip(self):
        event = astrbot_fixture.Event(text="你好，查一下库存")
        event.plugins_name = ["on"]
        replies = [reply async for reply in self.bridge.forward_chat(event)]
        self.assertEqual(self.fake_errors, [])
        self.assertEqual(len(replies), 1)
        self.assertEqual([part.text for part in replies[0].chain], ["查到：你好，查一下库存"])
        self.assertEqual(replies[0].kind, "llm")
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(self.qwen_requests), 1)
        body = self.qwen_requests[0]
        self.assertEqual((body["session_id"], body["user_id"], body["channel"]), (self.sid, "owner", "astrbot"))
        nonce = body["request_context"]["astrbot_bridge_turn_id"]
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        self.assertEqual([path for path, _ in self.gateway_requests], ["/v1/tools/list", "/v1/tools/call"])
        for _, callback in self.gateway_requests:
            self.assertEqual((callback["session_id"], callback["user_id"], callback["turn_id"]), (self.sid, "owner", nonce))
        self.assertEqual(len(self.executions), 1)
        _, wrapper, arguments = self.executions[0]
        self.assertIs(wrapper.context.event, event)
        self.assertIs(wrapper.context.context, self.context)
        self.assertEqual(arguments, {"query": "你好，查一下库存"})
        self.hooks.on_tool_start.assert_awaited_once()
        self.hooks.on_tool_end.assert_awaited_once()
        self.context.send_message.assert_not_awaited()
        self.assertNotIn(self.sid, self.bridge._active)
        duplicate = astrbot_fixture.Event(text=event.text)
        duplicate.plugins_name = ["on"]
        self.assertEqual([reply async for reply in self.bridge.forward_chat(duplicate)], [])
        self.assertEqual(len(self.qwen_requests), 1)

    async def test_proactive_final_callback_returns_to_persisted_origin_once(self):
        final = SimpleNamespace(object="message", status="completed", type="message", role="assistant",
                                id="scheduled-job-message", content=[{"type": "text", "text": "主动任务完成"}])
        channel = self.channel_module.AstrBotChannel.from_config(None, self.runtime.profile.channels.astrbot)
        await channel.send_event(user_id="owner", session_id=self.sid, event=final)
        await channel.send_event(user_id="owner", session_id=self.sid, event=final)
        # Restart loses the channel RAM cache; actual gateway SQLite still dedupes.
        restarted = self.channel_module.AstrBotChannel.from_config(None, self.runtime.profile.channels.astrbot)
        await restarted.send_event(user_id="owner", session_id=self.sid, event=final)
        for kind in ["reasoning", "function_call_output"]:
            await restarted.send_event(user_id="owner", session_id=self.sid, event=SimpleNamespace(**{**vars(final), "type": kind}))
        self.context.send_message.assert_awaited_once()
        origin, chain = self.context.send_message.await_args.args
        self.assertEqual(origin, self.route["origin"])
        self.assertEqual([part.text for part in chain.chain], ["主动任务完成"])
        self.assertEqual(len(self.gateway_requests), 2)
        self.assertEqual(self.gateway_requests[0][1]["delivery_id"], self.gateway_requests[1][1]["delivery_id"])
        self.assertEqual(self.executions, [])

    async def test_real_http_replay_and_late_old_turn_are_separate(self):
        nonce = "c" * 32
        event = self._active_turn(nonce)
        payload = {"session_id": self.sid, "user_id": "owner", "turn_id": nonce,
                   "call_id": "stable-call", "tool_name": "lookup", "arguments": {"query": "first"}}
        first = await self.callback.post("/v1/tools/call", payload)
        replay = await self.callback.post("/v1/tools/call", payload)
        self.assertEqual(first, replay)
        self.assertEqual(len(self.executions), 1)
        self.assertIs(self.executions[0][1].context.event, event)
        next_event = self._active_turn("d" * 32)
        for path, old_request in [
            ("/v1/tools/list", {"session_id": self.sid, "user_id": "owner", "turn_id": nonce}),
            ("/v1/tools/call", {**payload, "call_id": "late-unexecuted-call"}),
        ]:
            with self.assertRaisesRegex(BridgeError, "HTTP 409"):
                await self.callback.post(path, old_request)
        self.assertEqual(len(self.executions), 1)
        # Same call ID in a different authenticated turn has its own receipt.
        await self.callback.post("/v1/tools/call", {**payload, "turn_id": "d" * 32})
        self.assertEqual(len(self.executions), 2)
        self.assertIs(self.executions[1][1].context.event, next_event)

    async def test_real_http_auth_route_allowlist_and_schema_fail_closed(self):
        self._active_turn()
        base = {"session_id": self.sid, "user_id": "owner", "turn_id": "c" * 32,
                "call_id": "validation-call", "tool_name": "lookup", "arguments": {"query": "ok"}}
        bad_token = BridgeClient(BridgeSettings(self.gateway_url, "wrong", timeout=3, attempts=1))
        with self.assertRaisesRegex(BridgeError, "HTTP 401"):
            await bad_token.post("/v1/tools/call", base)
        for change, status in [
            ({"user_id": "intruder"}, 403),
            ({"session_id": "ab_" + "f" * 32}, 403),
            ({"tool_name": "not-allowed"}, 403),
            ({"arguments": {"query": 123}}, 400),
            ({"arguments": {"query": "ok", "user_id": "intruder"}}, 400),
            ({"turn_id": None}, 400),
        ]:
            with self.assertRaisesRegex(BridgeError, f"HTTP {status}"):
                await self.callback.post("/v1/tools/call", {**base, **change})
        self.assertEqual(self.executions, [])
        self.hooks.on_tool_start.assert_not_awaited()

    async def test_missing_native_nonce_never_reaches_gateway(self):
        self._active_turn()
        await self._bind_runtime({"session_id": self.sid, "user_id": "owner", "channel": "astrbot", "request_context": {}})
        for action in [self.tools.astrbot_list_tools(), self.tools.astrbot_call_tool("lookup", {"query": "x"})]:
            with self.assertRaises(BridgeError):
                await action
        self.assertEqual(self.gateway_requests, [])
        self.assertEqual(self.executions, [])

    async def test_approval_poll_and_explicit_action_use_root_and_owner_agent(self):
        pending = await self.bridge._client.pending_approvals()
        self.assertEqual(self.approval_requests[0], ("GET", "/api/approval/list", None))
        self.assertEqual(pending[0]["session_id"], self.sid)
        self.assertNotIn("private reasoning", json.dumps(pending))
        event = astrbot_fixture.Event(message_id="approval-pending")
        replies = [reply async for reply in self.bridge.paw_command(event, "pending")]
        text = replies[0].chain[0].text
        self.assertIn("approve-this", text)
        self.assertIn("https://example.invalid/task", text)
        self.assertNotIn("another-agent", text)
        self.assertNotIn("another-session", text)
        self.assertNotIn("private reasoning", text)
        approve = astrbot_fixture.Event(message_id="approval-approved")
        result = [reply async for reply in self.bridge.paw_command(approve, "approve", "approve-this")]
        self.assertIn("已提交同意", result[0].chain[0].text)
        self.assertEqual(self.approval_requests[-1], ("POST", "/api/approval/approve", {
            "request_id": "approve-this", "session_id": self.sid, "user_id": "owner", "scope": "exact",
        }))
        posts = sum(method == "POST" for method, _, _ in self.approval_requests)
        intruder = astrbot_fixture.Event(user="intruder", message_id="intruder-approval")
        await self._consume(self.bridge.paw_command(intruder, "approve", "approve-this"))
        foreign = astrbot_fixture.Event(message_id="other-agent-approval")
        await self._consume(self.bridge.paw_command(foreign, "approve", "another-agent"))
        self.assertEqual(sum(method == "POST" for method, _, _ in self.approval_requests), posts)

    @staticmethod
    async def _consume(generator):
        return [result async for result in generator]


if __name__ == "__main__":
    unittest.main()
