"""Encrypted multi-target backup integration over real local authenticated TLS.

Only Docker snapshotting is replaced by a synthetic controller. Configuration,
journals, scheduling worker, archive encryption, TLS/WebDAV requests, retry and
retention all run their production implementations. No real cloud is contacted.
"""
from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import ssl
import unittest
import uuid

from bundle_updater.backup_archive import create_backup, extract_backup
from bundle_updater.webdav_backup import WebDAVBackups
from bundle_updater.webdav_transport import WebDAVClient
from tests import test_webdav_transport as transport_fixtures


PASSWORD = "Integration encrypted archive password 123"
BUNDLE = {"format": 1, "version": "0.7.0-alpha", "update_protocol": 1,
          "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}}
DEPLOYMENT = {"layout": "bundle", "architecture": "amd64", "images": {"qwenpaw": "sha256:" + "a" * 64}}


class SyntheticSnapshotController:
    def __init__(self, root):
        self.root = root
        self.source = root / "snapshot"
        self.archives = root / "archives"
        self.archives.mkdir()
        self.calls, self.deleted = [], []
        for name in ("astrbot", "qwenpaw", "napcat"):
            (self.source / "state" / name).mkdir(parents=True)
        (self.source / "state/qwenpaw/memory.txt").write_text("仅测试：记得喝水，记忆可以恢复。", encoding="utf-8")
        (self.source / "state/astrbot/persona.json").write_text('{"name":"fixture-assistant"}', encoding="utf-8")
        (self.source / "state/napcat/config.json").write_text('{"fixture":true}', encoding="utf-8")
        (self.source / ".env").write_text("FIXTURE_SECRET=synthetic-only-secret\n", encoding="utf-8")

    def export(self, password, destination):
        self.calls.append(destination)
        backup_id = uuid.uuid4().hex
        create_backup(self.source, self.get_archive(backup_id), password,
                      bundle_metadata=BUNDLE, deployment=DEPLOYMENT)
        return {"id": backup_id}

    def get_archive(self, backup_id):
        return self.archives / (backup_id + ".aqbackup")

    def delete_archive(self, backup_id):
        self.get_archive(backup_id).unlink()
        self.deleted.append(backup_id)


class WebDAVIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tls = transport_fixtures.RealTLSTests()
        self.tls.setUp()
        self.addCleanup(self.tls.doCleanups)
        self.root = Path(self.tls.temporary.name)
        self.peer = self.tls.peer
        self.peer.preserve_bodies = True
        self.peer.directories.update({"/cloud-a/", "/cloud-b/"})
        self.expected = {"/cloud-a/": ("account-a", "app-password-a"),
                         "/cloud-b/": ("account-b", "app-password-b")}

        def authenticate(request):
            prefix = next((p for p in self.expected if request.path.startswith(p)), None)
            if prefix is None:
                return transport_fixtures.Response(403)
            authorization = "Basic " + base64.b64encode(":".join(self.expected[prefix]).encode()).decode()
            if request.headers.get("Authorization") != authorization:
                return transport_fixtures.Response(401, b"do not expose server secret text")
            return None

        self.peer.hook = authenticate
        self.controller = SyntheticSnapshotController(self.root)
        self.now = 1792842000.0
        self.first_id, self.second_id = "a" * 32, "b" * 32
        self.worker = self.restart()

    def client(self, target, cls=WebDAVClient):
        def connection(host, *, port, timeout, context):
            # The generated local certificate is trusted only by this test seam.
            trusted = ssl.create_default_context(cafile=str(self.tls.cert_path))
            return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=trusted)
        return cls(target, connection_factory=connection, timeout=5)

    def restart(self, client_factory=None):
        worker = WebDAVBackups(self.root / "private", self.controller,
            client_factory=client_factory or self.client, clock=lambda: self.now)
        worker._sleep = lambda _: None
        return worker

    def config(self):
        origin = f"https://127.0.0.1:{self.tls.httpd.server_port}"
        return {"enabled": True, "time": "03:00", "utc_offset_minutes": 480, "keep_last": 2,
                "password": PASSWORD, "destinations": [
                    {"id": self.first_id, "name": "Cloud A", "url": origin + "/cloud-a/",
                     "username": "account-a", "password": "app-password-a", "enabled": True},
                    {"id": self.second_id, "name": "Cloud B", "url": origin + "/cloud-b/",
                     "username": "account-b", "password": "app-password-b", "enabled": True}]}

    def path(self, cloud, backup_id):
        return f"/cloud-{cloud}/" + self.worker.config["namespace"] + "/" + backup_id + ".aqbackup"

    def test_auth_failure_isolated_and_retry_delivers_same_decryptable_archive(self):
        config = self.config()
        config["destinations"][0]["password"] = "incorrect-password"
        self.worker.save(config)
        self.assertEqual(self.worker.test(self.first_id)["error"], "authentication")
        self.assertTrue(self.worker.test(self.second_id)["ok"])
        run = self.worker.run()
        self.assertEqual(run["status"], "partial")
        self.assertEqual([r["status"] for r in run["targets"]], ["failed", "completed"])
        self.assertEqual(run["targets"][0]["error"], "authentication")
        self.assertEqual(self.controller.calls, ["server"])
        encrypted = self.peer.files[self.path("b", run["backup_id"])]["body"]
        self.assertNotIn(b"synthetic-only-secret", encrypted)
        downloaded = self.root / "downloaded.aqbackup"
        downloaded.write_bytes(encrypted)
        restored = self.root / "restored"
        extract_backup(downloaded, restored, PASSWORD)
        self.assertEqual((restored / "state/qwenpaw/memory.txt").read_text(encoding="utf-8"),
                         "仅测试：记得喝水，记忆可以恢复。")
        self.assertEqual((restored / ".env").read_text(encoding="utf-8"), "FIXTURE_SECRET=synthetic-only-secret\n")
        self.worker.save(self.config())
        retried = self.worker.retry(run["id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["backup_id"], run["backup_id"])
        self.assertEqual(self.controller.calls, ["server"])
        self.assertEqual(self.peer.files[self.path("a", run["backup_id"])]["body"], encrypted)
        public = json.dumps(self.worker.public())
        for secret in [PASSWORD, "app-password-a", "app-password-b", "do not expose server secret text"]:
            self.assertNotIn(secret, public)

    def test_restart_after_remote_publish_recovers_without_second_export(self):
        interrupted = [False]

        class PublishThenInterrupt(WebDAVClient):
            def upload(inner, *args, **kwargs):
                result = super().upload(*args, **kwargs)
                if not interrupted[0]:
                    interrupted[0] = True
                    raise KeyboardInterrupt()
                return result

        self.worker = self.restart(client_factory=lambda target: self.client(target, PublishThenInterrupt))
        self.worker.save(self.config())
        with self.assertRaises(KeyboardInterrupt):
            self.worker.run()
        self.worker = self.restart()
        pending = self.worker.public()["last_run"]
        self.assertEqual(pending["status"], "interrupted")
        existing = self.peer.files[self.path("a", pending["backup_id"])]["body"]
        retried = self.worker.retry(pending["id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.peer.files[self.path("a", pending["backup_id"])]["body"], existing)
        self.assertEqual(self.peer.files[self.path("b", pending["backup_id"])]["body"], existing)
        self.assertTrue(any(r.method == "GET" and r.path == self.path("a", pending["backup_id"])
                            for r in self.peer.requests))

    def test_retention_only_deletes_receipted_archives_on_both_clouds(self):
        self.worker.save(self.config())
        first = self.worker.run()
        second = self.worker.run()
        unrelated_id = "f" * 32
        for cloud in ("a", "b"):
            self.peer.files[self.path(cloud, unrelated_id)] = {"size": 12, "body": b"user archive"}
        self.worker = self.restart()
        third = self.worker.run()
        self.assertEqual([first["status"], second["status"], third["status"]], ["completed"] * 3)
        for cloud in ("a", "b"):
            self.assertNotIn(self.path(cloud, first["backup_id"]), self.peer.files)
            self.assertIn(self.path(cloud, second["backup_id"]), self.peer.files)
            self.assertIn(self.path(cloud, third["backup_id"]), self.peer.files)
            self.assertEqual(self.peer.files[self.path(cloud, unrelated_id)]["body"], b"user archive")
        self.assertEqual(self.controller.deleted, [first["backup_id"]])
        remote_deletes = [r.path for r in self.peer.requests if r.method == "DELETE"]
        self.assertEqual(set(remote_deletes), {self.path("a", first["backup_id"]), self.path("b", first["backup_id"])})


if __name__ == "__main__":
    unittest.main()
