"""Protocol tests use lightweight API stubs, not an installed QwenPaw runtime."""

import asyncio
import importlib
import json
import os
import inspect
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

from qwenpaw_plugin_astrbot_bridge.bridge_client import BridgeClient, BridgeError, BridgeSettings
from qwenpaw_plugin_astrbot_bridge.turn_context import clear_turn_id, get_turn_id, set_turn_id


SESSION = "ab_" + "a" * 32
OTHER_SESSION = "ab_" + "b" * 32
TURN_ID = "c" * 32


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
        ]}
        for field in ["session_id", "root_session_id", "user_id", "channel", "agent_id"]:
            setattr(self.modules["qwenpaw.app.agent_context"], "get_current_" + field,
                    lambda field=field: getattr(self.context, field))
        self.modules["qwenpaw.config.config"].load_agent_config = lambda agent_id: self.profile
        self.modules["qwenpaw.app.agent_context"].get_current_approval_route = lambda: self.context.approval_route
        self.modules["qwenpaw.app.agent_context"].set_current_approval_route = lambda route: setattr(self.context, "approval_route", route)

        class BaseChannel:
            def __init__(self, process, on_reply_sent=None, display_config=None):
                self._process = process
                self._on_reply_sent = on_reply_sent

        self.modules["qwenpaw.app.channels.base"].BaseChannel = BaseChannel
        self.modules["qwenpaw.hooks.base"].LifecycleHook = type("LifecycleHook", (), {})
        self.modules["qwenpaw.runtime.hooks"].HookResult = SimpleNamespace
        self.modules["qwenpaw.runtime.phases"].Phase = SimpleNamespace(PRE_DISPATCH="pre_dispatch", FINALLY="finally")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = RuntimeStub()
        self.modules_patch = patch.dict(sys.modules, self.runtime.modules)
        self.modules_patch.start()
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
        self.modules_patch.stop()
        self.env_patch.stop()

    def channel(self):
        channel = self.channel_module.AstrBotChannel.from_config(None, self.runtime.profile.channels.astrbot)
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
        self.assertTrue(payload["call_id"])
        self.assertEqual(payload["arguments"], {"query": "hi"})

    async def test_model_cannot_supply_routing_arguments(self):
        with self.assertRaises(TypeError):
            await self.tools.astrbot_call_tool("search", {}, session_id=OTHER_SESSION)
        with self.assertRaises(TypeError):
            await self.tools.astrbot_call_tool("search", {}, turn_id=TURN_ID)
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
        self.assertEqual(post.call_args.args, ("/v1/tools/list", {"session_id": SESSION, "user_id": "owner", "turn_id": TURN_ID}))

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
        self.assertIn("控制台", encoded)

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
                              register_runtime_hook=unittest.mock.Mock())
        module.plugin.register(api)
        self.assertEqual(api.register_channel.call_args.kwargs["channel_class"].channel, "astrbot")
        self.assertEqual([call.kwargs["tool_name"] for call in api.register_tool.call_args_list],
                         ["astrbot_list_tools", "astrbot_call_tool"])
        self.assertEqual([call.args[0].phase for call in api.register_runtime_hook.call_args_list], ["pre_dispatch", "finally"])


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


if __name__ == "__main__":
    unittest.main()
