"""Privacy and reproducibility checks for the source-only release builder."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.write("README.md", "Public quick start\n")
        self.write("LICENSE", "Public project license\n")
        self.write("bundle-version.json", json.dumps({"format": 1, "version": release.DEFAULT_VERSION, "update_protocol": 1, "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}}))
        self.output = self.base / "bundle.zip"

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        return path

    def build(self, **kwargs):
        return release.build_release(self.root, self.output, **kwargs)

    def test_state_private_dumps_and_one_off_helpers_are_excluded(self):
        blocked = [
            ".git/config", ".env", ".env.production", "runtime/.env",
            "runtime/state/qwenpaw/session.json", "docs/legacy-memory-import-20261018.json",
            "docs/qq-network-fix-20261018.json", "docs/unknown-diagnostic.json",
            "scripts/cleanup_legacy_bootstrap.py", "scripts/cache/personal.json",
            "scripts/keys.pem", "tests/private.db", "deploy/old.zip", "docs/run.log",
            "tests/.env", "tests/secrets/provider.json", "unreviewed/private.py",
        ]
        for name in blocked:
            self.write(name, "private fixture material")
        self.write(".env.example", "API_KEY=\n")
        self.write("docs/qwenpaw-image-validation.json", '{"public":true}\n')
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            names = set(archive.namelist())
        self.assertFalse(names.intersection(blocked))
        self.assertIn(".env.example", names)
        self.assertIn("docs/qwenpaw-image-validation.json", names)

    def test_new_memory_compatibility_fix_is_included_with_exact_content(self):
        name = "qwenpaw_plugin_astrbot_bridge/deepseek_memory_compat.py"
        text = "def normalize_memory_messages(messages):\n    return list(messages)\n"
        self.write(name, text)
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read(name), text.encode())

    def test_copyright_and_third_party_license_text_are_included(self):
        public_files = {
            "COPYRIGHT.md": "Public contributor attribution\n",
            "licenses/NapCat-v4.18.33.txt": "Third-party license fixture\n",
            "licenses/QwenPaw.license": "Upstream license fixture\n",
            "licenses/LICENSE-NapCat": "Upstream original text fixture\n",
        }
        for name, text in public_files.items():
            self.write(name, text)
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            for name, text in public_files.items():
                self.assertEqual(archive.read(name), text.encode())

    def test_manifest_hashes_sizes_relative_names_and_reproducibility(self):
        self.write("deploy/setup.sh", "#!/bin/sh\ntrue\n")
        self.write("bundle_gateway/static/app.js", "console.log('public');\n")
        first_manifest = self.build()
        second_output = self.base / "other.zip"
        second_manifest = release.build_release(self.root, second_output)
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(self.output.read_bytes(), second_output.read_bytes())
        with zipfile.ZipFile(self.output) as archive:
            manifest = json.loads(archive.read(release.MANIFEST_NAME))
            self.assertEqual(manifest["version"], release.DEFAULT_VERSION)
            self.assertEqual(set(archive.namelist()), {item["path"] for item in manifest["files"]} | {release.MANIFEST_NAME})
            for item in manifest["files"]:
                path = item["path"]
                self.assertFalse(Path(path).is_absolute())
                self.assertNotIn("..", Path(path).parts)
                content = archive.read(path)
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
                self.assertEqual(len(content), item["size"])
            for info in archive.infolist():
                self.assertEqual(info.date_time, release.FIXED_ZIP_TIME)
                self.assertEqual(info.comment, b"")
        self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_secret_scan_does_not_echo_secret(self):
        fixture = "sk-" + "releaseTestOnly" * 3
        self.write("scripts/example.py", "API_KEY = " + repr(fixture))
        with self.assertRaises(release.ReleaseError) as caught:
            self.build()
        self.assertIn("provider-token", str(caught.exception))
        self.assertNotIn(fixture, str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_private_key_and_private_literal_are_rejected(self):
        marker = "-----BEGIN " + "PRIVATE KEY-----"
        self.write("README.md", marker)
        with self.assertRaisesRegex(release.ReleaseError, "private-key"):
            self.build()
        self.write("README.md", "fixture-account-owner")
        with self.assertRaisesRegex(release.ReleaseError, "private-pattern") as caught:
            self.build(private_patterns=("fixture-account-owner",))
        self.assertNotIn("fixture-account-owner", str(caught.exception))

    def test_publication_tool_and_its_tests_can_package_themselves(self):
        self.write("scripts/build_release.py", SCRIPT.read_text(encoding="utf-8"))
        self.write("tests/test_release_package.py", Path(__file__).read_text(encoding="utf-8"))
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            self.assertIn("scripts/build_release.py", archive.namelist())
            self.assertIn("tests/test_release_package.py", archive.namelist())

    def test_private_filename_is_rejected_without_disclosing_it(self):
        private_value = "fixture-private-owner"
        self.write("docs/" + private_value + ".md", "public-looking text")
        with self.assertRaisesRegex(release.ReleaseError, "Sensitive filename") as caught:
            self.build(private_patterns=(private_value,))
        self.assertNotIn(private_value, str(caught.exception))

    def test_binary_source_is_rejected(self):
        target = self.write("scripts/fixture.py", "placeholder")
        target.write_bytes(b"hello\0world")
        with self.assertRaisesRegex(release.ReleaseError, "Binary"):
            self.build()

    def test_output_cannot_overwrite_existing_file(self):
        self.output.write_bytes(b"existing")
        with self.assertRaisesRegex(release.ReleaseError, "already exists"):
            self.build()
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_root_version_and_path_policy(self):
        for version in ("../bad", "v0.5.0", "0.5", "0.5.0;exit"):
            with self.subTest(version=version), self.assertRaises(release.ReleaseError):
                self.build(version=version)
        self.assertFalse(release.is_public_path(Path("../README.md")))
        self.assertFalse(release.is_public_path(Path("runtime/data.json")))

    def test_private_patterns_file_must_remain_external(self):
        pattern_file = self.write("scripts/private-patterns.txt", "fixture-private-value\n")
        self.assertEqual(release.main([
            "--root", str(self.root), "--output", str(self.output),
            "--private-patterns", str(pattern_file),
        ]), 1)
        self.assertFalse(self.output.exists())

    def test_symlink_and_windows_junction_are_rejected(self):
        target = self.base / "external"
        target.mkdir()
        (target / "private.py").write_text("private fixture", encoding="utf-8")
        self.write("scripts/public.py", "pass\n")
        link = self.root / "scripts" / "linked"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, check=False)
            if result.returncode:
                self.skipTest("Windows junction creation is unavailable")
            self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        else:
            link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "Symlink or junction"):
            self.build()
        self.assertTrue((target / "private.py").exists())


if __name__ == "__main__":
    unittest.main()
