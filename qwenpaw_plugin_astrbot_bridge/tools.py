"""Tools bind authorization to QwenPaw's trusted turn context."""

from __future__ import annotations

import os
import re
from typing import Any
from uuid import uuid4

from .bridge_client import BridgeClient, BridgeError, BridgeSettings, config_value
from .turn_context import get_turn_id

_plugin_config: dict[str, Any] = {}


def configure_tools(config: dict[str, Any] | None) -> None:
    global _plugin_config
    _plugin_config = dict(config or {})


def trusted_route() -> tuple[str, str]:
    from qwenpaw.app.agent_context import (
        get_current_channel,
        get_current_agent_id,
        get_current_root_session_id,
        get_current_session_id,
        get_current_user_id,
    )

    # No tool argument can supply a session/user/channel or select another user's route.
    if get_current_channel() != "astrbot":
        raise BridgeError("AstrBot tools are available only within an AstrBot conversation.")
    expected_agent_id = os.environ.get("QWENPAW_AGENT_ID", _plugin_config.get("agent_id", "default"))
    if get_current_agent_id() != expected_agent_id:
        raise BridgeError("This QwenPaw agent is not authorized for the AstrBot bridge.")
    session_id = get_current_root_session_id() or get_current_session_id()
    user_id = get_current_user_id()
    if not session_id or not user_id:
        raise BridgeError("This turn has no authenticated AstrBot conversation route.")
    if not isinstance(session_id, str) or not isinstance(user_id, str):
        raise BridgeError("This turn has an invalid AstrBot conversation route.")
    if not re.fullmatch(r"ab_[0-9a-f]{32}", session_id):
        raise BridgeError("This turn does not reference a registered AstrBot conversation.")
    return session_id, user_id


def tool_settings() -> BridgeSettings:
    from qwenpaw.app.agent_context import get_current_agent_id
    from qwenpaw.config.config import load_agent_config

    agent_config = load_agent_config(get_current_agent_id())
    channel_config = config_value(agent_config.channels, "astrbot", {})
    return BridgeSettings.from_config(channel_config, _plugin_config)


async def astrbot_list_tools() -> dict[str, Any]:
    """List the AstrBot plugin tools allowed for this conversation.

    Returns names, descriptions, and JSON argument schemas. Only an allowed
    conversation can list tools; the gateway enforces its configured allowlist.
    """
    session_id, user_id = trusted_route()
    turn_id = get_turn_id()
    return await BridgeClient(tool_settings()).post(
        "/v1/tools/list", {"session_id": session_id, "user_id": user_id, "turn_id": turn_id}
    )


async def astrbot_call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one allowed AstrBot plugin tool in the current conversation.

    Get its exact name and argument schema from astrbot_list_tools first.
    Routing and identity come from the runtime; do not supply session or user
    IDs. The gateway enforces authorization and tool permissions.
    """
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise BridgeError("tool_name must be a nonempty string.")
    if not isinstance(arguments, dict):
        raise BridgeError("arguments must be a JSON object.")
    session_id, user_id = trusted_route()
    turn_id = get_turn_id()
    return await BridgeClient(tool_settings()).post(
        "/v1/tools/call",
        {
            "call_id": str(uuid4()),
            "session_id": session_id,
            "user_id": user_id,
            "turn_id": turn_id,
            "tool_name": tool_name,
            "arguments": arguments,
        },
    )
