"""Protocol tests use lightweight API stubs, not an installed QwenPaw runtime."""

import asyncio
from contextlib import ExitStack
import importlib
import json
import os
import inspect
import sys
import types
import unittest
import tempfile
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

from module_stubs import module_overrides
from qwenpaw_plugin_astrbot_bridge.bridge_client import BridgeClient, BridgeError, BridgeSettings
from qwenpaw_plugin_astrbot_bridge.turn_context import clear_turn_id, get_turn_id, set_turn_id
from qwenpaw_plugin_astrbot_bridge.media import media_descriptor, media_workspace


SESSION = "ab_" + "a" * 32
OTHER_SESSION = "ab_" + "b" * 32
TURN_ID = "c" * 32
PERSON_AGENT = "abp_" + "1" * 32
OTHER_PERSON_AGENT = "abp_" + "2" * 32
GROUP_AGENT = "abg_" + "3" * 32


class RuntimeStub:
    """Only the exact official symbols the plugin consumes are exposed."""

    def __init__(self):
        self.context = SimpleNamespace(
            session_id=SESSION, root_session_id=SESSION, user_id="owner", channel="astrbot", agent_id="default",
            approval_route={"root_session_id": SESSION, "user_id": "owner", "channel": "astrbot", "channel_meta": {}}
        )
        self.profile = SimpleNamespace(channels=SimpleNamespace(astrbot=SimpleNamespace(
            enabled=True, callback_url="http://astrbot:9186", callback_token="unit-test-token"
        )))
        self.modules = {name: types.ModuleType(name) for name in [
            "qwenpaw", "qwenpaw.app", "qwenpaw.app.agent_context", "qwenpaw.app.channels",
            "qwenpaw.app.channels.base", "qwenpaw.config", "qwenpaw.config.config",
            "qwenpaw.hooks", "qwenpaw.hooks.base", "qwenpaw.runtime",
            "qwenpaw.runtime.hooks", "qwenpaw.runtime.phases",
            "agentscope", "agentscope.message", "agentscope.middleware", "agentscope.tool",
        ]}
        for field in ["session_id", "root_session_id", "user_id", "channel", "agent_id"]:
            setattr(self.modules["qwenpaw.app.agent_context"], "get_current_" + field,
                    lambda field=field: getattr(self.context, field))
        # Match the official distinction: get falls back, peek never does.
        self.modules["qwenpaw.app.agent_context"].get_current_agent_id = lambda: self.context.agent_id or "default"
        self.modules["qwenpaw.app.agent_context"].peek_current_agent_id = lambda: self.context.agent_id or ""
        self.modules["qwenpaw.config.config"].load_agent_config = lambda agent_id: self.profile
        self.modules["qwenpaw.app.agent_context"].get_current_approval_route = lambda: self.context.approval_route
        self.modules["qwenpaw.app.agent_context"].set_current_approval_route = lambda route: setattr(self.context, "approval_route", route)

        class BaseChannel:
            def __init__(self, process, on_reply_sent=None, display_config=None):
                self._process = process
                self._on_reply_sent = on_reply_sent
                self._workspace = None

            def set_workspace(self, workspace, command_registry=None):
                self._workspace = workspace
                self._command_registry = command_registry

        self.modules["agentscope.message"].TextBlock = SimpleNamespace
        self.modules["agentscope.message"].ToolResultState = SimpleNamespace(ERROR="error")
        self.modules["agentscope.middleware"].MiddlewareBase = type("MiddlewareBase", (), {})
        self.modules["agentscope.tool"].ToolResponse = SimpleNamespace
        self.modules["qwenpaw.app.channels.base"].BaseChannel = BaseChannel
        self.modules["qwenpaw.hooks.base"].LifecycleHook = type("LifecycleHook", (), {})
        self.modules["qwenpaw.runtime.hooks"].HookResult = SimpleNamespace
        self.modules["qwenpaw.runtime.phases"].Phase = SimpleNamespace(PRE_DISPATCH="pre_dispatch", FINALLY="finally")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = RuntimeStub()
        self.temp = tempfile.TemporaryDirectory()
        self.media_root = Path(self.temp.name)
        self.runtime.profile.channels.astrbot.files_dir = str(self.media_root)
        self.modules_patch = ExitStack()
        self.modules_patch.enter_context(module_overrides(self.runtime.modules))
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.tools = importlib.import_module("qwenpaw_plugin_astrbot_bridge.tools")
        self.tools.configure_tools({})
        self.channel_module = importlib.import_module("qwenpaw_plugin_astrbot_bridge.channel")

    async def asyncSetUp(self):
        set_turn_id(TURN_ID)

    async def asyncTearDown(self):
        clear_turn_id()

    def tearDown(self):
        clear_turn_id()
        self.modules_patch.close()
        self.env_patch.stop()
        self.temp.cleanup()

    def media_file(self, name="report.txt", content=b"actual report bytes"):
        workspace = media_workspace(str(self.media_root), SESSION)
        path = Path(workspace["outbound_dir"]) / name
        path.write_bytes(content)
        return path

    def channel(self):
        channel = self.channel_module.AstrBotChannel.from_config(None, self.runtime.profile.channels.astrbot)
        channel.set_workspace(SimpleNamespace(agent_id=self.runtime.context.agent_id))
        channel._client.post = AsyncMock(return_value={"accepted": True})
        return channel

    def message(self, **changes):
        data = {"object": "message", "status": "completed", "type": "message", "role": "assistant",
                "id": "msg_one", "content": [{"type": "text", "text": "任务完成"}]}
        data.update(changes)
        return SimpleNamespace(**data)

    async def test_tool_call_binds_runtime_route(self):
        self.runtime.context.session_id = OTHER_SESSION
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            post.return_value = {"result": "ok"}
            result = await self.tools.astrbot_call_tool("search", {"query": "hi"})
        self.assertEqual(result, {"result": "ok"})
        path, payload = post.call_args.args
        self.assertEqual(path, "/v1/tools/call")
        self.assertEqual(payload["session_id"], SESSION)
        self.assertEqual(payload["user_id"], "owner")
        self.assertEqual(payload["turn_id"], TURN_ID)
        self.assertEqual(payload["agent_id"], "default")
        self.assertTrue(payload["call_id"])
        self.assertEqual(payload["arguments"], {"query": "hi"})

    async def test_model_cannot_supply_routing_arguments(self):
        with self.assertRaises(TypeError):
            await self.tools.astrbot_call_tool("search", {}, session_id=OTHER_SESSION)
        with self.assertRaises(TypeError):
            await self.tools.astrbot_call_tool("search", {}, turn_id=TURN_ID)
        with self.assertRaises(TypeError):
            await self.tools.astrbot_call_tool("search", {}, agent_id=PERSON_AGENT)
        self.assertEqual(list(inspect.signature(self.tools.astrbot_call_tool).parameters), ["tool_name", "arguments"])
        self.assertEqual(list(inspect.signature(self.tools.astrbot_list_tools).parameters), [])

    async def test_scheduled_or_stale_context_without_nonce_cannot_use_tools(self):
        clear_turn_id()
        with self.assertRaises(BridgeError):
            await self.tools.astrbot_list_tools()
        with self.assertRaises(BridgeError):
            await self.tools.astrbot_call_tool("lookup", {"query": "x"})

    async def test_hook_reads_native_context_and_clears_each_request(self):
        hooks = importlib.import_module("qwenpaw_plugin_astrbot_bridge.runtime_hooks")
        hook = hooks.BridgeTurnContextHook()
        self.assertEqual(hook.phase, "pre_dispatch")
        valid = SimpleNamespace(request=SimpleNamespace(channel="astrbot", request_context={"astrbot_bridge_turn_id": TURN_ID}))
        await hook.run(valid)
        self.assertEqual(get_turn_id(), TURN_ID)
        self.assertEqual(hook.after, ("contextvars_setup",))
        self.assertEqual(self.runtime.context.approval_route["channel_meta"]["astrbot_bridge_turn_id"], TURN_ID)
        for request in [
            SimpleNamespace(channel="astrbot", request_context={}),
            SimpleNamespace(channel="astrbot", request_context={"astrbot_bridge_turn_id": "invalid"}),
            SimpleNamespace(channel="console", request_context={"astrbot_bridge_turn_id": TURN_ID}),
        ]:
            set_turn_id(TURN_ID)
            await hook.run(SimpleNamespace(request=request))
            with self.assertRaises(BridgeError):
                get_turn_id()

    async def test_fork_context_inherits_nonce_without_mutating_parent_metadata(self):
        hooks = importlib.import_module("qwenpaw_plugin_astrbot_bridge.runtime_hooks")
        parent_route = {"channel_meta": {"other": "preserved"}}
        self.runtime.context.approval_route = parent_route
        await hooks.BridgeTurnContextHook().run(SimpleNamespace(request=SimpleNamespace(
            channel="astrbot", request_context={"astrbot_bridge_turn_id": TURN_ID}
        )))
        self.assertEqual(parent_route, {"channel_meta": {"other": "preserved"}})
        native_child_context = {
            "_spawn_subagent": True,
            "channel_meta": self.runtime.context.approval_route["channel_meta"],
        }
        clear_turn_id()
        await hooks.BridgeTurnContextHook().run(SimpleNamespace(request=SimpleNamespace(
            channel="astrbot", request_context=native_child_context
        )))
        self.assertEqual(get_turn_id(), TURN_ID)
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            await self.tools.astrbot_list_tools()
        self.assertEqual(post.call_args.args[1]["turn_id"], TURN_ID)
        # A plain request cannot use the inherited metadata escape hatch.
        await hooks.BridgeTurnContextHook().run(SimpleNamespace(request=SimpleNamespace(
            channel="astrbot", request_context={"channel_meta": native_child_context["channel_meta"]}
        )))
        with self.assertRaises(BridgeError):
            get_turn_id()

    async def test_cleanup_prevents_reuse_but_child_task_retains_original_turn(self):
        hooks = importlib.import_module("qwenpaw_plugin_astrbot_bridge.runtime_hooks")
        ready = asyncio.Event()
        proceed = asyncio.Event()

        async def late_task():
            ready.set()
            await proceed.wait()
            return get_turn_id()

        set_turn_id(TURN_ID)
        child = asyncio.create_task(late_task())
        await ready.wait()
        await hooks.BridgeTurnCleanupHook().run(SimpleNamespace())
        with self.assertRaises(BridgeError):
            get_turn_id()
        set_turn_id("d" * 32)
        proceed.set()
        self.assertEqual(await child, TURN_ID)
        self.assertEqual(get_turn_id(), "d" * 32)

    async def test_console_channel_cannot_use_plugin_tools(self):
        self.runtime.context.channel = "console"
        with self.assertRaises(BridgeError):
            await self.tools.astrbot_list_tools()

    async def test_other_agent_cannot_use_plugin_tools(self):
        self.runtime.context.agent_id = "other"
        with self.assertRaises(BridgeError):
            await self.tools.astrbot_list_tools()

    async def test_unregistered_session_cannot_use_plugin_tools(self):
        self.runtime.context.root_session_id = "console-session"
        with self.assertRaises(BridgeError):
            await self.tools.astrbot_list_tools()

    async def test_missing_user_cannot_use_plugin_tools(self):
        self.runtime.context.user_id = None
        with self.assertRaises(BridgeError):
            await self.tools.astrbot_list_tools()

    async def test_tool_listing_uses_authenticated_route(self):
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            post.return_value = {"tools": []}
            await self.tools.astrbot_list_tools()
        self.assertEqual(post.call_args.args, ("/v1/tools/list", {"session_id": SESSION, "user_id": "owner", "turn_id": TURN_ID, "agent_id": "default"}))

    async def test_managed_agents_send_real_identity_on_every_tool_callback(self):
        self.media_file()
        for agent_id in (PERSON_AGENT, OTHER_PERSON_AGENT, GROUP_AGENT):
            with self.subTest(agent_id=agent_id):
                self.runtime.context.agent_id = agent_id
                with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
                    post.return_value = {"accepted": True, "tools": []}
                    await self.tools.astrbot_list_tools()
                    await self.tools.astrbot_call_tool("search", {})
                    await self.tools.astrbot_send_file("outbound/report.txt")
                self.assertEqual([call.args[0] for call in post.call_args_list],
                                 ["/v1/tools/list", "/v1/tools/call", "/v1/deliver"])
                self.assertTrue(all(call.args[1]["agent_id"] == agent_id for call in post.call_args_list))
                self.assertTrue(all(call.args[1]["session_id"] == SESSION for call in post.call_args_list))

    async def test_missing_agent_cannot_fall_back_to_active_default(self):
        self.runtime.context.agent_id = None
        self.assertEqual(self.runtime.modules["qwenpaw.app.agent_context"].get_current_agent_id(), "default")
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            for operation in (self.tools.astrbot_list_tools, self.tools.astrbot_media_workspace):
                with self.assertRaises(BridgeError):
                    await operation()
            with self.assertRaises(BridgeError):
                await self.tools.astrbot_call_tool("search", {})
            with self.assertRaises(BridgeError):
                await self.tools.astrbot_send_file("outbound/report.txt")
            post.assert_not_awaited()

    async def test_malformed_managed_agent_ids_fail_closed(self):
        for agent_id in ("abp_", "abp_" + "A" * 32, "abp_" + "1" * 31,
                         PERSON_AGENT + "/x", "abx_" + "1" * 32, 123):
            self.runtime.context.agent_id = agent_id
            with self.subTest(agent_id=agent_id), self.assertRaises(BridgeError):
                self.tools.trusted_route()

    async def test_same_route_different_agent_cannot_impersonate_owner_at_gateway(self):
        # This tests the wire identity and error path. The real server's route
        # ownership checks have their own AstrBot/integration tests.
        async def gateway(path, payload):
            if payload["agent_id"] != PERSON_AGENT:
                raise BridgeError("AstrBot bridge rejected request (HTTP 403).")
            return {"tools": []}
        with patch.object(self.tools.BridgeClient, "post", side_effect=gateway) as post:
            self.runtime.context.agent_id = PERSON_AGENT
            await self.tools.astrbot_list_tools()
            self.runtime.context.agent_id = OTHER_PERSON_AGENT
            with self.assertRaises(BridgeError):
                await self.tools.astrbot_list_tools()
        self.assertEqual([call.args[1]["agent_id"] for call in post.call_args_list],
                         [PERSON_AGENT, OTHER_PERSON_AGENT])

    async def test_cron_final_and_fixed_text_keep_workspace_identity_without_request(self):
        self.runtime.context.agent_id = PERSON_AGENT
        channel = self.channel()
        # Official Cron can dispatch after request cleanup, using the channel
        # that ChannelManager bound to its actual Agent workspace.
        self.runtime.context.agent_id = None
        self.runtime.context.channel = None
        self.runtime.context.user_id = None
        self.runtime.context.session_id = None
        self.runtime.context.root_session_id = None
        await channel.send_event(user_id="owner", session_id=SESSION, event=self.message(),
                                 meta={"agent_id": OTHER_PERSON_AGENT})
        await channel.send(SESSION, "reminder", {"session_id": SESSION, "user_id": "owner",
                                               "agent_id": OTHER_PERSON_AGENT})
        self.assertEqual(channel._client.post.await_count, 2)
        self.assertTrue(all(call.args[1]["agent_id"] == PERSON_AGENT
                            for call in channel._client.post.call_args_list))

    async def test_channel_missing_native_agent_ignores_spoofed_metadata(self):
        channel = self.channel_module.AstrBotChannel.from_config(
            None, self.runtime.profile.channels.astrbot,
            workspace_dir="/app/working/workspaces/default")
        channel._client.post = AsyncMock(return_value={"accepted": True})
        self.runtime.context.agent_id = None
        self.runtime.context.channel = None
        with self.assertRaises(BridgeError):
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message(),
                                     meta={"agent_id": "default"})
        channel._client.post.assert_not_awaited()

    async def test_channel_cannot_reuse_another_agent_workspace(self):
        self.runtime.context.agent_id = PERSON_AGENT
        channel = self.channel()
        self.runtime.context.agent_id = OTHER_PERSON_AGENT
        with self.assertRaises(BridgeError):
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        channel._client.post.assert_not_awaited()

    async def test_console_cannot_send_using_bound_astrbot_channel(self):
        channel = self.channel()
        self.runtime.context.channel = "console"
        with self.assertRaises(BridgeError):
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        self.runtime.context.agent_id = None
        with self.assertRaises(BridgeError):
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        channel._client.post.assert_not_awaited()

    async def test_invalid_bound_workspace_cannot_fall_back_to_real_default(self):
        channel = self.channel()
        channel.set_workspace(SimpleNamespace())
        with self.assertRaises(BridgeError):
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        channel._client.post.assert_not_awaited()

    async def test_final_message_accepted_once(self):
        channel = self.channel()
        event = self.message()
        await channel.send_event(user_id="owner", session_id=SESSION, event=event)
        await channel.send_event(user_id="owner", session_id=SESSION, event=event)
        channel._client.post.assert_awaited_once()
        payload = channel._client.post.call_args.args[1]
        self.assertEqual(payload["content"], [{"type": "text", "text": "任务完成"}])

    async def test_failed_acceptance_does_not_mark_delivery_done(self):
        channel = self.channel()
        channel._client.post.side_effect = [BridgeError("offline"), {"accepted": True}]
        with self.assertRaises(BridgeError):
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        calls = channel._client.post.call_args_list
        self.assertEqual(calls[0].args[1]["delivery_id"], calls[1].args[1]["delivery_id"])

    async def test_gateway_false_acceptance_is_a_safe_proactive_error(self):
        channel = self.channel()
        channel._client.post.return_value = {
            "accepted": False, "error": "private server diagnostics", "delivery_status": "route_unavailable",
        }
        with self.assertRaisesRegex(BridgeError, "do not repeat") as caught:
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message())
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(channel._accepted), 0)
        channel._client.post.assert_awaited_once()

    async def test_reasoning_tool_delta_and_user_are_never_sent(self):
        channel = self.channel()
        for changes in [
            {"type": "reasoning"}, {"type": "function_call_output"}, {"type": "mcp_tool_call_output"},
            {"status": "in_progress"}, {"role": "user"}, {"object": "content"},
        ]:
            await channel.send_event(user_id="owner", session_id=SESSION, event=self.message(**changes))
        channel._client.post.assert_not_called()

    async def test_identical_fixed_text_jobs_are_separate_executions(self):
        channel = self.channel()
        for _ in range(2):
            await channel.send(SESSION, "same daily reminder", {"session_id": SESSION, "user_id": "owner"})
        self.assertEqual(channel._client.post.await_count, 2)
        first, second = channel._client.post.call_args_list
        self.assertNotEqual(first.args[1]["delivery_id"], second.args[1]["delivery_id"])

    async def test_media_paths_and_urls_are_not_disclosed(self):
        channel = self.channel()
        event = self.message(content=[
            {"type": "image", "image_url": "/private/account-secret.png"},
            {"type": "file", "file_url": "https://private.example/?token=secret"},
            {"type": "audio", "data": "sensitivebase64"},
        ])
        await channel.send_event(user_id="owner", session_id=SESSION, event=event)
        payload = channel._client.post.call_args.args[1]
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private", encoded)
        self.assertNotIn("sensitivebase64", encoded)
        self.assertTrue(all(item["type"] == "text" for item in payload["content"]))
        self.assertIn("共享工作区", encoded)

    async def test_no_native_input_is_accepted(self):
        with self.assertRaises(BridgeError):
            self.channel().build_agent_request_from_native({"sender": "spoof"})

    async def test_factory_accepts_official_extra_kwargs(self):
        channel = self.channel_module.AstrBotChannel.from_config(
            process=None, config=self.runtime.profile.channels.astrbot,
            no_text_debounce=True, workspace_dir="/app/working/workspaces/default"
        )
        self.assertEqual(channel.channel, "astrbot")
        self.assertEqual(channel.to_handle_from_target(user_id="owner", session_id=SESSION), SESSION)

    async def test_registration_exposes_channel_and_context_bound_tools(self):
        module = importlib.import_module("qwenpaw_plugin_astrbot_bridge.plugin")
        api = SimpleNamespace(config={}, register_channel=unittest.mock.Mock(), register_tool=unittest.mock.Mock(),
                              register_runtime_hook=unittest.mock.Mock(), register_middleware=unittest.mock.Mock())
        module.plugin.register(api)
        self.assertEqual(api.register_channel.call_args.kwargs["channel_class"].channel, "astrbot")
        self.assertEqual([call.kwargs["tool_name"] for call in api.register_tool.call_args_list],
                         ["astrbot_list_tools", "astrbot_call_tool", "astrbot_media_workspace", "astrbot_send_file", "astrbot_browser"])
        self.assertEqual([call.args[0].phase for call in api.register_runtime_hook.call_args_list], ["pre_dispatch", "finally"])
        api.register_middleware.assert_called_once()
        self.assertEqual(api.register_middleware.call_args.args[0].__name__, "workspace_guard_factory")
        self.assertEqual(api.register_middleware.call_args.kwargs, {"priority": 0})

    async def test_media_workspace_uses_true_root_and_allows_proactive_context(self):
        clear_turn_id()
        self.runtime.context.session_id = OTHER_SESSION
        workspace = await self.tools.astrbot_media_workspace()
        self.assertEqual(Path(workspace["outbound_dir"]), self.media_root.resolve() / SESSION / "outbound")
        self.assertEqual(Path(workspace["inbound_dir"]), self.media_root.resolve() / SESSION / "inbound")
        self.assertEqual(workspace["max_file_bytes"], 20971520)
        self.assertEqual(workspace["max_attachments"], 4)
        self.assertEqual(list(inspect.signature(self.tools.astrbot_media_workspace).parameters), [])

    async def test_media_send_reads_real_bytes_and_uses_runtime_identity(self):
        path = self.media_file()
        clear_turn_id()
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            post.return_value = {"accepted": True, "delivery_status": "submitted_to_adapter"}
            await self.tools.astrbot_send_file("outbound/report.txt")
        endpoint, payload = post.call_args.args
        self.assertEqual(endpoint, "/v1/deliver")
        self.assertEqual((payload["session_id"], payload["user_id"], payload["agent_id"]), (SESSION, "owner", "default"))
        self.assertEqual(payload["content"], [{
            "type": "file", "path": SESSION + "/outbound/report.txt", "filename": "report.txt",
            "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }])
        self.assertTrue(payload["delivery_id"])
        self.assertEqual(list(inspect.signature(self.tools.astrbot_send_file).parameters), ["path", "kind"])
        with self.assertRaises(TypeError):
            await self.tools.astrbot_send_file(str(path), user_id="someone-else")

    async def test_media_send_false_acceptance_raises_without_automatic_resend(self):
        path = self.media_file()
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            post.return_value = {"accepted": False, "error": "private credentials or platform exception"}
            with self.assertRaisesRegex(BridgeError, "do not repeat") as caught:
                await self.tools.astrbot_send_file(str(path))
            post.assert_awaited_once()
        self.assertNotIn("private", str(caught.exception))

    async def test_native_assistant_media_uses_same_descriptor_and_dedupe(self):
        path = self.media_file("picture.png", b"image bytes fixture")
        channel = self.channel()
        event = self.message(content=[{"type": "text", "text": "请看图片"},
                                      {"type": "image", "image_url": path.as_uri()}])
        await channel.send_event(user_id="owner", session_id=SESSION, event=event)
        await channel.send_event(user_id="owner", session_id=SESSION, event=event)
        channel._client.post.assert_awaited_once()
        content = channel._client.post.call_args.args[1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "请看图片"})
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["path"], SESSION + "/outbound/picture.png")
        self.assertEqual(content[1]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    async def test_nested_unicode_file_uri_preserves_real_bytes(self):
        base = self.media_file().parent
        nested = base / "reports"
        nested.mkdir()
        path = nested / "报告.pdf"
        path.write_bytes(b"generated nested report")
        descriptor = media_descriptor(str(self.media_root), SESSION, path.as_uri(), "file")
        self.assertEqual(descriptor["path"], SESSION + "/outbound/reports/报告.pdf")
        self.assertEqual(descriptor["filename"], "报告.pdf")
        self.assertEqual(descriptor["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    async def test_native_audio_and_video_are_readable_descriptors(self):
        path = self.media_file("media.bin", b"media bytes fixture")
        channel = self.channel()
        await channel.send_content_parts(SESSION, [
            {"type": "audio", "data": str(path), "format": "wav"},
            {"type": "video", "video_url": path.as_uri()},
        ], {"session_id": SESSION, "user_id": "owner"})
        content = channel._client.post.call_args.args[1]["content"]
        self.assertEqual([part["type"] for part in content], ["audio", "video"])
        self.assertTrue(all(part["size"] == path.stat().st_size for part in content))

    async def test_media_rejects_other_session_inbound_urls_and_traversal(self):
        path = self.media_file()
        foreign = self.media_root / OTHER_SESSION / "outbound"
        foreign.mkdir(parents=True)
        (foreign / "other.txt").write_bytes(b"other private conversation")
        incoming = self.media_root / SESSION / "inbound" / "upload.txt"
        incoming.write_bytes(b"incoming source")
        for source in [str(foreign / "other.txt"), str(incoming), str(self.media_root / "secret.txt"),
                       "https://private.example/file?token=secret", "data:image/png;base64,c2VjcmV0",
                       "outbound/../inbound/upload.txt", "../outbound/report.txt", "file://remote-server/share.txt",
                       "file:///bridge-files/%2e%2e/private", "outbound\\report.txt"]:
            with self.subTest(source=source):
                with self.assertRaises(BridgeError):
                    media_descriptor(str(self.media_root), SESSION, source, "file")
        self.assertEqual(media_descriptor(str(self.media_root), SESSION, str(path), "file")["size"], path.stat().st_size)

    async def test_media_rejects_links_directories_empty_and_oversize_files(self):
        path = self.media_file()
        hardlink = path.parent / "linked.txt"
        try:
            os.link(path, hardlink)
        except OSError:
            self.skipTest("Hardlink creation unavailable on this host")
        with self.assertRaises(BridgeError):
            media_descriptor(str(self.media_root), SESSION, str(hardlink), "file")
        hardlink.unlink()
        empty = self.media_file("empty.txt", b"")
        for source in [str(empty), str(path.parent)]:
            with self.assertRaises(BridgeError):
                media_descriptor(str(self.media_root), SESSION, source, "file")
        with self.assertRaises(BridgeError):
            media_descriptor(str(self.media_root), SESSION, str(path), "file", max_file_bytes=2)

    async def test_media_rejects_symlink_even_to_own_outbound(self):
        path = self.media_file()
        link = path.parent / "symlink.txt"
        try:
            link.symlink_to(path)
        except OSError:
            self.skipTest("Symlink creation unavailable on this host")
        with self.assertRaises(BridgeError):
            media_descriptor(str(self.media_root), SESSION, str(link), "file")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO creation unavailable on this host")
    async def test_fifo_is_rejected_without_blocking_the_worker(self):
        path = self.media_file().parent / "pipe"
        os.mkfifo(path)
        # A bounded child process detects a blocking regression without leaving
        # a stuck non-daemon executor thread in the test process.
        code = (
            "import sys; from qwenpaw_plugin_astrbot_bridge.media import media_descriptor; "
            "from qwenpaw_plugin_astrbot_bridge.bridge_client import BridgeError; "
            "\ntry: media_descriptor(*sys.argv[1:], 'file')"
            "\nexcept BridgeError: sys.exit(0)"
            "\nelse: sys.exit(1)"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.media_root), SESSION, str(path)],
                                timeout=3, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    async def test_media_hash_rejects_changed_file(self):
        path = self.media_file()
        original_read = os.read
        modified = False

        def changed_read(fd, count):
            nonlocal modified
            data = original_read(fd, count)
            if not modified:
                modified = True
                path.write_bytes(b"modified during export")
            return data

        with patch("qwenpaw_plugin_astrbot_bridge.media.os.read", side_effect=changed_read):
            with self.assertRaises(BridgeError):
                media_descriptor(str(self.media_root), SESSION, str(path), "file")

    async def test_media_names_limits_and_bad_context_fail_before_network(self):
        path = self.media_file()
        for name in ["../bad.txt", "url:secret", "bad\nname"]:
            with self.assertRaises(BridgeError):
                media_descriptor(str(self.media_root), SESSION, str(path), "file", name)
        with patch.object(self.tools.BridgeClient, "post", new_callable=AsyncMock) as post:
            self.runtime.context.channel = "console"
            with self.assertRaises(BridgeError):
                await self.tools.astrbot_send_file(str(path))
            post.assert_not_awaited()

    async def test_more_than_four_native_attachments_gets_explicit_notice(self):
        path = self.media_file()
        channel = self.channel()
        await channel.send_content_parts(SESSION, [{"type": "file", "file_url": str(path)}] * 5,
                                         {"session_id": SESSION, "user_id": "owner"})
        content = channel._client.post.call_args.args[1]["content"]
        self.assertEqual(sum(part["type"] == "file" for part in content), 4)
        self.assertIn("附件未发送", content[-1]["text"])


class HttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_preserves_id_and_entire_body(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"accepted":true}'
        opener = unittest.mock.Mock()
        opener.open.side_effect = [URLError("offline"), response]
        settings = BridgeSettings("http://astrbot:9186", "test-token", attempts=2)
        with patch("qwenpaw_plugin_astrbot_bridge.bridge_client.build_opener", return_value=opener), \
                patch("qwenpaw_plugin_astrbot_bridge.bridge_client.time.sleep"):
            await BridgeClient(settings).post("/v1/deliver", {"delivery_id": "same-on-retry"})
        first, second = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.get_header("Authorization"), "Bearer test-token")

    async def test_internal_bridge_ignores_external_proxy_environment(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"tools":[]}'
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.dict(os.environ, {"HTTP_PROXY": "http://external-proxy:8080"}), \
                patch("qwenpaw_plugin_astrbot_bridge.bridge_client.build_opener", return_value=opener) as build:
            await BridgeClient(BridgeSettings("http://astrbot:9186", "test")).post("/v1/tools/list", {})
        self.assertEqual(build.call_args.args[0].proxies, {})

    async def test_rejected_response_does_not_echo_sensitive_body(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = HTTPError("http://private?token=secret", 403, "raw-secret", {}, None)
        with patch("qwenpaw_plugin_astrbot_bridge.bridge_client.build_opener", return_value=opener):
            with self.assertRaisesRegex(BridgeError, r"HTTP 403") as error:
                await BridgeClient(BridgeSettings("http://astrbot:9186", "test")).post("/v1/tools/list", {})
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)

    async def test_redirect_is_rejected_instead_of_replaying_credentials(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = HTTPError("http://astrbot:9186", 302, "redirect", {}, None)
        with patch("qwenpaw_plugin_astrbot_bridge.bridge_client.build_opener", return_value=opener):
            with self.assertRaisesRegex(BridgeError, "HTTP 302"):
                await BridgeClient(BridgeSettings("http://astrbot:9186", "test")).post("/v1/tools/list", {})

    async def test_environment_wins_and_empty_token_fails_closed(self):
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "env-token", "ASTRBOT_BRIDGE_URL": "http://gateway:9186"}, clear=True):
            settings = BridgeSettings.from_config({"bridge": {"callback_url": "http://wrong:9186", "callback_token": "cfg"}})
            self.assertEqual(settings.base_url, "http://gateway:9186")
            self.assertEqual(settings.token, "env-token")
        with patch.dict(os.environ, {"BRIDGE_TOKEN": ""}, clear=True):
            with self.assertRaises(BridgeError):
                BridgeSettings.from_config({"callback_token": "cfg"})

    async def test_invalid_url_or_operation_is_rejected(self):
        for url in ["file:///etc/passwd", "http://user:pass@host", "http://host?token=x"]:
            with patch.dict(os.environ, {"BRIDGE_TOKEN": "test", "ASTRBOT_BRIDGE_URL": url}, clear=True):
                with self.assertRaises(BridgeError):
                    BridgeSettings.from_config({})
        with self.assertRaises(BridgeError):
            await BridgeClient(BridgeSettings("http://astrbot:9186", "test")).post("/admin", {})

    async def test_shared_root_env_priority_and_limits(self):
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "test", "BRIDGE_FILES_ROOT": "root", "BRIDGE_FILES_DIR": "legacy",
                                     "BRIDGE_MAX_FILE_BYTES": "1024", "BRIDGE_MAX_FILES": "2"}, clear=True):
            settings = BridgeSettings.from_config({"files_dir": "config"})
            self.assertEqual((settings.files_dir, settings.max_file_bytes, settings.max_files), ("root", 1024, 2))
        for variable, value in [("BRIDGE_MAX_FILE_BYTES", "20971521"), ("BRIDGE_MAX_FILES", "5"),
                                ("BRIDGE_MAX_FILES", "0"), ("BRIDGE_MAX_FILE_BYTES", "invalid")]:
            with patch.dict(os.environ, {"BRIDGE_TOKEN": "test", variable: value}, clear=True):
                with self.assertRaises(BridgeError):
                    BridgeSettings.from_config({})


if __name__ == "__main__":
    unittest.main()
