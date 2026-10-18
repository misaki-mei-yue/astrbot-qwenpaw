"""Exercise host updates against fixture files and a fake Docker runner only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from bundle_updater.controller import Controller, CORE, SERVICES, TASK_GUARD, UpdateError


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


class FakeDocker:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.running = True
        self.fail_phase = None
        self.stop_refused = False
        self.on_candidate = None
        self.jobs_result = {"allowed": True}
        self.ids = {service: str(index) * 12 for index, service in enumerate(SERVICES, 1)}
        self.images = {service: "sha256:" + str(index) * 64 for index, service in enumerate(SERVICES, 1)}

    def __call__(self, arguments, *, cwd, env, input=None, timeout=180):
        self.calls.append({"args": arguments, "source": env["PROJECT_SOURCE"], "cwd": cwd,
                           "env": env, "input": input})
        args = arguments[3:]
        output = ""
        if args[:2] == ["context", "inspect"]:
            output = json.dumps("npipe:////./pipe/docker_engine")
        elif args[0] == "info":
            output = "linux"
        elif args[0] == "ps":
            service = args[-1].split("=")[-1]
            output = self.ids[service]
        elif args[0] == "inspect":
            role = next(name for name, value in self.ids.items() if value == args[-1])
            if ".Image" in args[2]:
                output = json.dumps(self.images[role])
            else:
                output = "true" if self.running else "false"
        elif args[0] == "stop":
            if not self.stop_refused:
                self.running = False
        elif args[0] == "compose":
            action = next((name for name in ("config", "pull", "build", "up", "exec") if name in args), "")
            if action == self.fail_phase:
                return subprocess.CompletedProcess(arguments, 1, b"", b"PRIVATE-ERROR-MARKER")
            if action == "up":
                self.running = True
                is_rollback = any("rollback-compose.json" in value for value in args)
                if not is_rollback and self.on_candidate:
                    self.on_candidate()
            elif action == "exec" and input and b"inspect_jobs" in input:
                output = json.dumps(self.jobs_result)
        return subprocess.CompletedProcess(arguments, 0, output.encode(), b"")

    def count(self, command):
        return sum(command in call["args"][3:] for call in self.calls)


class FixtureController(Controller):
    def __init__(self, *args, health_failures=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.health_failures = health_failures
        self.health_calls = 0

    def _wait_ready(self, timeout=600):
        self.health_calls += 1
        if self.health_calls <= self.health_failures:
            raise UpdateError("health_check_failed")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "package"
        self.root.mkdir()
        self.private = self.root / "runtime/updates/private"
        self.private.mkdir(parents=True)
        self.project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(self.root).lower().encode()).hexdigest()[:10]
        self.report = []
        self.docker = FakeDocker(self.root)
        for name, content in self.old_sources().items():
            write(self.root / name, content)
        self.install_manifest()
        write(self.root / "runtime/.env", "PRIVATE_CONFIG=preserve-me\n")
        write(self.root / "runtime/.combined-setup.json", '{"format":1,"mode":"fresh"}')
        write(self.private / "mode.json", '{"format":1,"build_qwenpaw":false}')
        write(self.root / "runtime/state/qwenpaw/chat.txt", "original private conversation")
        write(self.root / "runtime/state/napcat/login.txt", "original login")
        (self.root / "runtime/state/astrbot").mkdir()
        write(self.root / "personal.keep", "private extra file")
        self.candidate = self.archive()

    def old_sources(self):
        return {"README.md": "old readme", "LICENSE": "public fixture license",
                "deploy/compose.bundle.yaml": "services: {}\n",
                "deploy/bootstrap_qwenpaw.py": "# old fixture bootstrap\n",
                "scripts/obsolete.py": "# remove on upgrade\n",
                "bundle-version.json": json.dumps({"format": 1, "version": "0.6.0-alpha", "update_protocol": 1,
                    "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}})}

    def archive(self):
        sources = self.old_sources()
        sources["README.md"] = "new readme"
        sources["deploy/bootstrap_qwenpaw.py"] = "# new fixture bootstrap\n"
        sources.pop("scripts/obsolete.py")
        sources["scripts/new.py"] = "# added file\n"
        data = json.loads(sources["bundle-version.json"])
        data["version"] = "0.6.1-alpha"
        sources["bundle-version.json"] = json.dumps(data)
        manifest = {"format": 1, "version": "0.6.1-alpha", "files": []}
        archive = self.private / "candidate.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name, content in sources.items():
                raw = content.encode()
                manifest["files"].append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                output.writestr(info, raw)
            output.writestr("RELEASE-MANIFEST.json", json.dumps(manifest))
        return {"version": "0.6.1-alpha", "archive_path": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}

    def install_manifest(self):
        manifest = {"format": 1, "version": "0.6.0-alpha", "files": []}
        for name, content in self.old_sources().items():
            raw = content.encode()
            manifest["files"].append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        write(self.root / "RELEASE-MANIFEST.json", json.dumps(manifest))

    def controller(self, **kwargs):
        return FixtureController(self.root, self.project, "desktop-linux", self.private, self.report.append,
                                 runner=self.docker, **kwargs)

    def test_success_preserves_data_and_extra_files_and_removes_obsolete_sources(self):
        controller = self.controller()
        result = controller.apply(self.candidate)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual((self.root / "README.md").read_text(), "new readme")
        self.assertFalse((self.root / "scripts/obsolete.py").exists())
        self.assertTrue((self.root / "scripts/new.py").is_file())
        self.assertEqual((self.root / "runtime/state/qwenpaw/chat.txt").read_text(), "original private conversation")
        self.assertEqual((self.root / "personal.keep").read_text(), "private extra file")
        self.assertEqual((self.root / "runtime/.env").read_text(), "PRIVATE_CONFIG=preserve-me\n")
        self.assertTrue((self.private / result["transaction_id"] / "backup/state/qwenpaw/chat.txt").exists())
        self.assertIsNone(controller.recover())

    def test_candidate_build_points_to_staging_not_live_source(self):
        self.assertEqual(self.controller().apply(self.candidate)["state"], "completed")
        build = next(call for call in self.docker.calls if "build" in call["args"])
        self.assertIn("/stage", build["source"])
        self.assertNotEqual(build["source"], self.root.as_posix())
        self.assertEqual(build["env"]["INSTALL_ROOT"], (self.root / "runtime").as_posix())

    def test_health_failure_restores_old_data_and_exact_image_ids(self):
        def mutate():
            write(self.root / "runtime/state/qwenpaw/chat.txt", "candidate migrated database")
            write(self.root / "runtime/state/qwenpaw/new-schema.txt", "remove on rollback")
        self.docker.on_candidate = mutate
        result = self.controller(health_failures=1).apply(self.candidate)
        self.assertEqual(result["state"], "rolled_back", result)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertEqual((self.root / "runtime/state/qwenpaw/chat.txt").read_text(), "original private conversation")
        self.assertFalse((self.root / "runtime/state/qwenpaw/new-schema.txt").exists())
        self.assertFalse((self.root / "scripts/new.py").exists())
        self.assertTrue((self.root / "scripts/obsolete.py").exists())
        override = json.loads((self.private / result["transaction_id"] / "rollback-compose.json").read_text())
        self.assertEqual({key: value["image"] for key, value in override["services"].items()}, self.docker.images)
        self.assertTrue(all(item["pull_policy"] == "never" for item in override["services"].values()))
        recovery = [c for c in self.docker.calls if "up" in c["args"]][-1]
        self.assertIn("--no-build", recovery["args"])
        self.assertIn("never", recovery["args"])

    def test_local_modification_is_blocked_without_resetting_baseline(self):
        controller = self.controller()
        controller.ensure_baseline()
        baseline = controller.baseline_path.read_bytes()
        write(self.root / "README.md", "local change")
        result = controller.apply(self.candidate)
        self.assertEqual(result["error"], "local_source_modified")
        self.assertEqual(controller.baseline_path.read_bytes(), baseline)
        self.assertEqual(self.docker.count("stop"), 0)

    def test_source_build_mode_refuses_before_any_docker_call(self):
        write(self.private / "mode.json", '{"format":1,"build_qwenpaw":true}')
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "source_build_unsupported")
        self.assertEqual(self.docker.calls, [])

    def test_unknown_mode_is_not_inferred_as_official(self):
        (self.private / "mode.json").unlink()
        self.assertEqual(self.controller().apply(self.candidate)["error"], "deployment_mode_unknown")

    def test_checksum_tampering_is_blocked_before_stop(self):
        self.candidate["sha256"] = "0" * 64
        self.assertEqual(self.controller().apply(self.candidate)["error"], "candidate_checksum_changed")
        self.assertEqual(self.docker.count("stop"), 0)

    def test_external_archive_is_rejected(self):
        archive = Path(self.candidate["archive_path"])
        foreign = Path(self.temp.name) / "foreign.zip"
        shutil.copyfile(archive, foreign)
        self.candidate["archive_path"] = str(foreign)
        self.assertEqual(self.controller().apply(self.candidate)["error"], "invalid_candidate")

    def test_pull_and_build_failures_do_not_stop_services_or_expose_logs(self):
        for phase in ("pull", "build"):
            self.docker.fail_phase = phase
            result = self.controller().apply(self.candidate)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error"], phase + "_failed")
            self.assertFalse(result["recovery_required"])
            self.assertEqual(self.docker.count("stop"), 0)
            self.assertNotIn("PRIVATE-ERROR-MARKER", json.dumps(result) + json.dumps(self.report))

    def test_insufficient_disk_does_not_stop_services(self):
        with patch("bundle_updater.controller.shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "insufficient_backup_space")
        self.assertEqual(self.docker.count("stop"), 0)

    def test_hardlinked_state_is_rejected_without_following_or_stopping(self):
        original = self.root / "runtime/state/napcat/login.txt"
        try:
            os.link(original, self.root / "runtime/state/napcat/hardlink.txt")
        except OSError:
            self.skipTest("Hardlinks unavailable")
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "unsafe_backup_entry")
        self.assertEqual(self.docker.count("stop"), 0)

    def test_unmanaged_new_file_collision_preserves_file_and_rolls_back(self):
        controller = self.controller()
        controller.ensure_baseline()
        write(self.root / "scripts/new.py", "private extra source")
        result = controller.apply(self.candidate)
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["error"], "unmanaged_source_collision")
        self.assertEqual(self.docker.count("stop"), 0)
        # Before installation began this foreign file existed, so it must survive
        # even though the release declares the same destination.
        self.assertEqual((self.root / "scripts/new.py").read_text(), "private extra source")

    def test_recovery_after_partial_source_install_restores_snapshot(self):
        controller = self.controller()
        original_install = controller._install
        def interrupt(*args):
            original_install(*args)
            raise KeyboardInterrupt()
        controller._install = interrupt
        with self.assertRaises(KeyboardInterrupt):
            controller.apply(self.candidate)
        result = self.controller().recover()
        self.assertEqual(result["state"], "rolled_back", result)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertFalse((self.root / "scripts/new.py").exists())

    def test_recovery_before_stop_aborts_without_touching_services(self):
        controller = self.controller()
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()
        controller._old_images = interrupt
        with self.assertRaises(KeyboardInterrupt):
            controller.apply(self.candidate)
        result = self.controller().recover()
        self.assertFalse(result["recovery_required"])
        self.assertEqual(result["phase"], "aborted")
        self.assertEqual(self.docker.count("stop"), 0)

    def test_failed_rollback_blocks_new_apply(self):
        controller = self.controller(health_failures=2)
        result = controller.apply(self.candidate)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["recovery_required"])
        calls = len(self.docker.calls)
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "recovery_required")
        self.assertEqual(calls, len(self.docker.calls))
        self.assertTrue(self.controller().recover()["recovery_required"])

    def test_refused_stop_never_backs_up_or_changes_source(self):
        self.docker.stop_refused = True
        result = self.controller().apply(self.candidate)
        self.assertTrue(result["recovery_required"])
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertEqual(list(self.private.glob("*/backup")), [])

    def test_corrupt_backup_is_not_restored_over_live_state(self):
        controller = self.controller()
        def corrupt(*args):
            backup = self.private / controller.journal["transaction_id"] / "backup/state/qwenpaw/chat.txt"
            write(backup, "corrupted snapshot")
            raise KeyboardInterrupt()
        controller._install = corrupt
        with self.assertRaises(KeyboardInterrupt):
            controller.apply(self.candidate)
        result = self.controller().recover()
        self.assertTrue(result["recovery_required"])
        self.assertEqual((self.root / "runtime/state/qwenpaw/chat.txt").read_text(), "original private conversation")

    def test_nonlocal_context_is_rejected(self):
        runner = self.docker
        def remote(args, **kwargs):
            if args[3:5] == ["context", "inspect"]:
                return subprocess.CompletedProcess(args, 0, b'"ssh://other-machine"', b"")
            return runner(args, **kwargs)
        controller = self.controller()
        controller.runner = remote
        self.assertEqual(controller.apply(self.candidate)["error"], "remote_engine_forbidden")

    def test_remote_named_pipe_or_unix_authority_cannot_pass_recheck(self):
        for endpoint in ("npipe:////another-host/pipe/docker_engine", "unix://another-host/socket"):
            with self.subTest(endpoint=endpoint):
                def remote(args, **kwargs):
                    if args[3:5] == ["context", "inspect"]:
                        return subprocess.CompletedProcess(args, 0, json.dumps(endpoint).encode(), b"")
                    return self.docker(args, **kwargs)
                controller = self.controller()
                controller.runner = remote
                self.assertEqual(controller.apply(self.candidate)["error"], "remote_engine_forbidden")
        self.assertEqual(self.docker.count("stop"), 0)

    def test_project_scope_must_match_directory_hash(self):
        with self.assertRaisesRegex(UpdateError, "project_scope_mismatch"):
            Controller(self.root, "another-project", "desktop-linux", self.private, self.report.append)

    def test_malformed_journal_blocks_changes(self):
        write(self.private / "transaction.json", '{"format":1}')
        result = self.controller().apply(self.candidate)
        self.assertTrue(result["recovery_required"])
        self.assertEqual(self.docker.calls, [])

    def test_release_manifest_enrollment_preserves_custom_public_directory_files(self):
        manifest = {"format": 1, "version": "0.6.0-alpha", "files": []}
        for name, content in self.old_sources().items():
            raw = content.encode()
            manifest["files"].append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        write(self.root / "RELEASE-MANIFEST.json", json.dumps(manifest))
        write(self.root / "scripts/my-private-helper.py", "# private, preserve\n")
        write(self.root / "docs/my-private-notes.md", "private notes\n")
        controller = self.controller()
        baseline = controller.ensure_baseline()
        self.assertNotIn("scripts/my-private-helper.py", baseline["files"])
        self.assertNotIn("docs/my-private-notes.md", baseline["files"])
        result = controller.apply(self.candidate)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual((self.root / "scripts/my-private-helper.py").read_text(), "# private, preserve\n")
        self.assertEqual((self.root / "docs/my-private-notes.md").read_text(), "private notes\n")

    def test_corrupt_install_manifest_does_not_fall_back_to_rescanning(self):
        write(self.root / "RELEASE-MANIFEST.json", '{"format":1,"files":[]}')
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "invalid_install_manifest")
        self.assertFalse((self.private / "installed-files.json").exists())
        self.assertEqual(self.docker.count("stop"), 0)

    def test_recover_after_stop_before_backup_only_restarts_original_services(self):
        controller = self.controller()
        original = controller._stop_core
        def interrupt():
            original()
            raise KeyboardInterrupt()
        controller._stop_core = interrupt
        with self.assertRaises(KeyboardInterrupt):
            controller.apply(self.candidate)
        self.assertFalse(self.docker.running)
        result = self.controller().recover()
        self.assertEqual(result["state"], "rolled_back", result)
        self.assertTrue(self.docker.running)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertEqual((self.root / "runtime/state/qwenpaw/chat.txt").read_text(), "original private conversation")

    def test_apply_never_overlaps_an_uncommitted_transaction(self):
        controller = self.controller()
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()
        controller._old_images = interrupt
        with self.assertRaises(KeyboardInterrupt):
            controller.apply(self.candidate)
        count = len(self.docker.calls)
        result = self.controller().apply(self.candidate)
        self.assertTrue(result["recovery_required"])
        self.assertEqual(len(self.docker.calls), count)

    def test_state_symlink_never_follows_external_target(self):
        outside = Path(self.temp.name) / "outside"
        write(outside / "valuable.txt", "keep external data")
        try:
            (self.root / "runtime/state/qwenpaw/escape").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "unsafe_path")
        self.assertEqual(self.docker.count("stop"), 0)
        self.assertEqual((outside / "valuable.txt").read_text(), "keep external data")

    def test_docker_environment_cannot_redirect_context_or_project(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://wrong-host", "COMPOSE_FILE": "foreign.yaml", "COMPOSE_PROJECT_NAME": "other-project"}):
            result = self.controller().apply(self.candidate)
        self.assertEqual(result["state"], "completed", result)
        for call in self.docker.calls:
            self.assertNotIn("DOCKER_HOST", call["env"])
            self.assertNotIn("COMPOSE_FILE", call["env"])
            self.assertNotIn("COMPOSE_PROJECT_NAME", call["env"])

    def test_report_callback_failure_cannot_break_backup_and_rollback(self):
        controller = self.controller(health_failures=1)
        controller.report = lambda value: (_ for _ in ()).throw(RuntimeError("UI unavailable"))
        result = controller.apply(self.candidate)
        self.assertEqual(result["state"], "rolled_back", result)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")

    def test_missing_manifest_does_not_enroll_private_extra_files(self):
        (self.root / "RELEASE-MANIFEST.json").unlink()
        write(self.root / "scripts/private-helper.py", "# preserve\n")
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "release_manifest_required")
        self.assertFalse((self.private / "installed-files.json").exists())
        self.assertEqual((self.root / "scripts/private-helper.py").read_text(), "# preserve\n")

    def test_enabled_scheduled_jobs_block_before_pull_build_and_stop(self):
        self.docker.jobs_result = {"allowed": False, "reason": "scheduled_jobs_enabled"}
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "scheduled_jobs_enabled")
        for action in ("pull", "build", "stop", "up"):
            self.assertEqual(self.docker.count(action), 0)

    def test_unknown_task_api_fails_closed_before_pull_or_stop(self):
        self.docker.jobs_result = {"detail": "unsupported API response"}
        result = self.controller().apply(self.candidate)
        self.assertEqual(result["error"], "task_check_unavailable")
        self.assertEqual(self.docker.count("pull"), 0)
        self.assertEqual(self.docker.count("stop"), 0)

    def test_scheduler_reenabled_during_download_blocks_before_stop(self):
        original_runner = self.docker
        def enable_after_build(arguments, **kwargs):
            result = original_runner(arguments, **kwargs)
            if "build" in arguments[3:]:
                original_runner.jobs_result = {"allowed": False, "reason": "scheduled_jobs_enabled"}
            return result
        controller = self.controller()
        controller.runner = enable_after_build
        result = controller.apply(self.candidate)
        self.assertEqual(result["error"], "scheduled_jobs_enabled")
        self.assertEqual(self.docker.count("build"), 1)
        self.assertEqual(self.docker.count("stop"), 0)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")


class TaskGuardTests(unittest.TestCase):
    def guard(self, health, jobs=None):
        requests = []
        class Reply:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return json.dumps(self.value).encode()
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                self.timeout = timeout
                return Reply(health if request.full_url.endswith("/healthz") else jobs)
        output = io.StringIO()
        with patch.dict(os.environ, {"QWENPAW_RUNTIME_INTERNAL_TOKEN": "T" * 40}), patch("urllib.request.build_opener", return_value=Opener()), patch("sys.stdout", output):
            exec(compile(TASK_GUARD, "<read-only-job-guard>", "exec"), {})
        return json.loads(output.getvalue()), requests

    def test_paused_jobs_allow_update_using_only_fixed_get_endpoints(self):
        result, requests = self.guard({"status": "ok", "agents_loaded": ["default", "abp_example"]}, [{"enabled": False}])
        self.assertEqual(result, {"allowed": True})
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request.get_method() == "GET" and request.full_url.startswith("http://127.0.0.1:8088/api/") for request in requests))

    def test_enabled_jobs_refuse(self):
        result, _ = self.guard({"status": "ok", "agents_loaded": ["default"]}, [{"enabled": True}])
        self.assertEqual(result, {"allowed": False, "reason": "scheduled_jobs_enabled"})

    def test_invalid_agent_cannot_escape_api_path(self):
        result, requests = self.guard({"status": "ok", "agents_loaded": ["../../outside"]}, [])
        self.assertEqual(result, {"allowed": False, "reason": "task_check_unavailable"})
        self.assertEqual(len(requests), 1)

    def test_unknown_enabled_flag_or_too_many_agents_fail_closed(self):
        for health, jobs in (({"status": "ok", "agents_loaded": ["default"]}, [{"enabled": "false"}]),
                             ({"status": "ok", "agents_loaded": ["default"] * 129}, [])):
            result, _ = self.guard(health, jobs)
            self.assertEqual(result, {"allowed": False, "reason": "task_check_unavailable"})


if __name__ == "__main__":
    unittest.main()
