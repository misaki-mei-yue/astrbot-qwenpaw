"""Export only real files in the current session's shared outbound directory.

No remote downloads, data URLs or automatic copies from arbitrary workspaces.
The gateway independently checks this descriptor and snapshots the bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .bridge_client import BridgeError, BridgeSettings, config_value

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 4
MEDIA_KINDS = frozenset({"image", "file", "video", "audio"})
_MEDIA_FIELDS = {"image": "image_url", "file": "file_url", "video": "video_url", "audio": "data"}
_REFUSAL = "有附件未发送：仅支持本会话共享工作区 outbound 中的本地文件，每个附件不超过配置上限且每条消息最多4个。请先将文件生成到自己的共享工作区。"


def _kind(part):
    value = config_value(part, "type")
    return getattr(value, "value", value)


def _session(session_id: str) -> None:
    if not isinstance(session_id, str) or not re.fullmatch(r"ab_[0-9a-f]{32}", session_id):
        raise BridgeError("Invalid media conversation route.")


def _linked(path: Path) -> bool:
    value = path.lstat()
    # Junctions and other Windows reparse points also cross the boundary.
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _root(directory: str) -> Path:
    if not isinstance(directory, str) or not directory or "\x00" in directory:
        raise BridgeError("Configure an absolute shared media directory.")
    root = Path(directory)
    if not root.is_absolute() or ".." in root.parts:
        raise BridgeError("Configure an absolute shared media directory.")
    try:
        for node in [root, *root.parents]:
            if _linked(node):
                raise BridgeError("Shared media directories must not use links.")
        if not root.is_dir():
            raise BridgeError("The shared media directory is unavailable.")
        return root.resolve(strict=True) if os.name == "nt" else root
    except OSError:
        raise BridgeError("The shared media directory is unavailable.") from None


def _root_fd(root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            newer = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = newer
        return current
    except BaseException:
        os.close(current)
        raise


def _open_beneath(root: Path, parts: tuple[str, ...]) -> int:
    """Return a read-only descriptor; on Linux walk directories without links."""
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = _root_fd(root)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=current)
        finally:
            os.close(current)
    # Windows test/runtime fallback. Reject every reparse point before opening
    # and compare the opened inode with the pathname after opening.
    target = root
    for part in parts:
        target = target / part
        if _linked(target):
            raise BridgeError("Media links are not permitted.")
    return os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))


def media_workspace(directory: str, session_id: str, max_file_bytes=MAX_FILE_BYTES, max_files=MAX_ATTACHMENTS) -> dict:
    _session(session_id)
    root = _root(directory)
    try:
        if os.mkdir in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = _root_fd(root)
            session_fd = None
            try:
                try:
                    os.mkdir(session_id, mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                session_fd = os.open(session_id, flags, dir_fd=root_fd)
                for name in ("inbound", "outbound"):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=session_fd)
                    except FileExistsError:
                        pass
                    descriptor = os.open(name, flags, dir_fd=session_fd)
                    os.close(descriptor)
            finally:
                if session_fd is not None:
                    os.close(session_fd)
                os.close(root_fd)
        else:
            session = root / session_id
            session.mkdir(mode=0o700, exist_ok=True)
            if _linked(session):
                raise BridgeError("Media links are not permitted.")
            for name in ("inbound", "outbound"):
                child = session / name
                child.mkdir(mode=0o700, exist_ok=True)
                if _linked(child) or not child.is_dir():
                    raise BridgeError("Media links are not permitted.")
        return {
            "inbound_dir": str(root / session_id / "inbound"),
            "outbound_dir": str(root / session_id / "outbound"),
            "max_file_bytes": max_file_bytes,
            "max_attachments": max_files,
        }
    except (OSError, ValueError):
        raise BridgeError("This conversation's shared media workspace is unavailable.") from None


def _relative_source(root: Path, session_id: str, source: str) -> tuple[str, ...]:
    if not isinstance(source, str) or not source or len(source) > 4096 or any(ord(char) < 32 for char in source):
        raise BridgeError("Invalid local media reference.")
    native_drive = os.name == "nt" and bool(re.match(r"^[A-Za-z]:[\\/]", source))
    parsed = urlsplit(source)
    if parsed.scheme == "file":
        if parsed.netloc or parsed.query or parsed.fragment:
            raise BridgeError("Only local files in this conversation are permitted.")
        source = unquote(parsed.path, errors="strict")
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", source):
            source = source[1:]
    elif (parsed.scheme and not native_drive) or parsed.netloc or parsed.query or parsed.fragment:
        raise BridgeError("Remote and encoded media are not permitted.")
    path = Path(source)
    if ".." in path.parts or any(ord(char) < 32 for char in source):
        raise BridgeError("Media traversal is not permitted.")
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise BridgeError("Media must be inside this conversation's outbound directory.") from None
        parts = path.parts
    else:
        if "\\" in source or ":" in source:
            raise BridgeError("Invalid relative media reference.")
        parts = PurePosixPath(source).parts
        if parts and parts[0] == "outbound":
            parts = (session_id, *parts)
    if (
        len(parts) < 3 or parts[:2] != (session_id, "outbound")
        or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in parts)
    ):
        raise BridgeError("Media must belong to this conversation's outbound directory.")
    return tuple(parts)


def media_descriptor(directory: str, session_id: str, source: str, kind: str, filename=None, max_file_bytes=MAX_FILE_BYTES) -> dict:
    _session(session_id)
    if not isinstance(kind, str) or kind not in MEDIA_KINDS:
        raise BridgeError("Media kind must be image, file, video or audio.")
    root = _root(directory)
    descriptor = None
    try:
        parts = _relative_source(root, session_id, source)
        path = root.joinpath(*parts)
        descriptor = _open_beneath(root, parts)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= max_file_bytes:
            raise BridgeError("Media must be a nonempty regular file within its size limit, without links.")
        digest = hashlib.sha256()
        count = 0
        while True:
            data = os.read(descriptor, 128 * 1024)
            if not data:
                break
            count += len(data)
            if count > max_file_bytes:
                raise BridgeError("Media exceeds its size limit.")
            digest.update(data)
        after = os.fstat(descriptor)
        for node in [path, *path.parents]:
            if _linked(node):
                raise BridgeError("Media links are not permitted.")
        current = path.stat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
            or after.st_nlink != 1 or count != after.st_size
        ):
            raise BridgeError("Media changed while being exported; export it after writing finishes.")
        # The file basename is checked by the gateway as well. Display metadata
        # never gets a path, URL, control character or alternate data stream.
        name = filename or parts[-1]
        if not isinstance(name, str) or not name or len(name) > 255 or any(char in name for char in "/\\:") or any(ord(char) < 32 for char in name):
            raise BridgeError("Invalid media filename.")
        return {"type": kind, "path": "/".join(parts), "filename": name, "size": count, "sha256": digest.hexdigest()}
    except (OSError, ValueError, UnicodeError):
        raise BridgeError("This conversation's local media file cannot be exported.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def outgoing_content(parts: list, settings: BridgeSettings, session_id: str) -> list[dict]:
    result = []
    attachments = 0
    refusal_added = False
    for part in parts:
        kind = _kind(part)
        if kind in {"text", "refusal"}:
            text = config_value(part, "text" if kind == "text" else "refusal", "")
            if isinstance(text, str) and text.strip():
                result.append({"type": "text", "text": text})
        elif kind in MEDIA_KINDS:
            try:
                if attachments >= settings.max_files:
                    raise BridgeError("Too many media attachments.")
                source = config_value(part, _MEDIA_FIELDS[kind])
                item = await asyncio.to_thread(media_descriptor, settings.files_dir, session_id, source, kind, config_value(part, "filename"), settings.max_file_bytes)
                result.append(item)
                attachments += 1
            except BridgeError:
                if not refusal_added:
                    result.append({"type": "text", "text": _REFUSAL})
                    refusal_added = True
    return result
