"""Adapter contract tests, without launching AstrBot or a user's API."""

import asyncio
import importlib
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from module_stubs import module_overrides
from astrbot_plugin_qwenpaw_bridge.bridge_core import BridgeStore


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    return value


class Plain:
    def __init__(self, text):
        self.text = text


class MediaComponent:
    def __init__(self, file="", url="", path=None, **kwargs):
        self.file = file
        self.url = url
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=path, path=path)


class Image(MediaComponent): pass
class Record(MediaComponent): pass
class Video(MediaComponent): pass


class File:
    def __init__(self, name="file.bin", file="", url=""):
        self.name = name
        self.file_ = file
        self.file = file
        self.path = file
        self.url = url


class Chain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def set_result_content_type(self, kind):
        self.kind = kind
        return self


class Star:
    def __init__(self, context):
        self.context = context


def decorator(*args, **kwargs):
    return lambda function: function


ASTRBOT_MODULES = {
    "astrbot": module("astrbot"),
    "astrbot.api": module("astrbot.api", AstrBotConfig=dict, logger=logging.getLogger("test")),
    "astrbot.api.event": module(
        "astrbot.api.event", AstrMessageEvent=object, MessageChain=Chain,
        ResultContentType=types.SimpleNamespace(LLM_RESULT="llm"),
        filter=types.SimpleNamespace(command=decorator, event_message_type=decorator,
                                     EventMessageType=types.SimpleNamespace(ALL="all")),
    ),
    "astrbot.api.message_components": module("astrbot.api.message_components", Plain=Plain, Image=Image, Record=Record, Video=Video, File=File),
    "astrbot.api.star": module("astrbot.api.star", Context=object, Star=Star),
}
with module_overrides(ASTRBOT_MODULES):
    adapter = importlib.import_module("astrbot_plugin_qwenpaw_bridge.main")


class Event:
    def __init__(self, user="owner", origin="wx:FriendMessage:owner", message_id="msg-1", text="hello"):
        self.user = user
        self.unified_msg_origin = origin
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.text = text
        self.is_at_or_wake_command = True
        self.call_llm = False
        self._has_send_oper = False
        self.extras = {}
        self.stopped = False
        self.segments = []
        self.result = None
        self.send = AsyncMock()

    def get_sender_id(self): return self.user
    def get_platform_name(self): return "weixin_oc"
    def get_platform_id(self): return "wx"
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def get_message_str(self): return self.text
    def get_messages(self): return self.segments
    def is_stopped(self): return self.stopped
    def stop_event(self): self.stopped = True
    def plain_result(self, text): return Chain([Plain(text)])
    def chain_result(self, chain): return Chain(chain)
    def clear_result(self): self.result = None


class Request:
    def __init__(self, body, auth="Bearer " + "a" * 32):
        self.body = body
        self.headers = {"Authorization": auth}

    async def json(self): return self.body


class Tool:
    def __init__(self, name="lookup", module_path="plugin.on", **overrides):
        self.name = name
        self.description = "lookup description"
        self.parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        self.active = True
        self.handler_module_path = module_path
        self.is_background_task = False
        self.handler = object()
        self.__dict__.update(overrides)


class MCPTool(Tool): pass
class HandoffTool(Tool): pass


class ToolSet:
    def __init__(self): self.tools = []
    def add_tool(self, tool):
        self.tools = [old for old in self.tools if old.name != tool.name] + [tool]


class CallToolResult:
    def __init__(self, content, isError=False):
        self.content = content
        self.isError = isError

    def model_dump(self, **kwargs): return {"content": self.content, "isError": self.isError}


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_environment_uses_dashboard_owner_and_tool_settings(self):
        with patch.dict("os.environ", {"OWNER_USER_IDS": "  ", "TOOL_ALLOWLIST": ""}):
            config = {"owner_user_ids": ["dashboard-owner"], "tool_allowlist": ["lookup"]}
            self.assertEqual(adapter._setting_list(config, "owner_user_ids", "OWNER_USER_IDS"), {"dashboard-owner"})
            self.assertEqual(adapter._setting_list(config, "tool_allowlist", "TOOL_ALLOWLIST"), {"lookup"})
            self.assertEqual(adapter._setting_list({}, "owner_user_ids", "OWNER_USER_IDS"), set())
            self.assertEqual(adapter._setting_list({}, "tool_allowlist", "TOOL_ALLOWLIST"), set())

    async def test_nonempty_environment_remains_explicit_override(self):
        with patch.dict("os.environ", {"OWNER_USER_IDS": "env-owner", "TOOL_ALLOWLIST": "*"}):
            config = {"owner_user_ids": ["dashboard-owner"], "tool_allowlist": ["lookup"]}
            self.assertEqual(adapter._setting_list(config, "owner_user_ids", "OWNER_USER_IDS"), {"env-owner"})
            self.assertEqual(adapter._setting_list(config, "tool_allowlist", "TOOL_ALLOWLIST"), {"*"})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.bridge = adapter.QwenPawBridge(self.context, {"owner_user_ids": ["owner"],
            "files_root": str(Path(self.temp.name) / "shared"), "source_roots": [self.temp.name]})
        self.bridge.token = "a" * 32
        self.bridge._store = BridgeStore(Path(self.temp.name) / "state.sqlite3")
        self.bridge._client = types.SimpleNamespace(
            agent_id="default", chat=AsyncMock(return_value=[{"type": "text", "text": "reply"}]),
            pending_approvals=AsyncMock(return_value=[]), approval=AsyncMock(),
        )
        self.bridge._personal = types.SimpleNamespace(ensure_agent=AsyncMock(return_value={}), model_ready=AsyncMock(return_value=True), memory_list=AsyncMock(return_value=[]), remember=AsyncMock(return_value={"id": "a" * 32, "text": "prefers tea"}), correct=AsyncMock(return_value={}), forget=AsyncMock(return_value={}))
        self.route = self.bridge._store.personal_session("wx:FriendMessage:owner", "owner", "weixin_oc", "wx", True)
        self.sid = self.route["session_id"]

    def tearDown(self):
        self.bridge._store.close()
        self.temp.cleanup()

    async def test_owner_default_is_closed(self):
        closed = adapter.QwenPawBridge(self.context, {})
        self.assertEqual(closed.owners, set())
        self.assertEqual(closed.tool_allowlist, set())

    async def test_missing_model_does_not_submit_and_allows_same_message_after_configuration(self):
        self.bridge._personal.model_ready.return_value = False
        result = [item async for item in self.bridge.forward_chat(Event(message_id="configure-later"))]
        self.assertIn("还没有配置可用模型", result[0].chain[0].text)
        self.bridge._client.chat.assert_not_awaited()
        self.assertNotIn(self.sid, self.bridge._active)
        self.bridge._personal.model_ready.return_value = True
        result = [item async for item in self.bridge.forward_chat(Event(message_id="configure-later"))]
        self.assertEqual(result[0].chain[0].text, "reply")
        self.bridge._client.chat.assert_awaited_once()

    async def test_provision_failure_never_falls_back_to_shared_agent(self):
        self.bridge._personal.ensure_agent.side_effect = adapter.BridgeError("个人助手隔离设置已改变")
        result = [item async for item in self.bridge.forward_chat(Event())]
        self.assertIn("隔离设置已改变", result[0].chain[0].text)
        self.bridge._client.chat.assert_not_awaited()

    async def test_explicit_memory_works_without_model_and_preserves_spaces(self):
        self.bridge._personal.model_ready.return_value = False
        event = Event(text="/paw remember 我喜欢 清淡的茶")
        result = [item async for item in self.bridge.paw_command(event, "remember", "我喜欢")]
        self.bridge._personal.remember.assert_awaited_once_with(self.route["agent_id"], "我喜欢 清淡的茶")
        self.assertIn("已保存记忆", result[0].chain[0].text)
        self.assertIn("搜索索引会稍后同步", result[0].chain[0].text)
        self.bridge._personal.model_ready.assert_not_awaited()
        self.bridge._client.chat.assert_not_awaited()

    async def test_memory_index_failure_does_not_claim_write_failed(self):
        self.bridge._personal.remember.side_effect = adapter.BridgeError("记忆文件已更新，但检索索引尚未确认完成")
        result = [item async for item in self.bridge.paw_command(Event(text="/paw remember tea"), "remember", "tea")]
        self.assertIn("记忆文件已更新", result[0].chain[0].text)

    async def test_memory_saved_with_index_warning_shows_note_id_and_warning(self):
        self.bridge._personal.remember.return_value = {"id": "a" * 32, "text": "tea", "index_status": "pending", "index_warning": "刷新请求未完成，勿重复新增"}
        result = [item async for item in self.bridge.paw_command(Event(text="/paw remember tea"), "remember", "tea")]
        self.assertIn("a" * 32, result[0].chain[0].text)
        self.assertIn("勿重复新增", result[0].chain[0].text)

    async def test_memory_listing_can_reach_later_notes_and_bounds_reply_size(self):
        self.bridge._personal.memory_list.return_value = [{"id": f"{index:032x}", "text": "茶" * 8000} for index in range(21)]
        result = [item async for item in self.bridge.paw_command(Event(text="/paw memory 3"), "memory", "3")]
        text = result[0].chain[0].text
        self.assertIn(f"{20:032x}", text)
        self.assertIn("第 3/3 页", text)
        self.assertLess(len(text), 1000)
        result = [item async for item in self.bridge.paw_command(Event(text="/paw memory 4"), "memory", "4")]
        self.assertIn("有效页码", result[0].chain[0].text)

    async def test_correct_and_forget_are_bound_to_current_person(self):
        note = "b" * 32
        [item async for item in self.bridge.paw_command(Event(text=f"/paw correct {note} 喜欢红茶"), "correct", note)]
        self.bridge._personal.correct.assert_awaited_once_with(self.route["agent_id"], note, "喜欢红茶")
        result = [item async for item in self.bridge.paw_command(Event(text=f"/paw forget {note}"), "forget", note)]
        self.bridge._personal.forget.assert_awaited_once_with(self.route["agent_id"], note)
        self.assertIn("不会一起删除", result[0].chain[0].text)
        self.assertIn("短暂召回旧内容", result[0].chain[0].text)

    async def test_new_session_preserves_person_and_revokes_previous_callback(self):
        [item async for item in self.bridge.paw_command(Event(), "new")]
        current = self.bridge._personal_route(Event())
        self.assertEqual(current["agent_id"], self.route["agent_id"])
        self.assertNotEqual(current["session_id"], self.sid)
        with self.assertRaises(adapter.BridgeFault):
            self.bridge._route({"session_id": self.sid, "user_id": "owner", "agent_id": self.route["agent_id"]})

    async def test_groups_do_not_use_private_agent_and_different_members_do_not_share(self):
        first = self.bridge._personal_route(Event(origin="wx:GroupMessage:group"))
        other = self.bridge._personal_route(Event(user="other", origin="wx:GroupMessage:group"))
        self.assertEqual(first["scope"], "group")
        self.assertNotEqual(first["agent_id"], self.route["agent_id"])
        self.assertNotEqual(first["agent_id"], other["agent_id"])

    async def test_link_is_private_only_and_preserves_person_after_restart(self):
        denied = [item async for item in self.bridge.paw_command(Event(origin="wx:GroupMessage:group"), "link")]
        self.assertIn("私聊", denied[0].chain[0].text)
        code = self.bridge._store.identities.create_link("weixin_oc", "wx", "owner", "wx:FriendMessage:owner", True)
        event = Event(origin="qq:FriendMessage:owner", text=f"/paw link {code}")
        event.get_platform_name = lambda: "aiocqhttp"
        event.get_platform_id = lambda: "qq"
        result = [item async for item in self.bridge.paw_command(event, "link", code)]
        self.assertIn("绑定完成", result[0].chain[0].text)
        self.bridge._store.close()
        self.bridge._store = BridgeStore(Path(self.temp.name) / "state.sqlite3")
        qq = self.bridge._personal_route(event)
        self.assertEqual(qq["agent_id"], self.route["agent_id"])
        self.assertNotEqual(qq["session_id"], self.sid)

    async def test_missing_or_wrong_bearer_is_rejected(self):
        handler = AsyncMock()
        response = await self.bridge._auth(Request({}, "Bearer wrong"), handler)
        self.assertEqual(response.status, 401)
        handler.assert_not_awaited()

    async def test_callback_cannot_change_owner_or_session(self):
        for body in [
            {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "intruder"},
            {"session_id": "nonexistent", "user_id": "owner"},
        ]:
            with self.assertRaises(adapter.BridgeFault) as exc:
                self.bridge._route(body)
            self.assertEqual(exc.exception.status, 403)

    async def test_tool_callbacks_require_unexpired_original_event(self):
        with self.assertRaises(adapter.BridgeFault):
            self.bridge._turn(self.sid, "owner")
        self.bridge._active[self.sid] = adapter.ActiveTurn(Event(), "owner", time.monotonic() - 1)
        with self.assertRaises(adapter.BridgeFault):
            self.bridge._turn(self.sid, "owner")

    async def test_completed_delivery_replay_has_one_side_effect(self):
        body = {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "delivery_id": "d1",
                "content": [{"type": "text", "text": "later"}]}
        first = await self.bridge._deliver(Request(body))
        second = await self.bridge._deliver(Request(body))
        self.assertEqual(first.body, second.body)
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.context.send_message.await_args.args[0], self.route["origin"])

    async def test_partial_delivery_error_does_not_retry(self):
        self.context.send_message.side_effect = RuntimeError("private value must never be returned")
        body = {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "delivery_id": "d2",
                "content": [{"type": "text", "text": "later"}]}
        first = await self.bridge._deliver(Request(body))
        second = await self.bridge._deliver(Request(body))
        self.assertEqual(first.body, second.body)
        self.assertNotIn(b"private value", first.body)
        self.context.send_message.assert_awaited_once()

    async def test_queued_delivery_cannot_use_a_retired_session(self):
        lock = self.bridge._delivery_locks.setdefault(self.sid, asyncio.Lock())
        await lock.acquire()
        body = {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "delivery_id": "queued", "content": [{"type": "text", "text": "obsolete"}]}
        task = asyncio.create_task(self.bridge._deliver(Request(body)))
        await asyncio.sleep(0)
        [item async for item in self.bridge.paw_command(Event(), "new")]
        lock.release()
        with self.assertRaises(adapter.BridgeFault):
            await task
        self.context.send_message.assert_not_awaited()

    async def test_delivery_rechecks_route_after_waiting_for_media(self):
        async def media(*args, **kwargs):
            [item async for item in self.bridge.paw_command(Event(), "new")]
            return [Plain("obsolete")]
        self.bridge._media.output_components = media
        body = {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "delivery_id": "media", "content": [{"type": "text", "text": "obsolete"}]}
        with self.assertRaises(adapter.BridgeFault):
            await self.bridge._deliver(Request(body))
        self.context.send_message.assert_not_awaited()

    async def test_running_receipt_is_not_reported_as_completed(self):
        self.bridge._store.claim("tool", self.sid + ":c1")
        with self.assertRaises(adapter.BridgeFault) as exc:
            self.bridge._completed_receipt("tool", self.sid + ":c1")
        self.assertEqual(exc.exception.status, 409)

    async def test_existing_zero_argument_command_is_preserved(self):
        event = Event()
        event.extras = {"activated_handlers": [types.SimpleNamespace(handler_full_name="plugin.command")],
                        "handlers_parsed_params": {"plugin.command": {}}}
        result = [item async for item in self.bridge.forward_chat(event)]
        self.assertEqual(result, [])
        self.bridge._client.chat.assert_not_awaited()
        self.assertFalse(event.call_llm)

    async def test_unwoken_group_message_is_preserved(self):
        event = Event()
        event.is_at_or_wake_command = False
        self.assertEqual([item async for item in self.bridge.forward_chat(event)], [])
        self.bridge._client.chat.assert_not_awaited()

    async def test_unavailable_attachment_is_explicitly_refused(self):
        event = Event()
        event.segments = [Image(file=str(Path(self.temp.name) / "missing.png"))]
        result = [item async for item in self.bridge.forward_chat(event)]
        self.assertIn("本次未转发", result[0].chain[0].text)
        self.bridge._client.chat.assert_not_awaited()
        self.assertTrue(event.call_llm)

    async def test_reply_is_yielded_before_stopping_for_result_hooks(self):
        event = Event()
        stream = self.bridge.forward_chat(event)
        result = await anext(stream)
        self.assertEqual(result.kind, "llm")
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.call_llm)
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertTrue(event.is_stopped())
        self.assertNotIn(self.sid, self.bridge._active)

    async def test_platform_message_replay_does_not_resubmit_chat(self):
        first = Event(message_id="same-message")
        second = Event(message_id="same-message")
        self.assertEqual(len([item async for item in self.bridge.forward_chat(first)]), 1)
        self.assertEqual([item async for item in self.bridge.forward_chat(second)], [])
        self.bridge._client.chat.assert_awaited_once()

    async def test_failed_chat_is_not_resubmitted_on_platform_replay(self):
        self.bridge._client.chat.side_effect = RuntimeError("uncertain operation")
        first = Event(message_id="failed-message")
        second = Event(message_id="failed-message")
        self.assertEqual(len([item async for item in self.bridge.forward_chat(first)]), 1)
        self.assertEqual([item async for item in self.bridge.forward_chat(second)], [])
        self.bridge._client.chat.assert_awaited_once()

    async def test_approval_cannot_cross_session_or_agent(self):
        self.bridge._client.pending_approvals.return_value = [
            {"session_id": self.sid, "agent_id": self.route["agent_id"], "owner_agent_id": "other-agent", "request_id": "other"},
            {"session_id": "other-session", "owner_agent_id": self.route["agent_id"], "request_id": "cross"},
        ]
        for request_id in ("other", "cross"):
            result = [item async for item in self.bridge.paw_command(Event(), "approve", request_id)]
            self.assertIn("不属于", result[0].chain[0].text)
        self.bridge._client.approval.assert_not_awaited()

    async def test_pending_and_approval_use_original_route_owner(self):
        self.bridge._client.pending_approvals.return_value = [
            {"session_id": self.sid, "agent_id": self.route["agent_id"], "owner_agent_id": self.route["agent_id"], "request_id": "a1",
             "title": "write file", "target": "/workspace/report.txt"},
        ]
        result = [item async for item in self.bridge.paw_command(Event(), "pending")]
        self.assertIn("/workspace/report.txt", result[0].chain[0].text)
        [item async for item in self.bridge.paw_command(Event(), "approve", "a1")]
        self.bridge._client.approval.assert_awaited_once_with("a1", self.sid, "owner", True)

    async def test_tool_filter_honors_activation_and_session_plugin_scope(self):
        tools = [Tool(), Tool("off", active=False), Tool("background", is_background_task=True),
                 HandoffTool("handoff"), Tool("unknown", "missing"),
                 Tool("disabled", "plugin.disabled"), Tool("other", "plugin.other"),
                 Tool("globallyoff", "plugin.globallyoff")]
        self.context.get_llm_tool_manager = lambda: types.SimpleNamespace(func_list=tools)
        self.bridge.tool_allowlist = {"*"}
        event = Event()
        event.plugins_name = ["on", "disabled", "globallyoff"]
        plugins = {
            "plugin." + name: types.SimpleNamespace(name=name, activated=name != "globallyoff", reserved=False)
            for name in ("on", "disabled", "other", "globallyoff")
        }
        session_manager = types.SimpleNamespace(is_plugin_enabled_for_session=AsyncMock(side_effect=lambda sid, name: name != "disabled"))
        injected = {
            "astrbot.core.agent.handoff": module("handoff", HandoffTool=HandoffTool),
            "astrbot.core.agent.mcp_client": module("mcp_client", MCPTool=MCPTool),
            "astrbot.core.agent.tool": module("tool", ToolSet=ToolSet),
            "astrbot.core.star.session_plugin_manager": module("session_plugin_manager", SessionPluginManager=session_manager),
            "astrbot.core.star.star": module("star", star_map=plugins),
        }
        with module_overrides(injected):
            selected = await self.bridge._available_tools(event)
        self.assertEqual(set(selected), {"lookup"})

    async def test_official_executor_and_hooks_handle_direct_message(self):
        event = Event()
        turn = adapter.ActiveTurn(event, "owner", time.monotonic() + 60)
        hooks = types.SimpleNamespace(on_tool_start=AsyncMock(), on_tool_end=AsyncMock())

        class Executor:
            @staticmethod
            async def execute(tool, wrapper, **arguments):
                await wrapper.context.event.send(Chain([Plain("image result already sent")]))
                yield None

        injected = {
            "mcp.types": module("mcp.types", CallToolResult=CallToolResult),
            "astrbot.core.agent.run_context": module("run_context", ContextWrapper=lambda **kw: types.SimpleNamespace(**kw)),
            "astrbot.core.astr_agent_context": module("astr_agent_context", AstrAgentContext=lambda **kw: types.SimpleNamespace(**kw)),
            "astrbot.core.astr_agent_hooks": module("astr_agent_hooks", MAIN_AGENT_HOOKS=hooks),
            "astrbot.core.astr_agent_tool_exec": module("astr_agent_tool_exec", FunctionToolExecutor=Executor),
        }
        original_send = event.send
        with module_overrides(injected):
            result = await self.bridge._execute_tool(turn, Tool(), {"query": "safe"})
        hooks.on_tool_start.assert_awaited_once()
        hooks.on_tool_end.assert_awaited_once()
        original_send.assert_awaited_once()
        self.assertIs(event.send, original_send)
        self.assertTrue(result["direct_message_sent"])
        self.assertFalse(result["isError"])
        self.assertIn("already sent", result["content"][0]["text"])

    async def test_tool_end_hook_error_is_safe_and_returned(self):
        event = Event()
        turn = adapter.ActiveTurn(event, "owner", time.monotonic() + 60)
        hooks = types.SimpleNamespace(on_tool_start=AsyncMock(), on_tool_end=AsyncMock(side_effect=RuntimeError("private hook secret")))

        class Executor:
            @staticmethod
            async def execute(tool, wrapper, **arguments):
                yield CallToolResult([{"type": "text", "text": "normal result"}])

        injected = {
            "mcp.types": module("mcp.types", CallToolResult=CallToolResult),
            "astrbot.core.agent.run_context": module("run_context", ContextWrapper=lambda **kw: types.SimpleNamespace(**kw)),
            "astrbot.core.astr_agent_context": module("astr_agent_context", AstrAgentContext=lambda **kw: types.SimpleNamespace(**kw)),
            "astrbot.core.astr_agent_hooks": module("astr_agent_hooks", MAIN_AGENT_HOOKS=hooks),
            "astrbot.core.astr_agent_tool_exec": module("astr_agent_tool_exec", FunctionToolExecutor=Executor),
        }
        with module_overrides(injected):
            result = await self.bridge._execute_tool(turn, Tool(), {"query": "safe"})
        self.assertTrue(result["isError"])
        self.assertNotIn("private hook secret", str(result))

    async def test_tool_arguments_and_receipt_prevent_duplicate_execution(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("Install project jsonschema requirement to run this validation test")
        event = Event()
        turn = adapter.ActiveTurn(event, "owner", time.monotonic() + 60)
        self.bridge._active[self.sid] = turn
        self.bridge._available_tools = AsyncMock(return_value={"lookup": Tool()})
        self.bridge._execute_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "result"}], "isError": False})
        body = {"call_id": "call-1", "session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "turn_id": turn.turn_id,
                "tool_name": "lookup", "arguments": {"query": "safe"}}
        first = await self.bridge._tools_call(Request(body))
        second = await self.bridge._tools_call(Request(body))
        self.assertEqual(first.body, second.body)
        self.bridge._execute_tool.assert_awaited_once()
        body["call_id"] = "call-2"
        body["arguments"] = {"query": 42}
        with self.assertRaises(adapter.BridgeFault) as exc:
            await self.bridge._tools_call(Request(body))
        self.assertEqual(exc.exception.status, 400)
        self.assertIsNone(self.bridge._store.receipt("tool", self.sid + ":" + turn.turn_id + ":call-2"))

    async def test_queued_tool_cannot_use_a_replacement_turn(self):
        original = adapter.ActiveTurn(Event(), "owner", time.monotonic() + 60)
        self.bridge._active[self.sid] = original
        await original.tool_lock.acquire()
        body = {"call_id": "queued", "session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "turn_id": original.turn_id,
                "tool_name": "lookup", "arguments": {"query": "safe"}}
        task = asyncio.create_task(self.bridge._tools_call(Request(body)))
        await asyncio.sleep(0)
        self.bridge._active[self.sid] = adapter.ActiveTurn(Event(message_id="replacement"), "owner", time.monotonic() + 60)
        original.tool_lock.release()
        with self.assertRaises(adapter.BridgeFault) as exc:
            await task
        self.assertEqual(exc.exception.code, "astrbot_turn_mismatch_or_expired")

    async def test_same_session_chats_do_not_replace_active_event(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def chat(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return [{"type": "text", "text": "reply"}]

        self.bridge._client.chat.side_effect = chat
        first_event = Event(message_id="first")
        second_event = Event(message_id="second")

        async def consume(event):
            return [item async for item in self.bridge.forward_chat(event)]

        first = asyncio.create_task(consume(first_event))
        await asyncio.wait_for(started.wait(), timeout=3)
        second = asyncio.create_task(consume(second_event))
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        self.assertIs(self.bridge._active[self.sid].event, first_event)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(calls, 2)

    async def test_tool_callback_rejects_missing_or_previous_turn_nonce(self):
        old_turn = adapter.ActiveTurn(Event(message_id="old"), "owner", time.monotonic() + 60)
        current_turn = adapter.ActiveTurn(Event(message_id="new"), "owner", time.monotonic() + 60)
        self.bridge._active[self.sid] = current_turn
        self.bridge._available_tools = AsyncMock(return_value={})
        body = {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner"}
        with self.assertRaises(adapter.BridgeFault) as exc:
            await self.bridge._tools_list(Request(body))
        self.assertEqual(exc.exception.code, "invalid_turn_id")
        body["turn_id"] = old_turn.turn_id
        with self.assertRaises(adapter.BridgeFault) as exc:
            await self.bridge._tools_list(Request(body))
        self.assertEqual(exc.exception.code, "astrbot_turn_mismatch_or_expired")
        self.bridge._available_tools.assert_not_awaited()

    async def test_chat_passes_private_nonce_to_runtime_client(self):
        event = Event(message_id="nonce-message")
        stream = self.bridge.forward_chat(event)
        await anext(stream)
        nonce = self.bridge._active[self.sid].turn_id
        self.assertRegex(nonce, "^[0-9a-f]{32}$")
        self.bridge._client.chat.assert_awaited_once_with(self.sid, "owner", "hello", content=[{"type": "text", "text": "hello"}], turn_id=nonce, agent_id=self.route["agent_id"])
        await stream.aclose()

    async def test_completed_tool_receipt_is_bound_to_its_original_nonce(self):
        old = adapter.ActiveTurn(Event(message_id="old"), "owner", time.monotonic() + 60)
        new = adapter.ActiveTurn(Event(message_id="new"), "owner", time.monotonic() + 60)
        self.bridge._active[self.sid] = new
        key = self.sid + ":" + old.turn_id + ":same-call-id"
        result = {"content": [{"type": "text", "text": "old receipt"}], "isError": False}
        self.bridge._store.claim("tool", key)
        self.bridge._store.finish("tool", key, result)
        body = {"session_id": self.sid, "agent_id": self.route["agent_id"], "user_id": "owner", "turn_id": old.turn_id,
                "call_id": "same-call-id", "tool_name": "lookup", "arguments": {}}
        old_response = await self.bridge._tools_call(Request(body))
        self.assertIn(b"old receipt", old_response.body)
        self.bridge._available_tools = AsyncMock(return_value={})
        body["turn_id"] = new.turn_id
        with self.assertRaises(adapter.BridgeFault) as exc:
            await self.bridge._tools_call(Request(body))
        self.assertEqual(exc.exception.code, "tool_not_allowed")


if __name__ == "__main__":
    unittest.main()
