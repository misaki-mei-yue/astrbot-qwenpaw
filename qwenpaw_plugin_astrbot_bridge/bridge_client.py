"""Small internal HTTP client. Never use model input as routing authority."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class BridgeError(RuntimeError):
    """The bridge rejected or could not accept an operation."""


def config_value(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


@dataclass(frozen=True)
class BridgeSettings:
    base_url: str
    token: str
    files_dir: str = "/bridge-files"
    timeout: float = 30.0
    attempts: int = 3
    max_file_bytes: int = 20 * 1024 * 1024
    max_files: int = 4

    @classmethod
    def from_config(cls, *configs: Any) -> "BridgeSettings":
        values: dict[str, Any] = {}
        for config in reversed(configs):
            nested = config_value(config, "bridge", {})
            for name in ("callback_url", "callback_token", "files_dir", "max_file_bytes", "max_files"):
                value = config_value(config, name)
                if value is None:
                    value = config_value(nested, name)
                if value is not None:
                    values[name] = value
        url = os.environ.get(
            "ASTRBOT_BRIDGE_URL", values.get("callback_url", "http://astrbot:9186")
        ).rstrip("/")
        token = os.environ.get("BRIDGE_TOKEN", values.get("callback_token", ""))
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BridgeError("AstrBot bridge URL must be an HTTP(S) base URL.")
        if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
            raise BridgeError("Configure a nonempty BRIDGE_TOKEN before using the bridge.")
        limits = {}
        for field, variable, ceiling in (("max_file_bytes", "BRIDGE_MAX_FILE_BYTES", 20 * 1024 * 1024),
                                         ("max_files", "BRIDGE_MAX_FILES", 4)):
            value = os.environ.get(variable, values.get(field, ceiling))
            if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) <= ceiling:
                raise BridgeError("Configure positive media limits within 20MiB and 4 attachments.")
            limits[field] = int(value)
        return cls(
            base_url=url,
            token=token,
            files_dir=os.environ.get("BRIDGE_FILES_ROOT", os.environ.get("BRIDGE_FILES_DIR", values.get("files_dir", "/bridge-files"))),
            **limits,
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BridgeClient:
    """Retries use exactly the same call/delivery ID and serialized payload."""

    def __init__(self, settings: BridgeSettings):
        self.settings = settings

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path not in {"/v1/tools/list", "/v1/tools/call", "/v1/deliver"}:
            raise BridgeError("Unknown bridge operation.")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > 1_048_576:
            raise BridgeError("Bridge request exceeds its size limit.")
        return await asyncio.to_thread(self._post, path, data)

    def _post(self, path: str, data: bytes) -> dict[str, Any]:
        # A model/browser proxy must never receive private bridge credentials.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        for attempt in range(self.settings.attempts):
            request = Request(
                self.settings.base_url + path,
                data=data,
                headers={
                    "Authorization": "Bearer " + self.settings.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with opener.open(request, timeout=self.settings.timeout) as response:
                    raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise BridgeError("Bridge response exceeds its size limit.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise BridgeError("Bridge returned an invalid response.")
                return result
            except HTTPError as exc:
                # Never echo server response bodies, credentials, tool input, or URLs.
                if exc.code not in {429, 502, 503, 504} or attempt + 1 == self.settings.attempts:
                    raise BridgeError(f"AstrBot bridge rejected the operation (HTTP {exc.code}).") from None
            except (URLError, TimeoutError, ConnectionError, OSError):
                if attempt + 1 == self.settings.attempts:
                    raise BridgeError("AstrBot bridge is temporarily unreachable.") from None
            except (ValueError, UnicodeDecodeError):
                raise BridgeError("AstrBot bridge returned invalid JSON.") from None
            time.sleep(0.25 * (attempt + 1))
        raise BridgeError("AstrBot bridge is temporarily unreachable.")
