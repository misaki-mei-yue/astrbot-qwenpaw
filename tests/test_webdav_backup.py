import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from bundle_updater.webdav_backup import WebDAVBackups, WebDAVBackupError


class RemoteError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("private password url must never be public")


class Controller:
    def __init__(self, root):
        self.root, self.calls, self.deleted, self.archives = root, [], [], {}
        self.error = False

    def export(self, password, destination):
        self.calls.append((password, destination))
        if self.error:
            raise RuntimeError("model key or private file path")
        backup_id = uuid.uuid4().hex
        path = self.root / (backup_id + ".aqbackup")
        path.write_bytes(b"synthetic encrypted archive")
        self.archives[backup_id] = path
        return {"id": backup_id}

    def get_archive(self, backup_id):
        path = self.archives[backup_id]
        if not path.exists():
            raise FileNotFoundError()
        return path

    def delete_archive(self, backup_id):
        self.archives.pop(backup_id).unlink()
        self.deleted.append(backup_id)


class Clients:
    def __init__(self):
        self.uploads, self.probes, self.deletes = [], [], []
        self.failures, self.delete_error, self.probe_error = {}, False, None
        self.crash = False

    def __call__(self, target):
        owner = self
        class Client:
            def probe(self, namespace):
                owner.probes.append((target["id"], namespace))
                if owner.probe_error:
                    raise RemoteError(owner.probe_error)

            def upload(self, path, namespace, filename):
                owner.uploads.append((target["id"], path, namespace, filename))
                if owner.crash:
                    raise KeyboardInterrupt()
                errors = owner.failures.setdefault(target["id"], [])
                if errors:
                    code = errors.pop(0)
                    if code:
                        raise RemoteError(code)
                return {"filename": filename, "size": path.stat().st_size}

            def delete(self, namespace, filename):
                owner.deletes.append((target["id"], namespace, filename))
                if owner.delete_error:
                    raise RemoteError("permission")
        return Client()


class WebDAVBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller, self.clients = Controller(self.root), Clients()
        self.now = datetime(2026, 10, 24, 18, 50, tzinfo=timezone.utc).timestamp()
        self.events = []
        self.worker = self.restart()
        self.first, self.second = "a" * 32, "b" * 32

    def restart(self):
        worker = WebDAVBackups(self.root / "private", self.controller, report=self.events.append,
                               client_factory=self.clients, clock=lambda: self.now)
        worker._sleep = lambda _: None
        return worker

    def config(self, **changes):
        config = {"enabled": True, "time": "03:00", "utc_offset_minutes": 480, "keep_last": 2,
                  "password": "correct horse 1234", "destinations": [
                      {"id": self.first, "name": "网盘一", "url": "https://dav.example/first", "username": "user1", "password": "secret-one", "enabled": True},
                      {"id": self.second, "name": "网盘二", "url": "https://dav.example/second", "username": "user2", "password": "secret-two", "enabled": True}]}
        config.update(changes)
        return config

    def test_disabled_by_default_and_host_key_persistent(self):
        self.assertFalse(self.worker.public()["config"]["enabled"])
        self.assertIsNone(self.worker.public()["next_run"])
        self.assertFalse(self.worker.due())
        self.assertEqual(self.worker.key, self.restart().key)
        self.assertEqual(self.worker.public()["config"]["namespace"], self.restart().public()["config"]["namespace"])

    def test_passwords_encrypted_and_never_in_public_state(self):
        self.worker.save(self.config())
        state = json.dumps(self.worker.public())
        disk = b"".join(path.read_bytes() for path in (self.root / "private/webdav").iterdir())
        for secret in ("correct horse 1234", "secret-one", "secret-two"):
            self.assertNotIn(secret, state)
            self.assertNotIn(secret.encode(), disk)
        self.assertTrue(self.worker.public()["config"]["password_set"])
        restarted = self.restart()
        restarted.run()
        self.assertEqual(self.controller.calls[0], ("correct horse 1234", "server"))

    def test_missing_or_tampered_key_fails_closed(self):
        self.worker.save(self.config())
        key = self.root / "private/webdav/key"
        key.write_bytes(b"z" * 32)
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_state_invalid"):
            self.restart()
        key.unlink()
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_key_missing"):
            self.restart()

    def test_empty_passwords_keep_existing_and_rotating_password_is_supported(self):
        self.worker.save(self.config())
        changed = self.config(password="")
        for row in changed["destinations"]:
            row["password"] = ""
        self.worker.save(changed)
        self.assertEqual(self.restart().config["password"], "correct horse 1234")
        self.assertEqual(self.worker.config["destinations"][0]["password"], "secret-one")
        changed["destinations"][0]["password"] = "rotated"
        self.worker.save(changed)
        self.assertEqual(self.worker.config["destinations"][0]["password"], "rotated")

    def test_enable_save_recovers_when_config_write_fails_after_state_write(self):
        original = self.worker._save
        def fail_config(name, value):
            if name == "config":
                raise OSError("simulated full disk")
            return original(name, value)
        self.worker._save = fail_config
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_settings_pending"):
            self.worker.save(self.config())
        intent = self.root / "private/webdav/settings-update.json"
        self.assertTrue(intent.exists())
        self.assertNotIn(b"secret-one", intent.read_bytes())
        self.assertFalse(self.worker.due())
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_settings_pending"):
            self.worker.run()
        restarted = self.restart()
        self.assertTrue(restarted.config["enabled"])
        self.assertIsNotNone(restarted.state["next_run"])
        self.assertFalse(intent.exists())
        self.now = restarted.state["next_run"]
        self.assertTrue(restarted.due())
        self.assertEqual(restarted.run("scheduled")["status"], "completed")

    def test_pause_save_recovers_without_losing_disabled_schedule(self):
        self.worker.save(self.config())
        original = self.worker._save
        old_config = copy.deepcopy(self.worker.config)
        def fail_config(name, value):
            if name == "config":
                raise OSError("simulated interrupted write")
            return original(name, value)
        self.worker._save = fail_config
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_settings_pending"):
            self.worker.pause()
        self.now += 86400
        self.assertFalse(self.worker.due())
        restarted = self.restart()
        self.assertFalse(restarted.config["enabled"])
        self.assertIsNone(restarted.state["next_run"])
        self.assertEqual(restarted.config["destinations"], old_config["destinations"])
        self.assertFalse(restarted.due())

    def test_save_intent_recovers_when_first_state_write_fails(self):
        original = self.worker._save
        def fail_state(name, value):
            if name == "state":
                raise OSError("simulated interruption")
            return original(name, value)
        self.worker._save = fail_state
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_settings_pending"):
            self.worker.save(self.config())
        restarted = self.restart()
        self.assertTrue(restarted.config["enabled"])
        self.assertIsNotNone(restarted.state["next_run"])

    def test_invalid_intent_rejected_before_overwriting_committed_files(self):
        self.worker.save(self.config())
        path = self.root / "private/webdav/config.json"
        original = path.read_bytes()
        bad = {"format": 1, "config": copy.deepcopy(self.worker.config), "state": copy.deepcopy(self.worker.state)}
        bad["state"]["next_run"] = None
        self.worker._save("settings-update", bad)
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_state_invalid"):
            self.restart()
        self.assertEqual(path.read_bytes(), original)
        bad["state"] = copy.deepcopy(self.worker.state)
        bad["extra"] = True
        self.worker._save("settings-update", bad)
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_state_invalid"):
            self.restart()
        self.assertEqual(path.read_bytes(), original)

    def test_lone_pending_intent_without_key_is_not_replaced(self):
        self.worker.save(self.config())
        self.worker._save("settings-update", {"format": 1, "config": self.worker.config, "state": self.worker.state})
        for name in ("key", "config.json", "state.json"):
            (self.root / "private/webdav" / name).unlink()
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_key_missing"):
            self.restart()
        self.assertFalse((self.root / "private/webdav/key").exists())

    def test_later_save_completes_pending_transaction_before_applying_new_settings(self):
        original = self.worker._save
        def fail_config(name, value):
            if name == "config":
                raise OSError("simulated interruption")
            return original(name, value)
        self.worker._save = fail_config
        with self.assertRaises(WebDAVBackupError):
            self.worker.save(self.config())
        self.worker._save = original
        self.worker.save(self.config(time="04:00"))
        restarted = self.restart()
        self.assertEqual(restarted.config["time"], "04:00")
        self.assertTrue(restarted.config["enabled"])
        self.assertEqual(self.worker.state["next_run"], restarted.state["next_run"])

    def test_invalid_configuration_does_not_change_saved_config(self):
        self.worker.save(self.config())
        before = self.worker.public()
        invalid = [self.config(time="24:00"), self.config(utc_offset_minutes=True), self.config(keep_last=0),
                   self.config(password="short"), self.config(destinations=[]), self.config(destinations=[self.config()["destinations"][0]] * 2),
                   self.config(destinations=self.config()["destinations"] * 6)]
        for change in ({"url": "http://dav.example/"}, {"url": "https://user:pass@dav.example/"},
                       {"url": "https://dav.example/#secret"}, {"url": "https://dav.example/?token=x"},
                       {"url": "https://dav.example:bad"}, {"url": "https://dav.example/", "password": ""},
                       {"username": "x" * 513}, {"password": "x" * 2049}, {"enabled": 1}):
            config = self.config()
            config["destinations"][0].update(change)
            invalid.append(config)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(WebDAVBackupError):
                self.worker.save(config)
        self.assertEqual(before, self.worker.public())

    def test_one_export_for_all_targets(self):
        self.worker.save(self.config())
        result = self.worker.run()
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(len(self.clients.uploads), 2)
        self.assertEqual(self.clients.uploads[0][1:], self.clients.uploads[1][1:])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([row["status"] for row in result["targets"]], ["completed", "completed"])
        self.assertNotIn("identities", result)

    def test_target_failure_does_not_block_other_destination(self):
        self.worker.save(self.config())
        self.clients.failures[self.first] = ["authentication"]
        result = self.worker.run()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["targets"][0]["error"], "authentication")
        self.assertEqual(result["targets"][1]["status"], "completed")
        self.assertNotIn("private password", json.dumps(self.worker.public()))

    def test_transient_retries_are_bounded_and_permanent_failures_not_retried(self):
        self.worker.save(self.config())
        self.clients.failures[self.first] = ["timeout"] * 5
        self.clients.failures[self.second] = ["storage_full"] * 5
        result = self.worker.run()
        self.assertEqual([row["attempts"] for row in result["targets"]], [3, 1])
        self.assertEqual(len(self.clients.uploads), 4)
        self.assertEqual(result["status"], "failed")

    def test_transient_recovery_succeeds_without_second_export(self):
        self.worker.save(self.config())
        self.clients.failures[self.first] = ["connection", "remote_unavailable"]
        result = self.worker.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["targets"][0]["attempts"], 3)
        self.assertEqual(len(self.controller.calls), 1)

    def test_retry_only_failed_targets_reuses_existing_archive(self):
        self.worker.save(self.config())
        self.clients.failures[self.first] = ["authentication"]
        result = self.worker.run()
        self.clients.uploads.clear()
        retried = self.worker.retry(result["id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual([row[0] for row in self.clients.uploads], [self.first])
        self.assertEqual(retried["backup_id"], result["backup_id"])

    def test_retry_missing_archive_never_reexports(self):
        self.worker.save(self.config())
        self.clients.failures[self.first] = ["authentication"]
        result = self.worker.run()
        self.controller.archives[result["backup_id"]].unlink()
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_archive_unavailable"):
            self.worker.retry(result["id"])
        self.assertEqual(len(self.controller.calls), 1)

    def test_changed_target_needs_new_credentials_and_loses_old_deletion_ownership(self):
        self.worker.save(self.config())
        self.worker.run()
        self.clients.failures[self.first] = ["authentication"]
        failed = self.worker.run()
        changed = self.config()
        changed["destinations"][0].update(url="https://another.example/dav", password="")
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_credentials_required"):
            self.worker.save(changed)
        changed["destinations"][0]["password"] = "new-account-secret"
        self.worker.save(changed)
        self.assertNotIn(self.first, self.worker.state["receipts"])
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_target_changed"):
            self.worker.retry(failed["id"])

    def test_schedule_uses_fixed_offset_and_persists_across_restarts(self):
        self.worker.save(self.config())
        expected = datetime(2026, 10, 24, 19, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.worker.public()["next_run"], expected)
        self.assertFalse(self.worker.due())
        self.now = expected
        self.worker = self.restart()
        self.assertTrue(self.worker.due())
        self.worker.run("scheduled")
        self.assertEqual(self.worker.public()["next_run"], expected + 86400)
        self.assertFalse(self.restart().due())

    def test_missed_days_run_only_once_and_schedule_change_updates_next_time(self):
        self.worker.save(self.config())
        self.now += 5 * 86400
        self.worker = self.restart()
        self.worker.run("scheduled")
        self.assertFalse(self.worker.due())
        self.assertEqual(len(self.controller.calls), 1)
        self.worker.save(self.config(time="04:00"))
        expected = datetime.fromtimestamp(self.now, timezone.utc).replace(hour=20, minute=0)
        self.assertEqual(self.worker.public()["next_run"], expected.timestamp())
        self.worker.save(self.config(enabled=False))
        self.assertIsNone(self.worker.public()["next_run"])
        self.assertFalse(self.worker.due())

    def test_schedule_failed_export_does_not_immediately_retry(self):
        self.worker.save(self.config())
        self.now = self.worker.public()["next_run"]
        self.controller.error = True
        result = self.worker.run("scheduled")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "webdav_export_failed")
        self.assertEqual(self.clients.uploads, [])
        self.assertFalse(self.restart().due())

    def test_interrupted_upload_is_retryable_after_restart_without_export(self):
        self.worker.save(self.config())
        self.now = self.worker.public()["next_run"]
        self.clients.crash = True
        with self.assertRaises(KeyboardInterrupt):
            self.worker.run("scheduled")
        self.clients.crash = False
        self.worker = self.restart()
        result = self.worker.public()["last_run"]
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(self.worker.due())
        self.assertEqual(self.worker.retry(result["id"])["status"], "completed")
        self.assertEqual(len(self.controller.calls), 1)

    def test_retention_only_removes_owned_complete_archives_and_remote_receipts(self):
        manual = self.controller.export("manual password 1234", "server")["id"]
        self.worker.save(self.config(keep_last=1))
        first = self.worker.run()["backup_id"]
        self.clients.failures[self.first] = ["permission"]
        failed = self.worker.run()["backup_id"]
        last = self.worker.run()["backup_id"]
        self.assertEqual(self.controller.deleted, [first])
        self.assertIn(manual, self.controller.archives)
        self.assertIn(failed, self.controller.archives)
        self.assertIn(last, self.controller.archives)
        for target, namespace, filename in self.clients.deletes:
            self.assertEqual(namespace, self.worker.config["namespace"])
            self.assertIn(filename, {first + ".aqbackup", failed + ".aqbackup"})
            self.assertNotEqual(filename, manual + ".aqbackup")

    def test_remote_cleanup_failure_keeps_receipt_and_success_status(self):
        self.worker.save(self.config(keep_last=1))
        first = self.worker.run()["backup_id"]
        self.clients.delete_error = True
        second = self.worker.run()
        self.assertEqual(second["status"], "completed")
        self.assertIn(first + ".aqbackup", self.worker.state["receipts"][self.first])
        self.clients.delete_error = False
        self.worker.run()
        self.assertEqual(len(self.worker.state["receipts"][self.first]), 1)

    def test_test_target_does_not_export_and_redacts_unknown_errors(self):
        self.worker.save(self.config())
        self.clients.probe_error = "leaked provider secret"
        self.assertEqual(self.worker.test(self.first), {"target_id": self.first, "ok": False, "error": "webdav_operation_failed"})
        self.assertEqual(self.controller.calls, [])
        self.clients.probe_error = None
        self.assertTrue(self.worker.test(self.first)["ok"])

    def test_disabled_target_skipped_and_manual_run_allowed_without_schedule(self):
        config = self.config(enabled=False)
        config["destinations"][1]["enabled"] = False
        self.worker.save(config)
        result = self.worker.run()
        self.assertEqual(len(result["targets"]), 1)
        self.assertIsNone(self.worker.public()["next_run"])

    def test_restore_pause_is_durable_and_preserves_target_credentials(self):
        self.worker.save(self.config())
        old = copy.deepcopy(self.worker.config)
        self.worker.pause()
        restarted = self.restart()
        self.assertFalse(restarted.config["enabled"])
        self.assertIsNone(restarted.public()["next_run"])
        self.assertEqual(restarted.config["destinations"], old["destinations"])
        self.assertEqual(restarted.config["password"], old["password"])
        self.assertEqual(restarted.config["namespace"], old["namespace"])
        self.now += 10 * 86400
        self.assertFalse(restarted.due())

    def test_failed_uploads_rotate_only_after_new_valid_export_and_keep_latest(self):
        self.worker.save(self.config(keep_last=2))
        self.clients.failures[self.first] = ["authentication"] * 6
        results = [self.worker.run() for _ in range(4)]
        self.assertEqual(len(self.controller.calls), 4)
        self.assertEqual(self.controller.deleted, [row["backup_id"] for row in results[:2]])
        self.assertEqual({row["id"] for row in self.worker.state["local"]}, {row["backup_id"] for row in results[2:]})
        self.controller.error = True
        self.worker.run()
        self.assertEqual(len(self.controller.deleted), 2)
        self.assertEqual(len(self.worker.state["local"]), 2)

    def test_local_successful_and_unfinished_sets_have_independent_retention(self):
        self.worker.save(self.config(keep_last=2))
        successful = [self.worker.run()["backup_id"] for _ in range(3)]
        self.clients.failures[self.first] = ["authentication"] * 3
        unfinished = [self.worker.run()["backup_id"] for _ in range(3)]
        kept = {row["id"] for row in self.worker.state["local"]}
        self.assertEqual(kept, set(successful[1:] + unfinished[1:]))
        self.assertEqual(len(kept), 4)
        self.assertEqual(self.controller.deleted, [successful[0], unfinished[0]])

    def test_invalid_new_archive_does_not_remove_last_failure_copy(self):
        self.worker.save(self.config(keep_last=1))
        self.clients.failures[self.first] = ["authentication"]
        first = self.worker.run()["backup_id"]
        original = self.controller.get_archive
        def reject_new(backup_id):
            if backup_id != first:
                raise ValueError("corrupt archive")
            return original(backup_id)
        self.controller.get_archive = reject_new
        result = self.worker.run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.controller.deleted, [])
        self.assertIn(first, self.controller.archives)

    def test_percent_encoded_url_limit_is_checked_before_persisting(self):
        config = self.config()
        config["destinations"][0]["url"] = "https://dav.example/" + "中" * 230
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_url_invalid"):
            self.worker.save(config)
        self.assertEqual(self.restart().config["destinations"], [])

    def test_recent_history_bounded_and_foreign_retry_refused(self):
        self.worker.save(self.config())
        for _ in range(22):
            self.worker.run()
        self.assertEqual(len(self.worker.public()["history"]), 20)
        with self.assertRaisesRegex(WebDAVBackupError, "webdav_run_unknown"):
            self.worker.retry("c" * 32)


if __name__ == "__main__":
    unittest.main()
