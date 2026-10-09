"""Real Quart wrappers and loopback HTTP; no model, account or server startup."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from quart import Quart, g, request

from astrbot_plugin_qwenpaw_bridge.bridge_core import BridgeStore, QwenPawClient
from astrbot_plugin_qwenpaw_bridge.workbench import Workbench, WorkbenchError


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BridgeStore(Path(self.temp.name) / "routes.sqlite3")
        self.route = self.store.get_or_create_session("qq:FriendMessage:owner", "owner", "aiocqhttp")
        self.foreign_route = self.store.get_or_create_session("qq:FriendMessage:other", "other", "aiocqhttp")
        self.disabled_route = self.store.get_or_create_session("disabled:owner", "owner", "disabled")
        self.requests = []
        self.jobs = {}
        self.created = []
        self.create_status = 201
        self.health = {"status": "ok", "agents_loaded": ["default"]}
        self.memory_rows = {"daily": [{"filename": "2026-10-09.md", "size": 120, "modified_time": "2026-10-09T10:00:00Z", "path": "/private/secret/config", "api_key": "metadata-secret"}],
                            "digest": [{"filename": "projects/notes.md", "size": 80, "modified_time": "2026-10-09T10:00:00Z"}]}
        self.memory_content = "今天完成了整理。"
        self.runtime_token = "test-runtime-token-" * 3
        self.redirect_hits = 0
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.upstream)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.client = QwenPawClient(f"http://127.0.0.1:{port}", "default", self.runtime_token)
        self.context = SimpleNamespace(registered_web_apis=[])
        self.context.register_web_api = lambda route, handler, methods, description: self.context.registered_web_apis.append((route, handler, methods, description))
        self.bridge = SimpleNamespace(enabled=True, _client=self.client, _store=self.store,
                                      owners={"owner"}, platforms={"aiocqhttp", "weixin"},
                                      tool_allowlist={"read_file", "browser"}, _active={}, context=self.context)
        self.workbench = Workbench(self.bridge)
        self.workbench.register()
        self.app = Quart(__name__)
        self.app.config["TESTING"] = True
        @self.app.before_request
        async def fake_dashboard_auth():
            if request.headers.get("X-Test-Login") == "logged-in":
                g.username = "dashboard-owner"
        for route, handler, methods, _ in self.context.registered_web_apis:
            self.app.add_url_rule("/api/" + route, route, handler, methods=methods)
        self.dashboard = self.app.test_client()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.store.close()
        self.temp.cleanup()

    async def upstream(self, req):
        # All calls through the real client must include its private header.
        if req.headers.get("X-QwenPaw-Runtime-Token") != self.runtime_token:
            return web.json_response({"api_key": "private-upstream-secret"}, status=401)
        body = await req.json() if req.can_read_body else None
        self.requests.append((req.method, req.path_qs, body))
        if req.path == "/api/healthz":
            return web.json_response(self.health)
        if req.path == "/api/version":
            return web.json_response({"version": "2.2.1", "token": "private-upstream-secret"})
        if req.path == "/api/models/active":
            return web.json_response({"active_llm": {"provider_id": "test", "model": "fake-model", "api_key": "private-upstream-secret", "base_url": "https://private-endpoint"}})
        if req.path.endswith("/memory/runtime-status"):
            return web.json_response({"worker": {"status": "running"}, "auto_memory": {"enabled": True}, "recent": {"last_error": "private-upstream-secret"}})
        if req.path.endswith("/workspace/memory"):
            return web.json_response(self.memory_rows[req.query.get("section", "daily")])
        if "/workspace/memory/" in req.path:
            return web.json_response({"content": self.memory_content, "api_key": "private-upstream-secret", "path": "/private/secret/config"})
        if req.path.endswith("/tools"):
            return web.json_response([{"name": "browser", "description": "本地受控浏览器", "enabled": True, "token": "private-upstream-secret"}])
        if req.path.endswith("/cron/jobs"):
            if req.method == "GET":
                return web.json_response(list(self.jobs.values()))
            self.created.append(copy.deepcopy(body))
            if self.create_status != 201:
                return web.json_response({"detail": "private-upstream-secret /private/secret/config"}, status=self.create_status)
            job = {"id": "job-" + str(len(self.created)), **body}
            self.jobs[job["id"]] = job
            await asyncio.sleep(0)
            return web.json_response(job, status=201)
        if "/cron/jobs/" in req.path:
            suffix = req.path.rsplit("/cron/jobs/", 1)[1]
            job_id, _, action = suffix.partition("/")
            job = self.jobs.get(job_id)
            if not job:
                return web.json_response({"detail": "private-upstream-secret"}, status=404)
            if action:
                job["enabled"] = action == "resume"
                return web.json_response({"ok": True})
            return web.json_response({"spec": job})
        if req.path == "/bad-json":
            return web.Response(text="private-upstream-secret", content_type="application/json")
        if req.path == "/oversized":
            return web.Response(body=b'"' + b"x" * (2 * 1024 * 1024) + b'"', content_type="application/json")
        if req.path == "/redirect":
            return web.Response(status=302, headers={"Location": self.client.base_url + "/redirect-target"}, text="private-upstream-secret")
        if req.path == "/redirect-target":
            self.redirect_hits += 1
            return web.json_response({"secret": "private-upstream-secret"})
        if req.path.startswith("/error/"):
            return web.json_response({"detail": "private-upstream-secret /private/secret/config"}, status=int(req.path.rsplit("/", 1)[1]))
        return web.json_response({"detail": "private-upstream-secret"}, status=404)

    def task_body(self, **changes):
        body = dict(session_id=self.route["session_id"], name="每日整理", prompt="检查专用文件夹并总结。", kind="agent",
                    frequency="daily", time="09:30", request_id="a" * 32)
        body.update(changes)
        return body

    def job(self, route=None, **changes):
        route = route or self.route
        value = dict(id="existing", name="提醒", enabled=True, task_type="text", text="private-task-text",
                     schedule={"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
                     dispatch={"type": "channel", "channel": "astrbot", "target": {"session_id": route["session_id"], "user_id": route["user_id"]}},
                     meta={"token": "private-upstream-secret"})
        value.update(changes)
        return value

    async def dashboard_call(self, operation, method="GET", body=None, headers=None, raw=None):
        headers = {"X-Test-Login": "logged-in", **(headers or {})}
        kwargs = {"headers": headers}
        if raw is not None:
            kwargs["data"] = raw
        elif body is not None:
            kwargs["json"] = body
        response = await self.dashboard.open("/api/" + self.workbench.prefix + operation, method=method, **kwargs)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response, await response.get_json()

    async def test_registration_and_unregister_preserve_other_handlers(self):
        self.assertEqual(len(self.workbench.registrations), 7)
        unrelated = ("other", object(), ["GET"], "other")
        self.context.registered_web_apis.append(unrelated)
        self.workbench.unregister()
        self.assertEqual(self.context.registered_web_apis, [unrelated])

    async def test_real_quart_auth_blocks_every_management_handler(self):
        for operation, method in (("status", "GET"), ("tasks", "GET"), ("memory/read", "POST"), ("tasks/create", "POST")):
            response, body = await self.dashboard_call(operation, method, self.task_body() if method == "POST" else None, headers={"X-Test-Login": "anonymous"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(self.runtime_token, json.dumps(body))
        self.assertFalse(self.requests)

    async def test_cross_site_origin_and_fetch_metadata_block_mutations(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": "file://localhost"}, {"Sec-Fetch-Site": "cross-site"}):
            response, _ = await self.dashboard_call("tasks/create", "POST", self.task_body(), headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.created)
        response, _ = await self.dashboard_call("tasks/create", "POST", self.task_body(), headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.created), 1)

    async def test_json_type_syntax_and_request_size_boundaries(self):
        cases = (({}, b"{}", 415), ({"Content-Type": "application/json"}, b"[1]", 400),
                 ({"Content-Type": "application/json"}, b"{bad}", 400),
                 ({"Content-Type": "application/json"}, b"x" * 32769, 413))
        for headers, raw, status in cases:
            response, _ = await self.dashboard_call("tasks/create", "POST", headers=headers, raw=raw)
            self.assertEqual(response.status_code, status)
        self.assertFalse(self.created)

    async def test_status_has_no_tokens_paths_or_raw_upstream_errors(self):
        response, body = await self.dashboard_call("status")
        self.assertEqual(response.status_code, 200)
        value = body["data"]
        self.assertTrue(value["qwenpaw_ready"])
        self.assertEqual(value["version"], "2.2.1")
        self.assertEqual(value["owner_count"], 1)
        self.assertEqual(value["model"], {"configured": True, "name": "fake-model"})
        self.assertEqual([route["session_id"] for route in value["routes"]], [self.route["session_id"]])
        for private in (self.runtime_token, "private-upstream-secret", "/private/secret/config", "private-endpoint", self.foreign_route["session_id"]):
            self.assertNotIn(private, json.dumps(body))
        self.assertTrue(all(method == "GET" for method, _, _ in self.requests))

    async def test_health_200_requires_correct_shape_and_configured_agent(self):
        for health in ({}, {"status": "starting", "agents_loaded": ["default"]},
                       {"status": "ok", "agents_loaded": ["different-agent"]}, {"status": "ok", "agents_loaded": "default"}):
            self.health = health
            value = await self.workbench.status()
            self.assertFalse(value["qwenpaw_ready"])
            self.assertIn("health", value["failures"])

    async def test_api_rejects_redirect_without_forwarding_private_header(self):
        with self.assertRaises(WorkbenchError) as caught:
            await self.workbench._api("GET", "/redirect")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(self.redirect_hits, 0)
        self.assertNotIn("private-upstream-secret", str(caught.exception))

    async def test_api_bounds_streamed_response_and_invalid_json(self):
        for path, status in (("/oversized", 413), ("/bad-json", 502)):
            with self.assertRaises(WorkbenchError) as caught:
                await self.workbench._api("GET", path)
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn("private-upstream-secret", str(caught.exception))

    async def test_api_error_response_body_never_reaches_dashboard(self):
        for status in (401, 403, 404, 422, 503, 500):
            with self.assertRaises(WorkbenchError) as caught:
                await self.workbench._api("GET", "/error/" + str(status))
            self.assertEqual(caught.exception.status, status if status != 500 else 502)
            self.assertNotIn("private-upstream-secret", str(caught.exception))
            self.assertNotIn("/private/secret/config", str(caught.exception))

    async def test_only_saved_owner_and_enabled_platform_tasks_are_visible(self):
        owner = self.job()
        foreign = self.job(self.foreign_route, id="foreign")
        disabled = self.job(self.disabled_route, id="disabled")
        wrong_user = self.job(id="wrong-user")
        wrong_user["dispatch"]["target"]["user_id"] = "other"
        wrong_channel = self.job(id="wrong-channel")
        wrong_channel["dispatch"]["channel"] = "console"
        self.jobs = {job["id"]: job for job in (owner, foreign, disabled, wrong_user, wrong_channel)}
        result = await self.workbench.tasks()
        self.assertEqual([job["id"] for job in result["items"]], ["existing"])
        self.assertNotIn("private-task-text", json.dumps(result))
        self.assertNotIn("private-upstream-secret", json.dumps(result))

    async def test_create_spec_forces_route_and_tool_approval(self):
        result = await self.workbench.create_task(self.task_body(user_id="other", channel="console", tool_safety=False, dispatch={"target": {"session_id": self.foreign_route["session_id"]}}))
        spec = self.created[0]
        self.assertTrue(spec["runtime"]["tool_safety"])
        self.assertEqual(spec["dispatch"]["channel"], "astrbot")
        self.assertEqual(spec["dispatch"]["target"], {"session_id": self.route["session_id"], "user_id": "owner"})
        self.assertEqual(spec["schedule"]["cron"], "30 9 * * *")
        self.assertEqual(spec["request"]["input"][0]["content"][0]["text"], "检查专用文件夹并总结。")
        self.assertEqual(result["id"], "job-1")
        self.assertNotIn("request", result)

    async def test_foreign_and_disabled_sessions_cannot_create(self):
        for route in (self.foreign_route, self.disabled_route, {"session_id": "ab_" + "0" * 32}):
            with self.assertRaises(WorkbenchError) as caught:
                await self.workbench.create_task(self.task_body(session_id=route["session_id"]))
            self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.created)

    async def test_concurrent_duplicate_request_is_submitted_once(self):
        results = await asyncio.gather(*(self.workbench.create_task(self.task_body()) for _ in range(4)))
        self.assertEqual(len(self.created), 1)
        self.assertTrue(all(result == results[0] for result in results))
        again = await self.workbench.create_task(self.task_body())
        self.assertEqual(again, results[0])
        self.assertEqual(len(self.created), 1)

    async def test_uncertain_mutation_is_never_retried_by_duplicate_request(self):
        self.create_status = 500
        with self.assertRaises(WorkbenchError) as caught:
            await self.workbench.create_task(self.task_body())
        self.assertEqual(caught.exception.status, 502)
        self.create_status = 201
        with self.assertRaises(WorkbenchError) as caught:
            await self.workbench.create_task(self.task_body())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.created), 1)

    async def test_definitive_rejection_releases_receipt_for_corrected_request(self):
        self.create_status = 422
        with self.assertRaises(WorkbenchError):
            await self.workbench.create_task(self.task_body())
        self.assertIsNone(self.store.receipt("workbench_create", "a" * 32))
        self.create_status = 201
        result = await self.workbench.create_task(self.task_body())
        self.assertEqual(result["id"], "job-2")

    async def test_control_only_owner_bridge_job_and_only_pause_resume(self):
        self.jobs = {"own": self.job(id="own"), "foreign": self.job(self.foreign_route, id="foreign")}
        with self.assertRaises(WorkbenchError) as caught:
            await self.workbench.control_task({"id": "foreign", "action": "pause"})
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(any(method == "POST" for method, _, _ in self.requests))
        for body in ({"id": "own", "action": "run"}, {"id": "../config", "action": "pause"}):
            with self.assertRaises(WorkbenchError):
                await self.workbench.control_task(body)
        await self.workbench.control_task({"id": "own", "action": "pause"})
        self.assertFalse(self.jobs["own"]["enabled"])
        await self.workbench.control_task({"id": "own", "action": "resume"})
        self.assertTrue(self.jobs["own"]["enabled"])

    async def test_memory_listing_filters_paths_and_private_metadata(self):
        invalid = ("../secret.md", "/absolute.md", ".env.md", "folder/.secret.md", "folder\\secret.md", "secret%2emd", "secret.md?token=x", "secret.md\n", "config.json")
        self.memory_rows["daily"].extend({"filename": filename, "path": "/private/secret/config"} for filename in invalid)
        result = await self.workbench.memory()
        self.assertEqual([row["name"] for row in result["items"]], ["2026-10-09.md", "projects/notes.md"])
        self.assertNotIn("/private/secret/config", json.dumps(result))
        self.assertNotIn("metadata-secret", json.dumps(result))

    async def test_memory_read_requires_membership_and_does_not_return_metadata(self):
        result = await self.workbench.read_memory({"section": "digest", "name": "projects/notes.md"})
        self.assertEqual(result, {"content": self.memory_content, "name": "projects/notes.md", "section": "digest"})
        self.assertNotIn("private-upstream-secret", json.dumps(result))
        for body in ({"section": "daily", "name": "missing.md"}, {"section": "digest", "name": "2026-10-09.md"}):
            with self.assertRaises(WorkbenchError) as caught:
                await self.workbench.read_memory(body)
            self.assertEqual(caught.exception.status, 404)
        before = len(self.requests)
        for name in ("../config.md", "/absolute.md", "folder/.private.md", "http://evil/secret.md", "folder%2fsecret.md"):
            with self.assertRaises(WorkbenchError):
                await self.workbench.read_memory({"section": "daily", "name": name})
        self.assertEqual(len(self.requests), before)

    async def test_tools_and_unexpected_exceptions_are_redacted(self):
        result = await self.workbench.tools()
        self.assertEqual(result["items"][0]["name"], "browser")
        self.assertNotIn("private-upstream-secret", json.dumps(result))
        self.workbench.bridge._store.list_sessions = lambda: (_ for _ in ()).throw(RuntimeError("private-upstream-secret"))
        response, body = await self.dashboard_call("tasks/create", "POST", self.task_body())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-upstream-secret", json.dumps(body))

    async def test_disabled_bridge_does_not_request_upstream(self):
        self.bridge.enabled = False
        response, _ = await self.dashboard_call("status")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.requests)


if __name__ == "__main__":
    unittest.main()
