"""Portable, authenticated backup files; no Docker, networking or archived code execution.

The public envelope has fixed scrypt parameters and 1 MiB AES-GCM records. The
encrypted ZIP contains an authenticated manifest and the permitted data roots.
Extraction authenticates the entire envelope and verifies every ZIP member
before publishing a newly created destination directory.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


MAGIC = b"AQBKP\x00\x01\x00"
HEADER = struct.Struct(">8s16s8s")
RECORD = struct.Struct(">I")
CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 ** 3
MAX_EXPANDED_BYTES = 32 * 1024 ** 3
MAX_FILE_BYTES = 8 * 1024 ** 3
MAX_ENTRIES = 100_000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_CENTRAL_BYTES = 96 * 1024 * 1024
_AAD = b"astrbot-qwenpaw-backup-record-v1\x00"
_MAILBOX_AAD = b"astrbot-qwenpaw-backup-mailbox-v1\x00"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?\Z")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\..*)?\Z", re.IGNORECASE)
_ALLOWED_CODES = frozenset({"password", "archive_invalid", "archive_limit", "authentication", "payload_invalid", "source_invalid", "destination_invalid", "io_error", "secret_invalid"})


class BackupArchiveError(ValueError):
    """Safe public error: never includes a path, password or underlying exception."""

    def __init__(self, code: str):
        self.code = code if code in _ALLOWED_CODES else "archive_invalid"
        super().__init__(self.code)


def _fail(code: str):
    raise BackupArchiveError(code) from None


def _password(password: str) -> bytes:
    if not isinstance(password, str) or len(password) < 12:
        _fail("password")
    try:
        raw = password.encode("utf-8")
    except UnicodeError:
        _fail("password")
    if len(raw) > 1024:
        _fail("password")
    return raw


def _key(password: bytes, salt: bytes) -> bytes:
    # These work factors are constants, never obtained from the untrusted file.
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password)


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("payload_invalid")
        result[key] = value
    return result


def _integer(value) -> bool:
    return type(value) is int


def _report(callback, phase: str):
    if callback is not None:
        callback(phase)


def _checked_path(path: Path, code: str) -> Path:
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail(code)
    return path


def _regular(path: Path, code: str):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        _fail(code)
    return info


def _payload_path(name: str, *, directory: bool = False) -> str:
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 4096
            or "\\" in name or ":" in name or "\x00" in name or name.startswith("/")):
        _fail("payload_invalid")
    parts = name.split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " "))
           or _DEVICE.fullmatch(part) or len(part.encode("utf-8")) > 255
           or any(ord(char) < 32 for char in part) for part in parts):
        _fail("payload_invalid")
    if parts[0] == "state":
        if len(parts) == 1 and not directory:
            _fail("payload_invalid")
    elif name not in {".env", ".combined-setup.json"} or directory:
        _fail("payload_invalid")
    return name


def _path_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _metadata(bundle: dict, deployment: dict):
    if (not isinstance(bundle, dict) or bundle.get("format") != 1
            or not isinstance(bundle.get("version"), str) or len(bundle["version"]) > 100
            or not _VERSION.fullmatch(bundle["version"])
            or not isinstance(bundle.get("components"), dict)
            or set(bundle["components"]) != {"astrbot", "qwenpaw", "napcat"}
            or not all(isinstance(value, str) and 0 < len(value) <= 100 for value in bundle["components"].values())
            or ("update_protocol" in bundle and bundle["update_protocol"] != 1)
            or not isinstance(deployment, dict)):
        _fail("payload_invalid")
    if len(_canonical({"bundle": bundle, "deployment": deployment})) > 64 * 1024:
        _fail("payload_invalid")


def _manifest(value) -> dict:
    expected = {"format", "kind", "created_at", "bundle", "deployment", "files", "directories", "total_size", "file_count"}
    if (not isinstance(value, dict) or set(value) != expected or value["format"] != 1
            or value["kind"] != "astrbot-qwenpaw-backup"
            or not isinstance(value["created_at"], str) or len(value["created_at"]) > 40
            or not isinstance(value["files"], list) or not isinstance(value["directories"], list)
            or len(value["files"]) + len(value["directories"]) + 1 > MAX_ENTRIES
            or not _integer(value["file_count"]) or value["file_count"] != len(value["files"])
            or not _integer(value["total_size"]) or not 0 <= value["total_size"] <= MAX_EXPANDED_BYTES):
        _fail("payload_invalid")
    try:
        created = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        if created.utcoffset() is None:
            _fail("payload_invalid")
    except ValueError:
        _fail("payload_invalid")
    _metadata(value["bundle"], value["deployment"])
    paths, directories = {}, set()
    for name in value["directories"]:
        _payload_path(name, directory=True)
        key = _path_key(name)
        if key in paths:
            _fail("payload_invalid")
        paths[key] = name
        directories.add(name)
    total = 0
    for item in value["files"]:
        if (not isinstance(item, dict) or set(item) not in ({"path", "size", "sha256"}, {"path", "size", "sha256", "mode"})
                or not _integer(item["size"]) or not 0 <= item["size"] <= MAX_FILE_BYTES
                or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])
                or not _integer(item.get("mode", 0o600)) or item.get("mode", 0o600) not in {0o600, 0o700}):
            _fail("payload_invalid")
        name = _payload_path(item["path"])
        key = _path_key(name)
        if key in paths:
            _fail("payload_invalid")
        paths[key] = name
        total += item["size"]
        if total > MAX_EXPANDED_BYTES:
            _fail("archive_limit")
    if total != value["total_size"]:
        _fail("payload_invalid")
    # Parents must be explicitly listed; this also rejects file/directory clashes.
    for name in paths.values():
        parts = name.split("/")
        if any("/".join(parts[:index]) not in directories for index in range(1, len(parts))):
            _fail("payload_invalid")
    return value


def _zip_info(name: str, directory: bool = False, mode: int = 0o600) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name + ("/" if directory else ""))
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | mode)) << 16
    if directory:
        info.external_attr |= 0x10
    info.compress_type = zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
    return info


def _scan(source: Path):
    files, directories, names = [], [], set()
    pending, discovered = [], 0

    def queue(directory: Path, prefix: str):
        nonlocal discovered
        children = []
        for child in directory.iterdir():
            discovered += 1
            if discovered + 1 > MAX_ENTRIES:
                _fail("archive_limit")
            children.append((child, prefix + child.name))
        pending.extend(reversed(sorted(children, key=lambda item: item[1])))

    queue(source, "")
    while pending:
        path, name = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("source_invalid")
        directory = stat.S_ISDIR(info.st_mode)
        try:
            _payload_path(name, directory=directory)
        except BackupArchiveError:
            _fail("source_invalid")
        key = _path_key(name)
        if key in names:
            _fail("source_invalid")
        names.add(key)
        if len(names) + 1 > MAX_ENTRIES:
            _fail("archive_limit")
        if directory:
            directories.append(name)
            queue(path, name + "/")
        else:
            _regular(path, "source_invalid")
            if info.st_size > MAX_FILE_BYTES:
                _fail("archive_limit")
            files.append((name, path, info.st_size))

    if sum(item[2] for item in files) > MAX_EXPANDED_BYTES:
        _fail("archive_limit")
    return files, directories


def _encrypt(plaintext: Path, output: Path, password: bytes):
    header = HEADER.pack(MAGIC, os.urandom(16), os.urandom(8))
    _, salt, prefix = HEADER.unpack(header)
    cipher = AESGCM(_key(password, salt))
    written, sequence = len(header), 0
    with plaintext.open("rb") as source, output.open("xb") as target:
        output.chmod(0o600)
        target.write(header)
        while True:
            block = source.read(CHUNK_SIZE)
            length = RECORD.pack(len(block))
            final = not block
            nonce = prefix + struct.pack(">I", sequence)
            aad = _AAD + header + struct.pack(">Q", sequence) + length + bytes([final])
            encrypted = cipher.encrypt(nonce, block, aad)
            written += len(length) + len(encrypted)
            if written > MAX_ARCHIVE_BYTES:
                _fail("archive_limit")
            target.write(length)
            target.write(encrypted)
            if final:
                break
            sequence += 1
        target.flush()
        os.fsync(target.fileno())


def _read_exact(stream, size: int) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        _fail("archive_invalid")
    return value


def _decrypt(archive: Path, plaintext: Path, password: bytes):
    with archive.open("rb") as source, plaintext.open("xb") as target:
        plaintext.chmod(0o600)
        header = _read_exact(source, HEADER.size)
        magic, salt, prefix = HEADER.unpack(header)
        if magic != MAGIC:
            _fail("archive_invalid")
        cipher, sequence, total = AESGCM(_key(password, salt)), 0, 0
        while True:
            length = _read_exact(source, RECORD.size)
            size = RECORD.unpack(length)[0]
            if size > CHUNK_SIZE or sequence >= 2 ** 32:
                _fail("archive_invalid")
            encrypted = _read_exact(source, size + 16)
            final = size == 0
            aad = _AAD + header + struct.pack(">Q", sequence) + length + bytes([final])
            try:
                block = cipher.decrypt(prefix + struct.pack(">I", sequence), encrypted, aad)
            except InvalidTag:
                _fail("authentication")
            if final:
                if source.read(1):
                    _fail("archive_invalid")
                return
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                _fail("archive_limit")
            target.write(block)
            sequence += 1


def create_backup(source: Path, destination: Path, password: str, *, bundle_metadata: dict, deployment: dict, report=None) -> dict:
    """Create a new .aqbackup, preserving an existing target on every failure."""
    secret = _password(password)
    try:
        source = _checked_path(source, "source_invalid")
        destination = _checked_path(destination, "destination_invalid")
        if (not source.is_dir() or destination.exists() or not destination.parent.is_dir()
                or destination == source or source in destination.parents):
            _fail("destination_invalid")
        _metadata(bundle_metadata, deployment)
        _report(report, "packing")
        files, directories = _scan(source)
        manifest = {"format": 1, "kind": "astrbot-qwenpaw-backup", "created_at": datetime.now(timezone.utc).isoformat(),
                    "bundle": bundle_metadata, "deployment": deployment, "files": [], "directories": directories,
                    "total_size": 0, "file_count": len(files)}
        with tempfile.TemporaryDirectory(prefix=".aqbackup-", dir=destination.parent) as name:
            temporary = Path(name)
            temporary.chmod(0o700)
            plaintext, sealed = temporary / "payload.zip", temporary / "backup.aqbackup"
            with zipfile.ZipFile(plaintext, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
                plaintext.chmod(0o600)
                for directory in directories:
                    bundle.writestr(_zip_info(directory, True), b"")
                for relative, path, expected_size in files:
                    info = _regular(_checked_path(path, "source_invalid"), "source_invalid")
                    if info.st_size != expected_size:
                        _fail("source_invalid")
                    mode = 0o700 if info.st_mode & stat.S_IXUSR else 0o600
                    digest, size = hashlib.sha256(), 0
                    with path.open("rb") as stream, bundle.open(_zip_info(relative, mode=mode), "w", force_zip64=True) as target:
                        while True:
                            block = stream.read(CHUNK_SIZE)
                            if not block:
                                break
                            size += len(block)
                            if size > expected_size or size > MAX_FILE_BYTES:
                                _fail("source_invalid")
                            digest.update(block)
                            target.write(block)
                    if size != expected_size:
                        _fail("source_invalid")
                    manifest["files"].append({"path": relative, "size": size, "sha256": digest.hexdigest(), "mode": mode})
                    manifest["total_size"] += size
                    if bundle.fp.tell() > MAX_ARCHIVE_BYTES:
                        _fail("archive_limit")
                _manifest(manifest)
                raw = _canonical(manifest)
                if len(raw) > MAX_MANIFEST_BYTES:
                    _fail("archive_limit")
                bundle.writestr(_zip_info("manifest.json"), raw)
            if plaintext.stat().st_size > MAX_ARCHIVE_BYTES:
                _fail("archive_limit")
            _report(report, "encrypting")
            _encrypt(plaintext, sealed, secret)
            # A hard link publishes without overwriting a file that appeared after validation.
            os.link(sealed, destination)
        return manifest
    except BackupArchiveError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, zipfile.BadZipFile, OverflowError, zlib.error):
        _fail("io_error")


def _zip_layout(plaintext: Path):
    """Bound entry parsing before ZipFile allocates its central directory list."""
    size = plaintext.stat().st_size
    if size < 22:
        _fail("payload_invalid")
    with plaintext.open("rb") as stream:
        stream.seek(size - 22)
        end = struct.unpack("<4s4H2LH", _read_exact(stream, 22))
        signature, disk, central_disk, disk_count, count, central_size, offset, comment = end
        if signature != b"PK\x05\x06" or disk or central_disk or disk_count != count or comment:
            _fail("payload_invalid")
        boundary = size - 22
        locator = b""
        if size >= 42:
            stream.seek(size - 42)
            locator = _read_exact(stream, 20)
        if locator.startswith(b"PK\x06\x07"):
            _, zip64_disk, position, disks = struct.unpack("<4sLQL", locator)
            if zip64_disk or disks != 1 or position + 56 != size - 42:
                _fail("payload_invalid")
            stream.seek(position)
            record = struct.unpack("<4sQ2H2L4Q", _read_exact(stream, 56))
            signature, record_size, _, _, disk, central_disk, disk_count, count, central_size, offset = record
            if signature != b"PK\x06\x06" or record_size != 44 or disk or central_disk or disk_count != count:
                _fail("payload_invalid")
            boundary = position
        elif count == 0xffff or central_size == 0xffffffff or offset == 0xffffffff:
            _fail("payload_invalid")
        if count > MAX_ENTRIES or central_size > MAX_CENTRAL_BYTES:
            _fail("archive_limit")
        if offset + central_size != boundary:
            _fail("payload_invalid")
        stream.seek(offset)
        actual = 0
        while stream.tell() < boundary:
            fixed = _read_exact(stream, 46)
            if fixed[:4] != b"PK\x01\x02":
                _fail("payload_invalid")
            name_size, extra_size, comment_size, member_disk = struct.unpack_from("<4H", fixed, 28)
            actual += 1
            if actual > count or actual > MAX_ENTRIES:
                _fail("archive_limit")
            if not 0 < name_size <= 4097 or extra_size > 256 or comment_size or member_disk:
                _fail("payload_invalid")
            if stream.tell() + name_size + extra_size > boundary:
                _fail("payload_invalid")
            stream.seek(name_size + extra_size, 1)
        if stream.tell() != boundary or actual != count:
            _fail("payload_invalid")
    return offset


def _verify_zip(plaintext: Path):
    central_offset = _zip_layout(plaintext)
    with zipfile.ZipFile(plaintext, "r", allowZip64=True) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_ENTRIES or bundle.comment or bundle.start_dir != central_offset:
            _fail("archive_limit")
        by_name, names, offsets, total = {}, set(), set(), 0
        for info in entries:
            if (info.filename != info.orig_filename or info.flag_bits & 1
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or info.header_offset in offsets or info.header_offset < 0 or info.header_offset >= bundle.start_dir
                    or info.file_size < 0 or info.compress_size < 0):
                _fail("payload_invalid")
            offsets.add(info.header_offset)
            directory = info.is_dir()
            name = info.filename[:-1] if directory else info.filename
            if name != "manifest.json":
                _payload_path(name, directory=directory)
            elif directory or info.file_size > MAX_MANIFEST_BYTES:
                _fail("payload_invalid")
            key = _path_key(name)
            if key in names:
                _fail("payload_invalid")
            names.add(key)
            mode = info.external_attr >> 16
            filetype = stat.S_IFMT(mode)
            if filetype not in ({0, stat.S_IFDIR} if directory else {0, stat.S_IFREG}) or info.external_attr & 0x400:
                _fail("payload_invalid")
            if stat.S_IMODE(mode) not in ({0, 0o700} if directory else {0, 0o600, 0o700}):
                _fail("payload_invalid")
            if directory:
                if info.file_size or info.compress_size or info.compress_type != zipfile.ZIP_STORED:
                    _fail("payload_invalid")
            else:
                if info.file_size > (MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES):
                    _fail("archive_limit")
                if name != "manifest.json":
                    total += info.file_size
                    if total > MAX_EXPANDED_BYTES:
                        _fail("archive_limit")
            by_name[name] = info
        if offsets and min(offsets) != 0:
            _fail("payload_invalid")
        if "manifest.json" not in by_name:
            _fail("payload_invalid")
        with bundle.open(by_name["manifest.json"], "r") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            _fail("archive_limit")
        manifest = _manifest(json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates))
        expected = {item["path"] for item in manifest["files"]} | set(manifest["directories"]) | {"manifest.json"}
        if expected != set(by_name) or total != manifest["total_size"]:
            _fail("payload_invalid")
        for directory in manifest["directories"]:
            if not by_name[directory].is_dir():
                _fail("payload_invalid")
        for item in manifest["files"]:
            info = by_name[item["path"]]
            if (info.is_dir() or info.file_size != item["size"]
                    or (stat.S_IMODE(info.external_attr >> 16) or 0o600) != item.get("mode", 0o600)):
                _fail("payload_invalid")
            digest, size = hashlib.sha256(), 0
            with bundle.open(info, "r") as stream:
                while True:
                    block = stream.read(CHUNK_SIZE)
                    if not block:
                        break
                    size += len(block)
                    if size > item["size"]:
                        _fail("payload_invalid")
                    digest.update(block)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                _fail("payload_invalid")
        return manifest


def extract_backup(archive: Path, destination: Path, password: str, *, report=None) -> dict:
    """Authenticate, validate and extract into an absent or empty destination."""
    secret = _password(password)
    try:
        archive = _checked_path(archive, "archive_invalid")
        info = _regular(archive, "archive_invalid")
        if info.st_size > MAX_ARCHIVE_BYTES:
            _fail("archive_limit")
        destination = _checked_path(destination, "destination_invalid")
        if (not destination.parent.is_dir() or (destination.exists() and
                (not destination.is_dir() or any(destination.iterdir())))
                or destination == archive or destination in archive.parents):
            _fail("destination_invalid")
        with tempfile.TemporaryDirectory(prefix=".aqbackup-", dir=destination.parent) as name:
            temporary = Path(name)
            temporary.chmod(0o700)
            plaintext, stage = temporary / "payload.zip", temporary / "approved"
            _report(report, "decrypting")
            _decrypt(archive, plaintext, secret)
            _report(report, "verifying")
            manifest = _verify_zip(plaintext)
            if shutil.disk_usage(temporary).free < manifest["total_size"] + 64 * 1024 ** 2:
                _fail("archive_limit")
            stage.mkdir(mode=0o700)
            with zipfile.ZipFile(plaintext, "r", allowZip64=True) as bundle:
                for directory in sorted(manifest["directories"], key=lambda item: (item.count("/"), item)):
                    (stage / directory).mkdir(mode=0o700)
                for item in manifest["files"]:
                    target = stage / item["path"]
                    with bundle.open(item["path"], "r") as source, target.open("xb") as output:
                        target.chmod(item.get("mode", 0o600))
                        shutil.copyfileobj(source, output, length=CHUNK_SIZE)
            _report(report, "verified")
            # Existing data never gets merged with archive content.
            if destination.exists():
                _checked_path(destination, "destination_invalid")
                if not destination.is_dir() or any(destination.iterdir()):
                    _fail("destination_invalid")
                destination.rmdir()
            os.rename(stage, destination)
        return manifest
    except BackupArchiveError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, zipfile.BadZipFile, OverflowError, NotImplementedError, zlib.error, EOFError, struct.error):
        _fail("archive_invalid")


def _mailbox_key(token: str) -> bytes:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 4096:
        _fail("secret_invalid")
    return hashlib.sha256(_MAILBOX_AAD + b"key\x00" + token.encode("utf-8")).digest()


def _context(context: str) -> bytes:
    if not isinstance(context, str) or not context or len(context.encode("utf-8")) > 4096:
        _fail("secret_invalid")
    return _MAILBOX_AAD + b"context\x00" + context.encode("utf-8")


def seal_secret(password: str, token: str, context: str) -> str:
    """Hide a backup password in an authenticated IPC request, bound to its job."""
    secret = _password(password)
    try:
        nonce = os.urandom(12)
        value = nonce + AESGCM(_mailbox_key(token)).encrypt(nonce, secret, _context(context))
        return "v1." + base64.urlsafe_b64encode(value).decode("ascii")
    except BackupArchiveError:
        raise
    except (ValueError, UnicodeError, TypeError):
        _fail("secret_invalid")


def open_secret(sealed: str, token: str, context: str) -> str:
    try:
        if not isinstance(sealed, str) or not sealed.startswith("v1.") or len(sealed) > 2048:
            _fail("secret_invalid")
        raw = base64.b64decode(sealed[3:], altchars=b"-_", validate=True)
        if not 28 <= len(raw) <= 1052:
            _fail("secret_invalid")
        password = AESGCM(_mailbox_key(token)).decrypt(raw[:12], raw[12:], _context(context)).decode("utf-8")
        _password(password)
        return password
    except BackupArchiveError:
        raise
    except (ValueError, UnicodeError, TypeError, InvalidTag):
        _fail("secret_invalid")
