"""Bounded encrypted settings messages shared by the portal and host helper."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_SETTINGS = 65536
REMOTE_PHASES = {"webdav-save": "remote_config", "webdav-test": "remote_testing",
                 "webdav-run": "remote_backup", "webdav-retry": "remote_retry"}
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")


def _text(value, limit, *, blank=True):
    return (isinstance(value, str) and (blank or bool(value))
            and len(value.encode("utf-8")) <= limit and not any(ord(c) < 32 for c in value))


def valid_settings(value):
    """Check structure at both sides of IPC; the host also checks saved state."""
    try:
        if not isinstance(value, dict) or set(value) != {"enabled", "time", "utc_offset_minutes", "keep_last", "password", "destinations"}:
            return False
        if (type(value["enabled"]) is not bool or not isinstance(value["time"], str)
                or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value["time"])
                or type(value["utc_offset_minutes"]) is not int or not -720 <= value["utc_offset_minutes"] <= 840
                or type(value["keep_last"]) is not int or not 1 <= value["keep_last"] <= 100
                or not _text(value["password"], 1024)
                or value["password"] and len(value["password"]) < 12
                or not isinstance(value["destinations"], list) or len(value["destinations"]) > 10):
            return False
        seen = set()
        for target in value["destinations"]:
            if not isinstance(target, dict) or set(target) != {"id", "name", "url", "username", "password", "enabled"}:
                return False
            identifier = target["id"]
            if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in seen
                    or not _text(target["name"], 120, blank=False)
                    or not _text(target["url"], 2048, blank=False)
                    or not _text(target["username"], 512, blank=False) or ":" in target["username"]
                    or not _text(target["password"], 2048) or type(target["enabled"]) is not bool):
                return False
            seen.add(identifier)
        return True
    except (UnicodeError, TypeError, ValueError):
        return False


def _key(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 4096:
        raise ValueError("Invalid settings envelope.")
    return hashlib.sha256(b"astrbot-qwenpaw:webdav-ipc:v1\x00" + token.encode()).digest()


def seal_settings(value, token, context):
    if not valid_settings(value):
        raise ValueError("Invalid WebDAV settings.")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_SETTINGS:
        raise ValueError("Invalid WebDAV settings.")
    nonce = os.urandom(12)
    return base64.urlsafe_b64encode(nonce + AESGCM(_key(token)).encrypt(nonce, raw, context.encode())).decode("ascii")


def open_settings(value, token, context):
    try:
        if not isinstance(value, str) or len(value) > 90000:
            raise ValueError()
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        if not 28 <= len(raw) <= MAX_SETTINGS + 28:
            raise ValueError()
        config = json.loads(AESGCM(_key(token)).decrypt(raw[:12], raw[12:], context.encode()))
        if not valid_settings(config):
            raise ValueError()
        return config
    except (ValueError, TypeError, UnicodeError, InvalidTag):
        raise ValueError("Invalid settings envelope.") from None
