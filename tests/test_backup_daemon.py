"""Offline backup host checks with real authenticated mailboxes and fake Docker work."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from bundle_updater import backup_daemon
from bundle_updater.backup_archive import seal_secret
from bundle_updater.common import FileLock, atomic_json, read_json, sign, verify
from deploy.prepare import env_value


TOKEN = "backup-daemon-fixture-token-" + "a" * 40
ROTATED_TOKEN = "backup-daemon-restored-token-" + "b" * 40
PASSWORD = "Offline-only-backup-password-123"
COMPONENTS = {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}
BACKUP_ID = "a" * 32
UPLOAD_ID = "b" * 32
PLAN_ID = "c" * 32


class BackupDaemonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True)
        self.project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(self.root).lower().encode()).hexdigest()[:10]
        atomic_json(self.runtime / ".combined-setup.json", {"format": 1, "mode": "fresh"})
        self.write_env(TOKEN)
        protect = patch.object(backup_daemon, "_protect_private")
        protect.start()
        self.addCleanup(protect.stop)
        self.calls = []
        self.catalog = []
        self.recover_result = None
        self.recover_error = None
        self.recover_hook = None
        self.export_error = None
        self.export_hook = None
        self.inspect_error = None
        self.restore_error = None
        self.restore_hook = None
        self.restore_result = {"state": "completed", "recovery_required": False}
        self.updater_events = []
        owner = self

        class FakeController:
            def __init__(self, root, project, context, private, report, *, runtime_root, layout):
                self.private, self.report = private, report
                owner.calls.append(("controller", root, project, context, runtime_root, layout))

            def recover(self):
                owner.calls.append(("recover",))
                if owner.recover_hook:
                    owner.recover_hook()
                if owner.recover_error:
                    raise owner.recover_error
                return owner.recover_result

            def list_backups(self):
                return owner.catalog

            def get_archive(self, identifier):
                return self.private / "archives" / (identifier + ".aqbackup")

            def _cleanup_plans(self):
                owner.calls.append(("cleanup",))

            def cleanup(self):
                self._cleanup_plans()

            def export(self, password, destination):
                owner.calls.append(("export", password, destination))
                self.report({"phase": "encrypting", "unsafe": str(owner.root) + TOKEN + PASSWORD})
                if owner.export_hook:
                    owner.export_hook()
                if owner.export_error:
                    raise owner.export_error
                archive = self.get_archive(BACKUP_ID)
                archive.parent.mkdir(exist_ok=True)
                archive.write_bytes(b"encrypted-ciphertext-test-fixture" * 4)
                entry = {"id": BACKUP_ID, "filename": "fixture.aqbackup", "size": archive.stat().st_size,
                         "created_at": time.time(), "version": "0.7.0-alpha", "archive_path": str(archive),
                         "private_env": TOKEN}
                owner.catalog[:] = [entry]
                return entry

            def inspect(self, archive, password):
                owner.calls.append(("inspect", archive, password, archive.read_bytes()))
                if owner.inspect_error:
                    raise owner.inspect_error
                return {"plan_id": PLAN_ID, "bundle_version": "0.7.0-alpha", "components": COMPONENTS,
                        "file_count": 13, "total_size": 2048, "requires_overwrite": True,
                        "warnings": ["platform_login_required", "source_must_be_stopped"]}

            def restore(self, plan_id, *, confirm_source_stopped, overwrite):
                owner.calls.append(("restore", plan_id, confirm_source_stopped, overwrite))
                if owner.restore_hook:
                    owner.restore_hook()
                if owner.restore_error:
                    raise owner.restore_error
                return owner.restore_result

        self.factory = FakeController
        stop = patch.object(backup_daemon.BackupDaemon, "_updater_stop", lambda instance: owner.updater_events.append("stop"))
        start = patch.object(backup_daemon.BackupDaemon, "_updater_start", lambda instance: owner.updater_events.append("start"))
        stop.start(); start.start()
        self.addCleanup(stop.stop); self.addCleanup(start.stop)

    def write_env(self, token):
        (self.runtime / ".env").write_text(
            "QWENPAW_RUNTIME_INTERNAL_TOKEN=" + env_value(token) + "\n"
            "PROJECT_SOURCE=" + env_value(str(self.root)) + "\n"
            "INSTALL_ROOT=" + env_value(str(self.runtime)) + "\n", encoding="utf-8")

    def make(self, initialize=True, **kwargs):
        helper = backup_daemon.BackupDaemon(self.root, self.project, "fixture-linux", controller_factory=self.factory,
                                             check_context=False, **kwargs)
        if initialize:
            helper.initialize()
        return helper

    def request(self, helper, action="export", *, identifier=None, token=None, changes=None, sealed_context=None):
        identifier = identifier or str(uuid.uuid4())
        token = token or helper.token
        body = {"id": identifier, "created": time.time(), "action": action}
        if action in {"export", "inspect"}:
            body["secret"] = seal_secret(PASSWORD, token, sealed_context or identifier + ":" + action)
        if action == "export":
            body["destination"] = "server"
        elif action == "inspect":
            body["upload_id"] = UPLOAD_ID
        elif action == "restore":
            body.update(plan_id=PLAN_ID, confirm_source_stopped=True, overwrite=True)
        if changes:
            body.update(changes)
        destination = helper.ipc / ("request-" + identifier + ".json")
        atomic_json(destination, sign(body, token), 0o644)
        return destination, identifier

    def finish(self, helper):
        helper.run_once()
        if helper.worker:
            helper.worker.join(timeout=5)
            self.assertFalse(helper.worker.is_alive(), "Offline helper job did not finish")
        return verify(read_json(helper.ipc / "status.json", 262144), helper.token)

    def actions(self, name):
        return [row for row in self.calls if row[0] == name]

    def assert_no_shared_secrets(self, helper):
        for name in ("status.json",):
            raw = (helper.ipc / name).read_text(encoding="utf-8")
            for private in (TOKEN, ROTATED_TOKEN, PASSWORD, str(self.root), "archive_path", "private_env"):
                self.assertNotIn(private, raw)

    def test_prepare_enforces_installation_and_context(self):
        with patch.object(backup_daemon, "_local_context") as checker:
            helper = backup_daemon.BackupDaemon(self.root, self.project, "fixture-linux", controller_factory=self.factory)
        checker.assert_called_once_with("fixture-linux")
        self.assertEqual(helper.operation_lock, self.runtime / "updates/private/operation.lock")
        self.assertTrue((helper.ipc / "uploads").is_dir())
        self.assertTrue((helper.ipc / "transfers").is_dir())
        self.assertTrue(helper.private.is_dir())

    def test_prepare_rejects_foreign_project_external_mode_and_paths(self):
        with self.assertRaises(ValueError):
            backup_daemon.prepare(self.root, "unrelated-project", "fixture", check_context=False)
        atomic_json(self.runtime / ".combined-setup.json", {"format": 1, "mode": "addon"})
        with self.assertRaises(ValueError):
            self.make()
        atomic_json(self.runtime / ".combined-setup.json", {"format": 1, "mode": "fresh"})
        (self.runtime / ".env").write_text("PROJECT_SOURCE=/foreign\nINSTALL_ROOT=/other\nQWENPAW_RUNTIME_INTERNAL_TOKEN=" + TOKEN)
        with self.assertRaises(ValueError):
            self.make()

    def test_signed_sealed_export_publishes_only_public_catalog_and_ciphertext(self):
        helper = self.make()
        request, identifier = self.request(helper)
        self.assertNotIn(PASSWORD, request.read_text())
        state = self.finish(helper)
        self.assertEqual(self.actions("export"), [("export", PASSWORD, "server")])
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["request_id"], identifier)
        self.assertFalse(state["active"])
        self.assertEqual(set(state["backups"][0]), {"id", "filename", "size", "created_at", "version"})
        self.assertEqual(state["result"], {"kind": "export", "backup_id": BACKUP_ID, "filename": "fixture.aqbackup"})
        self.assertEqual((helper.ipc / "transfers" / (BACKUP_ID + ".aqbackup")).read_bytes(), b"encrypted-ciphertext-test-fixture" * 4)
        self.assertFalse(request.exists())
        self.assert_no_shared_secrets(helper)

    def test_inspection_copies_into_private_storage_then_removes_both_uploads(self):
        helper = self.make()
        uploaded = helper.ipc / "uploads" / (UPLOAD_ID + ".aqbackup")
        uploaded.write_bytes(b"encrypted-input-fixture")
        self.request(helper, "inspect")
        state = self.finish(helper)
        inspected = self.actions("inspect")[0]
        self.assertEqual(inspected[1], helper.private / "uploads" / (UPLOAD_ID + ".aqbackup"))
        self.assertEqual(inspected[2:], (PASSWORD, b"encrypted-input-fixture"))
        self.assertFalse(uploaded.exists())
        self.assertFalse(inspected[1].exists())
        self.assertEqual(state["phase"], "ready_to_restore")
        self.assertEqual(state["plan"], {"plan_id": PLAN_ID, "version": "0.7.0-alpha", "components": COMPONENTS,
                                        "files": 13, "bytes": 2048, "target_has_data": True,
                                        "warnings": ["platform_login_required", "source_must_be_stopped"], "path_check": "passed"})
        self.assertEqual(self.actions("restore"), [])
        self.assert_no_shared_secrets(helper)

    def test_failed_inspection_removes_shared_and_private_uploads(self):
        helper = self.make()
        uploaded = helper.ipc / "uploads" / (UPLOAD_ID + ".aqbackup")
        uploaded.write_bytes(b"encrypted-input-fixture")
        self.inspect_error = ValueError("secret diagnostic " + PASSWORD + TOKEN)
        self.request(helper, "inspect")
        state = self.finish(helper)
        self.assertEqual(state["phase"], "failed")
        self.assertFalse(uploaded.exists())
        self.assertFalse((helper.private / "uploads" / uploaded.name).exists())
        self.assert_no_shared_secrets(helper)

    def test_restore_rotates_status_signing_key_and_restarts_updater(self):
        helper = self.make()
        self.restore_hook = lambda: self.write_env(ROTATED_TOKEN)
        self.request(helper, "restore")
        state = self.finish(helper)
        self.assertEqual(self.actions("restore"), [("restore", PLAN_ID, True, True)])
        self.assertEqual(helper.token, ROTATED_TOKEN)
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["result"], {"kind": "restore"})
        self.assertEqual(self.updater_events, ["stop", "start"])
        with self.assertRaises(ValueError):
            verify(read_json(helper.ipc / "status.json"), TOKEN)
        self.assert_no_shared_secrets(helper)
        self.request(helper, token=TOKEN)
        self.finish(helper)
        self.assertEqual(self.actions("export"), [])
        self.request(helper, token=ROTATED_TOKEN)
        self.finish(helper)
        self.assertEqual(len(self.actions("export")), 1)

    def test_restore_manual_recovery_blocks_future_jobs_and_updater_restart(self):
        helper = self.make()
        self.restore_result = {"state": "failed", "error": "manual_recovery_required", "recovery_required": True}
        self.request(helper, "restore")
        state = self.finish(helper)
        self.assertTrue(state["recovery_required"])
        self.assertEqual(self.updater_events, ["stop"])
        self.request(helper)
        self.finish(helper)
        self.assertEqual(self.actions("export"), [])

    def test_unexpected_restore_exception_latches_recovery_and_keeps_updater_stopped(self):
        helper = self.make()
        self.restore_hook = lambda: atomic_json(helper.private / "transaction.json", {"kind": "restore", "phase": "installing"})
        self.restore_error = RuntimeError("interrupted " + PASSWORD + TOKEN)
        self.request(helper, "restore")
        state = self.finish(helper)
        self.assertEqual(state["phase"], "failed")
        self.assertTrue(state["recovery_required"], "Unexpected restore failures must require recovery before another mutation")
        self.assertEqual(self.updater_events, ["stop"])
        self.request(helper)
        self.finish(helper)
        self.assertEqual(self.actions("export"), [])
        self.assert_no_shared_secrets(helper)

    def test_completed_restore_updater_restart_error_is_fixed_code(self):
        helper = self.make()
        with patch.object(helper, "_updater_start", side_effect=RuntimeError("private " + TOKEN + PASSWORD)):
            self.request(helper, "restore")
            state = self.finish(helper)
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["error"], "updater_restart_failed")
        self.assert_no_shared_secrets(helper)

    def test_invalid_signatures_stamps_payloads_and_actions_never_execute(self):
        helper = self.make()
        options = [
            {"token": "incorrect-token-" + "z" * 40},
            {"changes": {"created": time.time() - 121}}, {"changes": {"created": time.time() + 6}},
            {"changes": {"created": True}}, {"changes": {"root": "/foreign"}},
            {"changes": {"destination": "https://attacker.invalid"}}, {"changes": {"secret": 7}},
            {"changes": {"secret": "x" * 4097}}, {"action": "shell"},
            {"action": "inspect", "changes": {"upload_id": "../escape"}},
            {"action": "restore", "changes": {"plan_id": "../escape"}},
            {"action": "restore", "changes": {"confirm_source_stopped": False}},
            {"action": "restore", "changes": {"overwrite": "yes"}},
        ]
        for option in options:
            with self.subTest(option=option):
                item, _ = self.request(helper, **option)
                self.finish(helper)
                self.assertFalse(item.exists())
        self.assertEqual(self.actions("export"), [])
        self.assertEqual(self.actions("inspect"), [])
        self.assertEqual(self.actions("restore"), [])

    def test_password_ciphertext_bound_to_request_id_and_action(self):
        helper = self.make()
        for context in (str(uuid.uuid4()) + ":export", str(uuid.uuid4()) + ":inspect"):
            self.request(helper, sealed_context=context)
            state = self.finish(helper)
            self.assertEqual(state["phase"], "failed")
            self.assertEqual(state["error"], "secret_invalid")
        self.assertEqual(self.actions("export"), [])
        self.assert_no_shared_secrets(helper)

    def test_replay_rejected_across_helper_restarts(self):
        helper = self.make()
        _, identifier = self.request(helper)
        self.finish(helper)
        self.assertEqual(len(self.actions("export")), 1)
        restarted = self.make()
        self.request(restarted, identifier=identifier)
        self.finish(restarted)
        self.assertEqual(len(self.actions("export")), 1)

    def test_busy_request_is_consumed_and_never_replayed_after_first_job_finishes(self):
        helper = self.make()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold():
            started.set()
            release.wait(timeout=5)
        self.export_hook = hold
        self.request(helper)
        helper.run_once()
        self.assertTrue(started.wait(timeout=2))
        second, identifier = self.request(helper)
        helper.run_once()
        self.assertFalse(second.exists())
        self.assertTrue(helper.state["active"])
        release.set()
        helper.worker.join(timeout=5)
        self.export_hook = None
        self.request(helper, identifier=identifier)
        self.finish(helper)
        self.assertEqual(len(self.actions("export")), 1)

    def test_cross_operation_lock_blocks_backup_while_update_holds_it(self):
        helper = self.make()
        with FileLock(helper.operation_lock):
            self.request(helper)
            state = self.finish(helper)
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(self.actions("export"), [])

    def test_pending_update_recovery_blocks_backup(self):
        helper = self.make()
        atomic_json(self.runtime / "updates/private/transaction.json", {"phase": "installing"})
        self.request(helper)
        state = self.finish(helper)
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(self.actions("export"), [])

    def test_startup_recovery_refreshes_token_without_replaying_user_jobs(self):
        self.recover_result = {"state": "rolled_back", "recovery_required": False}
        self.recover_hook = lambda: self.write_env(ROTATED_TOKEN)
        helper = self.make()
        self.assertEqual(helper.token, ROTATED_TOKEN)
        state = verify(read_json(helper.ipc / "status.json"), ROTATED_TOKEN)
        self.assertEqual(state["phase"], "rolled_back")
        self.assertTrue(state["ready"])
        self.assertFalse(state["active"])
        self.assertEqual(self.actions("export") + self.actions("inspect") + self.actions("restore"), [])
        self.assert_no_shared_secrets(helper)

    def test_pending_restore_startup_stops_old_updater_and_restarts_on_recovered_key(self):
        private = self.runtime / "backups/private"
        private.mkdir(parents=True)
        atomic_json(private / "transaction.json", {"kind": "restore", "phase": "installing"})
        self.recover_result = {"state": "rolled_back", "recovery_required": False}
        self.recover_hook = lambda: self.write_env(ROTATED_TOKEN)
        helper = self.make()
        self.assertEqual(helper.token, ROTATED_TOKEN)
        self.assertEqual(self.updater_events, ["stop", "start"], "Startup restore recovery must coordinate the updater just like normal restore")

    def test_startup_recovery_error_exposes_no_exception_and_blocks_jobs(self):
        self.recover_error = RuntimeError(str(self.root) + TOKEN + PASSWORD)
        helper = self.make()
        self.assertTrue(helper.state["ready"])
        self.assertTrue(helper.state["recovery_required"])
        self.request(helper)
        self.finish(helper)
        self.assertEqual(self.actions("export"), [])
        self.assert_no_shared_secrets(helper)
        private_error = (helper.private / "last-error.json").read_text()
        self.assertNotIn(PASSWORD, private_error)
        self.assertNotIn(TOKEN, private_error)

    def test_failure_code_sanitization_never_persists_exception_text(self):
        helper = self.make()
        error = RuntimeError("private " + PASSWORD)
        error.code = "bad code/" + TOKEN
        helper.fail(error)
        self.assertEqual(helper.state["error"], "backup_failed")
        self.assert_no_shared_secrets(helper)
        self.assertEqual(set(read_json(helper.private / "last-error.json")), {"time", "code"})

    def test_stop_request_accepts_idle_and_is_signed(self):
        helper = self.make()
        identifier = str(uuid.uuid4())
        stop = helper.private / "stop.json"
        atomic_json(stop, sign({"id": identifier, "created": time.time()}, helper.token))
        helper.run_once()
        self.assertTrue(helper.exit_requested)
        self.assertFalse(stop.exists())
        self.assertEqual(verify(read_json(helper.private / "stop-result.json"), helper.token), {"id": identifier, "accepted": True})

    def test_stop_request_rejects_active_job(self):
        helper = self.make()
        helper.publish(active=True)
        identifier = str(uuid.uuid4())
        atomic_json(helper.private / "stop.json", sign({"id": identifier, "created": time.time()}, helper.token))
        helper.run_once()
        self.assertFalse(helper.exit_requested)
        self.assertEqual(verify(read_json(helper.private / "stop-result.json"), helper.token), {"id": identifier, "accepted": False})

    def test_malformed_stop_request_does_not_crash_service(self):
        helper = self.make()
        path = helper.private / "stop.json"
        cases = [sign({"id": str(uuid.uuid4()), "created": "not-a-time"}, helper.token),
                 sign({"id": 17, "created": time.time()}, helper.token),
                 sign({"id": str(uuid.uuid4()), "created": time.time()}, "wrong-key")]
        for envelope in cases:
            with self.subTest(envelope=envelope):
                atomic_json(path, envelope)
                helper.run_once()
                self.assertFalse(path.exists())
                self.assertFalse(helper.exit_requested)

    def test_status_rejects_stale_future_tampered_and_boolean_heartbeat(self):
        helper = self.make()
        for stamp in (time.time() - 21, time.time() + 1, True):
            atomic_json(helper.ipc / "status.json", sign({"heartbeat": stamp}, helper.token))
            self.assertIsNone(backup_daemon.status(helper.ipc, helper.token))
        atomic_json(helper.ipc / "status.json", sign({"heartbeat": time.time()}, "wrong-key"))
        self.assertIsNone(backup_daemon.status(helper.ipc, helper.token))
        helper.publish()
        self.assertIsNotNone(backup_daemon.status(helper.ipc, helper.token))

    def test_serve_survives_controller_initialization_failure_without_cleanup_crash(self):
        helper = self.make(initialize=False)
        def finish_loop(_seconds):
            helper.exit_requested = True
        with patch.object(helper, "make_controller", side_effect=RuntimeError("fixture failed " + PASSWORD)), \
                patch.object(backup_daemon.time, "sleep", side_effect=finish_loop):
            helper.serve()
        self.assertEqual(helper.state["phase"], "failed")
        self.assertTrue(helper.state["recovery_required"])
        self.assertFalse(helper.state["ready"])
        self.assert_no_shared_secrets(helper)

    def test_serve_runs_cleanup_and_stops_without_mutating_user_jobs(self):
        helper = self.make(initialize=False)
        def finish_loop(_seconds):
            helper.exit_requested = True
        with patch.object(backup_daemon.time, "sleep", side_effect=finish_loop):
            helper.serve()
        self.assertEqual(len(self.actions("cleanup")), 1)
        self.assertEqual(self.actions("export") + self.actions("inspect") + self.actions("restore"), [])

    def test_lifecycle_ensure_launches_fixed_host_command_without_secrets(self):
        helper = self.make()
        commands = []
        def spawn(command, **options):
            commands.append((command, options))
        def current(_ipc, _token):
            instance = commands[0][0][-1]
            return {"ready": True, "active": False, "instance": instance}
        with patch.object(backup_daemon, "prepare", return_value=(self.root, self.runtime, helper.ipc, helper.private, TOKEN)), \
                patch.object(backup_daemon, "running", return_value=False), \
                patch.object(backup_daemon.subprocess, "Popen", side_effect=spawn), \
                patch.object(backup_daemon, "status", side_effect=current):
            backup_daemon.lifecycle(self.root, self.project, "fixture-linux")
        self.assertEqual(len(commands), 1)
        command, options = commands[0]
        self.assertEqual(command[1], str(self.root / "scripts/run_bundle_backup.py"))
        self.assertIn("--serve", command)
        self.assertIn(str(self.runtime), command)
        self.assertFalse(options["shell"])
        self.assertEqual(options["cwd"], self.root)
        self.assertNotIn(TOKEN, " ".join(command))
        self.assertNotIn(PASSWORD, " ".join(command))
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & backup_daemon.subprocess.CREATE_NO_WINDOW)

    def test_lifecycle_ensure_existing_recovery_required_does_not_launch_or_clear_failure(self):
        helper = self.make()
        with patch.object(backup_daemon, "prepare", return_value=(self.root, self.runtime, helper.ipc, helper.private, TOKEN)), \
                patch.object(backup_daemon, "running", return_value=True), \
                patch.object(backup_daemon, "status", return_value={"ready": True, "active": False, "recovery_required": True}), \
                patch.object(backup_daemon.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError):
                backup_daemon.lifecycle(self.root, self.project, "fixture-linux")
        spawn.assert_not_called()

    def test_lifecycle_stop_refuses_unavailable_or_active_status(self):
        helper = self.make()
        for state in (None, {"active": True}):
            with self.subTest(state=state), \
                    patch.object(backup_daemon, "prepare", return_value=(self.root, self.runtime, helper.ipc, helper.private, TOKEN)), \
                    patch.object(backup_daemon, "running", return_value=True), \
                    patch.object(backup_daemon, "status", return_value=state):
                with self.assertRaises(ValueError):
                    backup_daemon.lifecycle(self.root, self.project, "fixture-linux", stop=True)
        self.assertFalse((helper.private / "stop.json").exists())

    def test_lifecycle_stop_uses_signed_host_request(self):
        helper = self.make()
        with patch.object(backup_daemon, "prepare", return_value=(self.root, self.runtime, helper.ipc, helper.private, TOKEN)), \
                patch.object(backup_daemon, "running", side_effect=[True, False]), \
                patch.object(backup_daemon, "status", return_value={"ready": True, "active": False}):
            backup_daemon.lifecycle(self.root, self.project, "fixture-linux", stop=True)
        request = verify(read_json(helper.private / "stop.json"), TOKEN)
        self.assertEqual(set(request), {"id", "created"})
        self.assertRegex(request["id"], backup_daemon.UUID)
        self.assertLess(abs(time.time() - request["created"]), 5)


if __name__ == "__main__":
    unittest.main()
