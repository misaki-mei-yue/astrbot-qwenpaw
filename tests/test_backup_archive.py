"""Portable backup integrity, authentication and hostile ZIP tests."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from bundle_updater import backup_archive as archive


PASSWORD = "fixture only secret 123"
BUNDLE = {"format": 1, "version": "0.7.0-alpha", "update_protocol": 1,
          "components": {"astrbot": "4.25.1", "qwenpaw": "2.2.1", "napcat": "4.18.33"}}
DEPLOYMENT = {"layout": "bundle", "architecture": "amd64", "images": {"qwenpaw": "sha256:" + "a" * 64}}


class BackupArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "snapshot"
        (self.source / "state" / "qwenpaw" / "empty").mkdir(parents=True)
        self.archive = self.root / "fixture.aqbackup"
        self.destination = self.root / "restored"

    def create(self, password=PASSWORD):
        return archive.create_backup(self.source, self.archive, password, bundle_metadata=deepcopy(BUNDLE), deployment=deepcopy(DEPLOYMENT))

    def assert_rejected(self, password=PASSWORD, codes=None):
        with self.assertRaises(archive.BackupArchiveError) as caught:
            archive.extract_backup(self.archive, self.destination, password)
        if codes is not None:
            self.assertIn(caught.exception.code, codes)
        self.assertEqual(str(caught.exception), caught.exception.code)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".aqbackup-*")), [])
        return caught.exception

    def crafted(self, *, payloads=None, directories=None, alter=None, extras=None, comment=b"", manifest_raw=None):
        payloads = {"state/data.bin": b"fixture"} if payloads is None else payloads
        directories = ["state"] if directories is None else directories
        manifest = {"format": 1, "kind": "astrbot-qwenpaw-backup", "created_at": "2026-10-19T00:00:00+00:00",
                    "bundle": deepcopy(BUNDLE), "deployment": deepcopy(DEPLOYMENT),
                    "files": [{"path": name, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()} for name, value in payloads.items()],
                    "directories": list(directories), "total_size": sum(len(value) for value in payloads.values()), "file_count": len(payloads)}
        if alter:
            alter(manifest)
        plaintext = self.root / "crafted.zip"
        with zipfile.ZipFile(plaintext, "w") as bundle:
            bundle.comment = comment
            for directory in directories:
                bundle.writestr(archive._zip_info(directory, True), b"")
            for name, value in payloads.items():
                bundle.writestr(archive._zip_info(name), value)
            for info, value in extras or []:
                bundle.writestr(info, value)
            bundle.writestr("manifest.json", manifest_raw if manifest_raw is not None else archive._canonical(manifest))
        archive._encrypt(plaintext, self.archive, PASSWORD.encode())
        plaintext.unlink()
        return manifest

    def test_roundtrip_sqlite_credentials_binary_unicode_and_empty_directories(self):
        (self.source / ".env").write_text("QWENPAW_TOKEN=fixture-private\n", encoding="utf-8")
        (self.source / ".combined-setup.json").write_text('{"migrated":true}', encoding="utf-8")
        database = self.source / "state" / "qwenpaw" / "memory.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE memory (content TEXT)")
            connection.execute("INSERT INTO memory VALUES (?)", ("记得喝水😋",))
            connection.commit()
        binary = os.urandom(archive.CHUNK_SIZE + 257)
        (self.source / "state" / "qwenpaw" / "附件.bin").write_bytes(binary)
        created = self.create()
        restored = archive.extract_backup(self.archive, self.destination, PASSWORD)
        self.assertEqual(created, restored)
        self.assertEqual(restored["bundle"], BUNDLE)
        self.assertEqual(restored["deployment"], DEPLOYMENT)
        self.assertEqual(restored["file_count"], 4)
        self.assertEqual((self.destination / "state" / "qwenpaw" / "附件.bin").read_bytes(), binary)
        self.assertTrue((self.destination / "state" / "qwenpaw" / "empty").is_dir())
        with closing(sqlite3.connect(self.destination / "state" / "qwenpaw" / "memory.db")) as connection:
            self.assertEqual(connection.execute("SELECT content FROM memory").fetchall(), [("记得喝水😋",)])
        self.assertFalse((self.destination / "manifest.json").exists())
        encrypted = self.archive.read_bytes()
        self.assertNotIn(b"QWENPAW_TOKEN", encrypted)
        self.assertNotIn(b"SQLite format", encrypted)
        self.assertEqual(list(self.root.glob(".aqbackup-*")), [])

    def test_empty_snapshot_and_empty_existing_destination_are_supported(self):
        self.create()
        self.destination.mkdir()
        manifest = archive.extract_backup(self.archive, self.destination, PASSWORD)
        self.assertEqual(manifest["files"], [])
        self.assertTrue((self.destination / "state" / "qwenpaw" / "empty").is_dir())

    def test_only_safe_owner_executable_bit_is_preserved(self):
        script = self.source / "state" / "run.sh"
        script.write_bytes(b"#!/bin/sh\nprintf fixture\\n\n")
        script.chmod(0o777)
        expected_mode = 0o700 if script.stat().st_mode & stat.S_IXUSR else 0o600
        created = self.create()
        row = next(item for item in created["files"] if item["path"] == "state/run.sh")
        self.assertEqual(row["mode"], expected_mode)
        archive.extract_backup(self.archive, self.destination, PASSWORD)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.destination / "state/run.sh").stat().st_mode), expected_mode)

    def test_older_manifest_without_mode_defaults_to_owner_read_write(self):
        self.crafted()
        manifest = archive.extract_backup(self.archive, self.destination, PASSWORD)
        self.assertNotIn("mode", manifest["files"][0])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.destination / "state/data.bin").stat().st_mode), 0o600)

    def test_unsafe_modes_and_manifest_zip_mode_disagreements_are_rejected(self):
        for mode in (0o777, 0o4600, True, 384.0, 0o700):
            self.crafted(alter=lambda value: value["files"][0].update(mode=mode))
            self.assert_rejected(codes={"payload_invalid"})
            self.archive.unlink()

    def test_unicode_password_is_preserved_without_trim_or_normalization(self):
        password = "  我爱自己的长期记忆和文件  "
        self.create(password)
        self.assert_rejected(password.strip(), {"authentication"})
        archive.extract_backup(self.archive, self.destination, password)
        self.assertTrue(self.destination.is_dir())

    def test_password_length_and_utf8_limits(self):
        for password in ("short", "a" * 11, "中" * 342, None, "\ud800" * 12):
            with self.subTest(password_length=len(password) if isinstance(password, str) else None):
                with self.assertRaises(archive.BackupArchiveError) as caught:
                    self.create(password)
                self.assertEqual(caught.exception.code, "password")
                self.assertFalse(self.archive.exists())
        self.create(" " * 12)
        archive.extract_backup(self.archive, self.destination, " " * 12)

    def test_random_salt_and_nonce_make_same_snapshot_distinct(self):
        self.create()
        second = self.root / "second.aqbackup"
        archive.create_backup(self.source, second, PASSWORD, bundle_metadata=BUNDLE, deployment=DEPLOYMENT)
        self.assertNotEqual(self.archive.read_bytes()[:archive.HEADER.size], second.read_bytes()[:archive.HEADER.size])

    def test_wrong_password_accepts_no_payload_and_removes_plaintext(self):
        (self.source / "state" / "secret.txt").write_text("do not leak", encoding="utf-8")
        self.create()
        self.assert_rejected("wrong fixture password", {"authentication"})

    def test_wrong_password_preserves_existing_empty_destination(self):
        self.create()
        self.destination.mkdir()
        with self.assertRaises(archive.BackupArchiveError):
            archive.extract_backup(self.archive, self.destination, "wrong fixture password")
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_tampered_header_ciphertext_and_tag_are_rejected(self):
        self.create()
        original = self.archive.read_bytes()
        for offset in (8, 24, archive.HEADER.size + 5, len(original) - 1):
            with self.subTest(offset=offset):
                modified = bytearray(original)
                modified[offset] ^= 1
                self.archive.write_bytes(modified)
                self.assert_rejected(codes={"authentication", "archive_invalid"})

    def test_truncation_and_missing_final_marker_are_rejected(self):
        self.create()
        original = self.archive.read_bytes()
        for length in (0, 7, archive.HEADER.size, len(original) - 1, len(original) - 20):
            with self.subTest(length=length):
                self.archive.write_bytes(original[:length])
                self.assert_rejected(codes={"archive_invalid"})

    def test_final_marker_and_trailing_data_are_authenticated(self):
        self.create()
        original = self.archive.read_bytes()
        self.archive.write_bytes(original + b"untrusted trailing data")
        self.assert_rejected(codes={"archive_invalid"})
        # Replacing a data record by a zero length marker cannot authenticate.
        modified = bytearray(original)
        modified[archive.HEADER.size:archive.HEADER.size + 4] = b"\x00" * 4
        self.archive.write_bytes(modified)
        self.assert_rejected(codes={"authentication"})

    def test_record_reorder_and_duplication_are_rejected(self):
        (self.source / "state" / "large.bin").write_bytes(os.urandom(archive.CHUNK_SIZE * 2 + 512))
        self.create()
        original = self.archive.read_bytes()
        records, cursor = [], archive.HEADER.size
        while cursor < len(original):
            length = archive.RECORD.unpack(original[cursor:cursor + 4])[0]
            end = cursor + 4 + length + 16
            records.append(original[cursor:end])
            cursor = end
        self.assertGreaterEqual(len(records), 4)
        for modified in ([records[1], records[0]] + records[2:], [records[0], records[0]] + records[2:]):
            self.archive.write_bytes(original[:archive.HEADER.size] + b"".join(modified))
            self.assert_rejected(codes={"authentication"})

    def test_invalid_magic_and_unbounded_record_length_fail_before_payload(self):
        self.create()
        original = self.archive.read_bytes()
        self.archive.write_bytes(b"BADMAGIC" + original[8:])
        self.assert_rejected(codes={"archive_invalid"})
        self.archive.write_bytes(original[:archive.HEADER.size] + struct.pack(">I", archive.CHUNK_SIZE + 1))
        self.assert_rejected(codes={"archive_invalid"})

    def test_existing_output_archive_is_never_overwritten(self):
        self.archive.write_bytes(b"keep this backup")
        with self.assertRaises(archive.BackupArchiveError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "destination_invalid")
        self.assertEqual(self.archive.read_bytes(), b"keep this backup")

    def test_existing_destination_data_is_never_merged_or_overwritten(self):
        self.create()
        self.destination.mkdir()
        (self.destination / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(archive.BackupArchiveError) as caught:
            archive.extract_backup(self.archive, self.destination, PASSWORD)
        self.assertEqual(caught.exception.code, "destination_invalid")
        self.assertEqual((self.destination / "keep.txt").read_text(), "keep")

    def test_source_rejects_unmanaged_code_and_windows_unsafe_names(self):
        for relative in ("compose.yaml", "state/CON.txt", "state/file.", "state/file:ads", "state/bad\\name"):
            with self.subTest(relative=relative):
                if os.name == "nt" and relative != "compose.yaml":
                    # Win32 normalizes trailing dots and treats colons as streams;
                    # hostile archive names are tested independently below.
                    continue
                target = self.source / relative
                try:
                    target.write_text("fixture", encoding="utf-8")
                except OSError:
                    continue
                try:
                    with self.assertRaises(archive.BackupArchiveError):
                        self.create()
                    self.assertFalse(self.archive.exists())
                finally:
                    target.unlink()

    def test_source_hardlinks_and_linked_ancestors_are_rejected(self):
        target = self.source / "state" / "real.txt"
        target.write_text("keep", encoding="utf-8")
        linked = self.source / "state" / "linked.txt"
        try:
            os.link(target, linked)
        except OSError:
            self.skipTest("Hard links unavailable")
        with self.assertRaises(archive.BackupArchiveError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "source_invalid")

    def test_source_symlink_is_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("private other data", encoding="utf-8")
        try:
            (self.source / "state" / "linked").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(archive.BackupArchiveError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "source_invalid")

    def test_manifest_payload_hash_size_and_count_must_match(self):
        mutations = [lambda value: value["files"][0].update(sha256="0" * 64),
                     lambda value: value["files"][0].update(size=0),
                     lambda value: value.update(total_size=0),
                     lambda value: value.update(file_count=2),
                     lambda value: value.update(format=2),
                     lambda value: value["bundle"].update(version="../../new-code"),
                     lambda value: value.update(kind="another-backup")]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.crafted(alter=mutate)
                self.assert_rejected(codes={"payload_invalid"})
                self.archive.unlink()

    def test_manifest_rejects_duplicate_json_keys(self):
        self.crafted(manifest_raw=b'{"format":1,"format":2}')
        self.assert_rejected(codes={"payload_invalid"})

    def test_unlisted_payload_and_missing_payload_are_rejected(self):
        self.crafted(extras=[("state/extra.txt", b"not listed")])
        self.assert_rejected(codes={"payload_invalid"})
        self.archive.unlink()
        self.crafted(alter=lambda value: value["files"][0].update(path="state/missing.txt"))
        self.assert_rejected(codes={"payload_invalid"})

    def test_traversal_drive_unc_ads_device_and_backslash_paths_are_rejected(self):
        names = ["../outside", "/absolute", "C:/outside", "//server/share", "state/../../outside",
                 "state/a\\b", "state/file:ads", "state/NUL", "state/COM1.txt", "state/LPT\u00b9.log",
                 "state/trailing.", "state/trailing ", "state//double", "state/./dot", "state/bad\x01name", ".env/child"]
        for name in names:
            with self.subTest(name=name):
                self.crafted(payloads={name: b"fixture"})
                self.assert_rejected(codes={"payload_invalid"})
                self.archive.unlink()
        self.assertFalse((self.root / "outside").exists())

    def test_case_unicode_normalization_and_file_directory_collisions_are_rejected(self):
        payloads = [{"state/A.txt": b"a", "state/a.txt": b"b"},
                    {"state/\u00e9.txt": b"a", "state/e\u0301.txt": b"b"}]
        for data in payloads:
            self.crafted(payloads=data)
            self.assert_rejected(codes={"payload_invalid"})
            self.archive.unlink()
        self.crafted(payloads={"state/nested": b"not directory"}, directories=["state", "state/nested"])
        self.assert_rejected(codes={"payload_invalid"})

    def test_payload_parent_directory_must_be_explicit_and_exact_case(self):
        self.crafted(payloads={"state/nested/file": b"fixture"}, directories=["state"])
        self.assert_rejected(codes={"payload_invalid"})

    def test_zip_symlink_special_file_and_dos_reparse_are_rejected(self):
        for mode, low_attributes in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFIFO | 0o600, 0), (stat.S_IFREG | 0o600, 0x400)):
            info = zipfile.ZipInfo("state/link")
            info.create_system = 3
            info.external_attr = (mode << 16) | low_attributes
            self.crafted(extras=[(info, b"target")])
            self.assert_rejected(codes={"payload_invalid"})
            self.archive.unlink()

    def test_zip_encrypted_flag_is_rejected(self):
        self.crafted()
        plaintext = self.root / "mutate.zip"
        archive._decrypt(self.archive, plaintext, PASSWORD.encode())
        data = bytearray(plaintext.read_bytes())
        # Flip the central-directory encrypted flag; no decryptor is ever invoked.
        central = data.index(b"PK\x01\x02")
        data[central + 8] |= 1
        plaintext.write_bytes(data)
        self.archive.unlink()
        archive._encrypt(plaintext, self.archive, PASSWORD.encode())
        plaintext.unlink()
        self.assert_rejected(codes={"payload_invalid"})

    def test_zip_comment_and_unsupported_compression_are_rejected(self):
        self.crafted(comment=b"unnecessary untrusted extra metadata")
        self.assert_rejected(codes={"payload_invalid"})
        self.archive.unlink()
        info = zipfile.ZipInfo("state/extra")
        info.compress_type = zipfile.ZIP_BZIP2
        self.crafted(extras=[(info, b"x")])
        self.assert_rejected(codes={"payload_invalid"})

    def test_manifest_file_and_expanded_limits_reject_before_extraction(self):
        self.crafted(payloads={"state/bomb": b"0" * 65536})
        with patch.object(archive, "MAX_FILE_BYTES", 1024):
            self.assert_rejected(codes={"archive_limit"})
        with patch.object(archive, "MAX_EXPANDED_BYTES", 1024):
            self.assert_rejected(codes={"archive_limit"})
        with patch.object(archive, "MAX_MANIFEST_BYTES", 10):
            self.assert_rejected(codes={"payload_invalid"})
        with patch.object(archive, "MAX_ENTRIES", 2):
            self.assert_rejected(codes={"archive_limit"})

    def test_archive_and_source_limits_cleanup_partial_files(self):
        (self.source / "state" / "data").write_bytes(b"a" * 64)
        with patch.object(archive, "MAX_FILE_BYTES", 8):
            with self.assertRaises(archive.BackupArchiveError) as caught:
                self.create()
            self.assertEqual(caught.exception.code, "archive_limit")
        with patch.object(archive, "MAX_ARCHIVE_BYTES", 128):
            with self.assertRaises(archive.BackupArchiveError) as caught:
                self.create()
            self.assertEqual(caught.exception.code, "archive_limit")
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob(".aqbackup-*")), [])
        self.create()
        with patch.object(archive, "MAX_ARCHIVE_BYTES", 128):
            self.assert_rejected(codes={"archive_limit"})

    def test_source_entry_limit_is_enforced_during_directory_enumeration(self):
        for index in range(10):
            (self.source / "state" / ("file-" + str(index))).write_bytes(b"fixture")
        with patch.object(archive, "MAX_ENTRIES", 6):
            with self.assertRaises(archive.BackupArchiveError) as caught:
                self.create()
            self.assertEqual(caught.exception.code, "archive_limit")
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob(".aqbackup-*")), [])

    def test_corrupt_zip_crc_rejects_every_payload_before_destination_creation(self):
        self.crafted()
        plaintext = self.root / "mutate.zip"
        archive._decrypt(self.archive, plaintext, PASSWORD.encode())
        with zipfile.ZipFile(plaintext) as bundle:
            central = bundle.start_dir
        data = bytearray(plaintext.read_bytes())
        while True:
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", data, central + 28)
            if data[central + 46:central + 46 + name_length] == b"state/data.bin":
                data[central + 16] ^= 1  # CRC must match the plaintext content.
                break
            central += 46 + name_length + extra_length + comment_length
        plaintext.write_bytes(data)
        self.archive.unlink()
        archive._encrypt(plaintext, self.archive, PASSWORD.encode())
        plaintext.unlink()
        self.assert_rejected(codes={"archive_invalid", "payload_invalid"})

    def test_central_directory_count_is_checked_before_zipfile_allocation(self):
        self.crafted()
        plaintext = self.root / "mutate.zip"
        archive._decrypt(self.archive, plaintext, PASSWORD.encode())
        data = bytearray(plaintext.read_bytes())
        # ZIP readers otherwise trust the central directory contents over this
        # advertised count and may allocate every entry before the caller checks.
        struct.pack_into("<HH", data, len(data) - 14, 1, 1)
        plaintext.write_bytes(data)
        self.archive.unlink()
        archive._encrypt(plaintext, self.archive, PASSWORD.encode())
        plaintext.unlink()
        with patch.object(archive.zipfile, "ZipFile", side_effect=AssertionError("must reject before parsing")):
            self.assert_rejected(codes={"archive_limit"})

    def test_plain_zip_prefix_or_trailer_is_rejected(self):
        self.crafted()
        plaintext = self.root / "mutate.zip"
        archive._decrypt(self.archive, plaintext, PASSWORD.encode())
        original = plaintext.read_bytes()
        for altered in (b"unlisted prefix" + original, original + b"unlisted suffix"):
            plaintext.write_bytes(altered)
            self.archive.unlink()
            archive._encrypt(plaintext, self.archive, PASSWORD.encode())
            self.assert_rejected(codes={"payload_invalid"})
        plaintext.unlink()

    def test_zip64_envelope_with_bounded_central_directory_is_supported(self):
        self.crafted()
        plaintext = self.root / "mutate.zip"
        archive._decrypt(self.archive, plaintext, PASSWORD.encode())
        original = plaintext.read_bytes()
        end = list(struct.unpack("<4s4H2LH", original[-22:]))
        count, central_size, central_offset = end[4], end[5], end[6]
        position = len(original) - 22
        record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, central_size, central_offset)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, position, 1)
        end[3:7] = [0xffff, 0xffff, 0xffffffff, 0xffffffff]
        plaintext.write_bytes(original[:-22] + record + locator + struct.pack("<4s4H2LH", *end))
        self.archive.unlink()
        archive._encrypt(plaintext, self.archive, PASSWORD.encode())
        plaintext.unlink()
        archive.extract_backup(self.archive, self.destination, PASSWORD)
        self.assertEqual((self.destination / "state/data.bin").read_bytes(), b"fixture")

    def test_insufficient_expanded_space_rejects_before_payload_publish(self):
        self.create()
        usage = shutil.disk_usage(self.root)
        with patch.object(archive.shutil, "disk_usage", return_value=type(usage)(usage.total, usage.used, 0)):
            self.assert_rejected(codes={"archive_limit"})

    def test_callbacks_receive_only_fixed_phase_names(self):
        phases = []
        archive.create_backup(self.source, self.archive, PASSWORD, bundle_metadata=BUNDLE, deployment=DEPLOYMENT, report=phases.append)
        archive.extract_backup(self.archive, self.destination, PASSWORD, report=phases.append)
        self.assertEqual(phases, ["packing", "encrypting", "decrypting", "verifying", "verified"])


class BackupMailboxTests(unittest.TestCase):
    def test_secret_roundtrip_unicode_whitespace_and_no_plaintext(self):
        password, token, context = "  超级安全的长期记忆备份密码  ", "fixture-token-" + "f" * 48, "job-123:export"
        sealed = archive.seal_secret(password, token, context)
        self.assertNotIn(password, sealed)
        self.assertEqual(archive.open_secret(sealed, token, context), password)
        self.assertNotEqual(sealed, archive.seal_secret(password, token, context))

    def test_token_context_and_ciphertext_cannot_be_changed_or_replayed(self):
        token, context = "fixture-token-" + "f" * 48, "job-123:export"
        sealed = archive.seal_secret(PASSWORD, token, context)
        for value, key, aad in ((sealed, token + "x", context), (sealed, token, "job-123:restore"),
                                (sealed, token, "job-456:export"), (sealed[:-5] + "AAAAA", token, context),
                                ("v2." + sealed[3:], token, context), ("v1.!!!!", token, context)):
            with self.subTest(aad=aad):
                with self.assertRaises(archive.BackupArchiveError) as caught:
                    archive.open_secret(value, key, aad)
                self.assertEqual(caught.exception.code, "secret_invalid")

    def test_invalid_token_or_unbounded_context_is_rejected(self):
        for token, context in (("short", "job"), ("a" * 32, ""), ("a" * 32, "x" * 4097)):
            with self.assertRaises(archive.BackupArchiveError):
                archive.seal_secret(PASSWORD, token, context)


if __name__ == "__main__":
    unittest.main()
