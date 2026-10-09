"""Prepare private deployment files, never run Docker or edit existing services."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

SECRET_KEYS = ("BRIDGE_TOKEN", "QWENPAW_RUNTIME_INTERNAL_TOKEN", "ONEBOT_TOKEN")
PROJECT_SOURCE = Path(__file__).resolve().parents[1]
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def reject_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError("Refusing a symlink in a deployment path.")


def private_dir(path: Path) -> None:
    reject_symlinks(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("Deployment directory path is not a directory.")
    path.chmod(0o700)


def parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("Deployment .env must be a regular file.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not ENV_KEY.fullmatch(key) or key in values:
            raise ValueError("Deployment .env contains an invalid or duplicate key.")
        if value.startswith("'"):
            if not value.endswith("'"):
                raise ValueError("Deployment .env contains an unterminated quoted value.")
            value = value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        elif any(char in value for char in "\"$#\r\n"):
            raise ValueError("Use single-quoted .env values; shell expansion is not supported.")
        values[key] = value
    return values


def env_value(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("Deployment environment values must be single-line strings.")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def validate_media_limits(env: dict[str, str]) -> None:
    for key, ceiling in (("BRIDGE_MAX_FILE_BYTES", 20 * 1024 * 1024), ("BRIDGE_MAX_FILES", 4)):
        value = env[key]
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= ceiling:
            raise ValueError(f"{key} must be a positive integer no greater than {ceiling}.")


def private_write(path: Path, text: str, *, replace: bool = False) -> None:
    reject_symlinks(path)
    if path.exists() and not replace:
        return
    fd, temp_name = tempfile.mkstemp(prefix=".prepare-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        temp_path.chmod(0o600)
        if replace:
            os.replace(temp_path, path)
        else:
            # Do not overwrite a file another setup process may have just made.
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            path.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)


def _prepare_locked(mode: str, root: Path, existing_dir: Path | None = None) -> dict[str, str]:
    reject_symlinks(root)
    if root == Path(root.anchor):
        raise ValueError("Choose a dedicated installation directory, not the filesystem root.")
    marker = root / ".combined-setup.json"
    env_path = root / ".env"
    reject_symlinks(marker)
    reject_symlinks(env_path)
    if mode == "fresh" and any(
        (root / name).exists() for name in ("data", "compose.yaml", "deployment-receipt.txt")
    ):
        raise ValueError("An existing AstrBot installation was found. Use upgrade_existing.sh.")
    if marker.exists():
        previous = json.loads(marker.read_text(encoding="utf-8"))
        if previous.get("mode") != mode:
            raise ValueError("The directory was prepared for a different deployment mode.")
    if mode == "existing":
        if existing_dir is None:
            raise ValueError("Supply the existing AstrBot installation directory.")
        existing_dir = Path(os.path.abspath(existing_dir))
        reject_symlinks(existing_dir)
        if not (existing_dir / "compose.yaml").is_file():
            raise ValueError("Expected an existing compose.yaml. Nothing was changed.")
        if root == existing_dir:
            raise ValueError("The add-on must have its own subdirectory or separate root.")

    env = parse_env(env_path)
    # Existing nonempty credentials remain byte-for-byte unchanged.
    for key in SECRET_KEYS:
        if env.get(key):
            if len(env[key]) < 16:
                raise ValueError(f"Existing {key} is too short; refusing to reset it automatically.")
        else:
            env[key] = secrets.token_urlsafe(32)
    defaults = {
        "COMPOSE_PROJECT_NAME": "bot-combined" if mode == "fresh" else "bot-combined-addon",
        "BRIDGE_NETWORK": "bot-combined-network",
        "QWENPAW_BASE_URL": "http://qwenpaw:8088",
        "QWENPAW_AGENT_ID": "default",
        "ASTRBOT_BRIDGE_URL": "http://astrbot:9186",
        "OWNER_USER_IDS": "",
        "TOOL_ALLOWLIST": "",
        "BRIDGE_MAX_FILE_BYTES": "20971520",
        "BRIDGE_MAX_FILES": "4",
        "BRIDGE_MEDIA_TIMEOUT": "60",
        "BRIDGE_SOURCE_ROOTS": "/AstrBot/data/temp",
        "BRIDGE_NAPCAT_HOSTS": "napcat",
        "QWENPAW_AUTH_ENABLED": "false",
        "ASTRBOT_MEMORY": "640m",
        "ASTRBOT_SWAP": "1280m",
        "QWENPAW_MEMORY": "1536m",
        "QWENPAW_SWAP": "3072m",
        "NAPCAT_MEMORY": "768m",
        "NAPCAT_SWAP": "1536m",
        "EXISTING_ASTRBOT_CONTAINER": "astrbot-weixin",
        "EXISTING_ASTRBOT_PROJECT": "astrbot-weixin",
    }
    for key, value in defaults.items():
        env.setdefault(key, value)
    validate_media_limits(env)
    env["INSTALL_ROOT"] = root.as_posix()
    env["PROJECT_SOURCE"] = PROJECT_SOURCE.as_posix()
    if existing_dir is not None:
        env["EXISTING_ASTRBOT_DIR"] = existing_dir.as_posix()
    encoded_env = "# Private generated configuration. Do not commit or paste its contents.\n"
    encoded_env += "\n".join(f"{key}={env_value(value)}" for key, value in env.items()) + "\n"
    private_dir(root)
    for suffix in (
        "state/bridge-files", "state/qwenpaw/working", "state/qwenpaw/secret",
        "state/qwenpaw/backups", "state/napcat/config", "state/napcat/qq",
    ):
        private_dir(root / suffix)
    if mode == "fresh":
        private_dir(root / "state/astrbot")
    private_write(env_path, encoded_env, replace=True)
    # NapCat's official entrypoint copies bundled config defaults when napcat.json
    # is absent. Supply its documented logging settings first so that bootstrap
    # does not accidentally overwrite the generated OneBot default below.
    private_write(
        root / "state/napcat/config/napcat.json",
        json.dumps({
            "fileLog": True, "consoleLog": True,
            "fileLogLevel": "info", "consoleLogLevel": "info",
        }, indent=2) + "\n",
    )
    network_config = {
        "network": {
            "httpServers": [], "httpSseServers": [], "httpClients": [],
            "websocketServers": [],
            "websocketClients": [{
                "enable": True, "name": "astrbot-bridge",
                "url": "ws://astrbot:6199/ws", "messagePostFormat": "array",
                "reportSelfMessage": False, "token": env["ONEBOT_TOKEN"],
                "debug": False, "heartInterval": 30000, "reconnectInterval": 5000,
            }],
            "plugins": [],
        },
        "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
    }
    # A user's existing account-specific or default NapCat config is never reset.
    private_write(
        root / "state/napcat/config/onebot11.json",
        json.dumps(network_config, ensure_ascii=False, indent=2) + "\n",
    )
    private_write(marker, json.dumps({"format": 1, "mode": mode}, indent=2) + "\n")
    return env


def prepare(mode: str, root: Path, existing_dir: Path | None = None) -> dict[str, str]:
    reject_symlinks(root.absolute())
    root = Path(os.path.abspath(root))
    if root == Path(root.anchor):
        raise ValueError("Choose a dedicated installation directory, not the filesystem root.")
    if mode == "fresh" and any(
        (root / name).exists() for name in ("data", "compose.yaml", "deployment-receipt.txt")
    ):
        raise ValueError("An existing AstrBot installation was found. Use upgrade_existing.sh.")
    if mode == "existing":
        if existing_dir is None:
            raise ValueError("Supply the existing AstrBot installation directory.")
        if root == Path(os.path.abspath(existing_dir)):
            raise ValueError("The add-on must have its own subdirectory or separate root.")
    private_dir(root)
    lock = root / ".combined-setup.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Another preparation is running or left a setup lock. Review before retrying.") from None
    try:
        os.close(descriptor)
        return _prepare_locked(mode, root, existing_dir)
    finally:
        lock.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fresh", "existing"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--existing-dir", type=Path)
    args = parser.parse_args()
    try:
        prepare(args.mode, args.root, args.existing_dir)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"STOP: {exc}\n")
    print("PREPARED: private configuration and directories are ready.")
    print("No software was installed, no container was started, and no existing service was changed.")
    print("Review docs/deployment.md before manually building or starting the selected Compose mode.")


if __name__ == "__main__":
    main()
