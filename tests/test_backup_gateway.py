"""Real HTTP checks for portable backups; synthetic mailboxes, no Docker or model."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from bundle_gateway import backups
from bundle_gateway.server import STATE, create_app
from bundle_updater.backup_archive import open_secret
from bundle_updater.common import atomic_json, read_json, sign, verify


class BackupGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="backup-http-")
        self.root = Path(self.temporary.name)
        self.ipc = self.root / "mailbox"
        for path in (self.ipc / "uploads", self.ipc / "transfers"):
            path.mkdir(parents=True)
        self.token = "synthetic-backup-token-" + "f" * 48
        self.native_calls = []

        async def native(request):
            self.native_calls.append(request.path)
            return web.Response(text="native")

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", native)
        self.native = TestServer(app)
        await self.native.start_server()
        target = str(self.native.make_url("/")).rstrip("/")
        app = create_app(token=self.token, backup_ipc=self.ipc,
                         upstreams={key: target for key in ("astrbot", "qwenpaw", "napcat")})
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.port = self.client.server.port
        self.portal = "http://localhost:" + str(self.port)
        app[STATE].update(port=self.port, portal=self.portal)
        self.api = app[STATE]["backups"]
        self.write_status()
        self.csrf = (await self.snapshot())[1]["csrf_token"]

    async def asyncTearDown(self):
        await self.client.close()
        await self.native.close()
        self.temporary.cleanup()

    def write_status(self, **changes):
        state = {"heartbeat": time.time(), "phase": "idle", "ready": True, "active": False, "backups": [], **changes}
        atomic_json(self.ipc / "status.json", sign(state, self.token))

    def headers(self, **changes):
        return {"Host": "localhost:" + str(self.port), **changes}

    def post_headers(self, *, form=False, **changes):
        value = self.headers(Origin=self.portal, **{"X-Bundle-Backup-Token": self.csrf})
        if not form:
            value["Content-Type"] = "application/json"
        value.update(changes)
        return value

    async def snapshot(self, **changes):
        async with self.client.get("/bundle-backup", headers=self.headers(**changes)) as response:
            return response.status, await response.json(), dict(response.headers)

    async def post(self, action="export", body=None, *, headers=None, raw=None):
        if raw is None:
            raw = json.dumps({"password": "fixture password long enough", "destination": "server"} if body is None else body)
        async with self.client.post("/bundle-backup/" + action, data=raw,
                                    headers=self.post_headers() if headers is None else headers) as response:
            return response.status, await response.text()

    def requests(self):
        return sorted(self.ipc.glob("request-*.json"))

    def uploads(self):
        return list((self.ipc / "uploads").glob("*.aqbackup"))

    def form(self, content=b"synthetic encrypted backup" * 4, *, name="file", filename="fixture.aqbackup", extra=False):
        data = FormData()
        data.add_field(name, content, filename=filename, content_type="application/octet-stream")
        if extra:
            data.add_field("unexpected", "extra field")
        return data

    async def upload(self, data=None, **changes):
        async with self.client.post("/bundle-backup/upload", data=data if data is not None else self.form(),
                                    headers=self.post_headers(form=True, **changes)) as response:
            return response.status, await response.text()

    async def test_snapshot_has_csrf_but_never_runtime_token_or_private_paths(self):
        self.write_status(message="/opt/private-runtime/sensitive", diagnostics={"token": self.token},
                          step="private-step", backup_path="C:/fixture-private", error="/secret/key")
        status, value, headers = await self.snapshot()
        self.assertEqual(status, 200)
        self.assertTrue(value["enabled"])
        self.assertNotEqual(value["csrf_token"], self.token)
        self.assertGreaterEqual(len(value["csrf_token"]), 32)
        for secret in (self.token, "private-runtime", "private-step", "C:/fixture-private", "/secret/key"):
            self.assertNotIn(secret, json.dumps(value))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.requests(), [])

    async def test_missing_stale_future_forged_and_unknown_status_disable_actions(self):
        for changes in ({"heartbeat": time.time() - 30}, {"heartbeat": time.time() + 30},
                        {"heartbeat": True}, {"phase": "execute"}, {"ready": False}):
            self.write_status(**changes)
            self.assertFalse((await self.snapshot())[1]["enabled"])
            self.assertEqual((await self.post())[0], 503)
        atomic_json(self.ipc / "status.json", sign({"heartbeat": time.time(), "phase": "idle", "ready": True}, "forged"))
        self.assertFalse((await self.snapshot())[1]["enabled"])
        (self.ipc / "status.json").unlink()
        self.assertFalse((await self.snapshot())[1]["enabled"])
        self.assertEqual(self.requests(), [])

    async def test_malformed_signed_status_never_crashes_snapshot(self):
        for value in ([], None, {"backups": {"id": "bad"}}, {"backups": 42}, {"backups": None}):
            if isinstance(value, dict):
                self.write_status(**value)
            else:
                atomic_json(self.ipc / "status.json", sign(value, self.token))
            self.assertEqual((await self.snapshot())[0], 200)

    async def test_exact_host_origin_csrf_and_content_type_are_required(self):
        changes = [{"Host": "attacker.invalid"}, {"Host": "127.0.0.1:" + str(self.port)},
                   {"Origin": ""}, {"Origin": "https://attacker.invalid"},
                   {"Origin": "http://qwenpaw.localhost:" + str(self.port)},
                   {"X-Bundle-Backup-Token": ""}, {"X-Bundle-Backup-Token": self.token},
                   {"X-Bundle-Backup-Token": "无效令牌"}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual((await self.post(headers=self.post_headers(**change)))[0], 403)
        for content_type in ("text/plain", "application/x-www-form-urlencoded"):
            self.assertEqual((await self.post(headers=self.post_headers(**{"Content-Type": content_type})))[0], 400)
        self.assertEqual(self.requests(), [])

    async def test_cross_site_fetch_and_all_native_hosts_are_blocked(self):
        async with self.client.get("/bundle-backup", headers=self.headers(**{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"})) as response:
            self.assertEqual(response.status, 403)
        for app in ("astrbot", "qwenpaw", "napcat"):
            authority = app + ".localhost:" + str(self.port)
            for method, path in (("GET", "/bundle-backup"), ("POST", "/bundle-backup/export"),
                                 ("POST", "/bundle-backup/upload"), ("GET", "/bundle-backup/download/" + "a" * 32)):
                headers = self.post_headers(Host=authority, Origin="http://" + authority)
                async with self.client.request(method, path, headers=headers, data="{}") as response:
                    self.assertEqual(response.status, 403)
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.requests(), [])

    async def test_export_mailbox_is_authenticated_and_password_is_sealed_for_job_and_action(self):
        password = "  正确保留空格的长期记忆备份密码  "
        status, text = await self.post(body={"password": password, "destination": "download"})
        self.assertEqual(status, 202)
        identifier = json.loads(text)["request_id"]
        self.assertEqual(len(self.requests()), 1)
        payload = verify(read_json(self.requests()[0]), self.token)
        self.assertEqual(set(payload), {"id", "created", "action", "destination", "secret"})
        self.assertEqual(payload["id"], identifier)
        self.assertEqual(payload["destination"], "download")
        self.assertEqual(open_secret(payload["secret"], self.token, identifier + ":export"), password)
        raw = self.requests()[0].read_text(encoding="utf-8")
        for private in (password, self.token, "备份密码"):
            self.assertNotIn(private, raw)

    async def test_inspect_upload_reference_and_password_are_sealed(self):
        identifier = "a" * 32
        status, _ = await self.post("inspect", {"upload_id": identifier, "password": "fixture password long enough"})
        self.assertEqual(status, 202)
        payload = verify(read_json(self.requests()[0]), self.token)
        self.assertEqual(payload["upload_id"], identifier)
        self.assertEqual(open_secret(payload["secret"], self.token, payload["id"] + ":inspect"), "fixture password long enough")

    async def test_invalid_json_fields_passwords_and_destination_types_are_rejected(self):
        for raw in ("", "{", "[]", "null", "true", '{"password":"a","password":"b","destination":"server"}'):
            self.assertEqual((await self.post(raw=raw))[0], 400)
        for body in ({"password": "short", "destination": "server"},
                     {"password": "中" * 342, "destination": "server"},
                     {"password": "\ud800" * 12, "destination": "server"},
                     {"password": "a" * 12, "destination": []},
                     {"password": "a" * 12, "destination": {}},
                     {"password": "a" * 12, "destination": "/opt/private"},
                     {"password": "a" * 12, "destination": "server", "command": "shell"}):
            self.assertEqual((await self.post(body=body))[0], 400)
        for upload_id in ("../secret", "A" * 32, [], None):
            self.assertEqual((await self.post("inspect", {"password": "a" * 12, "upload_id": upload_id}))[0], 400)
        self.assertEqual(self.requests(), [])

    async def test_oversized_and_delayed_chunked_json_cannot_hide_after_valid_prefix(self):
        body = json.dumps({"password": "a" * 12, "destination": "server"}).encode()
        self.assertEqual((await self.post(raw=body + b" " * 9000))[0], 400)

        async def chunks():
            yield body
            await asyncio.sleep(0.03)
            yield b" " * 9000

        async with self.client.post("/bundle-backup/export", headers=self.post_headers(), data=chunks()) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.requests(), [])

    async def test_active_recovery_and_duplicate_requests_cannot_overlap(self):
        for changes in ({"active": True}, {"recovery_required": True}):
            self.write_status(**changes)
            self.assertEqual((await self.post())[0], 409)
        self.write_status()
        responses = await asyncio.gather(self.post(), self.post())
        self.assertEqual(sorted(status for status, _ in responses), [202, 409])
        self.assertEqual(len(self.requests()), 1)

    def plan(self, **changes):
        return {"plan_id": "b" * 32, "version": "0.7.0-alpha", "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"},
                "files": 10, "bytes": 1024, "target_has_data": True, "warnings": ["source_must_be_stopped"], **changes}

    async def test_restore_requires_current_checked_plan_and_explicit_consents(self):
        self.write_status(phase="ready_to_restore", plan=self.plan())
        valid = {"plan_id": "b" * 32, "confirm_source_stopped": True, "overwrite": True}
        for changes in ({"plan_id": "c" * 32}, {"confirm_source_stopped": False}, {"overwrite": False}, {"overwrite": "yes"}):
            self.assertEqual((await self.post("restore", {**valid, **changes}))[0], 400)
        self.assertEqual((await self.post("restore", valid))[0], 202)
        payload = verify(read_json(self.requests()[0]), self.token)
        self.assertEqual(set(payload), {"id", "created", "action", "plan_id", "confirm_source_stopped", "overwrite"})
        self.assertEqual(payload["plan_id"], "b" * 32)

    async def test_upload_stores_only_ciphertext_under_random_id_and_does_not_queue_restore(self):
        content = os.urandom(1024)
        status, text = await self.upload(self.form(content, filename="../../private.aqbackup"))
        self.assertEqual(status, 201)
        identifier = json.loads(text)["upload_id"]
        self.assertRegex(identifier, r"^[0-9a-f]{32}$")
        self.assertEqual(self.uploads(), [self.ipc / "uploads" / (identifier + ".aqbackup")])
        self.assertEqual(self.uploads()[0].read_bytes(), content)
        self.assertEqual(self.requests(), [])
        replacement_status, replacement_body = await self.upload(self.form(b"replacement-encrypted-fixture" * 8))
        self.assertEqual(replacement_status, 201)
        replacement_id = json.loads(replacement_body)["upload_id"]
        self.assertNotEqual(replacement_id, identifier)
        self.assertEqual(self.uploads(), [self.ipc / "uploads" / (replacement_id + ".aqbackup")])
        self.assertEqual(self.uploads()[0].read_bytes(), b"replacement-encrypted-fixture" * 8)

    async def test_bad_upload_parts_names_extensions_small_files_and_extra_fields_are_rejected(self):
        forms = [self.form(name="other"), self.form(filename="bad.zip"), self.form(content=b"tiny"), self.form(extra=True)]
        for form in forms:
            self.assertEqual((await self.upload(form))[0], 400)
            self.assertEqual(self.uploads(), [])
        self.assertEqual((await self.upload(b"not multipart", **{"Content-Type": "text/plain"}))[0], 400)
        self.assertEqual(self.requests(), [])

    async def test_nested_multipart_upload_is_rejected_without_crashing(self):
        boundary = "outer-backup-test"
        content = ("--" + boundary + "\r\nContent-Type: multipart/mixed; boundary=inner\r\n\r\n--inner--\r\n--" + boundary + "--\r\n").encode()
        self.assertEqual((await self.upload(content, **{"Content-Type": "multipart/form-data; boundary=" + boundary}))[0], 400)
        self.assertEqual(self.uploads(), [])

    async def test_upload_size_limit_and_insufficient_space_cleanup(self):
        with patch.object(backups, "MAX_ARCHIVE", 128):
            self.assertEqual((await self.upload(self.form(b"x" * 256)))[0], 413)
        self.assertEqual(self.uploads(), [])
        usage = backups.shutil.disk_usage(self.root)
        with patch.object(backups.shutil, "disk_usage", return_value=type(usage)(usage.total, usage.used, 0)):
            self.assertEqual((await self.upload())[0], 507)
        self.assertEqual(self.uploads(), [])

    async def test_truncated_multipart_cleans_partially_written_ciphertext(self):
        boundary = "truncated-backup-test"
        prefix = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"backup.aqbackup\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        status, _ = await self.upload(prefix + b"x" * 256, **{"Content-Type": "multipart/form-data; boundary=" + boundary})
        self.assertEqual(status, 400)
        self.assertEqual(self.uploads(), [])

    def catalog(self, identifier="a" * 32):
        return {"id": identifier, "filename": "../../private-path", "size": 128, "version": "0.7.0-alpha", "created_at": "2026-10-19T00:00:00Z"}

    async def test_download_requires_csrf_allowed_catalog_id_and_regular_unlinked_file(self):
        identifier = "a" * 32
        content = b"fixture encrypted bytes"
        target = self.ipc / "transfers" / (identifier + ".aqbackup")
        target.write_bytes(content)
        self.write_status(backups=[self.catalog()])
        async with self.client.get("/bundle-backup/download/" + identifier + "?token=" + self.csrf, headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), content)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("private-path", response.headers["Content-Disposition"])
        for token, current_id, expected in (("wrong", identifier, 403), (self.token, identifier, 403),
                                             (self.csrf, "A" * 32, 403), (self.csrf, "b" * 32, 404)):
            async with self.client.get("/bundle-backup/download/" + current_id + "?token=" + token, headers=self.headers()) as response:
                self.assertEqual(response.status, expected)
        hardlink = self.root / "other-link"
        try:
            os.link(target, hardlink)
        except OSError:
            return
        async with self.client.get("/bundle-backup/download/" + identifier + "?token=" + self.csrf, headers=self.headers()) as response:
            self.assertEqual(response.status, 404)

    async def test_catalog_and_plan_project_only_fixed_fields_and_safe_names(self):
        self.write_status(backups=[self.catalog(), self.catalog(identifier="../../x")], plan=self.plan(
            private_path="/opt/private", password="never publish", warnings=["source_must_be_stopped", "/secret/raw-warning"]))
        value = (await self.snapshot())[1]
        self.assertEqual(len(value["backups"]), 1)
        self.assertEqual(value["backups"][0]["filename"], "astrbot-qwenpaw-" + "a" * 32 + ".aqbackup")
        for secret in ("private-path", "/opt/private", "never publish", "/secret/raw-warning"):
            self.assertNotIn(secret, json.dumps(value))
        self.write_status(plan=self.plan(warnings={"unexpected": "object"}))
        self.assertEqual((await self.snapshot())[1]["plan"]["warnings"], [])

    async def test_arbitrary_actions_and_methods_are_rejected(self):
        for method, path, expected in (("GET", "/bundle-backup/export", 405), ("PUT", "/bundle-backup/restore", 405),
                                       ("POST", "/bundle-backup/run-shell", 404)):
            async with self.client.request(method, path, headers=self.post_headers(), data="{}") as response:
                self.assertEqual(response.status, expected)
        self.assertEqual(self.requests(), [])


if __name__ == "__main__":
    unittest.main()
