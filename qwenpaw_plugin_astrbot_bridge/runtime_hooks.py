"""Read authority only from QwenPaw's authenticated native request context."""

from copy import deepcopy

from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime.hooks import HookResult
from qwenpaw.runtime.phases import Phase

from .bridge_client import BridgeError
from .turn_context import clear_turn_id, get_turn_id, set_turn_id


class BridgeTurnContextHook(LifecycleHook):
    phase = Phase.PRE_DISPATCH
    name = "astrbot_bridge_turn_context"
    priority = 11
    after = ("contextvars_setup",)

    async def run(self, ctx):
        clear_turn_id()
        request = ctx.request
        request_context = getattr(request, "request_context", None)
        if getattr(request, "channel", None) == "astrbot" and isinstance(request_context, dict):
            value = request_context.get("astrbot_bridge_turn_id")
            if "astrbot_bridge_turn_id" not in request_context and request_context.get("_spawn_subagent") is True:
                meta = request_context.get("channel_meta")
                if isinstance(meta, dict):
                    value = meta.get("astrbot_bridge_turn_id")
            set_turn_id(value)
            try:
                turn_id = get_turn_id()
            except BridgeError:
                return HookResult()
            # Official subagent requests copy approval_route.channel_meta.
            # Keep the nonce in that trusted ephemeral route, never in memory.
            from qwenpaw.app.agent_context import get_current_approval_route, set_current_approval_route

            route = deepcopy(get_current_approval_route() or {})
            meta = route.get("channel_meta")
            if not isinstance(meta, dict):
                meta = {}
                route["channel_meta"] = meta
            meta["astrbot_bridge_turn_id"] = turn_id
            set_current_approval_route(route)
        return HookResult()


class BridgeTurnCleanupHook(LifecycleHook):
    phase = Phase.FINALLY
    name = "astrbot_bridge_turn_cleanup"
    priority = 90

    async def run(self, ctx):
        clear_turn_id()
        return HookResult()
