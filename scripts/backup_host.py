"""Use installed crypto or create a private, project-scoped helper environment."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle_updater.common import FileLock, safe_path


def helper_python(runtime: Path):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return sys.executable
    except ImportError:
        pass
    directory = safe_path(runtime / "backup-host-env")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(runtime / ".backup-host-install.lock"):
        executable = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        if not executable.is_file():
            subprocess.run([sys.executable, "-m", "venv", str(directory)], check=True, timeout=120, **options)
        probe = subprocess.run([str(executable), "-c", "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.kdf.scrypt import Scrypt"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, **options)
        if probe.returncode:
            print("Preparing the project-local encrypted backup helper...", file=sys.stderr)
            subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "cryptography==50.0.2"],
                           check=True, timeout=600, stdout=sys.stderr, stderr=sys.stderr, **options)
        return str(executable)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        executable = helper_python(safe_path(args.runtime_root))
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        if not arguments:
            print(executable)
            return 0
        return subprocess.run([executable, str(Path(__file__).with_name("run_bundle_backup.py")), *arguments], check=False,
                              **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})).returncode
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Could not prepare encrypted backups. Check Python venv support and access to PyPI.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
