"""Outbound-only channel for final proactive results; input stays in AstrBot."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from qwenpaw.app.channels.base import BaseChannel

from .bridge_client import BridgeClient, BridgeError, BridgeSettings, config_value
from .media import outgoing_content
from .tools import authorized_agent_id


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def is_completed_assistant_message(event: Any) -> bool:
    return (
        _value(config_value(event, "object")) == "message"
        and _value(config_value(event, "status")) == "completed"
        and _value(config_value(event, "type")) == "message"
        and _value(config_value(event, "role")) == "assistant"
    )


class AstrBotChannel(BaseChannel):
    channel = "astrbot"
    uses_manager_queue = False

    def __init__(self, process, config, on_reply_sent=None, display_config=None):
        super().__init__(process, on_reply_sent=on_reply_sent, display_config=display_config)
        self.enabled = bool(config_value(config, "enabled", False))
        self.bot_prefix = ""
        self._settings = BridgeSettings.from_config(config)
        self._client = BridgeClient(self._settings)
        self._accepted: OrderedDict[str, None] = OrderedDict()
        self._send_lock = asyncio.Lock()

    @classmethod
    def from_config(cls, process, config, on_reply_sent=None, display_config=None, **kwargs):
        return cls(process, config, on_reply_sent, display_config)

    @classmethod
    def from_env(cls, process, on_reply_sent=None):
        return cls(process, {"enabled": True}, on_reply_sent)

    def build_agent_request_from_native(self, native_payload):
        raise BridgeError("AstrBot channel accepts outbound notifications only.")

    def resolve_session_id(self, sender_id, channel_meta=None):
        session_id = (channel_meta or {}).get("session_id")
        if not session_id:
            raise BridgeError("Proactive delivery requires a registered AstrBot session.")
        return session_id

    def to_handle_from_target(self, *, user_id, session_id):
        return session_id

    async def start(self):
        # No polling/QR login: AstrBot owns the platform connections.
        return None

    async def stop(self):
        return None

    async def health_check(self):
        return {
            "channel": self.channel,
            "status": "healthy" if self.enabled else "disabled",
            "detail": "Outbound bridge configured; platform delivery is verified by AstrBot.",
        }

    def _delivery_agent_id(self) -> str:
        from qwenpaw.app.agent_context import get_current_channel, peek_current_agent_id

        current = peek_current_agent_id()
        # Explicit Console requests must not become platform deliveries. A
        # scheduled dispatch can legitimately have no request ContextVar.
        if get_current_channel() not in (None, "", "astrbot"):
            raise BridgeError("This request is not an AstrBot conversation.")
        workspace = getattr(self, "_workspace", None)
        if workspace is not None:
            # ChannelManager.set_workspace injects the real owning workspace
            # after from_config. Cron uses that same per-Agent manager even
            # after the Agent request context has been cleaned up.
            bound = authorized_agent_id(getattr(workspace, "agent_id", None))
            if current and current != bound:
                raise BridgeError("The request Agent does not own this AstrBot channel.")
            return bound
        # Useful for direct native-context sends; never infer from metadata,
        # from a workspace path, or from the active/default Agent setting.
        return authorized_agent_id(current)

    async def _deliver(self, session_id, user_id, content, delivery_id):
        if not self.enabled or not content:
            return
        if not session_id or not user_id:
            raise BridgeError("Proactive delivery requires the original session and user.")
        agent_id = self._delivery_agent_id()
        acceptance_key = agent_id + ":" + delivery_id
        async with self._send_lock:
            if acceptance_key in self._accepted:
                return
            result = await self._client.post(
                "/v1/deliver",
                {"delivery_id": delivery_id, "session_id": session_id, "user_id": user_id,
                 "agent_id": agent_id, "content": content},
            )
            # This cache means gateway acceptance, not confirmed platform receipt.
            if result.get("accepted") is True:
                self._accepted[acceptance_key] = None
                while len(self._accepted) > 2048:
                    self._accepted.popitem(last=False)
            else:
                raise BridgeError("The gateway did not accept this delivery or its result is uncertain; do not repeat the operation automatically.")

    async def send(self, to_handle, text, meta=None):
        await self.send_content_parts(to_handle, [{"type": "text", "text": text}], meta)

    async def send_content_parts(self, to_handle, parts, meta=None):
        meta = meta or {}
        session_id = meta.get("session_id") or to_handle
        user_id = meta.get("user_id")
        # Fixed-text cron jobs execute separately even when their text is identical.
        delivery_id = str(meta.get("delivery_id") or uuid4())
        await self._deliver(session_id, user_id, await outgoing_content(parts, self._settings, session_id), delivery_id)

    async def send_event(self, *, user_id, session_id, event, meta=None):
        # Configure cron dispatch.mode='final'. Completed assistant message only;
        # reasoning, function/MCP output, deltas, errors, and progress never go to IM.
        if not is_completed_assistant_message(event):
            return
        event_id = config_value(event, "id") or config_value(event, "msg_id")
        delivery_id = str(
            uuid5(NAMESPACE_URL, f"astrbot-bridge:{session_id}:{user_id}:{event_id}")
            if event_id else uuid4()
        )
        await self._deliver(session_id, user_id, await outgoing_content(config_value(event, "content", []), self._settings, session_id), delivery_id)
