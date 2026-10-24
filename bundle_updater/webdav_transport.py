"""Small HTTPS-only WebDAV transport for encrypted backup archives.

Credentials are sent only to the explicitly configured origin. Redirects are
never followed, response bodies never become error messages, and archive PUTs
are streamed in bounded chunks. This module does not read proxy environment
variables or perform retries; the host scheduler owns retry policy.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


CHUNK_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 ** 3
_NAMESPACE = re.compile(r"astrbot-qwenpaw/[0-9a-f]{32}\Z")
_FILENAME = re.compile(r"[0-9a-f]{32}\.aqbackup\Z")
_CODES = frozenset({
    "url_invalid", "path_invalid", "credentials_invalid", "source_invalid",
    "authentication", "permission", "redirect", "timeout", "connection",
    "tls", "remote_unavailable", "remote_invalid", "remote_size",
    "conflict", "storage_full", "unsupported", "cleanup_failed",
})
_DAV = "{DAV:}"
_PROPERTIES = b'''<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>
<d:getcontentlength/></d:prop></d:propfind>'''


class WebDAVError(ValueError):
    """A public error code, without URL, credentials, body or server details."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "remote_invalid"
        super().__init__(self.code)


def _fail(code: str):
    raise WebDAVError(code) from None


def _controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def validate_target_url(url: str) -> str:
    """Return a canonical directory URL; no userinfo, redirects or traversal.

    Literal or encoded dot segments and encoded separators are rejected rather
    than normalized. Nested percent encoding is also rejected so intermediaries
    cannot reinterpret the configured directory after an additional decode.
    """
    if (not isinstance(url, str) or not url or len(url) > 4096
            or url != url.strip() or _controls(url) or "\\" in url
            or "?" in url or "#" in url):
        _fail("url_invalid")
    try:
        parts = urlsplit(url)
        if (parts.scheme.lower() != "https" or not parts.netloc
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            _fail("url_invalid")
        host = parts.hostname
        port = parts.port
        if (not host or "%" in host or any(char.isspace() for char in host)
                or (port is not None and not 1 <= port <= 65535)):
            _fail("url_invalid")
        if ":" in host:
            host = "[" + str(ipaddress.IPv6Address(host)) + "]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.?", host):
                _fail("url_invalid")
            if ".." in host:
                _fail("url_invalid")
        netloc = host + (":" + str(port) if port not in (None, 443) else "")
        path = parts.path or "/"
        if not path.startswith("/") or "//" in path:
            _fail("url_invalid")
        encoded = []
        for segment in path.strip("/").split("/") if path != "/" else []:
            decoded = unquote(segment, encoding="utf-8", errors="strict")
            if (not decoded or decoded in {".", ".."} or decoded != decoded.rstrip(" .") or _controls(decoded)
                    or any(char in decoded for char in "/\\%?#;")):
                _fail("url_invalid")
            encoded.append(quote(decoded, safe="-._~@!$&'()+,=:", encoding="utf-8", errors="strict"))
        return urlunsplit(("https", netloc, "/" + "/".join(encoded) + ("/" if encoded else ""), "", ""))
    except WebDAVError:
        raise
    except (ValueError, UnicodeError):
        _fail("url_invalid")


def _namespace(value: str) -> str:
    if not isinstance(value, str) or not _NAMESPACE.fullmatch(value):
        _fail("path_invalid")
    return value


def _filename(value: str) -> str:
    if not isinstance(value, str) or not _FILENAME.fullmatch(value):
        _fail("path_invalid")
    return value


class WebDAVClient:
    """One target, with independently created TLS connections for each request.

    ``connection_factory`` is a test seam with the HTTPSConnection signature:
    ``factory(host, port=..., timeout=..., context=...)``. Production callers
    should leave it unset. ``timeout`` bounds each socket operation; the request
    deadline additionally bounds streamed transfer work between operations.
    """

    def __init__(self, target: dict, *, connection_factory=None, timeout: float = 60,
                 request_seconds: float = 3600):
        if not isinstance(target, dict):
            _fail("credentials_invalid")
        self.url = validate_target_url(target.get("url"))
        parts = urlsplit(self.url)
        self._host, self._port = parts.hostname, parts.port or 443
        self._origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        self._base_path = parts.path
        username, password = target.get("username"), target.get("password")
        if (not isinstance(username, str) or not username or ":" in username
                or not isinstance(password, str) or not password
                or _controls(username) or _controls(password)):
            _fail("credentials_invalid")
        try:
            user_bytes, password_bytes = username.encode("utf-8"), password.encode("utf-8")
        except UnicodeError:
            _fail("credentials_invalid")
        if len(user_bytes) > 1024 or len(password_bytes) > 4096:
            _fail("credentials_invalid")
        self._authorization = "Basic " + base64.b64encode(user_bytes + b":" + password_bytes).decode("ascii")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 300 or isinstance(request_seconds, bool)
                or not isinstance(request_seconds, (int, float)) or not 0 < request_seconds <= 86400):
            _fail("credentials_invalid")
        self._timeout = timeout
        self._request_seconds = request_seconds
        self._factory = connection_factory or http.client.HTTPSConnection
        self._context = ssl.create_default_context()

    def _request(self, method: str, path: str, *, headers=None, body=None,
                 length: int = 0, read_body: bool = False, read_limit=MAX_RESPONSE_BYTES,
                 digest_size: int | None = None):
        # All paths originate from validated base/namespace or internally chosen
        # names. No server-provided Location or href is ever used as a request.
        if not path.startswith(self._base_path) or not path.startswith("/"):
            _fail("path_invalid")
        conn = None
        deadline = time.monotonic() + self._request_seconds

        def budget():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("timeout")
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.settimeout(min(self._timeout, remaining))

        try:
            conn = self._factory(self._host, port=self._port,
                                 timeout=min(self._timeout, self._request_seconds), context=self._context)
            conn.putrequest(method, path, skip_accept_encoding=True)
            conn.putheader("Authorization", self._authorization)
            conn.putheader("User-Agent", "AstrBot-QwenPaw-Backup/1")
            conn.putheader("Connection", "close")
            conn.putheader("Accept-Encoding", "identity")
            if method in {"PUT", "PROPFIND", "MKCOL", "MOVE"}:
                conn.putheader("Content-Length", str(length))
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            conn.endheaders()
            budget()
            if isinstance(body, bytes):
                conn.send(body)
            elif body is not None:
                sent = 0
                while sent < length:
                    budget()
                    chunk = body.read(min(CHUNK_BYTES, length - sent))
                    if not chunk:
                        _fail("source_invalid")
                    conn.send(chunk)
                    sent += len(chunk)
                if body.read(1):
                    _fail("source_invalid")
            budget()
            response = conn.getresponse()
            status_code = response.status
            # Discard all error and redirect bodies, which can echo credentials.
            self._check_status(status_code)
            selected_headers = {}
            for key in ("Content-Length", "Content-Encoding"):
                values = response.headers.get_all(key, [])
                if len(values) > 1:
                    _fail("remote_invalid")
                if values:
                    selected_headers[key.lower()] = values[0]
            result = b""
            if (read_body or digest_size is not None) and 200 <= status_code < 300:
                if selected_headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                    _fail("remote_invalid")
                declared = selected_headers.get("content-length")
                limit = digest_size if digest_size is not None else read_limit
                if declared is not None:
                    if not declared.isascii() or not declared.isdecimal():
                        _fail("remote_invalid")
                    if len(declared) > 12 or int(declared) > limit:
                        _fail("remote_size" if digest_size is not None else "remote_invalid")
                    if digest_size is not None and int(declared) != digest_size:
                        _fail("remote_size")
                pieces, size = [], 0
                digest = hashlib.sha256() if digest_size is not None else None
                while True:
                    budget()
                    chunk = response.read(min(CHUNK_BYTES if digest is not None else 16 * 1024, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        _fail("remote_size" if digest is not None else "remote_invalid")
                    if digest is not None:
                        digest.update(chunk)
                    else:
                        pieces.append(chunk)
                if digest is not None:
                    if size != digest_size:
                        _fail("remote_size")
                    result = digest.hexdigest().encode("ascii")
                else:
                    result = b"".join(pieces)
                    if declared is not None and size != int(declared):
                        _fail("remote_invalid")
            return status_code, selected_headers, result
        except WebDAVError:
            raise
        except ssl.SSLError:
            _fail("tls")
        except TimeoutError:
            _fail("timeout")
        except (OSError, http.client.HTTPException):
            _fail("connection")
        except (ValueError, UnicodeError, TypeError):
            _fail("remote_invalid")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _check_status(status: int):
        if 300 <= status < 400:
            _fail("redirect")
        if status == 401:
            _fail("authentication")
        if status == 403:
            _fail("permission")
        if status == 507:
            _fail("storage_full")
        if status == 429 or (500 <= status < 600 and status != 501):
            _fail("remote_unavailable")

    def _properties(self, path: str):
        code, _, raw = self._request("PROPFIND", path,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            body=_PROPERTIES, length=len(_PROPERTIES), read_body=True)
        if code == 404:
            _fail("remote_invalid")
        if code in {405, 501}:
            _fail("unsupported")
        if code != 207:
            _fail("remote_invalid")
        try:
            # WebDAV responses use UTF-8 here. Decode before inspecting markup
            # so UTF-16/32 encoded DTDs cannot bypass the entity prohibition.
            xml = raw.decode("utf-8-sig", errors="strict")
            if "\x00" in xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                _fail("remote_invalid")
            document = ET.fromstring(xml)
            if document.tag != _DAV + "multistatus":
                _fail("remote_invalid")
            responses = document.findall(_DAV + "response")
            if len(responses) != 1:
                _fail("remote_invalid")
            response = responses[0]
            href = response.findtext(_DAV + "href")
            if not href or _controls(href):
                _fail("remote_invalid")
            parsed = urlsplit(href)
            if (parsed.query or parsed.fragment or parsed.username is not None
                    or parsed.password is not None or (parsed.netloc and
                    urlunsplit((parsed.scheme, parsed.netloc, "", "", "")) != self._origin)):
                _fail("remote_invalid")
            if unquote(parsed.path, errors="strict").rstrip("/") != unquote(path, errors="strict").rstrip("/"):
                _fail("remote_invalid")
            collection, size = False, None
            found = False
            for propstat in response.findall(_DAV + "propstat"):
                status = propstat.findtext(_DAV + "status", "")
                if not re.fullmatch(r"HTTP/1\.[01] 200(?: .*)?", status):
                    continue
                prop = propstat.find(_DAV + "prop")
                if prop is None:
                    continue
                found = True
                kind = prop.find(_DAV + "resourcetype")
                if kind is not None:
                    collection = collection or kind.find(_DAV + "collection") is not None
                value = prop.findtext(_DAV + "getcontentlength")
                if value is not None:
                    if not value.isascii() or not value.isdecimal() or len(value) > 12:
                        _fail("remote_invalid")
                    if size is not None and size != int(value):
                        _fail("remote_invalid")
                    size = int(value)
            if not found:
                _fail("remote_invalid")
            return collection, size
        except WebDAVError:
            raise
        except (ET.ParseError, ValueError, UnicodeError):
            _fail("remote_invalid")

    def _ensure_directory(self, namespace: str) -> str:
        _namespace(namespace)
        if not self._properties(self._base_path)[0]:
            _fail("remote_invalid")
        path = self._base_path
        for segment in namespace.split("/"):
            path += segment + "/"
            code, _, _ = self._request("MKCOL", path)
            if code == 201:
                continue
            if code == 405 and self._properties(path)[0]:
                continue
            if code == 409:
                _fail("conflict")
            _fail("remote_invalid")
        return path

    def _size(self, path: str) -> int:
        code, headers, _ = self._request("HEAD", path)
        value = headers.get("content-length")
        if code == 200 and value is not None:
            if not value.isascii() or not value.isdecimal() or len(value) > 12:
                _fail("remote_invalid")
            return int(value)
        if code not in {200, 405, 501}:
            _fail("remote_invalid")
        collection, size = self._properties(path)
        if collection or size is None:
            _fail("remote_invalid")
        return size

    def _delete_path(self, path: str):
        code, _, _ = self._request("DELETE", path)
        if code not in {200, 204, 404}:
            _fail("cleanup_failed")

    def _matches_existing(self, path: str, source, expected_size: int) -> bool:
        """Recover a previously published upload without trusting size alone."""
        if self._size(path) != expected_size:
            return False
        try:
            source.seek(0)
            before = os.fstat(source.fileno())
            local_digest = hashlib.sha256()
            size = 0
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                local_digest.update(chunk)
                size += len(chunk)
                if size > expected_size:
                    _fail("source_invalid")
            after = os.fstat(source.fileno())
            if size != expected_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                _fail("source_invalid")
        except OSError:
            _fail("source_invalid")
        try:
            code, _, digest = self._request("GET", path, digest_size=expected_size)
        except WebDAVError as error:
            if error.code == "remote_size":
                return False
            raise
        return code == 200 and digest == local_digest.hexdigest().encode("ascii")

    def probe(self, namespace: str) -> None:
        directory = self._ensure_directory(namespace)
        path = directory + ".aq-probe-" + secrets.token_hex(16)
        payload = b"AstrBot-QwenPaw WebDAV probe\n" + secrets.token_bytes(64)
        owned = False
        try:
            # A random name plus If-None-Match prevents overwriting another file.
            owned = True
            code, _, _ = self._request("PUT", path, body=payload, length=len(payload),
                headers={"If-None-Match": "*", "Content-Type": "application/octet-stream"})
            if code == 412:
                owned = False
                _fail("conflict")
            if code not in {200, 201, 204}:
                _fail("remote_invalid")
            code, _, downloaded = self._request("GET", path, read_body=True, read_limit=1024)
            if code != 200 or downloaded != payload:
                _fail("remote_size")
        except Exception:
            if owned:
                try:
                    self._delete_path(path)
                except WebDAVError:
                    pass
            raise
        if owned:
            self._delete_path(path)

    def upload(self, archive: Path, namespace: str, filename: str) -> dict:
        _namespace(namespace)
        _filename(filename)
        try:
            archive = Path(archive)
            before = archive.lstat()
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARCHIVE_BYTES:
                _fail("source_invalid")
            source = archive.open("rb")
        except WebDAVError:
            raise
        except (OSError, ValueError, TypeError):
            _fail("source_invalid")
        with source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                _fail("source_invalid")
            directory = self._ensure_directory(namespace)
            temporary = directory + ".aq-upload-" + secrets.token_hex(16) + ".part"
            final = directory + filename
            owned = True
            try:
                code, _, _ = self._request("PUT", temporary, body=source, length=before.st_size,
                    headers={"If-None-Match": "*", "Content-Type": "application/octet-stream"})
                if code == 412:
                    owned = False
                    _fail("conflict")
                if code not in {200, 201, 204}:
                    _fail("remote_invalid")
                after = os.fstat(source.fileno())
                if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                    _fail("source_invalid")
                if self._size(temporary) != before.st_size:
                    _fail("remote_size")
                code, _, _ = self._request("MOVE", temporary,
                    headers={"Destination": self._origin + final, "Overwrite": "F"})
                if code == 412:
                    if not self._matches_existing(final, source, before.st_size):
                        _fail("conflict")
                    self._delete_path(temporary)
                    owned = False
                    return {"filename": filename, "size": before.st_size}
                if code not in {201, 204}:
                    _fail("remote_invalid")
                if self._size(final) != before.st_size:
                    _fail("remote_size")
                return {"filename": filename, "size": before.st_size}
            except Exception:
                if owned:
                    try:
                        self._delete_path(temporary)
                    except WebDAVError:
                        pass
                raise

    def delete(self, namespace: str, filename: str) -> None:
        _namespace(namespace)
        _filename(filename)
        self._delete_path(self._base_path + namespace + "/" + filename)
