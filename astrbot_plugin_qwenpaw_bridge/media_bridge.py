"""Bounded media transport, with per-session files and immutable send snapshots.

No model-selected URL is fetched on output. Incoming provider URLs use pinned
DNS answers, normal TLS verification and no redirects. File-system checks reject
links, reparse points, nonregular files and files outside the configured roots.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import ipaddress
import os
import re
import socket
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver


KINDS = {"image", "file", "video", "audio"}
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


class MediaFailure(ValueError):
    """Safe, nonsecret error codes for transport failures."""


def provider_component(event, component, kind: str):
    """Recover Video.url discarded by AstrBot 4.25's OneBot component model."""
    if kind != "video" or getattr(component, "url", None):
        return component
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    message = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
    if not isinstance(message, list):
        return component
    for part in message:
        if not isinstance(part, dict) or part.get("type") != "video":
            continue
        data = part.get("data")
        if not isinstance(data, dict) or data.get("file") != getattr(component, "file", None):
            continue
        url = data.get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            return SimpleNamespace(file=component.file, url=url)
    return component


def safe_filename(value: str | None, default: str = "attachment.bin") -> str:
    name = str(value or default).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]", "-", name).strip(" ._-")
    suffix = Path(name).suffix if len(Path(name).suffix) <= 12 else ""
    stem = name[:-len(suffix)] if suffix else name
    stem = stem[:80]
    while len((stem + suffix).encode("utf-8")) > 180:
        stem = stem[:-1]
    name = (stem.rstrip(" ._-") + suffix).strip(" ._-") or default
    if name.upper().split(".", 1)[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        name = "file-" + name
    return name


def media_filename(kind: str, filename: str, head: bytes, content_type="") -> str:
    """Keep real media suffixes, without trusting remote disposition paths."""
    extension = None
    if kind == "image":
        if head.startswith(b"\x89PNG\r\n\x1a\n"): extension = ".png"
        elif head.startswith(b"\xff\xd8\xff"): extension = ".jpg"
        elif head.startswith((b"GIF87a", b"GIF89a")): extension = ".gif"
        elif head.startswith(b"RIFF") and head[8:12] == b"WEBP": extension = ".webp"
        else: extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(content_type)
    elif kind == "video":
        if head[4:8] == b"ftyp": extension = ".mov" if head[8:12] == b"qt  " else ".mp4"
        elif head.startswith(b"\x1aE\xdf\xa3"): extension = ".webm" if b"webm" in head[:4096] else ".mkv"
        else: extension = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}.get(content_type)
    elif kind == "audio":
        if head.startswith(b"RIFF") and head[8:12] == b"WAVE": extension = ".wav"
        elif head.startswith((b"#!SILK", b"\x02#!SILK")): extension = ".silk"
        elif head.startswith(b"#!AMR"): extension = ".amr"
        elif head.startswith(b"OggS"): extension = ".ogg"
        elif head.startswith(b"fLaC"): extension = ".flac"
        elif len(head) >= 2 and head[0] == 255 and head[1] & 0xf6 == 0xf0: extension = ".aac"
        elif head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 255 and head[1] & 0xe0 == 0xe0): extension = ".mp3"
        elif head[4:8] == b"ftyp": extension = ".m4a"
        else: extension = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/aacp": ".aac"}.get(content_type)
    if not extension:
        return safe_filename(filename)
    suffix = Path(filename).suffix.lower()
    if suffix == extension or (extension == ".jpg" and suffix == ".jpeg"):
        return safe_filename(filename)
    return safe_filename(Path(filename).stem + extension)


def local_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise MediaFailure("invalid_media_path")
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc or parsed.query or parsed.fragment:
            raise MediaFailure("nonlocal_file_uri")
        value = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", value):
            value = value[1:]
        elif os.name != "nt":
            value = "/" + value.lstrip("/")
    if not os.path.isabs(value):
        raise MediaFailure("absolute_media_path_required")
    if ".." in Path(value).parts:
        raise MediaFailure("media_path_traversal_forbidden")
    path = Path(os.path.abspath(value))
    if os.name == "nt":
        # Windows may spell the same safe directory using an 8.3 alias. Check
        # every existing component before resolving that alias; links remain
        # forbidden, including broken links and reparse points.
        parent = path
        missing = []
        while True:
            try:
                parent.lstat()
                break
            except FileNotFoundError:
                if parent == parent.parent:
                    raise MediaFailure("media_path_unavailable") from None
                missing.append(parent.name)
                parent = parent.parent
            except OSError:
                raise MediaFailure("media_path_unavailable") from None
        _walk_no_links(parent)
        parent = parent.resolve(strict=True)
        _walk_no_links(parent)
        path = parent.joinpath(*reversed(missing))
    return path


def _has_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _walk_no_links(path: Path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    info = current.lstat()
    if _has_link(info):
        raise MediaFailure("media_links_forbidden")
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError:
            raise MediaFailure("media_path_unavailable") from None
        if _has_link(info):
            raise MediaFailure("media_links_forbidden")
    return info


def _directory_fd(path: Path, create=False) -> int:
    """Walk POSIX directories using pinned no-follow file descriptors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=fd)
            newer = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = newer
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_directory(path: Path):
    path = Path(os.path.abspath(path))
    try:
        if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
            os.close(_directory_fd(path, create=True))
        else:
            current = Path(path.anchor)
            for component in path.parts[1:]:
                current /= component
                with contextlib.suppress(FileExistsError):
                    current.mkdir(mode=0o700)
                info = current.lstat()
                if _has_link(info) or not stat.S_ISDIR(info.st_mode):
                    raise MediaFailure("media_links_forbidden")
        _walk_no_links(path)
    except OSError:
        raise MediaFailure("media_directory_unavailable") from None


@contextlib.contextmanager
def safe_reader(path: Path, roots: list[Path], max_bytes: int):
    path = local_path(str(path))
    if not any(path.is_relative_to(root) for root in roots):
        raise MediaFailure("media_path_outside_allowed_roots")
    before = _walk_no_links(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise MediaFailure("media_regular_single_link_file_required")
    if before.st_size > max_bytes:
        raise MediaFailure("media_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
            parent = _directory_fd(path.parent)
            try:
                fd = os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=parent)
            finally:
                os.close(parent)
        else:
            fd = os.open(path, flags)
    except OSError:
        raise MediaFailure("media_path_unavailable") from None
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (_has_link(opened) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise MediaFailure("media_changed_during_open")
        yield stream


@contextlib.contextmanager
def new_writer(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
            parent = _directory_fd(path.parent)
            try:
                fd = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            finally:
                os.close(parent)
        else:
            _walk_no_links(path.parent)
            fd = os.open(path, flags, 0o600)
    except OSError:
        raise MediaFailure("media_snapshot_unavailable") from None
    with os.fdopen(fd, "wb") as stream:
        yield stream
        opened = os.fstat(stream.fileno())
        current = _walk_no_links(path)
        if (opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise MediaFailure("media_snapshot_changed")


def _safe_unlink(path: Path):
    # Only our exclusive, unpredictable new files are ever removed.
    with contextlib.suppress(OSError, MediaFailure):
        _walk_no_links(path)
        path.unlink()


class PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: list[tuple[str, int]]):
        self.hostname = hostname
        self.addresses = addresses

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host.lower().rstrip(".") != self.hostname:
            raise OSError("unexpected media hostname")
        return [{"hostname": host, "host": ip, "port": port, "family": fam,
                 "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}
                for ip, fam in self.addresses]

    async def close(self):
        pass


def _private_napcat_address(address) -> bool:
    blocks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    return any(address in ipaddress.ip_network(block) for block in blocks if address.version == ipaddress.ip_network(block).version)


async def pinned_target(url: str, napcat_hosts: set[str]) -> tuple[str, PinnedResolver]:
    if not isinstance(url, str) or len(url) > 8192 or any(ord(char) < 32 for char in url):
        raise MediaFailure("invalid_media_url")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode().lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise MediaFailure("invalid_media_url") from None
    private_allowed = parsed.scheme == "http" and host in napcat_hosts
    if parsed.scheme == "http" and not private_allowed and parsed.port in {None, 80}:
        # Only verified HTTPS is requested. There is no HTTP or TLS fallback.
        parsed = parsed._replace(scheme="https")
        port = 443
    if (not host or parsed.username or parsed.password or parsed.fragment
            or (parsed.scheme != "https" and not private_allowed)):
        raise MediaFailure("media_https_required")
    try:
        answers = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 10
        )
    except (OSError, asyncio.TimeoutError):
        raise MediaFailure("media_dns_failed") from None
    addresses = []
    for family, _, _, _, sockaddr in answers:
        address = ipaddress.ip_address(sockaddr[0])
        mapped = getattr(address, "ipv4_mapped", None) or address
        public = (mapped.is_global and not mapped.is_multicast and not mapped.is_reserved
                  and not mapped.is_unspecified and not mapped.is_loopback and not mapped.is_link_local)
        if not (public or (private_allowed and _private_napcat_address(mapped))):
            raise MediaFailure("private_media_address_forbidden")
        item = (str(address), family)
        if item not in addresses:
            addresses.append(item)
    if not addresses:
        raise MediaFailure("media_dns_failed")
    netloc = f"[{host}]" if ":" in host else host
    if port != (443 if parsed.scheme == "https" else 80):
        netloc += ":" + str(port)
    normalized = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    return normalized, PinnedResolver(host, addresses)


@dataclass(frozen=True)
class StoredMedia:
    kind: str
    path: Path
    filename: str
    size: int
    sha256: str

    def native(self) -> dict:
        if self.kind == "image":
            return {"type": "image", "image_url": str(self.path)}
        if self.kind == "video":
            return {"type": "video", "video_url": str(self.path)}
        if self.kind == "audio":
            return {"type": "audio", "data": str(self.path), "format": self.path.suffix.lstrip(".") or "audio"}
        return {"type": "file", "file_url": str(self.path), "filename": self.filename}


class MediaBridge:
    def __init__(self, root: str | Path, source_roots: list[str | Path], *,
                 max_bytes=DEFAULT_MAX_BYTES, max_files=4, napcat_hosts=None, timeout=60, component_types=None):
        self.root = local_path(str(root))
        self.source_roots = [local_path(str(path)) for path in source_roots]
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.napcat_hosts = {str(host).lower().rstrip(".") for host in (napcat_hosts or {"napcat"})}
        self.timeout = int(timeout)
        self.component_types = component_types
        if not 1 <= self.max_bytes <= DEFAULT_MAX_BYTES or not 1 <= self.max_files <= 4 or not 5 <= self.timeout <= 300:
            raise MediaFailure("invalid_media_limits")

    def _session(self, sid: str) -> str:
        if not isinstance(sid, str) or not re.fullmatch(r"ab_[0-9a-f]{32}", sid):
            raise MediaFailure("invalid_media_session")
        return sid

    def _destination(self, sid: str, section: str, filename: str) -> Path:
        self._session(sid)
        parent = self.root / sid / section
        ensure_directory(parent)
        return parent / (uuid.uuid4().hex + "-" + safe_filename(filename))

    def _copy(self, source: Path, destination: Path, roots: list[Path], kind: str, filename: str,
              expected_size=None, expected_sha256=None, sniff=False) -> StoredMedia:
        digest = hashlib.sha256()
        size = 0
        try:
            with safe_reader(source, roots, self.max_bytes) as incoming:
                before = os.fstat(incoming.fileno())
                chunk = incoming.read(65536)
                if sniff:
                    filename = media_filename(kind, filename, chunk)
                    destination = destination.with_name(destination.name.split("-", 1)[0] + "-" + filename)
                with new_writer(destination) as output:
                    while chunk:
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise MediaFailure("media_too_large")
                        output.write(chunk)
                        digest.update(chunk)
                        chunk = incoming.read(65536)
                after = os.fstat(incoming.fileno())
                if (after.st_nlink != 1 or not stat.S_ISREG(after.st_mode)
                        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                    raise MediaFailure("media_changed_during_copy")
            sha = digest.hexdigest()
            if expected_size is not None and size != expected_size:
                raise MediaFailure("media_size_mismatch")
            if expected_sha256 is not None and sha != expected_sha256:
                raise MediaFailure("media_hash_mismatch")
            if not size and kind != "file":
                raise MediaFailure("empty_media")
            return StoredMedia(kind, destination, safe_filename(filename), size, sha)
        except BaseException:
            _safe_unlink(destination)
            raise

    async def _download(self, sid: str, kind: str, url: str, filename: str) -> StoredMedia:
        normalized, resolver = await pinned_target(url, self.napcat_hosts)
        destination = self._destination(sid, "inbound", filename)
        digest = hashlib.sha256()
        size = 0
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=True, family=socket.AF_UNSPEC)
        timeout = aiohttp.ClientTimeout(total=self.timeout, sock_connect=10, sock_read=10)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
                async with session.get(normalized, allow_redirects=False, auto_decompress=False,
                                       headers={"Accept-Encoding": "identity"}) as response:
                    if 300 <= response.status < 400:
                        raise MediaFailure("media_redirect_forbidden")
                    if response.status != 200:
                        raise MediaFailure("media_download_failed")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise MediaFailure("compressed_media_response_forbidden")
                    if response.content_length is not None and response.content_length > self.max_bytes:
                        raise MediaFailure("media_too_large")
                    first = await response.content.read(65536)
                    filename = media_filename(kind, filename, first, response.content_type)
                    destination = destination.with_name(destination.name.split("-", 1)[0] + "-" + filename)
                    with new_writer(destination) as output:
                        size = len(first)
                        if size > self.max_bytes:
                            raise MediaFailure("media_too_large")
                        output.write(first)
                        digest.update(first)
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise MediaFailure("media_too_large")
                            output.write(chunk)
                            digest.update(chunk)
            if not size and kind != "file":
                raise MediaFailure("empty_media")
            return StoredMedia(kind, destination, safe_filename(filename), size, digest.hexdigest())
        except asyncio.CancelledError:
            _safe_unlink(destination)
            raise
        except MediaFailure:
            _safe_unlink(destination)
            raise
        except Exception:
            _safe_unlink(destination)
            raise MediaFailure("media_download_failed") from None

    async def import_component(self, sid: str, kind: str, component) -> StoredMedia:
        self._session(sid)
        if kind not in KINDS:
            raise MediaFailure("unsupported_media_kind")
        defaults = {"image": "image.jpg", "video": "video.mp4", "audio": "audio.bin", "file": "file.bin"}
        filename = safe_filename(getattr(component, "name", None), defaults[kind])
        if kind == "file":
            reference = getattr(component, "file_", None) or getattr(component, "url", None)
        elif kind == "image":
            reference = getattr(component, "url", None) or getattr(component, "file", None)
        else:
            reference = getattr(component, "file", None)
        alternate = getattr(component, "url", None)
        if (kind in {"file", "video", "audio"} and isinstance(alternate, str)
                and alternate.startswith(("https://", "http://"))
                and not str(reference or "").startswith(("https://", "http://", "base64://"))):
            usable_local = False
            if reference:
                try:
                    candidate = local_path(reference)
                    if any(candidate.is_relative_to(root) for root in self.source_roots):
                        try:
                            candidate.lstat()
                            _walk_no_links(candidate)
                            usable_local = True
                        except FileNotFoundError:
                            pass
                except MediaFailure as exc:
                    if str(exc) not in {"absolute_media_path_required", "media_path_unavailable"}:
                        raise
            if not usable_local:
                reference = alternate
        if not isinstance(reference, str) or not reference:
            raise MediaFailure("media_source_unavailable")
        if reference.startswith(("https://", "http://")):
            if kind == "audio":
                candidate = safe_filename(urlsplit(reference).path.rsplit("/", 1)[-1], defaults[kind])
                if Path(candidate).suffix.lower() in {".wav", ".mp3", ".silk", ".slk", ".amr", ".opus", ".ogg", ".flac", ".m4a", ".aac"}:
                    filename = candidate
            return await self._download(sid, kind, reference, filename)
        destination = self._destination(sid, "inbound", filename)
        if reference.startswith("base64://"):
            encoded = reference[len("base64://"):]
            if len(encoded) > ((self.max_bytes + 2) // 3) * 4:
                raise MediaFailure("media_too_large")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise MediaFailure("invalid_media_base64") from None
            if len(decoded) > self.max_bytes:
                raise MediaFailure("media_too_large")
            if not decoded and kind != "file":
                raise MediaFailure("empty_media")
            filename = media_filename(kind, filename, decoded[:65536])
            destination = destination.with_name(destination.name.split("-", 1)[0] + "-" + filename)
            try:
                with new_writer(destination) as output:
                    output.write(decoded)
                return StoredMedia(kind, destination, filename, len(decoded), hashlib.sha256(decoded).hexdigest())
            except BaseException:
                _safe_unlink(destination)
                raise
        source = local_path(reference)
        if kind != "file":
            filename = safe_filename(source.name, defaults[kind])
            destination = self._destination(sid, "inbound", filename)
        return await asyncio.to_thread(self._copy, source, destination, self.source_roots, kind, filename, sniff=True)

    def _outbound_path(self, sid: str, value: str) -> Path:
        self._session(sid)
        if (not isinstance(value, str) or not value or "\x00" in value
                or ("\\" in value and not (os.name == "nt" and os.path.isabs(value)))):
            raise MediaFailure("invalid_outbound_media_path")
        if value.startswith("file:") or os.path.isabs(value):
            path = local_path(value)
        else:
            relative = PurePosixPath(value)
            if relative.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
                raise MediaFailure("invalid_outbound_media_path")
            path = self.root / Path(*relative.parts)
        allowed = self.root / sid / "outbound"
        if not path.is_relative_to(allowed) or path == allowed:
            raise MediaFailure("outbound_media_session_scope_required")
        return path

    async def snapshot(self, sid: str, block: dict, *, allow_native=False) -> StoredMedia:
        if not isinstance(block, dict) or block.get("type") not in KINDS:
            raise MediaFailure("unsupported_media_kind")
        kind = block["type"]
        if "path" in block:
            if not isinstance(block["path"], str) or block["path"].startswith("file:") or os.path.isabs(block["path"]):
                raise MediaFailure("relative_media_descriptor_path_required")
            path = self._outbound_path(sid, block["path"])
            size = block.get("size")
            digest = block.get("sha256")
            if (isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= self.max_bytes
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise MediaFailure("invalid_media_descriptor")
        elif allow_native:
            value = block.get({"image": "image_url", "video": "video_url", "audio": "data", "file": "file_url"}[kind])
            path = self._outbound_path(sid, value)
            size = digest = None
        else:
            raise MediaFailure("media_descriptor_required")
        filename = safe_filename(block.get("filename"), path.name)
        destination = self._destination(sid, "delivery", filename)
        return await asyncio.to_thread(self._copy, path, destination,
                                       [self.root / sid / "outbound"], kind, filename, size, digest)

    async def output_components(self, sid: str, content: list[dict], platform: str, *, allow_native=False):
        if self.component_types is None:
            from astrbot.api.message_components import File, Image, Plain, Record, Video
        else:
            File, Image, Plain, Record, Video = (self.component_types[name] for name in ("file", "image", "text", "audio", "video"))

        if not isinstance(content, list) or not content or len(content) > 100:
            raise MediaFailure("invalid_media_content")
        files = sum(isinstance(block, dict) and block.get("type") in KINDS for block in content)
        if files > self.max_files:
            raise MediaFailure("too_many_media_attachments")
        components = []
        snapshots = []
        try:
            for block in content:
                if not isinstance(block, dict):
                    raise MediaFailure("invalid_media_content")
                if block.get("type") == "text":
                    text = block.get("text")
                    if not isinstance(text, str) or len(text) > 100000:
                        raise MediaFailure("invalid_media_text")
                    if text.strip():
                        components.append(Plain(text))
                    continue
                asset = await self.snapshot(sid, block, allow_native=allow_native)
                snapshots.append(asset.path)
                if asset.kind == "image":
                    components.append(Image.fromFileSystem(str(asset.path)))
                elif asset.kind == "video":
                    components.append(Video.fromFileSystem(str(asset.path)))
                elif asset.kind == "audio" and platform != "weixin_oc":
                    components.append(Record.fromFileSystem(str(asset.path)))
                else:
                    if asset.kind == "audio":
                        components.append(Plain("微信接口暂不支持发送语音气泡，本次音频以文件发送。"))
                    components.append(File(name=asset.filename, file=str(asset.path)))
            if not components:
                raise MediaFailure("empty_media_content")
            return components
        except BaseException:
            for path in snapshots:
                _safe_unlink(path)
            raise
