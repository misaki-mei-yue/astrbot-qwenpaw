"""Small shared primitives; no Docker or network access in the web gateway."""
from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked update paths are not supported.")
    return path


def read_json(path: Path, limit: int = 65536):
    path = safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink > 1:
        raise ValueError("Invalid update document.")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Update document is too large.")
    return json.loads(raw.decode("utf-8"))


def atomic_json(path: Path, obj, mode: int = 0o600) -> None:
    path = safe_path(path)
    if path.exists() and (not path.is_file() or path.stat().st_nlink > 1):
        raise ValueError("Invalid update document target.")
    fd, name = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(obj, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sign(obj, token: str) -> dict:
    digest = hmac.new(token.encode("utf-8"), _canonical(obj), hashlib.sha256).hexdigest()
    return {"payload": obj, "signature": digest}


def verify(envelope, token: str):
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise ValueError("Invalid update signature.")
    signature = envelope["signature"]
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(sign(envelope["payload"], token)["signature"], signature):
        raise ValueError("Invalid update signature.")
    return envelope["payload"]


def metadata(root: Path) -> dict:
    value = read_json(root / "bundle-version.json")
    if (not isinstance(value, dict) or value.get("format") != 1
            or value.get("update_protocol") != 1
            or not isinstance(value.get("version"), str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?", value["version"])
            or not isinstance(value.get("components"), dict)
            or set(value["components"]) != {"astrbot", "qwenpaw", "napcat"}
            or not all(isinstance(v, str) and len(v) <= 100 for v in value["components"].values())):
        raise ValueError("Unsupported bundle metadata.")
    return value


class FileLock(AbstractContextManager):
    """Process lifetime lock, released by the OS after an interrupted daemon."""
    def __init__(self, path: Path):
        self.path = safe_path(path)
        self.stream = None

    def __enter__(self):
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink > 1):
            raise ValueError("Invalid update lock.")
        self.stream = self.path.open("a+b")
        self.path.chmod(0o600)
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise BlockingIOError("Updater is already running.") from exc
        return self

    def __exit__(self, *args):
        if self.stream:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
