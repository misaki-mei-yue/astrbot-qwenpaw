"""Signed mailbox -> real daemon -> real transaction controller, offline only."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from bundle_updater.common import atomic_json, read_json, verify, sign
from bundle_updater.daemon import UpdaterDaemon
from bundle_updater.releases import validate_archive
from deploy.prepare import env_value
from tests import test_update_controller as fixture


TOKEN = "integration-authentication-fixture-" + "x" * 40


class UpdateIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse synthetic files/fake Docker without importing TestCase classes
        # as globals, which would make unittest discover execute them twice.
        self.fixture = fixture.ControllerTests(methodName="test_success_preserves_data_and_extra_files_and_removes_obsolete_sources")
        original_sources = self.fixture.old_sources
        def sources_with_complete_host_package():
            return {**original_sources(), "deploy/prepare.py": "# synthetic deployment module\n",
                    "scripts/run_bundle_updater.py": "# synthetic entry\n",
                    "bundle_updater/__init__.py": "",
                    "bundle_updater/controller.py": "# synthetic controller module\n",
                    "bundle_updater/daemon.py": "# synthetic daemon module\n",
                    "bundle_updater/common.py": "# synthetic common module\n",
                    "bundle_updater/releases.py": "# synthetic release module\n"}
        self.fixture.old_sources = sources_with_complete_host_package
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        fixture.write(self.root / "runtime/.env", "QWENPAW_RUNTIME_INTERNAL_TOKEN=" + env_value(TOKEN) + "\nPROJECT_SOURCE=" + env_value(str(self.root)) + "\n")
        fixture.write(self.root / "docs/private-extra.md", "private extra document, never installer owned\n")
        fixture.write(self.root / "scripts/private-helper.py", "# private helper, never installer owned\n")
        self.clock = lambda: 1000.0
        self.health_failures = 0
        self.reports = []

    def checker(self, current_version, cache):
        self.assertEqual(current_version, "0.6.0-alpha")
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / "release.zip"
        shutil.copyfile(self.fixture.candidate["archive_path"], archive)
        return {**validate_archive(archive, "0.6.1-alpha"), "archive_path": str(archive),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "notes": "Synthetic update notes"}

    def make_daemon(self):
        def factory(root, project, context, private_dir, report):
            def observe(value):
                self.reports.append(value)
                report(value)
            return fixture.FixtureController(root, project, context, private_dir, observe,
                                             runner=self.fixture.docker, health_failures=self.health_failures)
        instance = UpdaterDaemon(self.root, self.fixture.project, "desktop-linux",
                                 controller_factory=factory, release_checker=self.checker,
                                 clock=self.clock, context_checker=lambda value: None)
        instance.initialize()
        self.assertTrue(instance.ready)
        self.assertTrue(instance.state["update_capable"])
        self.assertFalse(instance.state["recovery_required"])
        return instance

    def request(self, instance, action, candidate_id=None):
        identifier = str(uuid.uuid4())
        payload = {"id": identifier, "action": action, "created": self.clock()}
        if candidate_id is not None:
            payload["candidate_id"] = candidate_id
        atomic_json(instance.ipc / ("request-" + identifier + ".json"), sign(payload, TOKEN), mode=0o644)
        instance.run_once()
        if instance.worker:
            instance.worker.join(timeout=30)
            self.assertFalse(instance.worker.is_alive(), "Offline transaction did not finish")
        return verify(read_json(instance.ipc / "status.json"), TOKEN)

    def assert_private_extras_preserved(self):
        self.assertEqual((self.root / "docs/private-extra.md").read_text(), "private extra document, never installer owned\n")
        self.assertEqual((self.root / "scripts/private-helper.py").read_text(), "# private helper, never installer owned\n")
        self.assertEqual((self.root / "personal.keep").read_text(), "private extra file")

    def test_signed_check_apply_commits_and_reports_installed_new_version(self):
        instance = self.make_daemon()
        checked = self.request(instance, "check")
        self.assertEqual(checked["phase"], "available")
        self.assertEqual(checked["current_version"], "0.6.0-alpha")
        self.assertEqual(checked["candidate"]["version"], "0.6.1-alpha")
        self.assertEqual(set(checked["candidate"]), {"id", "version", "components", "notes", "html_url"})
        state = self.request(instance, "apply", checked["candidate"]["id"])
        self.assertEqual(state["phase"], "completed", state)
        self.assertEqual(state["current_version"], "0.6.1-alpha")
        self.assertFalse(state["recovery_required"])
        self.assertIsNone(state["candidate"])
        self.assertTrue(instance.restart)
        self.assertEqual((self.root / "README.md").read_text(), "new readme")
        self.assertEqual((self.root / "runtime/state/qwenpaw/chat.txt").read_text(), "original private conversation")
        self.assert_private_extras_preserved()
        phases = [item["phase"] for item in self.reports]
        self.assertIn("backed_up", phases)
        self.assertIn("installing", phases)
        self.assertIn("completed", phases)
        encoded = json.dumps(state)
        self.assertNotIn(TOKEN, encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("original private conversation", encoded)
        self.assertFalse((instance.private / "candidate.json").exists())

    def test_signed_apply_health_failure_restores_old_version_data_and_private_extras(self):
        self.health_failures = 1
        database = self.root / "runtime/state/qwenpaw/history.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE history(message TEXT)")
            connection.execute("INSERT INTO history VALUES (?)", ("original synthetic history",))
        def migrate_state():
            fixture.write(self.root / "runtime/state/qwenpaw/chat.txt", "candidate-only migration")
            fixture.write(self.root / "runtime/state/qwenpaw/candidate-schema.txt", "candidate-only schema")
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("ALTER TABLE history ADD COLUMN candidate_only TEXT")
                connection.execute("UPDATE history SET message='candidate changed history'")
        self.fixture.docker.on_candidate = migrate_state
        instance = self.make_daemon()
        checked = self.request(instance, "check")
        state = self.request(instance, "apply", checked["candidate"]["id"])
        self.assertEqual(state["phase"], "rolled_back", state)
        self.assertEqual(state["current_version"], "0.6.0-alpha")
        self.assertFalse(state["recovery_required"])
        self.assertFalse(instance.restart)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertEqual((self.root / "runtime/state/qwenpaw/chat.txt").read_text(), "original private conversation")
        self.assertFalse((self.root / "runtime/state/qwenpaw/candidate-schema.txt").exists())
        self.assertFalse((self.root / "scripts/new.py").exists())
        self.assertTrue((self.root / "scripts/obsolete.py").exists())
        with closing(sqlite3.connect(database)) as connection, connection:
            self.assertEqual(connection.execute("SELECT * FROM history").fetchall(), [("original synthetic history",)])
            self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(history)")], ["message"])
        self.assert_private_extras_preserved()
        self.assertIn("rolling_back", [item["phase"] for item in self.reports])
        self.assertIn("rolled_back", [item["phase"] for item in self.reports])
        # Failed candidates remain available for explicit inspection/recheck;
        # the controller/daemon do not silently replay the update.
        self.assertTrue((instance.private / "candidate.json").is_file())
        applied = self.fixture.docker.count("up")
        instance.run_once()
        self.assertEqual(self.fixture.docker.count("up"), applied)


if __name__ == "__main__":
    unittest.main()
