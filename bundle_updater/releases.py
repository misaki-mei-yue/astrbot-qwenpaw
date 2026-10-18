"""Fetch reviewed bundle releases and validate source archives before installation.

No code in a downloaded release is imported or executed by this module. The
repository and HTTPS origins are fixed; checksums provide transport integrity,
not an independent signature against a compromised publisher account.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


REPOSITORY = "misaki-mei-yue/astrbot-qwenpaw"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
MANIFEST_NAME = "RELEASE-MANIFEST.json"
VERSION_NAME = "bundle-version.json"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 2000
REQUEST_TIMEOUT = 20
DOWNLOAD_DEADLINE = 180
SEMVER_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
PRIVATE_PARTS = frozenset({
    ".git", "runtime", "data", "working", "secret", "secrets", ".secrets",
    "cache", "caches", ".cache", "__pycache__", ".pytest_cache", "node_modules",
    ".venv", "venv", "tmp", "temp", "backup", "backups", "artifacts", "dist",
    "build", "logs", "sessions", "memory", "state", "user_data", "credentials",
    "cookies", "browser_profiles", "login_data",
})
PRIVATE_SUFFIXES = frozenset({
    ".db", ".sqlite", ".sqlite3", ".log", ".key", ".pem", ".pfx", ".p12",
    ".bak", ".before", ".lock", ".wal", ".shm", ".pyc", ".pyo", ".zip",
    ".tar", ".gz", ".tgz", ".7z",
})
PUBLIC_ROOT_FILES = frozenset({
    ".dockerignore", ".env.example", ".gitattributes", ".gitignore",
    "CHANGELOG.md", "CONTRIBUTING.md", "COPYRIGHT.md", "LICENSE", "README.md",
    "SECURITY.md", "THIRD_PARTY_NOTICES.md", "requirements-dev.txt", "start.cmd",
    "start.ps1", "stop.cmd", "stop.ps1", VERSION_NAME, MANIFEST_NAME,
})
PUBLIC_DIRECTORIES = frozenset({
    ".github", "astrbot_plugin_qwenpaw_bridge", "qwenpaw_plugin_astrbot_bridge",
    "bundle_gateway", "bundle_updater", "deploy", "docs", "licenses", "scripts",
    "tests",
})
PRIVATE_NAMES = frozenset({
    "credentials.json", "credential.json", "tokens.json", "token.json", "cookies.json",
    "login.json", "accounts.json", "session.json", "sessions.json", "id_rsa", "id_ed25519",
})
SECRET_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"
    r"|(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
)


class ReleaseError(ValueError):
    """An updater error whose display text never contains a response or local path."""

    def __init__(self, code: str, message: str):
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "update_error"
        safe = re.sub(r"https?://[^\s]+|[A-Za-z]:[\\/][^\s]+|/(?:home|Users|tmp|opt|app|root|mnt)/[^\s]+",
                      "[redacted]", str(message))
        safe = SECRET_RE.sub("[redacted]", safe)
        safe = " ".join(safe.split())[:500]
        super().__init__(safe or "The release operation failed.")


def _fail(code: str, message: str):
    raise ReleaseError(code, message)


def _version_key(value: str) -> tuple:
    if not isinstance(value, str) or len(value) > 80:
        _fail("invalid_version", "The bundle version is invalid.")
    match = SEMVER_RE.fullmatch(value)
    if match is None:
        _fail("invalid_version", "The bundle version is invalid.")
    pre = match.group(4)
    if pre is None:
        suffix = (1, ())
    else:
        identifiers = pre.split(".")
        if any(item.isdigit() and len(item) > 1 and item.startswith("0") for item in identifiers):
            _fail("invalid_version", "The bundle version is invalid.")
        suffix = (0, tuple((0, int(item)) if item.isdigit() else (1, item) for item in identifiers))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), suffix)


def _allowed_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            return False
        host = parsed.hostname
        if host == "api.github.com":
            return parsed.path == f"/repos/{REPOSITORY}/releases" or bool(re.fullmatch(
                rf"/repos/{re.escape(REPOSITORY)}/releases/assets/[0-9]+", parsed.path))
        if host == "github.com":
            return parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
        return host in {
            "release-assets.githubusercontent.com", "objects.githubusercontent.com",
            "github-releases.githubusercontent.com",
        }
    except (ValueError, TypeError):
        return False


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            _fail("unsafe_redirect", "The release service returned an untrusted redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request_bytes(opener, url: str, limit: int, *, destination: Path | None = None) -> bytes:
    if not _allowed_url(url):
        _fail("unsafe_url", "The release location is not trusted.")
    request = Request(url, headers={
        "User-Agent": "astrbot-qwenpaw-updater/1", "Accept": "application/vnd.github+json"
        if url.startswith("https://api.github.com/") else "application/octet-stream",
        "Accept-Encoding": "identity",
    })
    output = None
    try:
        open_request = getattr(opener, "open", opener)
        with open_request(request, timeout=REQUEST_TIMEOUT) as response:
            if getattr(response, "status", 200) != 200:
                _fail("download_failed", "The release service did not return a successful response.")
            if not _allowed_url(response.geturl()):
                _fail("unsafe_redirect", "The release service returned an untrusted redirect.")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except (TypeError, ValueError):
                    _fail("invalid_response", "The release service returned invalid download metadata.")
                if declared < 0 or declared > limit:
                    _fail("download_too_large", "The release download exceeds its size limit.")
            if destination is not None:
                output = destination.open("xb")
            chunks = []
            total = 0
            started = time.monotonic()
            while True:
                if time.monotonic() - started > DOWNLOAD_DEADLINE:
                    _fail("download_timeout", "The release download took too long.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    _fail("download_too_large", "The release download exceeds its size limit.")
                if output is None:
                    chunks.append(chunk)
                else:
                    output.write(chunk)
            if length is not None and total != declared:
                _fail("incomplete_download", "The release download was incomplete.")
            return b"".join(chunks)
    except ReleaseError:
        raise
    except HTTPError as error:
        if error.code == 403 or error.code == 429:
            _fail("rate_limited", "The release service rate limit was reached. Try again later.")
        _fail("download_failed", "The release download failed.")
    except (OSError, URLError, TimeoutError, ValueError):
        _fail("download_failed", "The release download failed. Check network access and try again.")
    finally:
        if output is not None:
            output.close()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_metadata", "Release metadata contains duplicate fields.")
        result[key] = value
    return result


def _json(content: bytes):
    try:
        return json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        _fail("invalid_metadata", "The release metadata could not be read.")


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _check_path(path: Path):
    for entry in (path, *path.parents):
        if (entry.exists() or entry.is_symlink()) and _is_link(entry):
            _fail("unsafe_local_path", "Update files cannot use symbolic links or junctions.")


def _safe_name(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > 240 or "\\" in name
            or name.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or unicodedata.normalize("NFC", name) != name):
        _fail("unsafe_archive_path", "The release archive contains an unsafe filename.")
    parts = name.split("/")
    for part in parts:
        if (part in {"", ".", ".."} or part.endswith((".", " "))
                or re.search(r'[<>:"|?*]', part)
                or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", part.split(".")[0])):
            _fail("unsafe_archive_path", "The release archive contains an unsafe filename.")
        folded = part.casefold()
        if (folded in PRIVATE_PARTS or folded in PRIVATE_NAMES
                or (folded.startswith(".env") and name != ".env.example")):
            _fail("private_archive_path", "The release archive contains private runtime or credential files.")
    if Path(parts[-1]).suffix.casefold() in PRIVATE_SUFFIXES:
        _fail("private_archive_path", "The release archive contains private runtime or credential files.")
    if (len(parts) == 1 and name not in PUBLIC_ROOT_FILES
            or len(parts) > 1 and parts[0] not in PUBLIC_DIRECTORIES):
        _fail("unexpected_archive_path", "The release archive contains files outside the public source layout.")
    return name


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        with archive.open(info) as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) != info.file_size or len(content) > MAX_FILE_BYTES:
            _fail("invalid_archive_size", "The release archive contains an invalid file size.")
        text = content.decode("utf-8-sig")
        if "\0" in text or SECRET_RE.search(text):
            _fail("private_archive_content", "The release archive contains binary or credential content.")
        return content
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        _fail("invalid_archive", "The release archive could not be safely read.")


def _validate_zip(archive: zipfile.ZipFile, expected_version: str) -> dict:
    _version_key(expected_version)
    infos = archive.infolist()
    if not infos or len(infos) > MAX_FILES:
        _fail("invalid_archive_count", "The release archive has too many or too few files.")
    indexed = {}
    folded_names = set()
    expanded = 0
    for info in infos:
        if info.orig_filename != info.filename:
            _fail("unsafe_archive_path", "The release archive contains an unsafe filename.")
        name = _safe_name(info.filename)
        key = name.casefold()
        # Detect file/directory collisions too, including case-folded ancestors.
        pieces = key.split("/")
        if (key in folded_names or any("/".join(pieces[:index]) in folded_names
                                      for index in range(1, len(pieces)))
                or any(old.startswith(key + "/") for old in folded_names)):
            _fail("duplicate_archive_path", "The release archive has duplicate or conflicting filenames.")
        folded_names.add(key)
        file_type = stat.S_IFMT(info.external_attr >> 16)
        if (info.is_dir() or file_type not in (0, stat.S_IFREG)
                or info.external_attr & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            _fail("unsafe_archive_entry", "The release archive contains a link or unsupported entry.")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            _fail("invalid_archive_size", "A release file exceeds its size limit.")
        if info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 200:
            _fail("archive_compression_ratio", "The release archive has an unsafe compression ratio.")
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            _fail("invalid_archive_size", "The expanded release exceeds its size limit.")
        indexed[name] = info
    if MANIFEST_NAME not in indexed or VERSION_NAME not in indexed:
        _fail("missing_manifest", "The release archive is missing its manifest or bundle version.")
    manifest = _json(_read_member(archive, indexed[MANIFEST_NAME]))
    if (not isinstance(manifest, dict) or type(manifest.get("format")) is not int
            or manifest["format"] != 1 or manifest.get("version") != expected_version
            or not isinstance(manifest.get("files"), list)):
        _fail("invalid_manifest", "The release manifest does not match the requested bundle version.")
    entries = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            _fail("invalid_manifest", "The release manifest has an invalid file entry.")
        name = _safe_name(item.get("path"))
        if (name == MANIFEST_NAME or name in entries or type(item.get("size")) is not int
                or item["size"] < 0 or item["size"] > MAX_FILE_BYTES
                or not isinstance(item.get("sha256"), str) or not SHA_RE.fullmatch(item["sha256"])):
            _fail("invalid_manifest", "The release manifest has an invalid file entry.")
        entries[name] = item
    if set(indexed) != set(entries) | {MANIFEST_NAME} or not {"README.md", "LICENSE", VERSION_NAME} <= set(entries):
        _fail("manifest_file_mismatch", "The release files do not exactly match the manifest.")
    for name, item in entries.items():
        if indexed[name].file_size != item["size"]:
            _fail("manifest_size_mismatch", "A release file does not match its declared size.")
        content = _read_member(archive, indexed[name])
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            _fail("manifest_hash_mismatch", "A release file does not match its declared checksum.")
    version_data = _json(_read_member(archive, indexed[VERSION_NAME]))
    if (not isinstance(version_data, dict) or type(version_data.get("format")) is not int
            or version_data["format"] != 1 or version_data.get("version") != expected_version
            or type(version_data.get("update_protocol")) is not int or version_data["update_protocol"] != 1):
        _fail("unsupported_update_protocol", "This release requires an unsupported update protocol.")
    components = version_data.get("components")
    if not isinstance(components, dict) or set(components) != {"astrbot", "qwenpaw", "napcat"}:
        _fail("invalid_components", "The release is missing its component versions.")
    for value in components.values():
        _version_key(value)
    return {"manifest": manifest, "components": components, "version": expected_version, "update_protocol": 1}


def _open_archive(path: Path):
    _check_path(path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        _fail("invalid_archive_size", "The release archive is missing or exceeds its size limit.")
    return zipfile.ZipFile(path)


def validate_archive(path: Path, expected_version: str) -> dict:
    """Verify every file; return manifest, components, version, update_protocol."""
    try:
        with _open_archive(Path(path)) as archive:
            return _validate_zip(archive, expected_version)
    except ReleaseError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        _fail("invalid_archive", "The release archive could not be safely read.")


def extract_verified_archive(path: Path, destination: Path, expected_version: str) -> dict:
    """Validate before writing and extract exclusively into a new or empty directory."""
    destination = Path(os.path.abspath(destination))
    written = []
    created_dirs = []
    try:
        _check_path(destination)
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            _fail("destination_not_empty", "The release destination must be a new or empty directory.")
        if not destination.parent.is_dir():
            _fail("invalid_destination", "The release destination parent must already exist.")
        with _open_archive(Path(path)) as archive:
            verified = _validate_zip(archive, expected_version)
            if not destination.exists():
                destination.mkdir()
                created_dirs.append(destination)
            items = {item["path"]: item for item in verified["manifest"]["files"]}
            for info in archive.infolist():
                name = _safe_name(info.filename)
                target = destination.joinpath(*name.split("/"))
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_dirs.append(directory)
                _check_path(target)
                content = _read_member(archive, info)
                if name != MANIFEST_NAME and hashlib.sha256(content).hexdigest() != items[name]["sha256"]:
                    _fail("manifest_hash_mismatch", "A release file changed during extraction.")
                with target.open("xb") as output:
                    written.append(target)
                    output.write(content)
                if os.name != "nt":
                    target.chmod(0o755 if name.endswith(".sh") else 0o644)
            return verified
    except Exception as error:
        # Only remove files/directories created by this invocation; no recursive
        # deletion and no mutation of a pre-existing nonempty destination.
        for target in reversed(written):
            try:
                _check_path(target)
                if not _is_link(target):
                    target.unlink()
            except (OSError, ReleaseError):
                pass
        for directory in reversed(created_dirs):
            try:
                _check_path(directory)
                if not _is_link(directory):
                    directory.rmdir()
            except (OSError, ReleaseError):
                pass
        if isinstance(error, ReleaseError):
            raise
        _fail("extraction_failed", "The verified release could not be extracted safely.")


class _PlainNotes(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _notes(value) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainNotes()
    parser.feed(value[:32000])
    text = "".join(parser.parts)
    text = re.sub(r"!?\[([^\]]*)\]\([^\n)]*\)", r"\1", text)
    return "".join(char for char in text if ord(char) >= 32 or char in "\n\t")[:8000]


def _asset(release: dict, name: str) -> dict:
    assets = release.get("assets")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name] if isinstance(assets, list) else []
    if len(matches) != 1:
        _fail("missing_release_asset", "The release does not have exactly one required download asset.")
    asset = matches[0]
    if type(asset.get("id")) is not int or asset["id"] < 1 or type(asset.get("size")) is not int or asset["size"] < 1:
        _fail("invalid_release_asset", "The release download metadata is invalid.")
    expected = f"https://github.com/{REPOSITORY}/releases/download/{quote(release['tag_name'], safe='')}/{quote(name, safe='')}"
    if asset.get("browser_download_url") != expected:
        _fail("unsafe_url", "The release download does not belong to the fixed bundle repository.")
    if asset.get("state", "uploaded") != "uploaded":
        _fail("invalid_release_asset", "The release download has not finished uploading.")
    digest = asset.get("digest")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        _fail("invalid_release_digest", "The release asset digest is invalid.")
    return asset


def _checksum(content: bytes, name: str) -> str:
    try:
        text = content.decode("ascii")
    except UnicodeError:
        _fail("invalid_checksum", "The release checksum file is invalid.")
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if match is not None and match.group(2) == name:
            matches.append(match.group(1))
    if len(matches) != 1:
        _fail("invalid_checksum", "The release checksum file does not uniquely identify the bundle.")
    return matches[0]


def check_for_update(current_version: str, cache_dir: Path, *, allow_prerelease: bool | None = None, opener=None) -> dict | None:
    """Download and validate a newer fixed-repository release, without installing.

    Alpha/beta installations include prereleases by default. Stable installations
    require a newer stable release unless explicitly opted into prereleases.
    ``opener`` is an optional urllib-compatible transport for offline tests.
    """
    current = _version_key(current_version)
    if allow_prerelease is None:
        allow_prerelease = current[3][0] == 0
    if type(allow_prerelease) is not bool:
        _fail("invalid_prerelease_setting", "The prerelease preference must be a boolean.")
    transport = opener if opener is not None else build_opener(_SafeRedirect())
    releases = _json(_request_bytes(transport, API_URL, 4 * 1024 * 1024))
    if not isinstance(releases, list):
        _fail("invalid_metadata", "The release service returned invalid release metadata.")
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            continue
        try:
            key = _version_key(tag[1:])
        except ReleaseError:
            continue
        if not allow_prerelease and (key[3][0] == 0 or release.get("prerelease") is True):
            continue
        if key <= current:
            continue
        if type(release.get("id")) is not int or release["id"] < 1:
            _fail("invalid_metadata", "The release identifier is invalid.")
        candidates.append((key, release["id"], release))
    if not candidates:
        return None
    _, _, release = max(candidates, key=lambda item: (item[0], item[1]))
    version = release["tag_name"][1:]
    name = f"astrbot-qwenpaw-{version}.zip"
    package = _asset(release, name)
    checksums = _asset(release, "SHA256SUMS")
    if package["size"] > MAX_ARCHIVE_BYTES or checksums["size"] > 128 * 1024:
        _fail("download_too_large", "A release download exceeds its size limit.")
    sums = _request_bytes(transport, checksums["browser_download_url"], 128 * 1024)
    if len(sums) != checksums["size"]:
        _fail("incomplete_download", "The release checksum download was incomplete.")
    if checksums.get("digest") and hashlib.sha256(sums).hexdigest() != checksums["digest"][7:]:
        _fail("checksum_digest_mismatch", "The release checksum file does not match its published digest.")
    expected_sha = _checksum(sums, name)
    if package.get("digest") and package["digest"][7:] != expected_sha:
        _fail("checksum_digest_mismatch", "The release checksum disagrees with the published asset digest.")
    cache_dir = Path(os.path.abspath(cache_dir))
    temporary = None
    try:
        _check_path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        _check_path(cache_dir)
        temporary = Path(tempfile.mkdtemp(prefix="verified-release-", dir=cache_dir))
        archive_path = temporary / name
        _request_bytes(transport, package["browser_download_url"], MAX_ARCHIVE_BYTES, destination=archive_path)
        if archive_path.stat().st_size != package["size"]:
            _fail("incomplete_download", "The release package download was incomplete.")
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if digest != expected_sha:
            _fail("checksum_mismatch", "The downloaded release does not match its checksum.")
        verified = validate_archive(archive_path, version)
        return {
            **verified, "tag": release["tag_name"], "release_id": release["id"],
            "asset_id": package["id"], "asset_name": name,
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{quote(release['tag_name'], safe='')}",
            "notes": _notes(release.get("body")), "sha256": digest,
            "archive_path": str(archive_path),
        }
    except ReleaseError:
        raise
    except OSError:
        _fail("cache_failed", "The release cache could not be written.")
    finally:
        # Successful validated downloads remain cached for the host updater.
        if temporary is not None and "verified" not in locals():
            for item in temporary.iterdir():
                if item.is_file() and not item.is_symlink():
                    item.unlink(missing_ok=True)
            temporary.rmdir()
