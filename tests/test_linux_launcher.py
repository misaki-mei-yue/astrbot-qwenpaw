"""Linux launch/adoption flow tested without Docker, models or real user data."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy.prepare import env_value
from scripts import manage_bundle as launcher


class LinuxLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        (self.root / "deploy").mkdir()
        (self.root / "deploy/bootstrap_qwenpaw.py").write_text("# fixture bootstrap\n", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.calls = []
        self.events = []
        self.fail_backup_stop = False
        self.fail_ensure = False

    def checked(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        self.events.append(("command", list(command)))
        if command == ["docker", "context", "show"]:
            return "default"
        if "info" in command:
            return "linux"
        if self.fail_backup_stop and "--stop" in command and any("run_bundle_backup.py" in item for item in command):
            raise subprocess.CalledProcessError(1, command)
        if self.fail_ensure and "--ensure" in command:
            raise subprocess.CalledProcessError(1, command)
        return "" if kwargs.get("capture") else None

    def prepare(self, mode, runtime, existing):
        self.events.append(("prepare", runtime))
        self.prepared(runtime, "astrbot-qwenpaw-fixture", mode=mode)

    def prepared(self, runtime, project="bot-combined", mode="fresh", source=None, install=None):
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / ".combined-setup.json").write_text(json.dumps({"format": 1, "mode": mode}), encoding="utf-8")
        values = {"PROJECT_SOURCE": (source or self.root).as_posix(), "INSTALL_ROOT": (install or runtime).as_posix(),
                  "COMPOSE_PROJECT_NAME": project, "BRIDGE_TOKEN": "fixture-secret"}
        (runtime / ".env").write_text("\n".join(k + "=" + env_value(v) for k, v in values.items()), encoding="utf-8")

    def run_launcher(self, args):
        with (patch.object(launcher, "ROOT", self.root), patch.object(launcher, "is_windows", return_value=False),
              patch.object(launcher, "checked", side_effect=self.checked), patch.object(launcher, "prepare", side_effect=self.prepare),
              patch.object(launcher, "helper_python", return_value="backup-python"),
              patch.object(launcher, "wait_health", side_effect=lambda *a: self.events.append(("health", a))),
              patch("bundle_updater.daemon._local_context") as context_check,
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            result = launcher.main(args)
        self.assertTrue(context_check.called)
        return result

    def test_fresh_linux_bundle_only_expected_compose_and_services(self):
        self.assertEqual(self.run_launcher(["start"]), 0)
        commands = [c for c, _ in self.calls]
        compose = [c for c in commands if "compose" in c and "--project-directory" in c]
        self.assertTrue(compose)
        for command in compose:
            self.assertEqual(command[command.index("--env-file") + 1], str(self.runtime / ".env"))
            self.assertIn(str(self.root / "deploy/compose.bundle.yaml"), command)
            self.assertNotIn(str(self.root / "deploy/compose.yaml"), command)
            self.assertNotIn(str(self.root / "deploy/compose.manage.yaml"), command)
            self.assertEqual(command[command.index("--context") + 1], "default")
        self.assertTrue(any("build" in c and c[-1] == "gateway" for c in commands))
        self.assertFalse(any("down" in c or "rm" in c or "--volumes" in c for c in commands))
        bootstrap = [(c, kw) for c, kw in self.calls if "exec" in c]
        self.assertEqual(len(bootstrap), 1)
        self.assertEqual(bootstrap[0][0][-5:], ["exec", "-T", "qwenpaw", "/app/venv/bin/python", "-"])
        self.assertEqual(bootstrap[0][1]["input"], "# fixture bootstrap\n")
        names = [event[0] for event in self.events]
        first_health = names.index("health")
        bootstrap_index = next(i for i, e in enumerate(self.events) if e[0] == "command" and "exec" in e[1])
        self.assertLess(first_health, bootstrap_index)
        self.assertEqual(self.events[-1][0], "health")

    def test_adopt_existing_fresh_data_dir_adds_gateway_without_prepare(self):
        data = Path(self.temp.name) / "previous-vps-data"
        self.prepared(data, "bot-combined")
        self.assertEqual(self.run_launcher(["start", "--data-dir", str(data)]), 0)
        self.assertFalse(any(event[0] == "prepare" for event in self.events))
        compose = [c for c, _ in self.calls if "--project-directory" in c]
        for command in compose:
            self.assertIn(str(self.root / "deploy/compose.yaml"), command)
            self.assertIn(str(self.root / "deploy/compose.manage.yaml"), command)
            self.assertEqual(command[command.index("--env-file") + 1], str(data / ".env"))
            self.assertEqual(command[command.index("--project-name") + 1], "bot-combined")
        build = next(c for c in compose if "build" in c)
        self.assertEqual(build[-3:], ["build", "gateway", "qwenpaw"])
        self.assertFalse(any("run_bundle_updater.py" in value for c, _ in self.calls for value in c))

    def test_addon_other_source_and_wrong_runtime_are_refused(self):
        for mode, wrong_source, wrong_runtime in (("existing", False, False), ("fresh", True, False), ("fresh", False, True)):
            with self.subTest(mode=mode, source=wrong_source, runtime=wrong_runtime):
                self.calls.clear()
                self.events.clear()
                data = Path(self.temp.name) / "adoption"
                self.prepared(data, mode=mode, source=Path(self.temp.name) if wrong_source else None,
                              install=Path(self.temp.name) if wrong_runtime else None)
                self.assertEqual(self.run_launcher(["start", "--data-dir", str(data)]), 1)
                self.assertFalse(any("--ensure" in c or "up" in c for c, _ in self.calls))
                self.assertFalse(any(e[0] == "prepare" for e in self.events))

    def test_interrupted_backup_recovery_precedes_prepare_and_up(self):
        self.prepared(self.runtime)
        journal = self.runtime / "backups/private/transaction.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_launcher(["start"]), 0)
        ensure = next(i for i, e in enumerate(self.events) if e[0] == "command" and "--ensure" in e[1])
        prepare = next(i for i, e in enumerate(self.events) if e[0] == "prepare")
        self.assertLess(ensure, prepare)
        self.assertIn("run_bundle_backup.py", " ".join(self.events[ensure][1]))

    def test_failed_recovery_never_prepares_or_starts_containers(self):
        self.prepared(self.runtime)
        journal = self.runtime / "backups/private/transaction.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}", encoding="utf-8")
        self.fail_ensure = True
        self.assertEqual(self.run_launcher(["start"]), 1)
        self.assertFalse(any(e[0] == "prepare" for e in self.events))
        self.assertFalse(any("up" in c or "pull" in c or "build" in c for c, _ in self.calls))

    def test_stable_updater_recovery_precedes_prepare(self):
        self.prepared(self.runtime)
        stable = self.runtime / "updates/private/host-entry.py"
        stable.parent.mkdir(parents=True)
        stable.write_text("# fixture", encoding="utf-8")
        self.assertEqual(self.run_launcher(["start"]), 0)
        stable_event = next(i for i, e in enumerate(self.events) if e[0] == "command" and str(stable) in e[1])
        prepare_event = next(i for i, e in enumerate(self.events) if e[0] == "prepare")
        self.assertLess(stable_event, prepare_event)

    def test_stop_helpers_before_core_stop_and_active_guard(self):
        self.prepared(self.runtime)
        self.fail_backup_stop = True
        self.assertEqual(self.run_launcher(["stop"]), 1)
        self.assertFalse(any("compose" in c and "stop" in c for c, _ in self.calls))
        self.fail_backup_stop = False
        self.calls.clear()
        self.events.clear()
        self.assertEqual(self.run_launcher(["stop"]), 0)
        backup = next(i for i, (c, _) in enumerate(self.calls) if "--stop" in c and any("run_bundle_backup.py" in v for v in c))
        updater = next(i for i, (c, _) in enumerate(self.calls) if "--stop" in c and any("run_bundle_updater.py" in v for v in c))
        compose = next(i for i, (c, _) in enumerate(self.calls) if "compose" in c and "stop" in c)
        self.assertLess(backup, updater)
        self.assertLess(updater, compose)

    def test_checked_removes_host_and_private_env_overrides(self):
        self.prepared(self.runtime)
        fake = subprocess.CompletedProcess([], 0, stdout="ok\n")
        command = ["docker", "--context", "default", "compose", "--env-file", str(self.runtime / ".env"), "config"]
        with (patch.dict(os.environ, {"DOCKER_HOST": "ssh://wrong", "COMPOSE_FILE": "outside", "PROJECT_SOURCE": "outside",
                                      "INSTALL_ROOT": "outside", "BRIDGE_TOKEN": "wrong-secret"}),
              patch.object(launcher.subprocess, "run", return_value=fake) as run,
              patch.object(launcher, "ROOT", self.root)):
            self.assertEqual(launcher.checked(command, capture=True), "ok")
        env = run.call_args.kwargs["env"]
        for key in ("DOCKER_HOST", "COMPOSE_FILE", "PROJECT_SOURCE", "INSTALL_ROOT", "BRIDGE_TOKEN"):
            self.assertNotIn(key, env)
        self.assertFalse(run.call_args.kwargs["shell"])


if __name__ == "__main__":
    unittest.main()
