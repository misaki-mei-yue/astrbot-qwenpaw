"""Offline daemon tests: signed IPC, host isolation, readiness and recovery gates."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from bundle_updater import daemon
from bundle_updater.common import FileLock, atomic_json, read_json, sign, verify
from bundle_updater.releases import ReleaseError
from deploy.prepare import env_value
from tests.test_update_releases import COMPONENTS, VERSION, archive_bytes


TOKEN = "offline-fixture-authentication-" + "x" * 32
PROTECT_PRIVATE = daemon._protect_private


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        protection = patch.object(daemon, "_protect_private")
        protection.start()
        self.addCleanup(protection.stop)
        self.write_metadata("0.5.0-alpha")
        for relative in ("deploy/compose.bundle.yaml", "deploy/prepare.py", "scripts/run_bundle_updater.py", "bundle_updater/controller.py"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture\n", encoding="utf-8")
        package = Path(daemon.__file__).resolve().parent
        for source in package.glob("*.py"):
            target = self.root / "bundle_updater" / source.name
            target.write_bytes(source.read_bytes())
        (self.root / "deploy/prepare.py").write_bytes((package.parent / "deploy/prepare.py").read_bytes())
        runtime = self.root / "runtime"
        runtime.mkdir()
        atomic_json(runtime / ".combined-setup.json", {"format": 1, "mode": "fresh"})
        (runtime / ".env").write_text(
            "QWENPAW_RUNTIME_INTERNAL_TOKEN=" + env_value(TOKEN) + "\nPROJECT_SOURCE=" + env_value(str(self.root)) + "\n", encoding="utf-8")
        self.project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(self.root).lower().encode()).hexdigest()[:10]
        self.clock = lambda: 1000.0
        self.check_calls = []
        self.apply_calls = []
        self.recover_result = None
        self.recover_error = None
        self.apply_error = None
        self.baseline_error = None
        self.enrollment_calls = 0
        self.recover_hook = None
        self.apply_result = {"state": "completed", "version": VERSION, "recovery_required": False}
        owner = self

        class Controller:
            def __init__(self, root, project, context, private_dir, report):
                self.report = report

            def recover(self):
                if owner.recover_hook:
                    owner.recover_hook()
                if owner.recover_error:
                    raise owner.recover_error
                return owner.recover_result

            def ensure_baseline(self):
                owner.enrollment_calls += 1
                if owner.baseline_error:
                    raise owner.baseline_error

            def apply(self, candidate):
                owner.apply_calls.append(candidate)
                self.report("backup")
                if owner.apply_error:
                    raise owner.apply_error
                if owner.apply_result["state"] == "completed":
                    owner.write_metadata(VERSION)
                self.report("health")
                return owner.apply_result

        self.controller_factory = Controller

    def write_metadata(self, version):
        atomic_json(self.root / "bundle-version.json", {"format": 1, "version": version,
                                                       "components": COMPONENTS, "update_protocol": 1})

    def checker(self, current, cache):
        self.check_calls.append(current)
        cache.mkdir(parents=True, exist_ok=True)
        payload = archive_bytes()
        archive = cache / "release.zip"
        archive.write_bytes(payload)
        return {"version": VERSION, "components": COMPONENTS, "notes": "Offline release notes",
                "sha256": hashlib.sha256(payload).hexdigest(), "archive_path": str(archive)}

    def make(self, checker=None, **kwargs):
        instance = daemon.UpdaterDaemon(self.root, self.project, "desktop-linux", controller_factory=self.controller_factory,
                                       release_checker=checker or self.checker, clock=self.clock,
                                       context_checker=lambda context: None, **kwargs)
        instance.initialize()
        return instance

    def request(self, instance, action="check", *, identifier=None, candidate_id=None, created=1000.0, payload_changes=None, token=TOKEN):
        identifier = identifier or str(uuid.uuid4())
        payload = {"id": identifier, "action": action, "created": created}
        if candidate_id is not None:
            payload["candidate_id"] = candidate_id
        if payload_changes:
            payload.update(payload_changes)
        path = instance.ipc / ("request-" + identifier + ".json")
        atomic_json(path, sign(payload, token), mode=0o644)
        return path, identifier

    def finish(self, instance):
        instance.run_once()
        if instance.worker:
            instance.worker.join(timeout=5)
            self.assertFalse(instance.worker.is_alive())
        return verify(read_json(instance.ipc / "status.json"), TOKEN)

    def test_signed_check_exposes_only_public_candidate_fields(self):
        instance = self.make()
        self.request(instance)
        state = self.finish(instance)
        self.assertEqual(state["phase"], "available")
        self.assertFalse(state["active"])
        self.assertEqual(state["candidate"]["version"], VERSION)
        self.assertEqual(set(state["candidate"]), {"id", "version", "components", "notes", "html_url"})
        self.assertNotIn(str(self.root), json.dumps(state))
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertTrue((instance.private / "candidate.json").is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(instance.ipc.stat().st_mode), 0o1777)
            self.assertEqual(stat.S_IMODE(instance.private.stat().st_mode), 0o700)

    def test_no_update_sets_checked_idle(self):
        instance = self.make(checker=lambda current, cache: None)
        self.request(instance)
        state = self.finish(instance)
        self.assertEqual(state["phase"], "idle")
        self.assertTrue(state["checked"])
        self.assertIsNone(state["candidate"])

    def test_apply_requires_checked_id_and_revalidates_before_controller(self):
        instance = self.make()
        self.request(instance)
        candidate = self.finish(instance)["candidate"]
        self.request(instance, "apply", candidate_id=candidate["id"])
        state = self.finish(instance)
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["current_version"], VERSION)
        self.assertFalse(state["recovery_required"])
        self.assertTrue(instance.restart)
        self.assertEqual(len(self.apply_calls), 1)
        self.assertEqual(self.apply_calls[0]["archive_sha256"], self.apply_calls[0]["sha256"])
        self.assertFalse((instance.private / "candidate.json").exists())

    def test_unchecked_unknown_expired_and_tampered_candidates_do_not_apply(self):
        instance = self.make()
        self.request(instance, "apply", candidate_id="a" * 32)
        self.assertEqual(self.finish(instance)["phase"], "failed")
        self.assertEqual(self.apply_calls, [])
        self.request(instance)
        candidate = self.finish(instance)["candidate"]
        instance.candidate["checked_at"] = -10000.0
        self.request(instance, "apply", candidate_id=candidate["id"])
        self.assertEqual(self.finish(instance)["phase"], "failed")
        self.assertEqual(self.apply_calls, [])
        self.request(instance)
        candidate = self.finish(instance)["candidate"]
        Path(instance.candidate["candidate"]["archive_path"]).write_bytes(b"tampered")
        self.request(instance, "apply", candidate_id=candidate["id"])
        state = self.finish(instance)
        self.assertEqual(state["phase"], "failed")
        self.assertFalse(state["recovery_required"])
        self.assertEqual(self.apply_calls, [])

    def test_rejects_invalid_signature_extra_fields_stale_future_and_actions(self):
        instance = self.make()
        cases = [
            {"token": "incorrect-signature-fixture"}, {"created": 700.0}, {"created": 1010.0},
            {"created": True}, {"payload_changes": {"root": "other"}}, {"action": "shell"},
            {"action": "apply", "candidate_id": "../escape"},
        ]
        for options in cases:
            with self.subTest(options=options):
                self.request(instance, **options)
                self.finish(instance)
        self.assertEqual(self.check_calls, [])
        self.assertEqual(self.apply_calls, [])

    def test_replays_are_rejected_across_daemon_restarts(self):
        instance = self.make()
        _, identifier = self.request(instance)
        self.finish(instance)
        self.assertEqual(len(self.check_calls), 1)
        second = self.make()
        self.request(second, identifier=identifier)
        self.finish(second)
        self.assertEqual(len(self.check_calls), 1)

    def test_second_job_is_rejected_while_first_is_running(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(current, cache):
            started.set()
            release.wait(timeout=5)
            return None

        instance = self.make(checker=blocked)
        self.request(instance)
        instance.run_once()
        self.assertTrue(started.wait(timeout=2))
        second, _ = self.request(instance)
        instance.run_once()
        self.assertFalse(second.exists())
        self.assertTrue(instance.state["active"])
        release.set()
        instance.worker.join(timeout=5)
        self.assertEqual(instance.state["phase"], "idle")
        self.assertIn("busy_request_rejected", (instance.private / "updater.log").read_text())

    def test_recovery_failure_blocks_apply_and_does_not_disclose_exception(self):
        self.recover_error = RuntimeError("private diagnostic fixture " + TOKEN)
        instance = self.make()
        self.assertTrue(instance.state["recovery_required"])
        self.request(instance, "apply", candidate_id="b" * 32)
        state = self.finish(instance)
        self.assertEqual(self.apply_calls, [])
        self.assertTrue(state["recovery_required"])
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertNotIn(TOKEN, (instance.private / "updater.log").read_text())

    def test_source_checkout_or_local_edits_allow_start_but_block_autoapply(self):
        for code in ["release_manifest_required", "local_source_modified"]:
            self.baseline_error = ReleaseError(code, "The source cannot be updated automatically.")
            with self.subTest(code=code):
                instance = self.make()
                self.assertTrue(instance.ready)
                self.assertFalse(instance.state["update_capable"])
                self.assertFalse(instance.state["recovery_required"])
                self.assertEqual(instance.state["error"], code)
                self.request(instance)
                state = self.finish(instance)
                self.assertEqual(state["phase"], "available")
                self.request(instance, "apply", candidate_id=state["candidate"]["id"])
                state = self.finish(instance)
                self.assertEqual(state["error"], code)
                self.assertEqual(self.apply_calls, [])

    def test_recovery_with_manual_work_required_does_not_enroll_baseline(self):
        self.recover_result = {"state": "failed", "recovery_required": True, "error": "manual_recovery_required"}
        instance = self.make()
        self.assertEqual(self.enrollment_calls, 0)
        self.assertTrue(instance.state["recovery_required"])
        self.assertEqual(instance.state["error"], "manual_recovery_required")

    def test_unexpected_controller_exception_requires_recovery(self):
        instance = self.make()
        self.request(instance)
        candidate = self.finish(instance)["candidate"]
        self.apply_error = RuntimeError("transaction interrupted")
        self.request(instance, "apply", candidate_id=candidate["id"])
        state = self.finish(instance)
        self.assertEqual(state["phase"], "failed")
        self.assertTrue(state["recovery_required"])

    def test_restart_preserves_completed_status_after_noop_recovery(self):
        instance = self.make()
        instance._publish(phase="completed", active=False)
        second = self.make()
        self.assertEqual(second.state["phase"], "completed")
        self.assertFalse(second.state["active"])

    def test_idle_stop_is_signed_and_busy_stop_never_kills_task(self):
        instance = self.make()
        identifier = str(uuid.uuid4())
        stop = {"id": identifier, "action": "stop", "created": self.clock()}
        atomic_json(instance.private / "stop-request.json", sign(stop, TOKEN))
        instance.run_once()
        self.assertTrue(instance.exiting)
        self.assertTrue(verify(read_json(instance.private / "stop-result.json"), TOKEN)["accepted"])
        instance.exiting = False
        instance._publish(active=True, phase="updating")
        atomic_json(instance.private / "stop-request.json", sign({**stop, "id": str(uuid.uuid4())}, TOKEN))
        instance.run_once()
        self.assertFalse(instance.exiting)
        self.assertFalse(verify(read_json(instance.private / "stop-result.json"), TOKEN)["accepted"])

    def test_candidate_cannot_reference_an_external_local_archive(self):
        def wrong_location(current, cache):
            candidate = self.checker(current, cache)
            candidate["archive_path"] = str(self.root / "outside.zip")
            return candidate
        instance = self.make(checker=wrong_location)
        self.request(instance)
        state = self.finish(instance)
        self.assertEqual(state["phase"], "failed")
        self.assertNotIn("candidate", state)

    def test_fresh_marker_project_source_and_derived_project_are_required(self):
        with self.assertRaises(ReleaseError):
            daemon.UpdaterDaemon(self.root, "other-project", "desktop-linux", context_checker=lambda context: None)
        atomic_json(self.root / "runtime/.combined-setup.json", {"format": 1, "mode": "existing"})
        with self.assertRaises(ReleaseError) as caught:
            self.make()
        self.assertEqual(caught.exception.code, "unsupported_mode")
        atomic_json(self.root / "runtime/.combined-setup.json", {"format": 1, "mode": "fresh"})
        (self.root / "runtime/.env").write_text("QWENPAW_RUNTIME_INTERNAL_TOKEN=" + env_value(TOKEN) + "\n", encoding="utf-8")
        with self.assertRaises(ReleaseError) as caught:
            self.make()
        self.assertEqual(caught.exception.code, "invalid_runtime")

    def test_lifetime_file_lock_is_exclusive(self):
        instance = self.make()
        with FileLock(instance.private / "daemon.lock"):
            self.assertTrue(daemon._running(instance.private))
        self.assertFalse(daemon._running(instance.private))

    def test_private_snapshot_boot_survives_missing_live_helper_files(self):
        instance = self.make()
        snapshot = daemon._snapshot_host(self.root, instance.private)
        pointer = read_json(instance.private / "current-host.json")
        boot = instance.private / "host-entry.py"
        first_boot = boot.read_bytes()
        self.assertEqual(pointer["directory"], snapshot.name)
        self.assertIn("deploy/__init__.py", pointer["files"])
        self.assertEqual(daemon._snapshot_host(self.root, instance.private), snapshot)
        self.assertEqual(boot.read_bytes(), first_boot)
        for name in ("daemon.py", "controller.py", "common.py", "releases.py"):
            (self.root / "bundle_updater" / name).unlink()
        (self.root / "deploy/prepare.py").unlink()
        (self.root / "bundle-version.json").unlink()
        result = subprocess.run([sys.executable, str(boot), "--root", str(self.root), "--help"],
                                capture_output=True, text=True, timeout=20, cwd=self.temp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ensure", result.stdout)

    def test_private_snapshot_fails_closed_on_tamper_and_never_changes_during_transaction(self):
        instance = self.make()
        snapshot = daemon._snapshot_host(self.root, instance.private)
        pointer = (instance.private / "current-host.json").read_bytes()
        atomic_json(instance.private / "transaction.json", {"format": 1, "phase": "installing"})
        with self.assertRaises(ReleaseError):
            daemon._snapshot_host(self.root, instance.private)
        self.assertEqual((instance.private / "current-host.json").read_bytes(), pointer)
        (snapshot / "bundle_updater/common.py").write_text("tampered", encoding="utf-8")
        result = subprocess.run([sys.executable, str(instance.private / "host-entry.py"), "--root", str(self.root), "--help"],
                                capture_output=True, text=True, timeout=20, cwd=self.temp.name)
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not be verified", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)

    def test_new_helper_snapshot_keeps_old_snapshot_and_fixed_boot(self):
        instance = self.make()
        previous = daemon._snapshot_host(self.root, instance.private)
        boot = (instance.private / "host-entry.py").read_bytes()
        source = self.root / "bundle_updater/daemon.py"
        source.write_bytes(source.read_bytes() + b"\n# Offline helper update fixture.\n")
        current = daemon._snapshot_host(self.root, instance.private)
        self.assertNotEqual(previous, current)
        self.assertTrue(previous.is_dir())
        self.assertEqual(read_json(instance.private / "current-host.json")["directory"], current.name)
        self.assertEqual((instance.private / "host-entry.py").read_bytes(), boot)

    def test_post_commit_refresh_requires_reviewed_source_and_skips_pending_recovery(self):
        instance = self.make()
        with patch("bundle_updater.controller.Controller") as controller, patch.object(daemon, "_snapshot_host") as snapshot:
            self.assertTrue(daemon._refresh_host_if_reviewed(self.root, self.project, "desktop-linux", instance.private))
            controller.return_value.ensure_baseline.assert_called_once()
            snapshot.assert_called_once()
        with patch("bundle_updater.controller.Controller") as controller, patch.object(daemon, "_snapshot_host") as snapshot:
            controller.return_value.ensure_baseline.side_effect = ReleaseError("local_source_modified", "Local changes exist.")
            self.assertFalse(daemon._refresh_host_if_reviewed(self.root, self.project, "desktop-linux", instance.private))
            snapshot.assert_not_called()
        atomic_json(instance.private / "transaction.json", {"format": 1, "phase": "installing"})
        with patch("bundle_updater.controller.Controller") as controller, patch.object(daemon, "_snapshot_host") as snapshot:
            self.assertFalse(daemon._refresh_host_if_reviewed(self.root, self.project, "desktop-linux", instance.private))
            controller.assert_not_called()
            snapshot.assert_not_called()

    def test_spawn_uses_verified_private_boot_and_hides_windows_process(self):
        instance = self.make()
        daemon._snapshot_host(self.root, instance.private)
        with patch.object(daemon.subprocess, "Popen") as popen, patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote.invalid:2375", "COMPOSE_FILE": "other.yaml"}):
            daemon._spawn(self.root, self.project, "desktop-linux", False, "f" * 32)
        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[1], str(instance.private / "host-entry.py"))
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertNotIn("DOCKER_HOST", popen.call_args.kwargs["env"])
        self.assertNotIn("COMPOSE_FILE", popen.call_args.kwargs["env"])
        self.assertNotIn(TOKEN, json.dumps(arguments))
        if os.name == "nt":
            self.assertTrue(popen.call_args.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "Windows-specific ACL command construction")
    def test_windows_private_acl_grants_only_current_sid_system_and_admins(self):
        private = self.root / "runtime/updates/private"
        private.mkdir(parents=True)
        sid = "S-1-5-21-111111111-222222222-333333333-1001"
        with patch.object(daemon.subprocess, "run") as run:
            run.side_effect = [SimpleNamespace(returncode=0, stdout='"Fixture","' + sid + '"\n'),
                               SimpleNamespace(returncode=0, stdout="ok"), SimpleNamespace(returncode=0, stdout="ok")]
            PROTECT_PRIVATE(private)
        self.assertEqual(len(run.call_args_list), 3)
        grant = run.call_args_list[2].args[0]
        self.assertEqual(grant[:4], ["icacls", str(private), "/inheritance:r", "/grant:r"])
        self.assertEqual(set(grant[4:]), {"*" + sid + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"})
        self.assertFalse(any("/T" in call.args[0] for call in run.call_args_list))
        self.assertTrue(all(call.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW for call in run.call_args_list))
        self.assertTrue(read_json(private / "acl-ready.json")["restricted"])

    @unittest.skipUnless(os.name == "nt", "Windows-specific ACL failure handling")
    def test_windows_acl_failure_stops_before_private_data_is_created(self):
        private = self.root / "runtime/updates/private"
        private.mkdir(parents=True)
        with patch.object(daemon.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='"Fixture","invalid identifier"\n')
            with self.assertRaises(ReleaseError) as caught:
                PROTECT_PRIVATE(private)
        self.assertEqual(caught.exception.code, "private_acl_failed")
        self.assertFalse((private / "acl-ready.json").exists())

    def test_pending_recovery_uses_cached_version_before_validating_live_source(self):
        instance = self.make()
        atomic_json(instance.private / "transaction.json", {"format": 1, "phase": "installing"})
        (self.root / "bundle-version.json").unlink()
        (self.root / "deploy/compose.bundle.yaml").unlink()
        prepared = daemon.UpdaterDaemon(self.root, self.project, "desktop-linux", controller_factory=self.controller_factory,
                                        release_checker=self.checker, clock=self.clock, context_checker=lambda context: None)
        self.assertEqual(prepared.state["current_version"], "0.5.0-alpha")
        def restore():
            self.write_metadata("0.5.0-alpha")
            (self.root / "deploy/compose.bundle.yaml").write_text("restored", encoding="utf-8")
            atomic_json(instance.private / "transaction.json", {"format": 1, "phase": "rolled_back"})
        self.recover_hook = restore
        prepared.initialize()
        self.assertFalse(prepared.state["recovery_required"])
        self.assertEqual(read_json(instance.private / "installed-version.json")["version"], "0.5.0-alpha")

    def test_docker_context_is_local_and_commands_never_use_shell(self):
        for endpoint in ["unix:///var/run/docker.sock", "npipe:////./pipe/dockerDesktopLinuxEngine"]:
            with self.subTest(endpoint=endpoint), patch.object(daemon.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps(endpoint)
                with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote.invalid:2375", "COMPOSE_FILE": "other.yaml"}):
                    daemon._local_context("desktop-linux")
                self.assertFalse(run.call_args.kwargs["shell"])
                self.assertNotIn("DOCKER_HOST", run.call_args.kwargs["env"])
                self.assertNotIn("COMPOSE_FILE", run.call_args.kwargs["env"])
        for endpoint in ["tcp://127.0.0.1:2375", "ssh://remote.invalid", "npipe:////server/pipe/docker_engine", "unix://remote/socket"]:
            with self.subTest(endpoint=endpoint), patch.object(daemon.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps(endpoint)
                with self.assertRaises(ReleaseError):
                    daemon._local_context("desktop-linux")

    def test_ensure_starts_new_nonce_and_waits_for_ready_not_old_heartbeat(self):
        instance = self.make()
        instance._publish(active=False)
        spawned = []

        def spawn(root, project, context, build, nonce):
            spawned.append(nonce)
            atomic_json(instance.ipc / "status.json", sign({"heartbeat": time.time(), "daemon_nonce": nonce,
                                                          "active": False, "recovery_required": False}, TOKEN))

        with patch.object(daemon, "_local_context"), patch.object(daemon, "_spawn", side_effect=spawn), patch.object(daemon, "_running", side_effect=[False, True]):
            self.assertTrue(daemon.ensure_daemon(self.root, self.project, "desktop-linux")["ready"])
        self.assertEqual(len(spawned), 1)
        self.assertNotEqual(spawned[0], instance.daemon_nonce)
        self.assertFalse(read_json(instance.private / "mode.json")["build_qwenpaw"])

    def test_ensure_reuses_only_ready_same_mode_locked_daemon(self):
        instance = self.make()
        instance.clock = time.time
        instance._publish(active=False)
        mode = {"format": 1, "mode": "fresh", "project": self.project, "context": "desktop-linux", "build_qwenpaw": False}
        atomic_json(instance.private / "mode.json", mode)
        with patch.object(daemon, "_local_context"), patch.object(daemon, "_spawn") as spawn, patch.object(daemon, "_running", return_value=True):
            self.assertTrue(daemon.ensure_daemon(self.root, self.project, "desktop-linux")["ready"])
            spawn.assert_not_called()
        for changes in [{"active": True}, {"active": False, "recovery_required": True}]:
            instance._publish(**changes)
            with patch.object(daemon, "_local_context"), patch.object(daemon, "_spawn") as spawn, patch.object(daemon, "_running", return_value=True):
                with self.assertRaises(ReleaseError):
                    daemon.ensure_daemon(self.root, self.project, "desktop-linux")
                spawn.assert_not_called()

    def test_failed_startup_recovery_does_not_release_launcher_gate(self):
        instance = self.make()

        def spawn(root, project, context, build, nonce):
            atomic_json(instance.ipc / "status.json", sign({"heartbeat": time.time(), "daemon_nonce": nonce,
                                                          "active": False, "recovery_required": True}, TOKEN))

        with patch.object(daemon, "_local_context"), patch.object(daemon, "_spawn", side_effect=spawn), patch.object(daemon, "_running", side_effect=[False, True]):
            with self.assertRaises(ReleaseError) as caught:
                daemon.ensure_daemon(self.root, self.project, "desktop-linux")
        self.assertEqual(caught.exception.code, "recovery_required")

    def test_stop_busy_returns_failure_without_terminating_anything(self):
        instance = self.make()
        instance.clock = time.time
        instance._publish(active=True, phase="updating")
        with patch.object(daemon, "_local_context"), patch.object(daemon, "_running", return_value=True):
            with self.assertRaises(ReleaseError) as caught:
                daemon.stop_daemon(self.root, self.project, "desktop-linux")
        self.assertEqual(caught.exception.code, "updater_busy")
        self.assertFalse((instance.private / "stop-request.json").exists())

    def test_main_hides_unexpected_error_details(self):
        output = io.StringIO()
        with patch.object(daemon, "ensure_daemon", side_effect=RuntimeError(TOKEN)), patch("sys.stderr", output):
            result = daemon.main(["--ensure", "--root", str(self.root), "--project", self.project, "--context", "desktop-linux"])
        self.assertEqual(result, 1)
        self.assertNotIn(TOKEN, output.getvalue())


if __name__ == "__main__":
    unittest.main()
