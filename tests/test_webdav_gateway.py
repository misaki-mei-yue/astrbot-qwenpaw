"""Real HTTP authorization and redaction of remote-backup IPC."""
import copy
import json
import unittest
from tests import test_backup_gateway as fixture
from tests.test_webdav_host import CONFIG
from bundle_updater.common import read_json, verify
from bundle_updater.webdav_protocol import open_settings


class WebDAVGatewayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.BackupGatewayTests.asyncSetUp
    asyncTearDown = fixture.BackupGatewayTests.asyncTearDown
    write_status = fixture.BackupGatewayTests.write_status
    headers = fixture.BackupGatewayTests.headers
    post_headers = fixture.BackupGatewayTests.post_headers
    snapshot = fixture.BackupGatewayTests.snapshot
    post = fixture.BackupGatewayTests.post
    requests = fixture.BackupGatewayTests.requests

    async def test_save_seals_all_settings_and_response_omits_secrets(self):
        status, raw = await self.post("webdav-save", CONFIG)
        self.assertEqual(status, 202, raw)
        request = verify(read_json(self.requests()[0], 131072), self.token)
        self.assertEqual(set(request), {"id", "created", "action", "settings"})
        self.assertEqual(open_settings(request["settings"], self.token, request["id"] + ":webdav-save"), CONFIG)
        self.assertNotIn(CONFIG["password"], raw + self.requests()[0].read_text())

    async def test_all_remote_mutations_require_portal_origin_and_csrf(self):
        for action, body in (("webdav-save", CONFIG), ("webdav-run", {}),
                             ("webdav-test", {"target_id": "a" * 32}), ("webdav-retry", {"run_id": "b" * 32})):
            for headers in (self.headers(), self.post_headers(Origin="https://foreign.example"),
                            self.post_headers(**{"X-Bundle-Backup-Token": "wrong"})):
                status, _ = await self.post(action, body, headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.requests(), [])

    async def test_remote_body_limits_and_identifier_validation(self):
        for action, body in (("webdav-run", {"shell": "execute"}), ("webdav-test", {"target_id": "../bad"}),
                             ("webdav-retry", {"run_id": "bad"}), ("webdav-save", {**CONFIG, "keep_last": 0}),
                             ("webdav-save", {**CONFIG, "destinations": CONFIG["destinations"] * 11})):
            status, _ = await self.post(action, body)
            self.assertEqual(status, 400)
        status, _ = await self.post("webdav-save", raw="x" * 65537)
        self.assertEqual(status, 400)
        self.assertEqual(self.requests(), [])

    async def test_public_projection_discards_nested_passwords_and_raw_errors(self):
        config = copy.deepcopy(CONFIG)
        config["password_set"] = True
        config["namespace"] = "astrbot-qwenpaw/" + "e" * 32
        config["destinations"][0]["password_set"] = True
        remote = {"config": config, "key": "private-key", "next_run": 2000000000,
                  "last_run": {"id": "a" * 32, "trigger": "scheduled", "status": "partial",
                      "password": "hidden", "targets": [{"id": "d" * 32, "name": "Fixture", "status": "failed",
                      "attempts": 1, "error": "https://user:secret@invalid", "authorization": "hidden"}]},
                  "test_result": {"target_id": "d" * 32, "ok": False, "error": "authentication", "password": "hidden"}}
        self.write_status(webdav=remote)
        status, public, _ = await self.snapshot()
        self.assertEqual(status, 200)
        self.assertEqual(public["webdav"]["last_run"]["trigger"], "scheduled")
        raw = json.dumps(public)
        for secret in (CONFIG["password"], CONFIG["destinations"][0]["password"], "private-key", "hidden", "user:secret"):
            self.assertNotIn(secret, raw)

