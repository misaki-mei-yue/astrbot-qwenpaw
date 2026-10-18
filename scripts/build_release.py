#!/usr/bin/env python3
"""Build a deterministic, source-only release without copying local state.

The allowlist is deliberately independent from Git's tracked-file list. A file
being tracked does not make it public. This is a second gate after reviewing the
publication checkout; it cannot replace that review. Additional private strings
can be supplied from an external UTF-8 file, one literal string per line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Iterable
import zipfile


DEFAULT_VERSION = "0.6.0-alpha"
MANIFEST_NAME = "RELEASE-MANIFEST.json"
PUBLIC_ROOT_FILES = frozenset({
    ".dockerignore", ".env.example", ".gitattributes", ".gitignore",
    "CHANGELOG.md", "CONTRIBUTING.md", "COPYRIGHT.md", "LICENSE", "README.md", "SECURITY.md",
    "THIRD_PARTY_NOTICES.md", "requirements-dev.txt", "start.cmd", "start.ps1",
    "stop.cmd", "stop.ps1", "bundle-version.json",
})
PUBLIC_DIRECTORIES = frozenset({
    ".github", "astrbot_plugin_qwenpaw_bridge", "qwenpaw_plugin_astrbot_bridge",
    "bundle_gateway", "bundle_updater", "deploy", "docs", "licenses", "scripts", "tests",
})
# These reports describe public component compatibility. Site-specific migration
# receipts and diagnostic dumps are intentionally not accepted.
PUBLIC_DOC_REPORTS = frozenset({
    "astrbot-runtime-validation.json", "personal-astrbot-validation.json",
    "personal-qwenpaw-validation.json", "qwenpaw-browser-validation.json",
    "qwenpaw-image-validation.json", "qwenpaw-linux-browser-validation.json",
    "qwenpaw-linux-runtime-validation.json", "qwenpaw-runtime-validation.json",
    "upstream-media-validation.json", "gateway-navigation-fix-20261017.json",
    "deepseek-memory-compat-20261018.json",
})
TEXT_SUFFIXES = frozenset({
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".sh", ".ps1",
    ".cmd", ".bat", ".cjs", ".mjs", ".js", ".ts", ".html", ".css",
    ".toml", ".ini", ".cfg", ".caddyfile", ".example", ".license",
})
TEXT_FILENAMES = frozenset({"Dockerfile", "Caddyfile", "LICENSE"})
PRIVATE_COMPONENTS = frozenset({
    ".git", "runtime", "data", "working", "secret", "secrets", ".secrets",
    "cache", "caches", ".cache", "__pycache__", ".pytest_cache", "node_modules",
    ".venv", "venv", "tmp", "temp", "backup", "backups", "artifacts", "dist",
    "build", "logs", "sessions", "memory", "state", "user_data",
})
PRIVATE_FILENAMES = frozenset({"cleanup_legacy_bootstrap.py"})
PRIVATE_SUFFIXES = frozenset({
    ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz", ".tgz", ".7z",
    ".log", ".pyc", ".pyo", ".key", ".pem", ".pfx", ".p12", ".bak",
    ".before", ".lock", ".wal", ".shm",
})
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?\Z")
SECRET_RULES = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("provider-token", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("cloud-access-key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("local-user-path", re.compile(r"(?i)(?:[A-Z]:[\\/]+U[s]ers[\\/]+[^\s\"'<>]+|/U[s]ers/[^\s\"'<>]+|/h[o]me/[^\s\"'<>]+)")),
)
MAX_TEXT_BYTES = 5 * 1024 * 1024
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ReleaseError(ValueError):
    """A publication gate rejected the source tree."""


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_ancestors(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.exists() and _is_link(ancestor):
            raise ReleaseError("A publication path contains a symlink or junction")


def _safe_relative(relative: Path, private_patterns: tuple[str, ...]) -> str:
    label = relative.as_posix()
    for value in private_patterns:
        label = label.replace(value, "[redacted]")
    for _, expression in SECRET_RULES:
        label = expression.sub("[redacted]", label)
    return label


def is_public_path(relative: Path) -> bool:
    """Return whether a lexical relative path is admitted by the public policy."""
    parts = relative.parts
    if not parts or relative.is_absolute() or any(p in {"", ".", ".."} for p in parts):
        return False
    folded = tuple(p.casefold() for p in parts)
    if any(p in PRIVATE_COMPONENTS for p in folded):
        return False
    if any(p.startswith(".env") for p in folded) and parts != (".env.example",):
        return False
    if relative.name.casefold() in PRIVATE_FILENAMES:
        return False
    if relative.suffix.casefold() in PRIVATE_SUFFIXES:
        return False
    if len(parts) == 1:
        return relative.name in PUBLIC_ROOT_FILES
    if parts[0] not in PUBLIC_DIRECTORIES:
        return False
    if parts[0] == "docs" and relative.suffix.casefold() == ".json":
        return len(parts) == 2 and relative.name in PUBLIC_DOC_REPORTS
    if (parts[0] == "licenses" and relative.name.upper().startswith("LICENSE")
            and relative.suffix.casefold() in {"", ".txt", ".md", ".license"}):
        return True
    return relative.suffix.casefold() in TEXT_SUFFIXES or relative.name in TEXT_FILENAMES


def _read_source(path: Path, relative: Path, private_patterns: tuple[str, ...]) -> bytes:
    label = _safe_relative(relative, private_patterns)
    if any(value in relative.as_posix() for value in private_patterns):
        raise ReleaseError(f"Sensitive filename (private-pattern) rejected: {label}")
    for category, expression in SECRET_RULES:
        if expression.search(relative.as_posix()):
            raise ReleaseError(f"Sensitive filename ({category}) rejected: {label}")
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ReleaseError(f"Source exceeds the text size limit: {label}")
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ReleaseError(f"Non-UTF-8 source rejected: {label}") from None
    if "\0" in text:
        raise ReleaseError(f"Binary source rejected: {label}")
    for category, expression in SECRET_RULES:
        if expression.search(text):
            raise ReleaseError(f"Sensitive content ({category}) rejected: {label}")
    if any(value in text for value in private_patterns):
        raise ReleaseError(f"Sensitive content (private-pattern) rejected: {label}")
    return content


def collect_public_sources(root: Path, private_patterns: Iterable[str] = ()) -> list[tuple[str, bytes]]:
    """Read admitted files; never follow a symlink or Windows junction."""
    root = Path(os.path.abspath(root))
    _check_ancestors(root)
    if _is_link(root):
        raise ReleaseError("The source root must not be a symlink or junction")
    if not root.is_dir():
        raise ReleaseError("The source root must be a directory")
    root = root.resolve()
    patterns = tuple(value for value in private_patterns if value)
    found: list[tuple[str, bytes]] = []

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
            relative = path.relative_to(root)
            # Check links before exclusions at every visited level. Excluded
            # state directories are never opened or recursively enumerated.
            if _is_link(path):
                raise ReleaseError(f"Symlink or junction rejected: {_safe_relative(relative, patterns)}")
            if path.is_dir():
                folded = tuple(part.casefold() for part in relative.parts)
                if any(part in PRIVATE_COMPONENTS for part in folded):
                    continue
                if relative.parts[0] in PUBLIC_DIRECTORIES:
                    visit(path)
                continue
            if not path.is_file():
                raise ReleaseError(f"Non-regular source rejected: {_safe_relative(relative, patterns)}")
            if is_public_path(relative):
                if not _within(path.resolve(), root):
                    raise ReleaseError("Source escaped the publication root")
                found.append((relative.as_posix(), _read_source(path, relative, patterns)))

    visit(root)
    paths = {name for name, _ in found}
    if not {"README.md", "LICENSE"} <= paths:
        raise ReleaseError("A release requires public README.md and LICENSE files")
    return sorted(found)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
    info.create_system = 3
    mode = 0o755 if name.endswith(".sh") else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_release(root: Path, output: Path, version: str = DEFAULT_VERSION,
                  private_patterns: Iterable[str] = ()) -> dict:
    if len(version) > 80 or not VERSION_RE.fullmatch(version):
        raise ReleaseError("Version must be a semantic version such as 0.5.0-alpha")
    root = Path(os.path.abspath(root))
    output = Path(os.path.abspath(output))
    _check_ancestors(output.parent)
    if output.suffix.casefold() != ".zip":
        raise ReleaseError("The output must use a .zip suffix")
    if output.exists() or output.is_symlink():
        raise ReleaseError("The output already exists; select a new output file")
    if not output.parent.is_dir() or _is_link(output.parent):
        raise ReleaseError("The output directory must exist and must not be a link")
    sources = collect_public_sources(root, private_patterns)
    by_name = dict(sources)
    if "bundle-version.json" not in by_name:
        raise ReleaseError("A release requires bundle-version.json")
    version_metadata = json.loads(by_name["bundle-version.json"])
    if version_metadata.get("version") != version or version_metadata.get("update_protocol") != 1:
        raise ReleaseError("Release version and updater metadata must match")
    manifest = {
        "format": 1,
        "version": version,
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(),
                   "size": len(content)} for name, content in sources],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix="release-", suffix=".tmp", dir=output.parent, delete=False) as temp:
            temp_name = Path(temp.name)
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in sources:
                archive.writestr(_zip_info(name), content)
            archive.writestr(_zip_info(MANIFEST_NAME), manifest_bytes)
        # Opening exclusively avoids overwriting an output created concurrently.
        with output.open("xb") as destination, temp_name.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
    finally:
        if temp_name is not None:
            temp_name.unlink(missing_ok=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--private-patterns", type=Path,
                        help="External UTF-8 file containing one private literal string per line")
    args = parser.parse_args(argv)
    try:
        patterns: tuple[str, ...] = ()
        if args.private_patterns is not None:
            pattern_path = args.private_patterns.resolve()
            if _within(pattern_path, args.root.resolve()):
                raise ReleaseError("The private-patterns file must be outside the source root")
            patterns = tuple(line.strip() for line in pattern_path.read_text(encoding="utf-8-sig").splitlines() if line.strip())
        manifest = build_release(args.root, args.output, args.version, patterns)
    except (ReleaseError, OSError) as error:
        # OSError may contain a local absolute path. Never expose its raw text.
        message = str(error) if isinstance(error, ReleaseError) else "Filesystem operation failed"
        print(f"Release rejected: {message}", file=sys.stderr)
        return 1
    print(f"Built release {manifest['version']}: {len(manifest['files'])} public source files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
