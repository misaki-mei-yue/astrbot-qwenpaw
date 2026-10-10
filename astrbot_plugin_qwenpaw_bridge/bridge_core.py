"""Persistent routes and the QwenPaw 2.2.1 console streaming contract.

This module deliberately imports neither AstrBot nor QwenPaw. Network retries
attach to an existing run; they never submit the user's task a second time.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
import re
from uuid import uuid4
from pathlib import Path
from typing import AsyncIterable
from urllib.parse import quote, urlsplit

import aiohttp

from .identity import IdentityStore


class BridgeError(RuntimeError):
    """An actionable failure safe to report without an upstream response body."""


class BridgeStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS routes (
                session_id TEXT PRIMARY KEY, origin TEXT NOT NULL,
                user_id TEXT NOT NULL, platform TEXT NOT NULL,
                UNIQUE(origin, user_id)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                category TEXT NOT NULL, key TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, updated REAL NOT NULL,
                PRIMARY KEY(category, key)
            );
        """)
        self.db.commit()
        # Existing shared-agent routes are retained but are not implicitly
        # authorized for the new personal workspaces.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(routes)")}
        for name, definition in {
            "agent_id": "TEXT NOT NULL DEFAULT ''",
            "person_id": "TEXT NOT NULL DEFAULT ''",
            "scope": "TEXT NOT NULL DEFAULT ''",
            "platform_id": "TEXT NOT NULL DEFAULT ''",
            "active": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE routes ADD COLUMN {name} {definition}")
        self.db.execute("CREATE TABLE IF NOT EXISTS retired_routes (session_id TEXT PRIMARY KEY, route_json TEXT NOT NULL)")
        self.db.commit()
        self.identities = IdentityStore(self.db)

    def get_or_create_session(self, origin: str, user_id: str, platform: str) -> dict:
        if not all(isinstance(x, str) and x for x in (origin, user_id, platform)):
            raise ValueError("Route fields must be nonempty strings")
        row = self.db.execute(
            "SELECT * FROM routes WHERE origin=? AND user_id=?", (origin, user_id)
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO routes (session_id,origin,user_id,platform,active) VALUES (?,?,?,?,1)",
                ("ab_" + secrets.token_hex(16), origin, user_id, platform),
            )
            self.db.commit()
            row = self.db.execute(
                "SELECT * FROM routes WHERE origin=? AND user_id=?", (origin, user_id)
            ).fetchone()
        return dict(row)

    def personal_session(self, origin: str, user_id: str, platform: str,
                         platform_id: str, is_private: bool, *, new_session: bool = False) -> dict:
        identity = self.identities.resolve(platform, platform_id, user_id, origin, is_private)
        old = self.db.execute("SELECT * FROM routes WHERE origin=? AND user_id=?", (origin, user_id)).fetchone()
        keys = ("agent_id", "person_id", "scope") if is_private else ("agent_id", "scope")
        if not new_session and old and old["active"] and all(old[key] == identity[key] for key in keys) and old["platform_id"] == platform_id and old["platform"] == platform:
            # Linking private accounts does not move a group's memory or its
            # scheduled recipient. Only the informational person key changes.
            if old["person_id"] != identity["person_id"]:
                with self.db:
                    self.db.execute("UPDATE routes SET person_id=? WHERE session_id=?", (identity["person_id"], old["session_id"]))
                return self.get_session(old["session_id"])
            return dict(old)
        sid = "ab_" + secrets.token_hex(16)
        with self.db:
            if old:
                self.db.execute("INSERT OR IGNORE INTO retired_routes VALUES (?,?)", (old["session_id"], json.dumps(dict(old))))
                self.db.execute("DELETE FROM routes WHERE session_id=?", (old["session_id"],))
            self.db.execute(
                "INSERT INTO routes (session_id,origin,user_id,platform,agent_id,person_id,scope,platform_id,active) VALUES (?,?,?,?,?,?,?,?,1)",
                (sid, origin, user_id, platform, identity["agent_id"], identity["person_id"], identity["scope"], platform_id),
            )
        return self.get_session(sid)

    def redeem_link(self, platform, platform_id, user_id, origin, is_private, code):
        """Commit binding and immediate retirement of old private routes together."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            identity = self.identities.redeem_link(platform, platform_id, user_id, origin, is_private, code)
            old_routes = self.db.execute(
                "SELECT * FROM routes WHERE platform=? AND platform_id=? AND user_id=? AND scope='private' AND agent_id<>?",
                (platform, platform_id, user_id, identity["agent_id"]),
            ).fetchall()
            for old in old_routes:
                self.db.execute("INSERT OR IGNORE INTO retired_routes VALUES (?,?)", (old["session_id"], json.dumps(dict(old))))
                self.db.execute("DELETE FROM routes WHERE session_id=?", (old["session_id"],))
        return identity

    def get_session(self, session_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM routes WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def claim(self, category: str, key: str) -> bool:
        if not category or not key or len(key) > 512:
            raise ValueError("Invalid idempotency key")
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO receipts VALUES (?,?,'running',NULL,?)",
            (category, key, time.time()),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def receipt(self, category: str, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT state,result FROM receipts WHERE category=? AND key=?",
            (category, key),
        ).fetchone()
        if row is None:
            return None
        return {"state": row["state"], "result": json.loads(row["result"]) if row["result"] else None}

    def finish(self, category: str, key: str, result: dict | None = None):
        self.db.execute(
            "UPDATE receipts SET state='done',result=?,updated=? WHERE category=? AND key=?",
            (json.dumps(result, ensure_ascii=False) if result is not None else None,
             time.time(), category, key),
        )
        self.db.commit()

    def fail(self, category: str, key: str):
        """Release only a claim known to have produced no external side effect."""
        self.db.execute(
            "DELETE FROM receipts WHERE category=? AND key=? AND state='running'",
            (category, key),
        )
        self.db.commit()

    def close(self):
        self.db.close()


async def decode_sse(chunks: AsyncIterable[bytes], max_event_bytes=1024 * 1024):
    """Parse SSE at byte boundaries, including split UTF-8 and CRLF frames."""
    pending = bytearray()
    fields: list[bytes] = []
    size = 0
    async for chunk in chunks:
        pending.extend(chunk)
        if len(pending) + size > max_event_bytes:
            raise BridgeError("QwenPaw stream event is too large")
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            line = line.rstrip(b"\r")
            if not line:
                if fields:
                    try:
                        raw = b"\n".join(fields).decode("utf-8")
                        if raw != "[DONE]":
                            event = json.loads(raw)
                            if isinstance(event, dict):
                                yield event
                    except (ValueError, UnicodeError) as exc:
                        raise BridgeError("Invalid QwenPaw stream event") from exc
                fields, size = [], 0
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                fields.append(value)
                size += len(value)
    # A partial event is intentionally not accepted as a completed answer.
    if pending or fields:
        raise BridgeError("QwenPaw stream ended inside an event")


class AssistantCollector:
    """Emit final assistant messages once; never leak reasoning or tool events."""
    def __init__(self):
        self.messages: dict[str, list[dict]] = {}
        self.tool_media: dict[str, dict] = {}
        self.completed = False

    def _sent_files(self, message: dict):
        """QwenPaw 2.2.1 send_file_to_user emits a tool DataContent envelope.

        Only that explicit send tool may add attachments; other tool output,
        including screenshots used internally, stays private. Paths are still
        untrusted here and MUST pass the adapter's per-session file boundary.
        """
        if (message.get("role") != "tool" or message.get("status") != "completed"
                or message.get("type") not in ("plugin_call_output", "function_call_output")):
            return
        for part in message.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "data":
                continue
            data = part.get("data")
            if not isinstance(data, dict) or data.get("name") != "send_file_to_user":
                continue
            output = data.get("output")
            if not isinstance(output, str) or len(output) > 1024 * 1024:
                continue
            try:
                blocks = json.loads(output)
            except (ValueError, TypeError):
                continue
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if not isinstance(kind, str):
                    continue
                source = block.get("source")
                if not isinstance(source, dict) or source.get("type") != "url":
                    continue
                # The fixed upstream envelope retains ordinary documents as
                # DataBlock(type='data'); its channel renderer calls them file.
                if kind == "data":
                    mime = source.get("media_type")
                    major = mime.split("/", 1)[0] if isinstance(mime, str) else ""
                    kind = major if major in ("image", "video", "audio") else "file"
                key = {"image": "image_url", "video": "video_url", "audio": "data", "file": "file_url"}.get(kind)
                if not key:
                    continue
                url = source.get("url")
                if not isinstance(url, str) or not url or len(url) > 4096:
                    continue
                content = {"type": kind, key: url}
                identity = json.dumps(content, sort_keys=True)
                self.tool_media[identity] = content
                if len(self.tool_media) > 4:
                    raise BridgeError("QwenPaw answer has more than four attachments")

    def _message(self, message: dict):
        if message.get("role") != "assistant" or message.get("type", "message") != "message":
            return
        if message.get("status", "completed") != "completed":
            return
        blocks = []
        for item in message.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("text", "output_text") and isinstance(item.get("text"), str):
                blocks.append({"type": "text", "text": item["text"]})
            elif item.get("type") in ("image", "file", "video", "audio"):
                blocks.append(dict(item))
        if not blocks:
            return
        # Anonymous stream/output copies are deduplicated by their content.
        key = str(message.get("id") or json.dumps(blocks, sort_keys=True, ensure_ascii=False))
        self.messages[key] = blocks
        if sum(len(json.dumps(v, ensure_ascii=False)) for v in self.messages.values()) > 1024 * 1024:
            raise BridgeError("QwenPaw answer is too large")

    def feed(self, event: dict):
        if event.get("error"):
            raise BridgeError("QwenPaw reported an execution error; inspect its console")
        kind, status = event.get("object"), event.get("status")
        if status in ("failed", "cancelled", "canceled", "incomplete") and kind == "response":
            raise BridgeError("QwenPaw task did not complete; inspect its console")
        if kind == "message" and status == "completed":
            self._sent_files(event)
            self._message(event)
        elif kind == "response" and status == "completed":
            # The response snapshot is authoritative: do not concatenate its
            # messages with already seen stream copies (IDs may be absent).
            output = event.get("output")
            if isinstance(output, list) and output:
                previous = self.messages
                self.messages = {}
                for message in output:
                    if isinstance(message, dict):
                        self._sent_files(message)
                        self._message(message)
                if not self.messages:
                    self.messages = previous
            self.completed = True

    def result(self) -> list[dict]:
        if not self.completed:
            raise BridgeError("QwenPaw task is still running or its final response was lost; no task was resubmitted")
        result = [block for blocks in self.messages.values() for block in blocks]
        seen_media = set()
        for block in result:
            url = block.get("image_url") or block.get("video_url") or block.get("file_url") or block.get("data")
            if isinstance(url, str):
                seen_media.add((block.get("type"), url))
        for block in self.tool_media.values():
            key = (block["type"], next(value for name, value in block.items() if name != "type"))
            if key not in seen_media:
                result.append(block)
                seen_media.add(key)
        return result


class QwenPawClient:
    def __init__(self, base_url: str, agent_id: str, runtime_token: str, timeout=900):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("QwenPaw base URL must be HTTP(S) without embedded credentials")
        if not runtime_token or len(runtime_token) < 32:
            raise ValueError("QwenPaw runtime token must have at least 32 characters")
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._runtime_token = runtime_token
        self.timeout = float(timeout)
        self._http: aiohttp.ClientSession | None = None

    async def _session(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                headers={"X-QwenPaw-Runtime-Token": self._runtime_token},
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=15),
            )
        return self._http

    # Keep the secret out of the object repr and exception messages.
    @property
    def _runtime_token(self):
        return self.__token

    @_runtime_token.setter
    def _runtime_token(self, value):
        self.__token = value

    async def chat(self, session_id: str, user_id: str, text: str, content=None, turn_id=None, *, agent_id=None) -> list[dict]:
        turn_id = turn_id or uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", turn_id):
            raise ValueError("Invalid bridge turn ID")
        request_context = {
            "astrbot_bridge_turn_id": turn_id,
            "channel_meta": {"astrbot_bridge_turn_id": turn_id},
        }
        payload = {
            "input": [{"role": "user", "content": content or [{"type": "text", "text": text}]}],
            "session_id": session_id, "user_id": user_id, "channel": "astrbot",
            "request_context": request_context,
        }
        http = await self._session()
        selected_agent = agent_id or self.agent_id
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", selected_agent):
            raise ValueError("Invalid QwenPaw agent ID")
        url = self.base_url + "/api/agents/" + quote(selected_agent, safe="") + "/console/chat"
        collector = AssistantCollector()
        for attempt in range(2):
            request_payload = payload if attempt == 0 else {
                "input": [], "session_id": session_id, "user_id": user_id,
                "channel": "astrbot", "reconnect": True,
                "request_context": request_context,
            }
            try:
                async with http.post(url, json=request_payload, allow_redirects=False) as response:
                    if response.status == 409:
                        raise BridgeError("This QwenPaw session already has a running task; inspect its console before retrying")
                    if response.status in (401, 403):
                        raise BridgeError("QwenPaw authentication failed; check the runtime token")
                    if response.status != 200:
                        raise BridgeError(f"QwenPaw returned HTTP {response.status}; inspect its console")
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise BridgeError("QwenPaw did not return its expected event stream")
                    async for event in decode_sse(response.content.iter_any()):
                        collector.feed(event)
                if collector.completed:
                    return collector.result()
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError):
                if attempt:
                    raise BridgeError("Connection to QwenPaw was lost; the task may still be running and was not resubmitted") from None
            # No retry of user input: the second request only attaches to an
            # existing background run, whether the first POST reached it or not.
        return collector.result()

    async def _json(self, method: str, path: str, payload=None):
        http = await self._session()
        try:
            async with http.request(method, self.base_url + path, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response:
                if response.status != 200:
                    raise BridgeError(f"QwenPaw API returned HTTP {response.status}")
                body = await response.json()
                if not isinstance(body, dict):
                    raise BridgeError("Invalid QwenPaw API response")
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BridgeError("QwenPaw API is unavailable") from None

    async def pending_approvals(self) -> list[dict]:
        body = await self._json("GET", "/api/approval/list")
        result = []
        for approval in body.get("pending_approvals") or []:
            if not isinstance(approval, dict):
                continue
            # Do not forward chain-of-thought. Display just the operation and
            # its target; the AstrBot adapter validates the saved route owner.
            result.append({
                **{k: approval[k] for k in ("request_id", "owner_agent_id", "tool_name", "severity", "timeout_seconds") if k in approval},
                "session_id": approval.get("root_session_id") or approval.get("session_id", ""),
                "title": str(approval.get("tool_display_name") or approval.get("tool_name") or "Tool operation"),
                "target": str(approval.get("exact_target") or ""),
            })
        return result

    async def approval(self, request_id: str, session_id: str, user_id: str, approve: bool):
        return await self._json("POST", "/api/approval/" + ("approve" if approve else "deny"), {
            "request_id": request_id, "session_id": session_id,
            "user_id": user_id, "scope": "exact",
        })

    async def close(self):
        if self._http is not None:
            await self._http.close()
