"""Tools bind authorization to QwenPaw's trusted turn context."""

from __future__ import annotations

import os
import re
import asyncio
from typing import Any
from uuid import uuid4

from .bridge_client import BridgeClient, BridgeError, BridgeSettings, config_value
from .turn_context import get_turn_id
from .media import media_descriptor, media_workspace

_plugin_config: dict[str, Any] = {}


def configure_tools(config: dict[str, Any] | None) -> None:
    global _plugin_config
    _plugin_config = dict(config or {})


def authorized_agent_id(agent_id: Any) -> str:
    """Validate an identity already obtained from the native runtime.

    This helper does not authenticate model arguments or channel metadata.
    The AstrBot gateway separately binds the returned Agent to its route.
    """
    expected = os.environ.get("QWENPAW_AGENT_ID", _plugin_config.get("agent_id", "default"))
    if not isinstance(agent_id, str) or not agent_id:
        raise BridgeError("This turn has no authenticated QwenPaw Agent identity.")
    if agent_id != expected and not re.fullmatch(r"ab[pg]_[0-9a-f]{32}", agent_id):
        raise BridgeError("This QwenPaw agent is not authorized for the AstrBot bridge.")
    return agent_id


def trusted_agent_id() -> str:
    # get_current_agent_id falls back to the configured active Agent when its
    # ContextVar is absent. The official peek API deliberately does not.
    from qwenpaw.app.agent_context import peek_current_agent_id

    return authorized_agent_id(peek_current_agent_id())


def trusted_route() -> tuple[str, str]:
    from qwenpaw.app.agent_context import (
        get_current_channel,
        get_current_root_session_id,
        get_current_session_id,
        get_current_user_id,
    )

    # No tool argument can supply a session/user/channel or select another user's route.
    if get_current_channel() != "astrbot":
        raise BridgeError("AstrBot tools are available only within an AstrBot conversation.")
    trusted_agent_id()
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
    from qwenpaw.config.config import load_agent_config

    agent_config = load_agent_config(trusted_agent_id())
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
        "/v1/tools/list", {"session_id": session_id, "user_id": user_id, "turn_id": turn_id,
                           "agent_id": trusted_agent_id()}
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
            "agent_id": trusted_agent_id(),
            "tool_name": tool_name,
            "arguments": arguments,
        },
    )


async def astrbot_media_workspace() -> dict[str, Any]:
    """Get this conversation's shared inbound/outbound directories and limits.

    Generate files in outbound before sending. Never use another conversation's
    directory. The local paths are for file tools, not public download links.
    """
    session_id, _ = trusted_route()
    settings = tool_settings()
    return await asyncio.to_thread(media_workspace, settings.files_dir, session_id, settings.max_file_bytes, settings.max_files)


async def astrbot_send_file(path: str, kind: str = "file") -> dict[str, Any]:
    """Send one existing file from this conversation's shared outbound directory.

    path must be a local outbound file returned by astrbot_media_workspace,
    or outbound/filename. kind is file, image, video or audio. No arbitrary
    URL, data URL, other workspace or other user's file can be sent. This tool
    also works in proactive jobs: identity comes from the runtime, not arguments.
    Gateway acceptance means submitted to the adapter, not a platform receipt.
    """
    session_id, user_id = trusted_route()
    agent_id = trusted_agent_id()
    settings = tool_settings()
    content = await asyncio.to_thread(media_descriptor, settings.files_dir, session_id, path, kind, max_file_bytes=settings.max_file_bytes)
    # Created once per tool invocation; BridgeClient serializes once and keeps
    # exactly this ID and body during HTTP retries.
    result = await BridgeClient(settings).post("/v1/deliver", {
        "delivery_id": str(uuid4()), "session_id": session_id, "user_id": user_id,
        "agent_id": agent_id, "content": [content],
    })
    if result.get("accepted") is not True:
        raise BridgeError("The gateway did not accept this delivery or its result is uncertain; do not repeat the operation automatically.")
    return result
