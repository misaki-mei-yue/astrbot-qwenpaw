"""Portable controller transactions using encrypted archives and real SQLite."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bundle_updater.backup_controller import BackupController, BackupError, CORE, STATE_ROOTS
from deploy.prepare import env_value, parse_env


PASSWORD = "a private test password"
METADATA = {"format": 1, "version": "0.7.0-alpha", "update_protocol": 1,
            "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class Docker:
    def __init__(self):
        self.controller = None
        self.calls = []
        self.running = {}
        self.refuse_stop = False
        self.fail_restore_start = 0
        self.fail_bridge = 0
        self.extra_mount = False
        self.endpoint = "unix:///var/run/docker.sock"
        self.engine_arch = "x86_64"
        self.on_start = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        c = self.controller
        args = argv[3:]
        roles = c.services
        ids = {s: str(i) * 12 for i, s in enumerate(roles, 1)}
        self.running = self.running or {s: True for s in roles}

        def mounts(service, inspect=False):
            rows = []
            for target, (source, rw) in c._expected_mounts()[service].items():
                rows.append({"Type": "bind", "Source": str(source), "Destination": target, "RW": rw} if inspect
                            else {"type": "bind", "source": str(source), "target": target, "read_only": not rw})
            if self.extra_mount and service == "astrbot":
                rows.append({"Type": "bind", "Source": "/outside", "Destination": "/outside", "RW": True} if inspect
                            else {"type": "bind", "source": "/outside", "target": "/outside"})
            return rows

        output = ""
        if args[:2] == ["context", "inspect"]:
            output = json.dumps(self.endpoint)
        elif args[0] == "info":
            output = json.dumps({"OSType": "linux", "Architecture": self.engine_arch})
        elif args[0] == "ps":
            role = next((a.split("=", 2)[-1] for a in args if a.startswith("label=com.docker.compose.service=")), None)
            output = ids[role] if role else "\n".join(ids.values())
        elif args[0] == "inspect":
            if "--format" in args:
                role = next(s for s in roles if ids[s] == args[-1])
                output = json.dumps(self.running[role])
            else:
                output = json.dumps([{"Id": ids[s], "Image": "sha256:" + str(i) * 64,
                                      "State": {"Running": self.running[s]},
                                      "Config": {"Labels": {"com.docker.compose.project": c.project, "com.docker.compose.service": s}},
                                      "Mounts": mounts(s, True)} for i, s in enumerate(roles, 1)])
        elif args[0] == "stop":
            if not self.refuse_stop:
                for s in CORE:
                    self.running[s] = False
        elif args[0] == "start":
            for s in CORE:
                if ids[s] in args:
                    self.running[s] = True
        elif args[0] == "compose":
            if "config" in args:
                output = json.dumps({"services": {s: {"volumes": mounts(s)} for s in roles}})
            elif "up" in args:
                rollback = any("rollback-compose.json" in a for a in args)
                if not rollback and self.fail_restore_start:
                    self.fail_restore_start -= 1
                    return subprocess.CompletedProcess(argv, 1, b"", b"PRIVATE ERROR DETAIL")
                self.running.update({s: True for s in roles})
                if self.on_start:
                    self.on_start(rollback)
            elif "create" in args:
                self.running.update({s: False for s in roles})
            elif "start" in args:
                for s in roles:
                    if s in args:
                        self.running[s] = True
            elif "exec" in args and self.fail_bridge:
                self.fail_bridge -= 1
                return subprocess.CompletedProcess(argv, 1, b"", b"PRIVATE ERROR DETAIL")
        return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

    def changes(self):
        return [args for args, _ in self.calls if any(v in args[3:] for v in ("stop", "start", "up"))]


class Fixture(BackupController):
    def __init__(self, *args, health_failures=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.health_failures = health_failures
        self.health_calls = 0

    def _wait_ready(self, timeout=600):
        self.health_calls += 1
        if self.health_calls <= self.health_failures:
            raise BackupError("health_check_failed")


class BackupControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, name, *, layout="bundle", health_failures=0):
        root = self.base / name
        root.mkdir()
        runtime = root / "runtime" if layout == "bundle" else self.base / (name + "-data")
        project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:10] if layout == "bundle" else "portable-fixture"
        for suffix in STATE_ROOTS:
            (runtime / "state" / suffix).mkdir(parents=True, exist_ok=True)
        for suffix in ("compose.bundle.yaml", "compose.yaml", "compose.manage.yaml", "console.Caddyfile"):
            write(root / "deploy" / suffix, "# fixture")
        write(root / "bundle-version.json", json.dumps(METADATA))
        values = {"BRIDGE_TOKEN": name + "-bridge-secret-0123456789", "ONEBOT_TOKEN": name + "-qq-secret-0123456789",
                  "QWENPAW_RUNTIME_INTERNAL_TOKEN": name + "-ownership-secret-0123456789",
                  "INSTALL_ROOT": runtime.as_posix(), "PROJECT_SOURCE": root.as_posix(), "COMPOSE_PROJECT_NAME": project,
                  "BRIDGE_NETWORK": project + "-network", "QWENPAW_BASE_URL": "http://qwenpaw:8088",
                  "QWENPAW_HTTP_PROXY": "http://" + name + ":10808", "CUSTOM_PRIVATE_SETTING": "preserve " + name}
        write(runtime / ".env", "\n".join(k + "=" + env_value(v) for k, v in values.items()) + "\n")
        write(runtime / ".combined-setup.json", '{"format":1,"mode":"fresh"}')
        with closing(sqlite3.connect(runtime / "state/astrbot/chat.db")) as connection:
            connection.execute("create table chat (text text)")
            connection.execute("insert into chat values (?)", (name + " original chat",))
            connection.commit()
        write(runtime / "state/qwenpaw/secret/secret.json", json.dumps({"provider_key": name + "-api-secret"}))
        write(runtime / "state/qwenpaw/working/memory.md", name + " original memory")
        write(runtime / "state/qwenpaw/working/cron.json", '{"enabled":true,"job":"preserve me"}')
        write(runtime / "state/napcat/qq/token", name + " original login")
        docker = Docker()
        reports = []
        controller = Fixture(root, project, "default", runtime / "backups/private", reports.append,
                             runtime_root=runtime, layout=layout, runner=docker, health_failures=health_failures)
        docker.controller = controller
        return controller, docker, reports

    def archive_plan(self, source, target):
        exported = source.export(PASSWORD)
        imported = target.private / "uploaded.aqbackup"
        imported.write_bytes(Path(exported["archive_path"]).read_bytes())
        return target.inspect(imported, PASSWORD), exported

    def chat(self, controller):
        with closing(sqlite3.connect(controller.runtime / "state/astrbot/chat.db")) as connection:
            return connection.execute("select text from chat").fetchone()[0]

    def test_export_restart_before_encryption_catalog_and_secrets_not_public(self):
        source, docker, reports = self.fixture("source")
        entry = source.export(PASSWORD, "download")
        self.assertEqual(Path(entry["archive_path"]), source.get_archive(entry["id"]))
        self.assertEqual(source.list_backups()[0]["destination"], "download")
        self.assertNotIn("archive_path", source.list_backups()[0])
        phases = [r["phase"] for r in reports]
        self.assertLess(phases.index("restarting"), phases.index("encrypting"))
        self.assertTrue(all(docker.running.values()))
        self.assertFalse((source.private / "transactions" / entry["id"] / "snapshot").exists())
        self.assertNotIn(b"original memory", Path(entry["archive_path"]).read_bytes())
        self.assertNotIn("ownership-secret", json.dumps(reports + source.list_backups()))
        self.assertEqual(self.chat(source), "source original chat")

    def test_cross_root_restore_sqlite_env_all_keys_and_bind_inodes(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target", layout="linux")
        inode = (target.runtime / "state/astrbot").stat().st_ino
        plan, _ = self.archive_plan(source, target)
        self.assertTrue(plan["requires_overwrite"])
        self.assertNotIn("secret", json.dumps(plan))
        result = target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.chat(target), "source original chat")
        self.assertEqual((target.runtime / "state/astrbot").stat().st_ino, inode)
        values = parse_env(target.runtime / ".env")
        self.assertEqual(values["INSTALL_ROOT"], target.runtime.as_posix())
        self.assertEqual(values["PROJECT_SOURCE"], target.root.as_posix())
        self.assertEqual(values["COMPOSE_PROJECT_NAME"], target.project)
        self.assertEqual(values["QWENPAW_RUNTIME_INTERNAL_TOKEN"], "source-ownership-secret-0123456789")
        self.assertEqual(values["ONEBOT_TOKEN"], "source-qq-secret-0123456789")
        self.assertEqual(values["BRIDGE_TOKEN"], "source-bridge-secret-0123456789")
        self.assertEqual(values["CUSTOM_PRIVATE_SETTING"], "preserve source")
        self.assertEqual(values["QWENPAW_HTTP_PROXY"], "http://target:10808")
        self.assertEqual((target.runtime / "state/qwenpaw/working/cron.json").read_text(), '{"enabled":true,"job":"preserve me"}')
        ups = [v for v, _ in docker.calls if "up" in v]
        self.assertIn("console", ups[0])
        self.assertIn("gateway", ups[0])

    def test_restore_requires_both_confirmations_before_stop(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target")
        plan, _ = self.archive_plan(source, target)
        for source_off, overwrite, code in ((False, True, "source_stop_confirmation_required"), (True, False, "overwrite_confirmation_required")):
            with self.assertRaisesRegex(BackupError, code):
                target.restore(plan["plan_id"], confirm_source_stopped=source_off, overwrite=overwrite)
        self.assertEqual(docker.changes(), [])
        self.assertEqual(self.chat(target), "target original chat")

    def test_wrong_password_and_ciphertext_tamper_do_not_change_target(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target")
        export = source.export(PASSWORD)
        archive = target.private / "upload.aqbackup"
        archive.write_bytes(Path(export["archive_path"]).read_bytes())
        with self.assertRaises(BackupError):
            target.inspect(archive, "wrong password long enough")
        content = bytearray(archive.read_bytes())
        content[-1] ^= 1
        archive.write_bytes(content)
        with self.assertRaises(BackupError):
            target.inspect(archive, PASSWORD)
        self.assertEqual(docker.changes(), [])
        self.assertEqual(self.chat(target), "target original chat")
        self.assertEqual(list((target.private / "plans").iterdir()), [])

    def test_staged_tamper_blocks_before_stop(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target")
        plan, _ = self.archive_plan(source, target)
        write(target.private / "plans" / plan["plan_id"] / "payload/state/qwenpaw/working/memory.md", "tampered")
        with self.assertRaisesRegex(BackupError, "snapshot_verification_failed"):
            target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertEqual(docker.changes(), [])

    def test_different_component_or_architecture_rejected(self):
        for incompatibility in ("version", "architecture"):
            with self.subTest(incompatibility=incompatibility):
                source, _, _ = self.fixture("source-" + incompatibility)
                target, docker, _ = self.fixture("target-" + incompatibility)
                if incompatibility == "version":
                    data = json.loads((target.root / "bundle-version.json").read_text())
                    data["components"]["astrbot"] = "different"
                    write(target.root / "bundle-version.json", json.dumps(data))
                else:
                    docker.engine_arch = "arm64"
                with self.assertRaisesRegex(BackupError, "component_version_mismatch|architecture_mismatch"):
                    self.archive_plan(source, target)
                self.assertEqual(docker.changes(), [])

    def test_health_failure_rolls_back_sqlite_and_env_exactly(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target", health_failures=1)
        original_env = (target.runtime / ".env").read_bytes()
        original_db = (target.runtime / "state/astrbot/chat.db").read_bytes()
        plan, _ = self.archive_plan(source, target)
        result = target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertEqual(result["state"], "rolled_back")
        self.assertFalse(result["recovery_required"])
        self.assertEqual((target.runtime / ".env").read_bytes(), original_env)
        self.assertEqual((target.runtime / "state/astrbot/chat.db").read_bytes(), original_db)
        self.assertEqual(self.chat(target), "target original chat")
        self.assertTrue(any("rollback-compose.json" in a for args, _ in docker.calls for a in args))

    def test_failed_rollback_blocks_then_new_host_recovery_is_idempotent(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target", health_failures=2)
        plan, _ = self.archive_plan(source, target)
        result = target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertTrue(result["recovery_required"])
        with self.assertRaisesRegex(BackupError, "recovery_required"):
            target.export(PASSWORD)
        replacement = Fixture(target.root, target.project, target.context, target.private, lambda _: None, runner=docker)
        docker.controller = replacement
        recovered = replacement.recover()
        self.assertEqual(recovered["state"], "rolled_back")
        self.assertEqual(self.chat(replacement), "target original chat")
        self.assertIsNone(replacement.recover())

    def test_rollback_never_starts_previously_stopped_scheduler(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target", health_failures=1)
        plan, _ = self.archive_plan(source, target)
        docker.running["qwenpaw"] = False
        result = target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertEqual(result["state"], "rolled_back")
        self.assertFalse(docker.running["qwenpaw"])
        created = [args for args, _ in docker.calls if "create" in args]
        self.assertEqual(len(created), 1)
        start = [args for args, _ in docker.calls if "compose" in args and "start" in args]
        self.assertNotIn("qwenpaw", start[-1])

    def test_refused_stop_never_copies_or_replaces_target(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target")
        plan, _ = self.archive_plan(source, target)
        docker.refuse_stop = True
        result = target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertTrue(result["recovery_required"])
        self.assertEqual(self.chat(target), "target original chat")
        journal = json.loads(target.journal_path.read_text())
        self.assertFalse(journal["backup_complete"])

    def test_external_mounts_remote_engine_addon_links_fail_before_stop(self):
        for case in ("extra", "remote", "addon", "path"):
            with self.subTest(case=case):
                c, docker, _ = self.fixture(case)
                if case == "extra":
                    docker.extra_mount = True
                elif case == "remote":
                    docker.endpoint = "ssh://server"
                elif case == "addon":
                    write(c.runtime / ".combined-setup.json", '{"format":1,"mode":"existing"}')
                else:
                    values = parse_env(c.runtime / ".env")
                    values["INSTALL_ROOT"] = "/outside"
                    write(c.runtime / ".env", "\n".join(k + "=" + env_value(v) for k, v in values.items()))
                with self.assertRaises(BackupError):
                    c.export(PASSWORD)
                self.assertEqual(docker.changes(), [])

    def test_crash_after_snapshot_before_replace_restores_on_startup(self):
        c, docker, _ = self.fixture("target")
        deployment = c._deployment()
        transaction = c._new_transaction("restore", deployment)
        c._checkpoint("stopping")
        c._stop()
        (transaction / "snapshot").mkdir()
        hashes = c._snapshot(transaction / "snapshot")
        write(transaction / "snapshot-hashes.json", json.dumps(hashes))
        c._checkpoint("installing", backup_complete=True)
        write(c.runtime / "state/qwenpaw/working/memory.md", "partial replacement")
        c2 = Fixture(c.root, c.project, c.context, c.private, lambda _: None, runner=docker)
        docker.controller = c2
        self.assertEqual(c2.recover()["state"], "rolled_back")
        self.assertEqual((c.runtime / "state/qwenpaw/working/memory.md").read_text(), "target original memory")
        self.assertTrue(all(docker.running.values()))

    def test_crash_during_export_restarts_without_replaying_jobs(self):
        c, docker, _ = self.fixture("source")
        deployment = c._deployment()
        c._new_transaction("export", deployment)
        c._checkpoint("stopping")
        c._stop()
        c2 = Fixture(c.root, c.project, c.context, c.private, lambda _: None, runner=docker)
        docker.controller = c2
        self.assertEqual(c2.recover()["state"], "rolled_back")
        self.assertTrue(all(docker.running.values()))
        self.assertFalse(any("exec" in args for args, _ in docker.calls))

    def test_expired_plan_and_short_password_no_service_mutation(self):
        source, _, _ = self.fixture("source")
        target, docker, _ = self.fixture("target")
        with self.assertRaisesRegex(BackupError, "invalid_password"):
            target.export("short")
        plan, _ = self.archive_plan(source, target)
        with patch("bundle_updater.backup_controller.time.time", return_value=plan["expires_at"] + 1):
            with self.assertRaisesRegex(BackupError, "restore_plan_expired"):
                target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        self.assertFalse((target.private / "plans" / plan["plan_id"]).exists())
        self.assertEqual(docker.changes(), [])

    def test_low_disk_and_catalog_tamper(self):
        c, docker, _ = self.fixture("source")
        with patch("bundle_updater.backup_controller.shutil.disk_usage", return_value=type("Disk", (), {"free": 1})()):
            with self.assertRaisesRegex(BackupError, "insufficient_backup_space"):
                c.export(PASSWORD)
        self.assertEqual(docker.changes(), [])
        exported = c.export(PASSWORD)
        with Path(exported["archive_path"]).open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(BackupError, "backup_archive_changed"):
            c.get_archive(exported["id"])


if __name__ == "__main__":
    unittest.main()
