"""AstrBot 4.25 adapter for the private QwenPaw bridge.

Owner text and supported media conversations are forwarded. Existing
commands retain AstrBot's normal permission checks and dispatch. Tool callbacks
are bound to the original live event; a persisted route permits later text
delivery, but does not permit a scheduled job to impersonate an expired event.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, ResultContentType, filter
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star

from .bridge_core import BridgeError, BridgeStore, QwenPawClient
from .identity import IdentityError
from .personal_agent import PersonalAgentManager
from .media_bridge import MediaBridge, MediaFailure, provider_component


class BridgeFault(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass
class ActiveTurn:
    event: Any
    user_id: str
    expires_at: float
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _setting_list(config: dict, key: str, env: str = "") -> set[str]:
    override = os.environ.get(env, "").strip() if env else ""
    value = override if override else config.get(key, [])
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _identifier(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise BridgeFault(400, "invalid_" + key)
    if any(ord(char) < 32 for char in value):
        raise BridgeFault(400, "invalid_" + key)
    return value


def _turn_nonce(payload: dict) -> str:
    value = payload.get("turn_id")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise BridgeFault(400, "invalid_turn_id")
    return value


def _has_selected_command(event: Any) -> bool:
    params = event.get_extra("handlers_parsed_params", {}) or {}
    handlers = event.get_extra("activated_handlers", []) or []
    return any(handler.handler_full_name in params for handler in handlers)


def _private_event(event: Any) -> bool:
    """Use the platform's message type, never words supplied in the message."""
    getter = getattr(event, "get_message_type", None)
    kind = getter() if callable(getter) else getattr(getattr(event, "message_obj", None), "type", None)
    kind = getattr(kind, "value", kind)
    if kind is not None:
        return kind == "FriendMessage"
    # AstrBot's persisted unified origin has a platform/type/session shape.
    parts = str(event.unified_msg_origin).split(":", 2)
    return len(parts) == 3 and parts[1] == "FriendMessage"


class QwenPawBridge(Star):
    """Keep chat ingress and plugin tools in AstrBot; delegate reasoning."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.enabled = bool(config.get("enabled", True))
        self.owners = _setting_list(config, "owner_user_ids", "OWNER_USER_IDS")
        self.owners.discard("*")
        self.tool_allowlist = _setting_list(config, "tool_allowlist", "TOOL_ALLOWLIST")
        self.platforms = _setting_list(config, "enabled_platforms") or {
            "weixin_oc", "aiocqhttp"
        }
        self.token = os.environ.get("BRIDGE_TOKEN", config.get("bridge_token", ""))
        self._client: QwenPawClient | None = None
        self._personal: PersonalAgentManager | None = None
        self._store: BridgeStore | None = None
        self._runner: web.AppRunner | None = None
        self._approval_task: asyncio.Task | None = None
        self._active: dict[str, ActiveTurn] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self.chat_timeout = max(30, min(int(config.get("chat_timeout", 900)), 3600))
        self.tool_timeout = max(5, min(int(config.get("tool_timeout", 120)), 600))
        roots = _setting_list(config, "source_roots", "BRIDGE_SOURCE_ROOTS")
        if not roots:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
                roots = {get_astrbot_temp_path()}
            except ImportError:
                roots = {"/AstrBot/data/temp"}
        self._media = MediaBridge(
            os.environ.get("BRIDGE_FILES_ROOT", config.get("files_root", "/bridge-files")),
            list(roots),
            max_bytes=int(os.environ.get("BRIDGE_MAX_FILE_BYTES", config.get("max_file_bytes", 20971520))),
            max_files=int(os.environ.get("BRIDGE_MAX_FILES", config.get("max_files", 4))),
            napcat_hosts=_setting_list(config, "napcat_hosts", "BRIDGE_NAPCAT_HOSTS") or {"napcat"},
            timeout=int(os.environ.get("BRIDGE_MEDIA_TIMEOUT", config.get("media_timeout", 60))),
            component_types={"text": Plain, "image": Image, "file": File, "video": Video, "audio": Record},
        )

    async def initialize(self):
        if not self.enabled:
            return
        if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", self.token or ""):
            raise ValueError("Set a random BRIDGE_TOKEN with at least 32 characters.")
        state = Path(os.environ.get(
            "BRIDGE_STATE_PATH",
            self.config.get("state_path", "/AstrBot/data/plugin_data/qwenpaw_bridge/bridge.sqlite3"),
        ))
        state.parent.mkdir(parents=True, exist_ok=True)
        self._store = BridgeStore(str(state))
        self._client = QwenPawClient(
            os.environ.get("QWENPAW_BASE_URL", self.config.get("qwenpaw_url", "http://qwenpaw:8088")),
            os.environ.get("QWENPAW_AGENT_ID", self.config.get("agent_id", "default")),
            os.environ.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", self.config.get("runtime_token", "")),
            timeout=self.chat_timeout,
        )
        self._personal = PersonalAgentManager(self._client)
        app = web.Application(client_max_size=1024 * 1024, middlewares=[self._auth])
        app.router.add_post("/v1/tools/list", self._tools_list)
        app.router.add_post("/v1/tools/call", self._tools_call)
        app.router.add_post("/v1/deliver", self._deliver)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, "0.0.0.0", 9186).start()
        except BaseException:
            await self.terminate()
            raise
        self._approval_task = asyncio.create_task(self._poll_approvals())
        logger.info("QwenPaw bridge ready on private port 9186; text and media transport enabled.")

    async def terminate(self):
        self.enabled = False
        if self._approval_task:
            self._approval_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._approval_task
            self._approval_task = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._client:
            await self._client.close()
            self._client = None
        self._personal = None
        self._active.clear()
        self._delivery_locks.clear()
        self._chat_locks.clear()
        if self._store:
            self._store.close()
            self._store = None

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        authorization = request.headers.get("Authorization", "")
        expected = "Bearer " + self.token
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except BridgeFault as exc:
            return web.json_response({"error": exc.code}, status=exc.status)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Runtime errors may contain URLs, user text or API credentials.
            logger.warning("QwenPaw bridge request failed; details withheld.")
            return web.json_response({"error": "bridge_internal_error"}, status=500)

    async def _payload(self, request: web.Request) -> dict:
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            raise BridgeFault(400, "invalid_json") from None
        if not isinstance(payload, dict):
            raise BridgeFault(400, "invalid_json")
        return payload

    def _route(self, payload: dict) -> tuple[str, str, dict]:
        if not self._store:
            raise BridgeFault(503, "bridge_not_ready")
        sid = _identifier(payload, "session_id")
        user = _identifier(payload, "user_id")
        route = self._store.get_session(sid)
        if not route or not route.get("active") or route.get("user_id") != user or user not in self.owners:
            raise BridgeFault(403, "route_not_authorized")
        expected_agent = route.get("agent_id")
        if expected_agent and payload.get("agent_id") != expected_agent:
            raise BridgeFault(403, "agent_route_mismatch")
        if not expected_agent and payload.get("agent_id") not in (None, self._client.agent_id):
            raise BridgeFault(403, "agent_route_mismatch")
        if route.get("platform") not in self.platforms:
            raise BridgeFault(403, "platform_not_authorized")
        return sid, user, route

    def _personal_route(self, event: AstrMessageEvent) -> dict:
        return self._store.personal_session(
            event.unified_msg_origin, event.get_sender_id(), event.get_platform_name(),
            event.get_platform_id(), _private_event(event),
        )

    async def _ensure_personal(self, route: dict):
        if self._personal is None:
            raise RuntimeError("Personal workspace manager is not ready")
        if route["scope"] == "private":
            self._store.identities.activate(route["person_id"])
        return await self._personal.ensure_agent(route["agent_id"], scope=route["scope"])

    def _turn(self, sid: str, user: str, turn_id: str | None = None) -> ActiveTurn:
        turn = self._active.get(sid)
        if not turn or turn.user_id != user or turn.expires_at < time.monotonic():
            raise BridgeFault(409, "no_active_astrbot_event")
        if turn.event.is_stopped():
            raise BridgeFault(409, "astrbot_event_stopped")
        if not turn_id or not hmac.compare_digest(turn.turn_id, turn_id):
            raise BridgeFault(409, "astrbot_turn_mismatch_or_expired")
        return turn

    def _completed_receipt(self, category: str, key: str) -> dict | None:
        receipt = self._store.receipt(category, key)
        if receipt is None:
            return None
        if receipt.get("state") != "done":
            raise BridgeFault(409, category + "_already_in_progress_or_uncertain")
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise BridgeFault(409, category + "_receipt_unavailable_do_not_repeat")
        return result

    async def _available_tools(self, event: AstrMessageEvent) -> dict[str, Any]:
        from astrbot.core.agent.handoff import HandoffTool
        from astrbot.core.agent.mcp_client import MCPTool
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.star.session_plugin_manager import SessionPluginManager
        from astrbot.core.star.star import star_map

        selected = ToolSet()
        if not self.tool_allowlist:
            return {}
        for tool in list(self.context.get_llm_tool_manager().func_list):
            if not tool.active or isinstance(tool, HandoffTool) or tool.is_background_task:
                continue
            if "*" not in self.tool_allowlist and tool.name not in self.tool_allowlist:
                continue
            module = tool.handler_module_path
            plugin = star_map.get(module) if module else None
            if plugin:
                if not plugin.activated:
                    continue
                if not plugin.reserved:
                    if event.plugins_name is not None and plugin.name not in event.plugins_name:
                        continue
                    if not await SessionPluginManager.is_plugin_enabled_for_session(
                        event.unified_msg_origin, plugin.name
                    ):
                        continue
            elif not isinstance(tool, MCPTool):
                # Unknown owners and internal tools are not published accidentally.
                continue
            selected.add_tool(tool)
        return {tool.name: tool for tool in selected.tools}

    async def _health(self, request: web.Request):
        return web.json_response({"ready": bool(self._client and self._store), "transport": "text_and_media"})

    async def _tools_list(self, request: web.Request):
        payload = await self._payload(request)
        sid, user, _ = self._route(payload)
        turn = self._turn(sid, user, _turn_nonce(payload))
        tools = await self._available_tools(turn.event)
        return web.json_response({"tools": [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.parameters}
            for tool in tools.values()
        ]})

    async def _tools_call(self, request: web.Request):
        payload = await self._payload(request)
        sid, user, _ = self._route(payload)
        turn_id = _turn_nonce(payload)
        call_id = _identifier(payload, "call_id")
        key = sid + ":" + turn_id + ":" + call_id
        receipt = self._completed_receipt("tool", key)
        if receipt is not None:
            return web.json_response(receipt)
        turn = self._turn(sid, user, turn_id)
        name = _identifier(payload, "tool_name")
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            raise BridgeFault(400, "invalid_arguments")
        async with turn.tool_lock:
            # Repeat all authorization checks after waiting for another tool.
            self._route(payload)
            if self._turn(sid, user, turn_id) is not turn:
                raise BridgeFault(409, "original_astrbot_event_expired")
            receipt = self._completed_receipt("tool", key)
            if receipt is not None:
                return web.json_response(receipt)
            tool = (await self._available_tools(turn.event)).get(name)
            if not tool:
                raise BridgeFault(403, "tool_not_allowed")
            import jsonschema
            try:
                jsonschema.validate(arguments, tool.parameters)
                allowed = tool.parameters.get("properties", {})
                if tool.handler and set(arguments) - set(allowed):
                    raise BridgeFault(400, "unexpected_tool_arguments")
            except jsonschema.ValidationError:
                raise BridgeFault(400, "invalid_tool_arguments") from None
            if not self._store.claim("tool", key):
                raise BridgeFault(409, "tool_call_already_in_progress_or_uncertain")
            result = await self._execute_tool(turn, tool, arguments)
            # Errors after execution begins must not release this idempotency key.
            self._store.finish("tool", key, result)
            return web.json_response(result)

    async def _execute_tool(self, turn: ActiveTurn, tool: Any, arguments: dict) -> dict:
        from mcp.types import CallToolResult
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.astr_agent_context import AstrAgentContext
        from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        wrapper = ContextWrapper(
            context=AstrAgentContext(context=self.context, event=turn.event),
            tool_call_timeout=self.tool_timeout,
        )
        content: list[dict] = []
        failed = False
        sent = 0
        send_failed = False
        event = turn.event
        original_send = event.send
        had_instance_send = "send" in getattr(event, "__dict__", {})

        async def tracked_send(*args, **kwargs):
            nonlocal sent, send_failed
            try:
                await original_send(*args, **kwargs)
            except Exception:
                send_failed = True
                raise
            sent += 1

        event.send = tracked_send
        last_result = None

        async def run():
            nonlocal last_result, failed
            await MAIN_AGENT_HOOKS.on_tool_start(wrapper, tool, arguments)
            if event.is_stopped():
                raise BridgeFault(409, "tool_stopped_by_plugin_hook")
            async for response in FunctionToolExecutor.execute(tool, wrapper, **arguments):
                if isinstance(response, CallToolResult):
                    last_result = response
                    failed = failed or bool(response.isError)
                    content.extend(response.model_dump(mode="json", exclude_none=True).get("content", []))

        try:
            await asyncio.wait_for(run(), timeout=self.tool_timeout)
        except asyncio.CancelledError:
            # An uncertain execution is permanently claimed, including shutdown.
            raise
        except Exception:
            failed = True
            content.append({"type": "text", "text": "AstrBot tool execution failed; do not repeat this call ID."})
        finally:
            try:
                try:
                    await MAIN_AGENT_HOOKS.on_tool_end(wrapper, tool, arguments, last_result)
                except Exception:
                    failed = True
                    event.clear_result()
                    content.append({"type": "text", "text": "The tool completed, but an AstrBot completion hook failed; do not repeat the operation."})
            finally:
                if had_instance_send:
                    event.send = original_send
                else:
                    event.__dict__.pop("send", None)
        if sent and not content:
            content.append({"type": "text", "text": "The tool already sent its result to the user. Do not repeat the operation or send the same result again."})
        if send_failed:
            failed = True
            content.append({"type": "text", "text": "The tool's direct message delivery failed or is uncertain; do not repeat its side effects."})
        return {"content": content, "isError": failed, "direct_message_sent": sent > 0}

    async def _deliver(self, request: web.Request):
        payload = await self._payload(request)
        sid, user, route = self._route(payload)
        delivery_id = _identifier(payload, "delivery_id")
        key = sid + ":" + delivery_id
        async with self._delivery_locks.setdefault(sid, asyncio.Lock()):
            sid, user, route = self._route(payload)
            receipt = self._completed_receipt("delivery", key)
            if receipt is not None:
                return web.json_response(receipt)
            try:
                chain = await self._media.output_components(sid, payload.get("content"), route["platform"])
            except MediaFailure as exc:
                raise BridgeFault(400, str(exc)) from None
            # Account linking or /paw new can revoke a route while a callback
            # waits for its lock or for an attachment snapshot to finish.
            sid, user, route = self._route(payload)
            if not self._store.claim("delivery", key):
                raise BridgeFault(409, "delivery_already_in_progress_or_uncertain")
            result = {"accepted": False, "session_id": sid}
            try:
                matched = await self.context.send_message(route["origin"], MessageChain(chain=chain))
                result = {
                    "accepted": bool(matched), "session_id": sid,
                    "delivery_status": "submitted_to_adapter" if matched else "route_unavailable",
                }
                if not matched:
                    result["error"] = "platform_route_unavailable"
            except Exception:
                # A platform may have delivered part of a chain before reporting failure.
                result["error"] = "delivery_failed_or_uncertain_do_not_repeat"
            self._store.finish("delivery", key, result)
            return web.json_response(result)

    @filter.command("paw")
    async def paw_command(self, event: AstrMessageEvent, action: str = "", request_id: str = ""):
        action = action.lower()
        if action == "whoami":
            yield event.plain_result(f"你的用户 ID：{event.get_sender_id()}\n平台 ID：{event.get_platform_id()}")
            event.stop_event()
            return
        if event.get_sender_id() not in self.owners:
            yield event.plain_result("此桥接仅允许配置的 owner 使用。先用 /paw whoami 查看自己的 ID。")
            event.stop_event()
            return
        if event.get_platform_name() not in self.platforms:
            yield event.plain_result("此平台尚未启用桥接。")
            event.stop_event()
            return
        if not self._store or not self._client:
            yield event.plain_result("桥接未启用或尚未就绪。")
            event.stop_event()
            return
        identity_args = (event.get_platform_name(), event.get_platform_id(), event.get_sender_id(), event.unified_msg_origin, _private_event(event))
        # AstrBot parses only positional tokens; preserve the full memory text.
        pieces = event.get_message_str().strip().split(maxsplit=2)
        argument = pieces[2] if len(pieces) == 3 else request_id
        if action == "link":
            try:
                if argument:
                    self._store.redeem_link(*identity_args, argument.strip())
                    message = "账号绑定完成：两边私聊将使用同一个个人记忆空间。群聊保持独立。"
                else:
                    code = self._store.identities.create_link(*identity_args)
                    message = f"在你另一个已获授权的账号私聊里发送：\n/paw link {code}\n绑定码10分钟有效，只给你自己的账号使用。已有独立记忆的账号不会被自动合并。"
            except IdentityError as exc:
                message = str(exc)
            yield event.plain_result(message)
            event.stop_event()
            return
        route = self._personal_route(event)
        if action == "status":
            kind = "个人" if route["scope"] == "private" else "当前群内的个人"
            message = f"桥接已授权。当前使用{kind}记忆空间。首次对话或记忆操作时自动建立工作区。\n助手ID：{route['agent_id']}\n接收会话：{route['session_id']}\n在QwenPaw后台按助手ID选择对应助手查看记忆、配置模型和任务。\n已放行AstrBot工具：{'全部符合会话权限的工具' if '*' in self.tool_allowlist else len(self.tool_allowlist)}。"
            yield event.plain_result(message)
            event.stop_event()
            return
        if action == "new":
            if self._chat_locks.get(route["agent_id"], asyncio.Lock()).locked():
                message = "当前还有任务执行中，请完成后再开始新会话。"
            else:
                self._store.personal_session(event.unified_msg_origin, event.get_sender_id(), event.get_platform_name(), event.get_platform_id(), _private_event(event), new_session=True)
                message = "已开始新会话，长期记忆保留。旧会话的主动任务需在QwenPaw中重新选择接收会话。"
            yield event.plain_result(message)
            event.stop_event()
            return
        if action in {"memory", "remember", "correct", "forget"}:
            lock = self._chat_locks.setdefault(route["agent_id"], asyncio.Lock())
            try:
                async with lock:
                    await self._ensure_personal(route)
                    aid = route["agent_id"]
                    note = None
                    if action == "memory":
                        if argument and (not argument.isdecimal() or len(argument) > 3 or int(argument) < 1):
                            raise BridgeError("用法：/paw memory [页码]，例如 /paw memory 2。")
                        notes = await self._personal.memory_list(aid)
                        pages = max(1, (len(notes) + 9) // 10)
                        page = int(argument) if argument else 1
                        if page > pages:
                            raise BridgeError(f"手动记忆共 {pages} 页，请使用 /paw memory 1 等有效页码。")
                        message = "\n".join(f"{item['id']}：{item['text'][:200]}{'…' if len(item['text']) > 200 else ''}" for item in notes[(page-1)*10:page*10]) or "还没有手动记忆。用 /paw remember 内容 记录。"
                        message += f"\n第 {page}/{pages} 页，共 {len(notes)} 条。使用 /paw memory 页码 翻页。"
                        message += "\n这里列出手动记忆；自动提炼的记忆可在对应QwenPaw工作区查看。"
                    elif action == "remember" and argument:
                        note = await self._personal.remember(aid, argument)
                        message = f"已保存记忆 {note['id']}：{note['text']}\n搜索索引会稍后同步，刚保存时不一定能立即召回。"
                    elif action == "correct" and len(argument.split(maxsplit=1)) == 2:
                        note_id, text = argument.split(maxsplit=1)
                        note = await self._personal.correct(aid, note_id, text)
                        message = "已更正这条手动记忆。搜索索引会稍后同步，期间可能短暂召回旧内容。"
                    elif action == "forget" and argument:
                        note = await self._personal.forget(aid, argument.strip())
                        message = "已移除这条手动记忆。搜索索引会稍后同步，期间可能短暂召回旧内容。历史聊天和另外提炼的自动记忆不会一起删除，可在QwenPaw中继续管理。"
                    else:
                        message = "用法：/paw remember 内容；/paw correct 记忆ID 新内容；/paw forget 记忆ID。"
                    if isinstance(note, dict) and note.get("index_warning"):
                        message += "\n" + str(note["index_warning"])[:600]
            except BridgeError as exc:
                message = str(exc)[:600]
            except Exception:
                message = "记忆操作未能确认完成，请检查QwenPaw工作区状态后再操作；不会切换到其他人的记忆空间。"
            yield event.plain_result(message)
            event.stop_event()
            return
        if action not in {"approve", "deny", "pending"}:
            yield event.plain_result("命令：/paw whoami；/paw status；/paw link [绑定码]；/paw new；/paw memory；/paw remember 内容；/paw correct 记忆ID 内容；/paw forget 记忆ID；/paw pending；/paw approve 请求ID；/paw deny 请求ID。")
            event.stop_event()
            return
        try:
            pending = [item for item in await self._client.pending_approvals()
                       if item.get("session_id") == route["session_id"]
                       and item.get("owner_agent_id") == route["agent_id"]]
            if action == "pending":
                message = "\n\n".join(
                    f"请求ID：{item.get('request_id', '')}\n操作：{str(item.get('title') or '需要审核的操作')[:200]}\n目标：{str(item.get('target') or '(服务未提供目标；建议拒绝并在后台核实)')[:1000]}"
                    for item in pending
                ) or "当前会话没有待审核请求。"
            elif not request_id or not any(item.get("request_id") == request_id for item in pending):
                message = "请求不存在，或不属于此用户的当前会话。用 /paw pending 查询。"
            else:
                await self._client.approval(
                    request_id, route["session_id"], event.get_sender_id(), action == "approve"
                )
                message = "已提交同意。" if action == "approve" else "已提交拒绝。"
        except Exception:
            message = "审核服务暂时不可用；未自动批准任何请求。"
        yield event.plain_result(message)
        event.stop_event()

    async def _poll_approvals(self):
        while True:
            try:
                for item in await self._client.pending_approvals():
                    sid = item.get("session_id")
                    request_id = item.get("request_id")
                    if not isinstance(sid, str) or not isinstance(request_id, str):
                        continue
                    route = self._store.get_session(sid)
                    if (not route or not route.get("active") or route.get("user_id") not in self.owners or route.get("platform") not in self.platforms
                            or item.get("owner_agent_id") != (route.get("agent_id") or self._client.agent_id)):
                        continue
                    key = sid + ":" + request_id
                    if not self._store.claim("approval_notice", key):
                        continue
                    title = str(item.get("title") or "需要审核的操作").replace("\n", " ")[:200]
                    target = str(item.get("target") or "(服务未提供目标；建议拒绝并在后台核实)")[:1000]
                    message = (
                        f"QwenPaw 等待你审核：{title}\n目标：{target}\n请求ID：{request_id}\n"
                        f"/paw approve {request_id}\n/paw deny {request_id}"
                    )
                    try:
                        await self.context.send_message(route["origin"], MessageChain(chain=[Plain(message)]))
                    finally:
                        self._store.finish("approval_notice", key)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("QwenPaw approval polling temporarily unavailable.")
            await asyncio.sleep(5)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10000)
    async def forward_chat(self, event: AstrMessageEvent):
        if (not self.enabled or event.is_stopped() or not event.is_at_or_wake_command
                or event.get_platform_name() not in self.platforms
                or _has_selected_command(event) or event._has_send_oper or event.call_llm):
            return
        event.call_llm = True
        if event.get_sender_id() not in self.owners:
            yield event.plain_result("桥接当前仅允许 owner 使用。用 /paw whoami 查看 ID 后添加 owner_user_ids。")
            event.stop_event()
            return
        if not self._client or not self._store:
            yield event.plain_result("QwenPaw 桥接尚未就绪。")
            event.stop_event()
            return
        media = []
        for segment in event.get_messages():
            for cls, kind in ((Image, "image"), (Record, "audio"), (Video, "video"), (File, "file")):
                if isinstance(segment, cls):
                    media.append((kind, segment))
                    break
        if len(media) > self._media.max_files:
            yield event.plain_result(f"每条消息最多转发{self._media.max_files}个附件，请分开发送。")
            event.stop_event()
            return
        text = event.get_message_str().strip()
        if not text and not media:
            yield event.plain_result("桥接需要文本或支持的图片、文件、视频、音频附件。")
            event.stop_event()
            return
        if len(text) > 32000:
            yield event.plain_result("文本太长，请分成少于32000字符的消息。")
            event.stop_event()
            return
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not message_id or len(message_id) > 256:
            yield event.plain_result("平台未提供可用于防重的消息ID，本次尚未转发。请查看平台连接状态。")
            event.stop_event()
            return
        route = self._personal_route(event)
        sid = route["session_id"]
        lock = self._chat_locks.setdefault(route["agent_id"], asyncio.Lock())
        async with lock:
            chat_key = sid + ":" + message_id
            if not self._store.claim("chat", chat_key):
                event.stop_event()
                return
            turn = ActiveTurn(event, event.get_sender_id(), time.monotonic() + self.chat_timeout + 30)
            self._active[sid] = turn
            submitted = False
            try:
                await self._ensure_personal(route)
                if not await self._personal.model_ready(route["agent_id"]):
                    raise BridgeError("QwenPaw 还没有配置可用模型。请先在其后台选择模型；个人记忆的手动保存和管理仍可使用。")
                native_content = []
                if text:
                    native_content.append({"type": "text", "text": text})
                for kind, segment in media:
                    asset = await self._media.import_component(sid, kind, provider_component(event, segment, kind))
                    native_content.append(asset.native())
                submitted = True
                content = await self._client.chat(
                    sid, event.get_sender_id(), text or "请处理本条消息的附件。",
                    content=native_content, turn_id=turn.turn_id,
                    agent_id=route["agent_id"],
                )
                chain = await self._media.output_components(sid, content, event.get_platform_name(), allow_native=True)
                result = event.chain_result(chain)
                yield result.set_result_content_type(ResultContentType.LLM_RESULT)
            except asyncio.CancelledError:
                raise
            except MediaFailure as exc:
                logger.warning("QwenPaw media rejected: %s", str(exc))
                if submitted:
                    yield event.plain_result("返回的附件未通过安全检查，本次没有发送附件；请检查文件路径、大小和完整性。")
                else:
                    yield event.plain_result("附件无法安全读取或下载，本次未转发；请检查附件大小、来源路径和链接。")
            except BridgeError as exc:
                yield event.plain_result(str(exc)[:600])
            except Exception:
                if submitted:
                    yield event.plain_result("QwenPaw 本次未返回可发送的内容。任务可能仍在执行，请勿重复提交；可查看后台状态。")
                else:
                    yield event.plain_result("个人助手或附件尚未准备好，本次未提交任务。请检查QwenPaw后台与桥接配置。")
            finally:
                self._active.pop(sid, None)
                if self._store:
                    if submitted:
                        self._store.finish("chat", chat_key, {"status": "finished_or_uncertain"})
                    else:
                        self._store.fail("chat", chat_key)
                event.stop_event()
