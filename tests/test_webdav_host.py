"""Authenticated host scheduling integration; no real accounts or Docker writes."""
import copy
import json
import time
import unittest
from unittest.mock import patch

from tests import test_backup_daemon as fixture
from bundle_updater.common import FileLock, atomic_json, read_json, verify
from bundle_updater.webdav_protocol import open_settings, seal_settings, valid_settings


CONFIG = {"enabled": True, "time": "03:00", "utc_offset_minutes": 480, "keep_last": 7,
          "password": "fixture-archive-password-12345", "destinations": [
              {"id": "d" * 32, "name": "Fixture", "url": "https://dav.example.invalid/backups/",
               "username": "fixture-user", "password": "fixture-app-password", "enabled": True}]}


class WebDAVHostTests(unittest.TestCase):
    setUp = fixture.BackupDaemonTests.setUp
    write_env = fixture.BackupDaemonTests.write_env
    make = fixture.BackupDaemonTests.make
    request = fixture.BackupDaemonTests.request
    finish = fixture.BackupDaemonTests.finish
    actions = fixture.BackupDaemonTests.actions

    def configured(self):
        helper = self.make()
        helper.remote.save(copy.deepcopy(CONFIG))
        class Client:
            def __init__(self, target):
                pass
            def upload(self, *args):
                return {"size": 128}
            def probe(self, *args):
                pass
            def delete(self, *args):
                pass
        helper.remote.client_factory = Client
        return helper

    def schedule(self, helper):
        helper.schedule_once()
        if helper.worker:
            helper.worker.join(timeout=5)
            self.assertFalse(helper.worker.is_alive())

    def test_settings_envelope_confidential_and_context_bound(self):
        sealed = seal_settings(CONFIG, fixture.TOKEN, "request:webdav-save")
        self.assertEqual(open_settings(sealed, fixture.TOKEN, "request:webdav-save"), CONFIG)
        self.assertNotIn(CONFIG["password"], sealed)
        for context, token in (("other:webdav-save", fixture.TOKEN), ("request:webdav-save", fixture.ROTATED_TOKEN)):
            with self.assertRaises(ValueError):
                open_settings(sealed, token, context)
        for changes in ({"keep_last": 0}, {"keep_last": True}, {"utc_offset_minutes": 900},
                        {"time": "24:00"}, {"password": "short"}, {"command": "execute"}):
            self.assertFalse(valid_settings({**CONFIG, **changes}))

    def test_save_message_never_publishes_or_persists_plaintext_secrets(self):
        helper = self.make()
        import uuid
        identifier = str(uuid.uuid4())
        sealed = seal_settings(CONFIG, helper.token, identifier + ":webdav-save")
        path, _ = self.request(helper, "webdav-save", identifier=identifier, changes={"settings": sealed})
        request_raw = path.read_text()
        state = self.finish(helper)
        self.assertEqual(state["phase"], "completed")
        self.assertTrue(state["webdav"]["config"]["password_set"])
        self.assertEqual(helper.remote.config["password"], CONFIG["password"])
        raw = request_raw + json.dumps(state) + (helper.private / "webdav/config.json").read_text()
        for secret in (CONFIG["password"], CONFIG["destinations"][0]["password"]):
            self.assertNotIn(secret, raw)

    def test_due_runs_once_and_persists_across_restart(self):
        helper = self.configured()
        helper.remote.state["next_run"] = time.time() - 3600
        helper.remote._persist()
        self.schedule(helper)
        self.assertEqual(len(self.actions("export")), 1)
        self.assertEqual(helper.state["webdav"]["last_run"]["trigger"], "scheduled")
        self.assertEqual(helper.state["webdav"]["last_run"]["status"], "completed")
        self.assertTrue((helper.ipc / "transfers" / (fixture.BACKUP_ID + ".aqbackup")).exists())
        helper.schedule_after = 0
        self.schedule(helper)
        restarted = self.make()
        self.schedule(restarted)
        self.assertEqual(len(self.actions("export")), 1)

    def test_due_waits_for_shared_update_lock_and_restore_confirmation(self):
        helper = self.configured()
        helper.remote.state["next_run"] = time.time() - 1
        with FileLock(helper.operation_lock):
            self.schedule(helper)
        self.assertEqual(self.actions("export"), [])
        self.assertTrue(helper.remote.due())
        helper.schedule_after = 0
        helper.state["plan"] = {"plan_id": fixture.PLAN_ID}
        helper.plan_expires = time.time() + 3600
        self.schedule(helper)
        self.assertEqual(self.actions("export"), [])
        helper.plan_expires = time.time() - 1
        self.schedule(helper)
        self.assertEqual(len(self.actions("export")), 1)

    def test_export_recovery_required_cannot_be_hidden_by_remote_failure_result(self):
        helper = self.configured()
        self.export_hook = lambda: atomic_json(helper.private / "transaction.json", {"phase": "stopping"})
        self.export_error = ValueError("fixture private exception")
        self.request(helper, "webdav-run")
        state = self.finish(helper)
        self.assertTrue(state["recovery_required"])
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(state["webdav"]["last_run"]["error"], "webdav_export_failed")
        self.assertNotIn("fixture private exception", json.dumps(state))

    def test_import_pauses_schedule_but_preserves_credentials(self):
        helper = self.configured()
        self.request(helper, "restore")
        state = self.finish(helper)
        self.assertEqual(state["phase"], "completed")
        self.assertFalse(state["webdav"]["config"]["enabled"])
        self.assertIsNone(state["webdav"]["next_run"])
        self.assertEqual(helper.remote.config["password"], CONFIG["password"])

    def test_status_callback_failure_does_not_prevent_updater_restart(self):
        helper = self.configured()
        with patch.object(helper.remote, "public", side_effect=RuntimeError("synthetic")):
            self.request(helper, "restore")
            self.finish(helper)
        self.assertEqual(self.updater_events, ["stop", "start"])

    def test_failed_connection_is_reported_without_fake_success(self):
        helper = self.configured()
        from bundle_updater.webdav_transport import WebDAVError
        with patch.object(helper.remote, "client_factory", side_effect=WebDAVError("authentication")):
            self.request(helper, "webdav-test", changes={"target_id": "d" * 32})
            state = self.finish(helper)
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(state["webdav"]["test_result"], {"target_id": "d" * 32, "ok": False, "error": "authentication"})

