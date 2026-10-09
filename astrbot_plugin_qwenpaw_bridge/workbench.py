"""Bounded management operations behind AstrBot's dashboard authentication.

The browser never receives the QwenPaw token or chooses an upstream URL.
Reading this page performs no model calls, file writes, or chat deliveries.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import aiohttp


class WorkbenchError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else ""


def _object(value):
    return value if isinstance(value, dict) else {}


def _memory_name(value):
    if not isinstance(value, str) or len(value) > 240 or not value.endswith(".md"):
        raise WorkbenchError("请选择列表中的记忆文件。")
    if any(part in ("", ".", "..") or part.startswith(".") for part in value.split("/")):
        raise WorkbenchError("记忆文件路径无效。")
    if any(c in value for c in "\\:%?#") or any(ord(c) < 32 for c in value):
        raise WorkbenchError("记忆文件路径无效。")
    return value


class Workbench:
    prefix = "astrbot_plugin_qwenpaw_bridge/workbench/"

    def __init__(self, bridge):
        self.bridge = bridge
        self.registrations = []
        self.create_lock = asyncio.Lock()

    def register(self):
        operations = {
            "status": ("GET", self.status),
            "tasks": ("GET", self.tasks),
            "memory": ("GET", self.memory),
            "memory/read": ("POST", self.read_memory),
            "tools": ("GET", self.tools),
            "tasks/create": ("POST", self.create_task),
            "tasks/control": ("POST", self.control_task),
        }
        for name, (method, operation) in operations.items():
            handler = self._handler(operation, method)
            route = self.prefix + name
            self.bridge.context.register_web_api(route, handler, [method], "组合工作台")
            self.registrations.append((route, handler))

    def unregister(self):
        registered = getattr(self.bridge.context, "registered_web_apis", None)
        if isinstance(registered, list):
            handlers = {handler for _, handler in self.registrations}
            registered[:] = [item for item in registered if item[1] not in handlers]
        self.registrations.clear()

    def _handler(self, operation, method):
        async def handler():
            from quart import g, jsonify, request

            try:
                # Defense in depth: only AstrBot's authenticated dashboard path.
                if not getattr(g, "username", None):
                    raise WorkbenchError("请先登录 AstrBot 后台。", 401)
                payload = {}
                if method == "POST":
                    if request.mimetype != "application/json":
                        raise WorkbenchError("请求格式必须为 JSON。", 415)
                    origin = request.headers.get("Origin")
                    if (request.headers.get("Sec-Fetch-Site") == "cross-site" or
                        (origin and (urlsplit(origin).scheme not in ("http", "https") or
                                     urlsplit(origin).netloc != request.host))):
                        raise WorkbenchError("请从 AstrBot 后台打开工作台。", 403)
                    if request.content_length and request.content_length > 32768:
                        raise WorkbenchError("请求内容过长。", 413)
                    raw = await request.get_data()
                    if len(raw) > 32768:
                        raise WorkbenchError("请求内容过长。", 413)
                    try:
                        payload = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise WorkbenchError("请求格式无效。") from None
                    if not isinstance(payload, dict):
                        raise WorkbenchError("请求格式无效。")
                result = await operation(payload)
                response = jsonify({"status": "ok", "data": result})
            except WorkbenchError as exc:
                response = jsonify({"status": "error", "message": str(exc)})
                response.status_code = exc.status
            except Exception:
                response = jsonify({"status": "error", "message": "操作未完成，请刷新检查状态。"})
                response.status_code = 500
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler

    def _ready(self):
        if not self.bridge.enabled or not self.bridge._client or not self.bridge._store:
            raise WorkbenchError("桥接尚未启动，请检查插件配置。", 503)

    def _agent_path(self, suffix):
        self._ready()
        return "/api/agents/" + quote(self.bridge._client.agent_id, safe="") + suffix

    async def _api(self, method, path, payload=None):
        self._ready()
        client = self.bridge._client
        try:
            session = await client._session()
            async with session.request(
                method, client.base_url + path, json=payload, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=12, connect=4),
            ) as response:
                if response.status not in (200, 201):
                    messages = {401: "智能体鉴权失败，请检查运行时密钥。",
                                403: "智能体拒绝了请求。", 404: "内容已不存在，请刷新列表。",
                                422: "任务配置未被接受，请检查时间和任务内容。",
                                503: "智能体正在启动，请稍后刷新。"}
                    raise WorkbenchError(messages.get(response.status, "智能体暂时无法完成请求。"),
                                         response.status if response.status in messages else 502)
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise WorkbenchError("内容过大，请在 QwenPaw 原生后台查看。", 413)
                return json.loads(data)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # No retries of mutations: a timeout may follow a completed operation.
            raise WorkbenchError("连接智能体失败；如刚才在创建任务，请先刷新列表确认。", 502) from None

    def _routes(self):
        self._ready()
        return {row["session_id"]: row for row in self.bridge._store.list_sessions()
                if row["user_id"] in self.bridge.owners and row["platform"] in self.bridge.platforms}

    async def status(self, _=None):
        self._ready()
        paths = {"health": "/api/healthz", "version": "/api/version",
                 "model": "/api/models/active?scope=effective&agent_id=" + quote(self.bridge._client.agent_id, safe=""),
                 "memory": self._agent_path("/memory/runtime-status")}
        results = await asyncio.gather(*(self._api("GET", path) for path in paths.values()), return_exceptions=True)
        upstream = dict(zip(paths, results))
        failures = {key: str(value) if isinstance(value, WorkbenchError) else "状态暂不可用。"
                    for key, value in upstream.items() if isinstance(value, Exception)}
        health = _object(upstream["health"])
        loaded = health.get("agents_loaded")
        ready = (health.get("status") == "ok" and isinstance(loaded, list)
                 and self.bridge._client.agent_id in loaded)
        if not ready and "health" not in failures:
            failures["health"] = "目标智能体尚未就绪，请稍后刷新。"
        model = _object(_object(upstream["model"]).get("active_llm"))
        memory = _object(upstream["memory"])
        worker = _object(memory.get("worker"))
        auto = _object(memory.get("auto_memory"))
        routes = list(self._routes().values())
        version = upstream["version"]
        return {
            "bridge_ready": True, "qwenpaw_ready": ready,
            "version": _text(version if isinstance(version, str) else _object(version).get("version"), 40),
            "model": {"configured": bool(model.get("provider_id") and model.get("model")),
                      "name": _text(model.get("model"), 120)},
            "memory": {"status": _text(worker.get("status"), 30),
                       "auto_enabled": auto.get("enabled") is True,
                       "reindexing": memory.get("reindexing") is True,
                       "has_error": bool(_object(memory.get("recent")).get("last_error"))},
            "owner_count": len(self.bridge.owners), "active_turns": len(self.bridge._active),
            "tool_allowlist_count": len(self.bridge.tool_allowlist),
            "all_tools_allowed": "*" in self.bridge.tool_allowlist,
            "routes": [{"session_id": row["session_id"], "user_id": row["user_id"],
                        "platform": row["platform"], "origin": row["origin"]} for row in routes],
            "failures": failures,
        }

    def _allowed_job(self, job):
        if not isinstance(job, dict):
            return False
        dispatch = _object(job.get("dispatch"))
        target = _object(dispatch.get("target"))
        route = self._routes().get(target.get("session_id"))
        return bool(dispatch.get("channel") == "astrbot" and route and
                    target.get("user_id") == route["user_id"])

    @staticmethod
    def _job_view(job):
        schedule = _object(job.get("schedule"))
        target = _object(_object(job.get("dispatch")).get("target"))
        return {"id": _text(job.get("id")), "name": _text(job.get("name")),
                "enabled": job.get("enabled") is True, "kind": _text(job.get("task_type"), 20),
                "schedule": {key: _text(schedule.get(key), 100) for key in ("type", "cron", "run_at", "timezone")},
                "session_id": _text(target.get("session_id"))}

    async def tasks(self, _=None):
        body = await self._api("GET", self._agent_path("/cron/jobs"))
        if not isinstance(body, list):
            raise WorkbenchError("智能体返回了无法识别的任务列表。", 502)
        return {"items": [self._job_view(job) for job in body if self._allowed_job(job)]}

    def _task_spec(self, body):
        route = self._routes().get(body.get("session_id"))
        if not route:
            raise WorkbenchError("请先从获准的微信或 QQ 会话与机器人聊天，再选择接收会话。", 403)
        name, prompt = body.get("name"), body.get("prompt")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise WorkbenchError("任务名称需为 1–80 字。")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000:
            raise WorkbenchError("任务内容需为 1–8000 字。")
        kind = body.get("kind")
        if kind not in ("text", "agent"):
            raise WorkbenchError("请选择提醒或智能任务。")
        frequency = body.get("frequency")
        if frequency == "once":
            try:
                when = datetime.fromisoformat(str(body.get("run_at", "")).replace("Z", "+00:00"))
                if when.tzinfo is None or when <= datetime.now(timezone.utc):
                    raise ValueError()
            except ValueError:
                raise WorkbenchError("请选择带时区的未来时间。") from None
            schedule = {"type": "once", "run_at": when.isoformat(), "timezone": "Asia/Shanghai"}
        elif frequency in ("daily", "weekdays"):
            match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(body.get("time", "")))
            if not match:
                raise WorkbenchError("请选择有效时间。")
            hour, minute = match.groups()
            schedule = {"type": "cron", "cron": f"{int(minute)} {int(hour)} * * " + ("mon-fri" if frequency == "weekdays" else "*"),
                        "timezone": "Asia/Shanghai"}
        else:
            raise WorkbenchError("请选择单次、每天或工作日。")
        spec = {"name": name.strip(), "enabled": True, "task_type": kind, "schedule": schedule,
                "dispatch": {"type": "channel", "channel": "astrbot", "mode": "final",
                             "target": {key: route[key] for key in ("session_id", "user_id")}},
                "runtime": {"share_session": True, "tool_safety": True, "timeout_seconds": 900,
                            "max_concurrency": 1}, "meta": {"created_by": "astrbot_workbench"}}
        if kind == "text":
            spec["text"] = prompt.strip()
        else:
            spec["request"] = {"input": [{"role": "user", "content": [{"type": "text", "text": prompt.strip()}]}]}
        return spec

    async def create_task(self, body):
        spec = self._task_spec(body)
        key = body.get("request_id")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            raise WorkbenchError("创建请求已失效，请刷新页面。")
        async with self.create_lock:
            store = self.bridge._store
            existing = store.receipt("workbench_create", key)
            if existing:
                if existing["state"] == "done":
                    return existing["result"]
                raise WorkbenchError("该创建请求可能已到达智能体，请先刷新任务列表确认。", 409)
            store.claim("workbench_create", key)
            try:
                job = await self._api("POST", self._agent_path("/cron/jobs"), spec)
            except WorkbenchError as exc:
                if exc.status in (401, 403, 404, 422):
                    store.fail("workbench_create", key)
                raise
            if not self._allowed_job(job):
                raise WorkbenchError("任务响应异常，请刷新列表确认是否已创建。", 502)
            result = self._job_view(job)
            store.finish("workbench_create", key, result)
            return result

    async def control_task(self, body):
        job_id, action = body.get("id"), body.get("action")
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise WorkbenchError("任务标识无效。")
        if action not in ("pause", "resume"):
            raise WorkbenchError("不支持该操作。")
        path = self._agent_path("/cron/jobs/" + quote(job_id, safe=""))
        job = _object(await self._api("GET", path)).get("spec")
        if not self._allowed_job(job):
            raise WorkbenchError("只能管理获准会话的桥接任务。", 403)
        return await self._api("POST", path + "/" + action)

    async def memory(self, _=None):
        groups = await asyncio.gather(*(self._api("GET", self._agent_path("/workspace/memory?section=" + section))
                                        for section in ("daily", "digest")))
        items = []
        for section, rows in zip(("daily", "digest"), groups):
            if not isinstance(rows, list):
                raise WorkbenchError("记忆目录返回格式异常。", 502)
            for row in rows[:500]:
                if not isinstance(row, dict):
                    continue
                try:
                    name = _memory_name(row.get("filename"))
                except WorkbenchError:
                    continue
                items.append({"name": name, "section": section,
                              "modified": _text(row.get("modified_time"), 60),
                              "size": row.get("size") if isinstance(row.get("size"), int) else 0})
        return {"items": items}

    async def read_memory(self, body):
        section, name = body.get("section"), _memory_name(body.get("name"))
        if section not in ("daily", "digest"):
            raise WorkbenchError("记忆类型无效。")
        # Membership avoids turning a management page into an arbitrary file API.
        listed = await self.memory()
        if not any(item["name"] == name and item["section"] == section for item in listed["items"]):
            raise WorkbenchError("该记忆已不存在，请刷新列表。", 404)
        body = await self._api("GET", self._agent_path("/workspace/memory/" + quote(name, safe="/") + "?section=" + section))
        content = _object(body).get("content")
        if not isinstance(content, str):
            raise WorkbenchError("记忆内容格式异常。", 502)
        return {"content": content, "name": name, "section": section}

    async def tools(self, _=None):
        body = await self._api("GET", self._agent_path("/tools"))
        if not isinstance(body, list):
            raise WorkbenchError("工具列表格式异常。", 502)
        return {"items": [{"name": _text(row.get("name"), 120), "enabled": row.get("enabled") is True,
                           "description": _text(row.get("description"), 400)}
                          for row in body[:500] if isinstance(row, dict)]}
