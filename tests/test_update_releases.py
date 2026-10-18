"""Offline checks for release authenticity boundaries and hostile source ZIPs."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request
import zipfile

from bundle_updater import releases


VERSION = "0.6.0-alpha"
COMPONENTS = {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}


def archive_bytes(version=VERSION, files=None, *, mutate_manifest=None, metadata=None, extra=None):
    contents = {
        "README.md": b"Public source release\n", "LICENSE": b"Public license fixture\n",
        "bundle-version.json": json.dumps(metadata or {
            "format": 1, "version": version, "components": COMPONENTS, "update_protocol": 1,
        }).encode(),
        "scripts/start.sh": b"#!/bin/sh\necho ready\n",
    }
    if files:
        contents.update(files)
    manifest = {"format": 1, "version": version, "files": [
        {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in contents.items()
    ]}
    if mutate_manifest:
        mutate_manifest(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        archive.writestr(releases.MANIFEST_NAME, json.dumps(manifest).encode())
        if extra:
            for name, content in extra.items():
                archive.writestr(name, content)
    return output.getvalue()


def release_fixture(version=VERSION, *, payload=None, **changes):
    payload = archive_bytes(version) if payload is None else payload
    name = f"astrbot-qwenpaw-{version}.zip"
    root = f"https://github.com/{releases.REPOSITORY}/releases/download/v{version}/"
    digest = hashlib.sha256(payload).hexdigest()
    sums = f"{digest}  {name}\n".encode()
    release = {
        "id": 100, "tag_name": "v" + version, "draft": False,
        "prerelease": "-" in version, "body": "## Changes\n<b>Safe notes</b> [docs](https://example.test)\n",
        "assets": [
            {"id": 101, "name": name, "size": len(payload), "state": "uploaded",
             "digest": "sha256:" + digest, "browser_download_url": root + name},
            {"id": 102, "name": "SHA256SUMS", "size": len(sums), "state": "uploaded",
             "digest": "sha256:" + hashlib.sha256(sums).hexdigest(), "browser_download_url": root + "SHA256SUMS"},
        ],
    }
    release.update(changes)
    return release, {root + name: payload, root + "SHA256SUMS": sums}


class Response(io.BytesIO):
    def __init__(self, value, url, *, headers=None, status=200):
        super().__init__(value)
        self.url = url
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(value))}

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, releases_list, assets=None):
        self.responses = {releases.API_URL: json.dumps(releases_list).encode(), **(assets or {})}
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        value = self.responses[request.full_url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            payload, options = value
            return Response(payload, request.full_url, **options)
        return Response(value, request.full_url)


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "fixture.zip"

    def write(self, value):
        self.path.write_bytes(value)
        return self.path

    def rejected(self, value, code=None):
        self.write(value)
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.validate_archive(self.path, VERSION)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.base), str(caught.exception))

    def test_valid_manifest_and_safe_extraction(self):
        self.write(archive_bytes())
        verified = releases.validate_archive(self.path, VERSION)
        self.assertEqual(verified["components"], COMPONENTS)
        self.assertEqual(verified["version"], VERSION)
        self.assertEqual(verified["update_protocol"], 1)
        destination = self.base / "new-release"
        self.assertEqual(releases.extract_verified_archive(self.path, destination, VERSION), verified)
        self.assertEqual((destination / "scripts/start.sh").read_bytes(), b"#!/bin/sh\necho ready\n")
        self.assertTrue((destination / releases.MANIFEST_NAME).is_file())

    def test_existing_nonempty_destination_is_never_modified(self):
        self.write(archive_bytes())
        target = self.base / "existing"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.extract_verified_archive(self.path, target, VERSION)
        self.assertEqual(caught.exception.code, "destination_not_empty")
        self.assertEqual(list(target.iterdir()), [target / "keep.txt"])
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_invalid_package_does_not_create_destination(self):
        self.write(archive_bytes(mutate_manifest=lambda manifest: manifest["files"][0].update(sha256="0" * 64)))
        target = self.base / "not-created"
        with self.assertRaises(releases.ReleaseError):
            releases.extract_verified_archive(self.path, target, VERSION)
        self.assertFalse(target.exists())

    def test_tampered_hash_size_version_and_protocol_are_rejected(self):
        mutations = [
            (lambda manifest: manifest["files"][0].update(sha256="0" * 64), "manifest_hash_mismatch"),
            (lambda manifest: manifest["files"][0].update(size=999), "manifest_size_mismatch"),
            (lambda manifest: manifest.update(version="9.0.0"), "invalid_manifest"),
            (lambda manifest: manifest.update(format=True), "invalid_manifest"),
        ]
        for mutate, code in mutations:
            with self.subTest(code=code):
                self.rejected(archive_bytes(mutate_manifest=mutate), code)
        metadata = {"format": 1, "version": VERSION, "components": COMPONENTS, "update_protocol": 2}
        self.rejected(archive_bytes(metadata=metadata), "unsupported_update_protocol")

    def test_extra_and_missing_files_are_rejected(self):
        self.rejected(archive_bytes(extra={"docs/unlisted.md": b"unlisted"}), "manifest_file_mismatch")
        self.rejected(archive_bytes(mutate_manifest=lambda manifest: manifest["files"].pop()), "manifest_file_mismatch")

    def test_private_runtime_and_credential_paths_are_rejected(self):
        names = [
            "runtime/private.txt", ".git/config", ".env", "deploy/.env.production", "tests/private.db",
            "docs/run.log", "scripts/credentials.json", "deploy/secrets/key.txt", "tests/cookies/session.txt",
            "docs/secret.pem", "qwenpaw_plugin_astrbot_bridge/user_data/private.json",
        ]
        for name in names:
            with self.subTest(name=name):
                self.rejected(archive_bytes(files={name: b"not public"}), "private_archive_path")

    def test_unsafe_and_windows_special_paths_are_rejected(self):
        for name in [
            "../escape.txt", "/absolute.txt", "scripts/../../escape.py", "scripts\\escape.py",
            "scripts/name:stream.py", "scripts/CON.txt", "scripts/Lpt9.py", "scripts/com1/file.py",
            "scripts/NUL", "scripts/name. /file.py", "scripts/name./file.py", "scripts//empty.py",
            "scripts/./dot.py", "scripts/a\x01.py", "scripts/A?.py",
        ]:
            with self.subTest(name=name):
                self.rejected(archive_bytes(files={name: b"blocked"}), "unsafe_archive_path")

    def test_casefold_duplicates_and_file_directory_collisions_are_rejected(self):
        self.rejected(archive_bytes(files={"scripts/A.py": b"a", "scripts/a.py": b"b"}), "duplicate_archive_path")
        self.rejected(archive_bytes(files={"scripts/a": b"a", "scripts/a/child.py": b"b"}), "duplicate_archive_path")

    def test_nul_in_original_zip_filename_is_not_silently_truncated(self):
        payload = archive_bytes().replace(b"README.md", b"README.m\x00")
        self.rejected(payload, "unsafe_archive_path")

    def test_symbolic_links_and_directory_entries_are_rejected(self):
        for mode, name in [(stat.S_IFLNK | 0o777, "scripts/link.py"), (stat.S_IFDIR | 0o755, "scripts/directory")]:
            output = io.BytesIO(archive_bytes())
            with zipfile.ZipFile(output, "a") as archive:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, b"target")
            with self.subTest(name=name):
                self.rejected(output.getvalue(), "unsafe_archive_entry")

    def test_oversize_compression_bombs_and_file_count_are_rejected(self):
        with patch.object(releases, "MAX_ARCHIVE_BYTES", 10):
            self.rejected(archive_bytes(), "invalid_archive_size")
        with patch.object(releases, "MAX_FILE_BYTES", 10):
            self.rejected(archive_bytes(), "invalid_archive_size")
        with patch.object(releases, "MAX_EXPANDED_BYTES", 20):
            self.rejected(archive_bytes(), "invalid_archive_size")
        with patch.object(releases, "MAX_FILES", 2):
            self.rejected(archive_bytes(), "invalid_archive_count")
        self.rejected(archive_bytes(files={"docs/bomb.txt": b"x" * (1024 * 1024 + 1)}), "archive_compression_ratio")

    def test_secret_content_and_binary_are_rejected(self):
        self.rejected(archive_bytes(files={"docs/credentials.txt": ("sk-" + "A" * 40).encode()}), "private_archive_content")
        self.rejected(archive_bytes(files={"docs/binary.txt": b"text\x00binary"}), "private_archive_content")

    def test_duplicate_json_fields_are_rejected(self):
        bad = b'{"format":1,"format":1,"version":"0.6.0-alpha","files":[]}'
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(releases.MANIFEST_NAME, bad)
            archive.writestr(releases.VERSION_NAME, b"{}")
        self.rejected(output.getvalue(), "invalid_metadata")

    def test_symlink_local_paths_are_rejected_where_supported(self):
        self.write(archive_bytes())
        link = self.base / "link.zip"
        try:
            link.symlink_to(self.path)
        except (NotImplementedError, OSError):
            self.skipTest("Symbolic links require platform permission")
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.validate_archive(link, VERSION)
        self.assertEqual(caught.exception.code, "unsafe_local_path")


class ReleaseDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache"

    def check(self, rows, assets=None, current="0.5.0-alpha", **kwargs):
        self.opener = FakeOpener(rows, assets)
        return releases.check_for_update(current, self.cache, opener=self.opener, **kwargs)

    def test_fetches_only_fixed_repository_and_validates_full_package(self):
        release, assets = release_fixture()
        result = self.check([release], assets)
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["tag"], "v" + VERSION)
        self.assertEqual(result["release_id"], 100)
        self.assertEqual(result["asset_id"], 101)
        self.assertEqual(result["components"], COMPONENTS)
        self.assertTrue(Path(result["archive_path"]).is_file())
        self.assertEqual(result["sha256"], hashlib.sha256(Path(result["archive_path"]).read_bytes()).hexdigest())
        self.assertNotIn("<b>", result["notes"])
        self.assertNotIn("https://example.test", result["notes"])
        self.assertTrue(all(timeout == releases.REQUEST_TIMEOUT for _, timeout in self.opener.calls))

    def test_no_new_release_never_creates_cache_or_downloads_assets(self):
        release, assets = release_fixture(version="0.4.0")
        self.assertIsNone(self.check([release], assets))
        self.assertFalse(self.cache.exists())
        self.assertEqual(len(self.opener.calls), 1)

    def test_semver_order_including_prereleases_and_stable(self):
        rows = []
        assets = {}
        for version in ["0.6.0-alpha.9", "0.6.0-alpha.10", "0.6.0-beta", "0.6.0", "0.5.99", "0.6.0-01"]:
            row, content = release_fixture(version=version)
            rows.append(row)
            assets.update(content)
        self.assertEqual(self.check(rows, assets)["version"], "0.6.0")
        self.assertEqual(self.check(rows[:2], assets)["version"], "0.6.0-alpha.10")
        self.assertEqual(self.check(rows[:3], assets)["version"], "0.6.0-beta")

    def test_stable_default_excludes_prereleases_but_alpha_includes_them(self):
        release, assets = release_fixture()
        self.assertIsNone(self.check([release], assets, current="0.5.0"))
        self.assertIsNotNone(self.check([release], assets, current="0.5.0", allow_prerelease=True))
        self.assertIsNone(self.check([release], assets, allow_prerelease=False))

    def test_drafts_invalid_versions_and_downgrades_are_ignored(self):
        draft, _ = release_fixture(version="9.0.0", draft=True)
        invalid, _ = release_fixture(version="01.2.3")
        older, _ = release_fixture(version="0.4.0")
        equal, _ = release_fixture(version="0.5.0-alpha")
        self.assertIsNone(self.check([draft, invalid, older, equal]))
        same_build, _ = release_fixture(version="0.5.0+other")
        self.assertIsNone(self.check([same_build], current="0.5.0+current"))

    def test_asset_metadata_cannot_point_to_other_repository_or_http(self):
        for location in [
            "https://github.com/other/project/releases/download/v1/bundle.zip",
            "http://github.com/example/release.zip", "https://127.0.0.1/private",
        ]:
            release, assets = release_fixture()
            release["assets"][0]["browser_download_url"] = location
            with self.subTest(location=location), self.assertRaises(releases.ReleaseError) as caught:
                self.check([release], assets)
            self.assertEqual(caught.exception.code, "unsafe_url")

    def test_redirect_allowlist_and_default_handler_reject_untrusted_origins(self):
        good = [
            "https://release-assets.githubusercontent.com/github-production-release-asset/1?sig=fixture",
            "https://objects.githubusercontent.com/github-production-release-asset/1?sig=fixture",
        ]
        bad = [
            "http://release-assets.githubusercontent.com/file", "https://release-assets.githubusercontent.com.evil.test/file",
            "https://user:password@github.com/anything", "https://api.github.com/repos/other/project/releases",
            "https://github.com/other/project/releases/download/v1/file", "https://localhost/file",
            "https://release-assets.githubusercontent.com:8443/file",
        ]
        self.assertTrue(all(releases._allowed_url(url) for url in good))
        self.assertFalse(any(releases._allowed_url(url) for url in bad))
        handler = releases._SafeRedirect()
        for location in bad:
            with self.subTest(location=location), self.assertRaises(releases.ReleaseError):
                handler.redirect_request(Request(releases.API_URL), None, 302, "redirect", {}, location)

    def test_response_redirect_is_rechecked_with_injected_transport(self):
        class Redirected(FakeOpener):
            def open(self, request, timeout):
                response = super().open(request, timeout)
                response.url = "https://untrusted.test/private?token=private-fixture"
                return response
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.check_for_update("0.5.0-alpha", self.cache, opener=Redirected([]))
        self.assertEqual(caught.exception.code, "unsafe_redirect")
        self.assertNotIn("private-fixture", str(caught.exception))

    def test_forged_checksum_and_digest_disagreement_are_rejected(self):
        release, assets = release_fixture()
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(releases.ReleaseError) as caught:
            self.check([release], assets)
        self.assertEqual(caught.exception.code, "checksum_digest_mismatch")
        release, assets = release_fixture()
        package = release["assets"][0]
        package["digest"] = None
        sums_asset = release["assets"][1]
        forged = f"{'0' * 64}  {package['name']}\n".encode()
        sums_asset["digest"] = "sha256:" + hashlib.sha256(forged).hexdigest()
        sums_asset["size"] = len(forged)
        assets[sums_asset["browser_download_url"]] = forged
        with self.assertRaises(releases.ReleaseError) as caught:
            self.check([release], assets)
        self.assertEqual(caught.exception.code, "checksum_mismatch")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_duplicate_checksum_target_and_changed_checksum_digest_are_rejected(self):
        release, assets = release_fixture()
        asset = release["assets"][1]
        sums = assets[asset["browser_download_url"]]
        assets[asset["browser_download_url"]] = sums + sums
        asset["size"] = len(sums) * 2
        asset["digest"] = "sha256:" + hashlib.sha256(sums + sums).hexdigest()
        with self.assertRaises(releases.ReleaseError) as caught:
            self.check([release], assets)
        self.assertEqual(caught.exception.code, "invalid_checksum")
        asset["digest"] = "sha256:" + "1" * 64
        with self.assertRaises(releases.ReleaseError) as caught:
            self.check([release], assets)
        self.assertEqual(caught.exception.code, "checksum_digest_mismatch")

    def test_legacy_package_without_update_manifest_cannot_install(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("README.md", "old release")
        release, assets = release_fixture(payload=output.getvalue())
        with self.assertRaises(releases.ReleaseError) as caught:
            self.check([release], assets)
        self.assertEqual(caught.exception.code, "missing_manifest")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_network_error_and_rate_limit_do_not_disclose_response_or_url(self):
        for error, expected in [
            (URLError("https://private.test/?token=never-show"), "download_failed"),
            (HTTPError(releases.API_URL, 403, "secret body", {}, None), "rate_limited"),
        ]:
            opener = FakeOpener([])
            opener.responses[releases.API_URL] = error
            with self.subTest(expected=expected), self.assertRaises(releases.ReleaseError) as caught:
                releases.check_for_update("0.5.0-alpha", self.cache, opener=opener)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("never-show", str(caught.exception))
            self.assertNotIn("secret body", str(caught.exception))

    def test_stream_size_and_content_length_are_enforced(self):
        opener = FakeOpener([])
        opener.responses[releases.API_URL] = (b"[]", {"headers": {"Content-Length": "9000000"}})
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.check_for_update("0.5.0-alpha", self.cache, opener=opener)
        self.assertEqual(caught.exception.code, "download_too_large")
        opener.responses[releases.API_URL] = (b"[]", {"headers": {"Content-Length": "4"}})
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.check_for_update("0.5.0-alpha", self.cache, opener=opener)
        self.assertEqual(caught.exception.code, "incomplete_download")

    def test_notes_are_plain_text_and_bounded(self):
        release, assets = release_fixture(body="<b>hello</b>" + "x" * 10000)
        result = self.check([release], assets)
        self.assertEqual(len(result["notes"]), 8000)
        self.assertTrue(result["notes"].startswith("hello"))
        self.assertNotIn("<", result["notes"])

    def test_release_error_redacts_tokens_and_local_paths(self):
        private_path = "C:" + "\\" + "U" + "sers" + "\\Fixture\\private"
        error = releases.ReleaseError("bad code", "failed https://example.test/?secret=fixture " + private_path)
        self.assertEqual(error.code, "update_error")
        self.assertNotIn("example.test", str(error))
        self.assertNotIn("Fixture", str(error))


if __name__ == "__main__":
    unittest.main()
