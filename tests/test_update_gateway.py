"""Real local HTTP tests for update consent, signed IPC, and origin isolation.

The test mailbox contains synthetic data only; no updater daemon or Docker runs.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from bundle_gateway.server import STATE, create_app
from bundle_updater.common import atomic_json, read_json, sign, verify


class UpdateGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bundle-update-http-test-")
        self.root = Path(self.temporary.name)
        self.ipc = self.root / "mailbox"
        self.ipc.mkdir()
        self.runtime_token = "synthetic-runtime-" + "a" * 48
        self.candidate = {
            "id": "b" * 32,
            "version": "0.6.1-alpha",
            "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"},
            "notes": "Synthetic release notes.\n<img src=x onerror=alert(1)>",
            "html_url": "https://attacker.invalid/private-link",
            "private_host_path": r"E:\SyntheticPrivateHost\backup",
        }
        atomic_json(self.root / "bundle-version.json", {
            "format": 1, "version": "0.6.0-alpha", "update_protocol": 1,
            "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"},
        })
        self.write_status()
        self.native_calls = []

        async def native(request):
            self.native_calls.append(request.path)
            return web.json_response({"native": True})

        native_app = web.Application()
        native_app.router.add_route("*", "/{tail:.*}", native)
        self.native = TestServer(native_app)
        await self.native.start_server()
        native_url = str(self.native.make_url("/")).rstrip("/")
        app = create_app(token=self.runtime_token, update_ipc=self.ipc, version_root=self.root,
                         upstreams={key: native_url for key in ("astrbot", "qwenpaw", "napcat")})
        self.gateway = TestServer(app)
        await self.gateway.start_server()
        self.port = self.gateway.port
        self.portal = f"http://localhost:{self.port}"
        app[STATE].update(port=self.port, portal=self.portal)
        self.api = app[STATE]["updates"]
        self.client = ClientSession()
        self.csrf = (await self.get_state())[1]["csrf_token"]

    async def asyncTearDown(self):
        await self.client.close()
        await self.gateway.close()
        await self.native.close()
        self.temporary.cleanup()

    def write_status(self, **values):
        state = {"heartbeat": time.time(), "phase": "idle", "active": False,
                 "current_version": "0.6.0-alpha", **values}
        atomic_json(self.ipc / "status.json", sign(state, self.runtime_token))

    def headers(self, **values):
        return {"Host": f"localhost:{self.port}", **values}

    def post_headers(self, **values):
        return self.headers(**{
            "Origin": self.portal, "Content-Type": "application/json", "X-Bundle-Update-Token": self.csrf, **values,
        })

    async def get_state(self, **headers):
        async with self.client.get(self.gateway.make_url("/bundle-update"), headers=self.headers(**headers)) as response:
            return response.status, await response.json(), dict(response.headers)

    async def post(self, action="check", body=None, *, headers=None, raw=None):
        if raw is None:
            raw = json.dumps({} if body is None else body)
        async with self.client.post(self.gateway.make_url("/bundle-update/" + action),
                                    headers=self.post_headers() if headers is None else headers, data=raw) as response:
            return response.status, await response.text()

    def requests(self):
        return sorted(self.ipc.glob("request-*.json"))

    async def test_snapshot_is_read_only_and_runtime_secret_is_not_exposed(self):
        status, body, headers = await self.get_state()
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["current_version"], "0.6.0-alpha")
        self.assertEqual(body["phase"], "idle")
        self.assertFalse(body["active"])
        self.assertIsInstance(body["csrf_token"], str)
        self.assertGreaterEqual(len(body["csrf_token"]), 32)
        self.assertNotEqual(body["csrf_token"], self.runtime_token)
        self.assertNotIn(self.runtime_token, json.dumps(body))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.requests(), [])

    async def test_missing_mailbox_status_disables_update(self):
        (self.ipc / "status.json").unlink()
        _, body, _ = await self.get_state()
        self.assertFalse(body["enabled"])
        self.assertFalse(body["active"])
        status, _ = await self.post()
        self.assertEqual(status, 503)
        self.assertEqual(self.requests(), [])

    async def test_stale_future_and_invalid_heartbeat_disable_updates(self):
        for heartbeat in (time.time() - 30, time.time() + 30, True, "now", None):
            with self.subTest(heartbeat=type(heartbeat).__name__):
                self.write_status(heartbeat=heartbeat)
                _, body, _ = await self.get_state()
                self.assertFalse(body["enabled"])

    async def test_forged_status_signature_is_rejected(self):
        envelope = sign({"heartbeat": time.time(), "phase": "idle", "active": False}, "untrusted-token")
        atomic_json(self.ipc / "status.json", envelope)
        _, body, _ = await self.get_state()
        self.assertFalse(body["enabled"])
        status, _ = await self.post()
        self.assertEqual(status, 503)
        self.assertEqual(self.requests(), [])

    async def test_invalid_status_document_does_not_break_the_gateway(self):
        for value in ({"payload": {}, "signature": "fake"}, {"payload": {}, "signature": "x", "extra": True}, sign([], self.runtime_token)):
            with self.subTest(value_type=type(value).__name__):
                atomic_json(self.ipc / "status.json", value)
                status, body, _ = await self.get_state()
                self.assertEqual(status, 200)
                self.assertFalse(body["enabled"])
        (self.ipc / "status.json").write_text("not JSON", encoding="utf-8")
        self.assertFalse((await self.get_state())[1]["enabled"])

    async def test_unknown_phase_disables_update(self):
        self.write_status(phase="execute-arbitrary-command")
        self.assertFalse((await self.get_state())[1]["enabled"])

    async def test_host_diagnostics_and_paths_are_not_forwarded(self):
        self.write_status(phase="failed", message=r"failure E:\SyntheticPrivateHost\backup",
                          step="/" + "home/synthetic-private-owner/secret", diagnostics={"token": self.runtime_token},
                          backup_path="/opt/private-workspace/backups", traceback="private diagnostic",
                          request_id="not-a-valid-identifier", current_version="E:/private-version")
        _, body, _ = await self.get_state()
        rendered = json.dumps(body)
        for secret in ("SyntheticPrivateHost", "synthetic-private-owner", "private-workspace", self.runtime_token,
                       "private diagnostic", "not-a-valid-identifier", "private-version"):
            self.assertNotIn(secret, rendered)
        self.assertNotIn("step", body)
        self.assertNotIn("diagnostics", body)
        self.assertEqual(body["current_version"], "0.6.0-alpha")

    async def test_candidate_has_exact_components_and_canonical_public_link(self):
        self.write_status(phase="available", candidate=self.candidate)
        _, body, _ = await self.get_state()
        candidate = body["candidate"]
        self.assertEqual(set(candidate), {"id", "version", "components", "notes", "html_url"})
        self.assertEqual(set(candidate["components"]), {"astrbot", "qwenpaw", "napcat"})
        self.assertEqual(candidate["notes"], self.candidate["notes"])
        self.assertEqual(candidate["html_url"], "https://github.com/misaki-mei-yue/astrbot-qwenpaw/releases/tag/v0.6.1-alpha")
        self.assertNotIn("SyntheticPrivateHost", json.dumps(candidate))

    async def test_invalid_candidate_fields_are_omitted(self):
        invalid = [
            {"id": "../../command"}, {"id": "b" * 31}, {"id": "B" * 32}, {"id": 2},
            {"version": "https://attacker.invalid/"}, {"version": "/opt/private-version"},
            {"components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1"}},
            {"components": {**self.candidate["components"], "shell": "arbitrary"}},
            {"components": {**self.candidate["components"], "astrbot": "E:/private-image"}},
            {"components": {**self.candidate["components"], "qwenpaw": "$(command)"}},
            {"components": {**self.candidate["components"], "napcat": "x" * 101}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.write_status(phase="available", candidate={**self.candidate, **changes})
                _, body, _ = await self.get_state()
                self.assertNotIn("candidate", body)

    async def test_candidate_notes_are_bounded(self):
        self.write_status(phase="available", candidate={**self.candidate, "notes": "a" * 20000})
        self.assertEqual(len((await self.get_state())[1]["candidate"]["notes"]), 16000)

    async def test_progress_is_bounded_and_booleans_do_not_become_progress(self):
        for value, expected in ((-100, 0), (500, 100), (56, 56), (True, None), (1.5, None)):
            with self.subTest(value=value):
                self.write_status(phase="updating", active=True, progress=value)
                body = (await self.get_state())[1]
                self.assertEqual(body.get("progress"), expected)

    async def test_post_requires_exact_origin_csrf_and_json_content_type(self):
        cases = [
            {"Origin": ""}, {"Origin": "https://attacker.invalid"},
            {"Origin": f"http://127.0.0.1:{self.port}"},
            {"X-Bundle-Update-Token": ""}, {"X-Bundle-Update-Token": self.runtime_token}, {"X-Bundle-Update-Token": "非 ASCII 无效令牌"},
            {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                status, _ = await self.post(headers=self.post_headers(**changes))
                self.assertEqual(status, 403)
        self.assertEqual(self.requests(), [])

    async def test_cross_site_browser_requests_are_rejected(self):
        for method in ("GET", "POST"):
            headers = self.post_headers(**{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"})
            path = "/bundle-update" if method == "GET" else "/bundle-update/check"
            async with self.client.request(method, self.gateway.make_url(path), headers=headers, data="{}") as response:
                self.assertEqual(response.status, 403)
        self.assertEqual(self.requests(), [])

    async def test_unknown_host_cannot_read_update_csrf(self):
        async with self.client.get(self.gateway.make_url("/bundle-update"), headers={"Host": "attacker.invalid"}) as response:
            self.assertEqual(response.status, 403)
            self.assertNotIn(self.csrf, await response.text())

    async def test_native_application_hosts_cannot_call_portal_update_api(self):
        for app in ("astrbot", "qwenpaw", "napcat"):
            for method, path in (("GET", "/bundle-update"), ("POST", "/bundle-update/check"), ("POST", "/bundle-update/apply")):
                headers = self.post_headers(Host=f"{app}.localhost:{self.port}", Origin=f"http://{app}.localhost:{self.port}")
                async with self.client.request(method, self.gateway.make_url(path), headers=headers, data="{}") as response:
                    self.assertIn(response.status, (403, 404))
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.requests(), [])

    async def test_check_only_installation_blocks_apply_without_blocking_release_checks(self):
        self.write_status(phase="available", candidate=self.candidate, update_capable=False,
                          error="local_source_modified")
        _, body, _ = await self.get_state()
        self.assertTrue(body["enabled"])
        self.assertFalse(body["update_capable"])
        self.assertIn("本地源码修改", body["message"])
        status, text = await self.post("apply", {"candidate_id": self.candidate["id"]})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(text)["error"], "unmanaged_source")
        self.assertEqual(self.requests(), [])
        self.assertEqual((await self.post("check"))[0], 202)
        self.assertEqual(len(self.requests()), 1)
    async def test_check_writes_one_authenticated_command_without_host_paths(self):
        status, text = await self.post()
        self.assertEqual(status, 202)
        response = json.loads(text)
        self.assertTrue(response["active"])
        self.assertEqual(response["phase"], "checking")
        self.assertEqual(len(self.requests()), 1)
        payload = verify(read_json(self.requests()[0]), self.runtime_token)
        self.assertEqual(set(payload), {"id", "action", "created"})
        self.assertEqual(payload["id"], response["request_id"])
        self.assertEqual(payload["action"], "check")
        self.assertLess(abs(time.time() - payload["created"]), 5)
        self.assertNotIn(self.runtime_token, self.requests()[0].read_text(encoding="utf-8"))

    async def test_apply_requires_current_candidate_and_writes_signed_intent(self):
        self.write_status(phase="available", candidate=self.candidate)
        status, text = await self.post("apply", {"candidate_id": self.candidate["id"]})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(text)["phase"], "updating")
        payload = verify(read_json(self.requests()[0]), self.runtime_token)
        self.assertEqual(payload["action"], "apply")
        self.assertEqual(payload["candidate_id"], self.candidate["id"])
        self.assertEqual(set(payload), {"id", "action", "created", "candidate_id"})

    async def test_apply_does_not_accept_a_different_or_unchecked_candidate(self):
        for state in ({}, {"phase": "available", "candidate": self.candidate}):
            with self.subTest(state=state):
                self.write_status(**state)
                status, text = await self.post("apply", {"candidate_id": "c" * 32})
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(text)["error"], "invalid_candidate")
        self.assertEqual(self.requests(), [])

    async def test_invalid_json_and_extra_request_fields_are_rejected(self):
        for raw in ("", "{", "[]", "null", "true", '"check"', '{"extra":true}'):
            with self.subTest(raw=raw):
                self.assertEqual((await self.post(raw=raw))[0], 400)
        for body in ({}, {"candidate_id": "../../x"}, {"candidate_id": 7}, {"candidate_id": "A" * 32},
                     {"candidate_id": "b" * 32, "command": "arbitrary"}):
            with self.subTest(body=body):
                self.assertEqual((await self.post("apply", body))[0], 400)
        self.assertEqual(self.requests(), [])

    async def test_oversized_requests_are_rejected_including_chunked_uploads(self):
        raw = '{"padding":"' + "a" * 5000 + '"}'
        self.assertEqual((await self.post(raw=raw))[0], 400)
        async def chunks():
            yield b'{"padding":"'
            yield b"a" * 5000
            yield b'"}'
        async with self.client.post(self.gateway.make_url("/bundle-update/check"), headers=self.post_headers(), data=chunks()) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.requests(), [])

    async def test_duplicate_concurrent_requests_produce_only_one_mailbox_entry(self):
        responses = await asyncio.gather(self.post(), self.post())
        self.assertEqual(sorted(status for status, _ in responses), [202, 409])
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual((await self.post())[0], 409)

    async def test_running_or_recovery_required_tasks_cannot_start_another_update(self):
        for values in ({"phase": "updating", "active": True}, {"phase": "failed", "recovery_required": True}):
            with self.subTest(values=values):
                self.write_status(**values)
                status, _ = await self.post()
                self.assertEqual(status, 409)
        self.assertEqual(self.requests(), [])

    async def test_update_routes_do_not_allow_arbitrary_methods_or_actions(self):
        for method, path, expected in (("GET", "/bundle-update/check", 405), ("PUT", "/bundle-update/apply", 405),
                                       ("POST", "/bundle-update/delete", 404)):
            async with self.client.request(method, self.gateway.make_url(path), headers=self.post_headers(), data="{}") as response:
                self.assertEqual(response.status, expected)
        self.assertEqual(self.requests(), [])


if __name__ == "__main__":
    unittest.main()
