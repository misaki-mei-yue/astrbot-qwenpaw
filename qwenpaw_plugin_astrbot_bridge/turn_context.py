"""Per-request authority, copied into tool tasks without model arguments."""

from __future__ import annotations

import re
from contextvars import ContextVar

from .bridge_client import BridgeError

_turn_id: ContextVar[str | None] = ContextVar("astrbot_bridge_turn_id", default=None)


def clear_turn_id() -> None:
    _turn_id.set(None)


def set_turn_id(value: object) -> None:
    clear_turn_id()
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
        _turn_id.set(value)


def get_turn_id() -> str:
    value = _turn_id.get()
    if not value:
        raise BridgeError("AstrBot plugin tools require the original active chat turn.")
    return value
