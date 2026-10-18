"""Local host updater: signed filesystem IPC, never a Docker socket in the UI."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from deploy.prepare import parse_env
from .common import FileLock, atomic_json, metadata, read_json, safe_path, sign, verify
from .releases import MAX_ARCHIVE_BYTES, ReleaseError, check_for_update, validate_archive


PHASES = {"idle", "checking", "available", "updating", "restarting", "completed", "failed", "rolled_back"}
ACTIVE_PHASES = {"checking", "updating", "restarting"}
REQUEST_AGE = 120
CANDIDATE_AGE = 3600
HEARTBEAT_INTERVAL = 3
READY_TIMEOUT = 840
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
CANDIDATE_RE = re.compile(r"[0-9a-f]{32}\Z")
ERROR_CODES = {
    "release_manifest_required", "local_source_modified", "untracked_destination_conflict",
    "insufficient_backup_space", "scheduled_jobs_enabled", "manual_recovery_required",
    "invalid_recovery_state", "source_build_unsupported", "expired_candidate", "invalid_candidate",
    "candidate_changed", "recovery_required", "download_failed", "download_timeout", "rate_limited",
    "checksum_mismatch", "checksum_digest_mismatch", "manifest_hash_mismatch", "manifest_size_mismatch",
    "missing_manifest", "missing_release_asset", "unsupported_update_protocol", "invalid_archive",
    "cache_failed", "download_too_large", "incomplete_download", "unsafe_redirect", "unsafe_url",
    "invalid_checksum", "update_failed", "task_check_unavailable", "unmanaged_source_collision",
    "candidate_checksum_changed", "invalid_install_manifest", "helper_refresh_failed", "private_acl_failed",
}


def _safe_error(error):
    code = error if isinstance(error, str) else getattr(error, "code", None)
    return code if isinstance(code, str) and code in ERROR_CODES else "update_failed"


def _error(code, message):
    raise ReleaseError(code, message)


def _protect_private(private: Path):
    """Restrict only this updater directory, never the user's general runtime."""
    if os.name != "nt":
        private.chmod(0o700)
        return
    marker = safe_path(private / "acl-ready.json")
    if marker.exists():
        value = read_json(marker, limit=4096)
        if value == {"format": 1, "restricted": True}:
            return
    options = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
               "encoding": "utf-8", "errors": "replace", "timeout": 20, "shell": False,
               "creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], **options)
        rows = list(csv.reader(identity.stdout.splitlines())) if len(identity.stdout) <= 4096 else []
        sid = rows[0][1] if identity.returncode == 0 and len(rows) == 1 and len(rows[0]) == 2 else ""
        if not re.fullmatch(r"S-1-[0-9]+(?:-[0-9]+){1,14}", sid):
            raise ValueError("Invalid account identifier.")
        # Reset removes pre-existing explicit grants on this directory. The
        # following command replaces inheritance with three explicit trustees.
        reset = subprocess.run(["icacls", str(private), "/reset"], **options)
        grant = subprocess.run(["icacls", str(private), "/inheritance:r", "/grant:r",
                                "*" + sid + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                                "*S-1-5-32-544:(OI)(CI)F"], **options) if reset.returncode == 0 else None
        if grant is None or grant.returncode != 0:
            raise ValueError("Unable to restrict update storage.")
        atomic_json(marker, {"format": 1, "restricted": True})
    except (OSError, ValueError, subprocess.SubprocessError):
        _error("private_acl_failed", "The updater could not protect its private backup directory.")


_HOST_ENTRY = '''from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundle_updater.daemon import main
raise SystemExit(main())
'''

_HOST_BOOT = '''"""Fixed local updater boot protocol 1; never execute an unverified helper."""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys

sys.dont_write_bytecode = True

def safe(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked helper path")
    return path

def boot():
    private = safe(Path(__file__).absolute().parent)
    arguments = sys.argv[1:]
    indices = [i for i, item in enumerate(arguments) if item == "--root"]
    if len(indices) != 1 or indices[0] + 1 >= len(arguments):
        raise ValueError("Missing root")
    if safe(Path(arguments[indices[0] + 1])) != private.parents[2]:
        raise ValueError("Invalid root")
    pointer = safe(private / "current-host.json")
    if not pointer.is_file() or pointer.stat().st_nlink != 1 or pointer.stat().st_size > 65536:
        raise ValueError("Invalid helper pointer")
    value = json.loads(pointer.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"format", "directory", "files"} or value["format"] != 1:
        raise ValueError("Invalid helper pointer")
    directory, files = value["directory"], value["files"]
    if not isinstance(directory, str) or not re.fullmatch(r"host-[0-9a-f]{64}", directory):
        raise ValueError("Invalid helper directory")
    if not isinstance(files, dict) or not 7 <= len(files) <= 32:
        raise ValueError("Invalid helper manifest")
    required = {"entry.py", "deploy/__init__.py", "deploy/prepare.py", "bundle_updater/__init__.py",
                "bundle_updater/daemon.py", "bundle_updater/common.py", "bundle_updater/releases.py", "bundle_updater/controller.py"}
    if not required <= set(files):
        raise ValueError("Incomplete helper")
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if directory != "host-" + fingerprint:
        raise ValueError("Invalid helper fingerprint")
    snapshot = safe(private / directory)
    for name, expected in files.items():
        if (not isinstance(name, str) or not re.fullmatch(r"(?:bundle_updater/[A-Za-z_][A-Za-z0-9_]*|deploy/(?:__init__|prepare)|entry)\\.py", name)
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("Invalid helper file")
        path = safe(snapshot / name)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 5 * 1024 * 1024:
            raise ValueError("Invalid helper file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Changed helper file")
    sys.path.insert(0, str(snapshot))
    runpy.run_path(str(snapshot / "entry.py"), run_name="__main__")

if __name__ == "__main__":
    try:
        boot()
    except Exception:
        print("The private update helper could not be verified. Preserve update backups and request recovery assistance.", file=sys.stderr)
        raise SystemExit(1)
'''


def _snapshot_host(root: Path, private: Path):
    """Publish a complete immutable host helper, then switch one private pointer."""
    if _pending_recovery(private):
        _error("helper_refresh_failed", "The recovery helper cannot change during an unfinished transaction.")
    files = {"entry.py": _HOST_ENTRY.encode(), "deploy/__init__.py": b""}
    source_package = safe_path(root / "bundle_updater")
    for source in sorted(source_package.glob("*.py")):
        safe_path(source)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", source.name) or not source.is_file() or source.stat().st_nlink != 1 or source.stat().st_size > 5 * 1024 * 1024:
            _error("helper_refresh_failed", "The host helper source could not be snapshotted safely.")
        files["bundle_updater/" + source.name] = source.read_bytes()
    prepare = safe_path(root / "deploy" / "prepare.py")
    if not prepare.is_file() or prepare.stat().st_nlink != 1 or prepare.stat().st_size > 5 * 1024 * 1024:
        _error("helper_refresh_failed", "The host helper deployment support is missing.")
    files["deploy/prepare.py"] = prepare.read_bytes()
    required = {"bundle_updater/__init__.py", "bundle_updater/daemon.py", "bundle_updater/common.py",
                "bundle_updater/releases.py", "bundle_updater/controller.py"}
    if not required <= set(files) or not 7 <= len(files) <= 32:
        _error("helper_refresh_failed", "The host helper source is incomplete.")
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot = safe_path(private / ("host-" + fingerprint))
    if not snapshot.exists():
        temporary = Path(tempfile.mkdtemp(prefix="host-build-", dir=private))
        written = []
        created_dirs = []
        try:
            for name, content in files.items():
                target = temporary / name
                if not target.parent.exists():
                    target.parent.mkdir()
                    created_dirs.append(target.parent)
                with target.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                target.chmod(0o600)
                written.append(target)
            os.replace(temporary, snapshot)
        finally:
            if temporary.exists():
                for target in reversed(written):
                    safe_path(target).unlink(missing_ok=True)
                for directory in reversed(created_dirs):
                    safe_path(directory).rmdir()
                safe_path(temporary).rmdir()
    for name, expected in hashes.items():
        target = safe_path(snapshot / name)
        if not target.is_file() or target.stat().st_nlink != 1 or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            _error("helper_refresh_failed", "The private host helper snapshot is incomplete or changed.")
    atomic_json(private / "current-host.json", {"format": 1, "directory": snapshot.name, "files": hashes})
    boot = safe_path(private / "host-entry.py")
    if boot.exists() and (not boot.is_file() or boot.stat().st_nlink != 1):
        _error("helper_refresh_failed", "The fixed recovery entry is not a regular private file.")
    if not boot.exists():
        descriptor, name = tempfile.mkstemp(prefix=".host-entry-", dir=private)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_HOST_BOOT.encode())
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            if not boot.exists():
                os.replace(temporary, boot)
        finally:
            temporary.unlink(missing_ok=True)
    return snapshot


def _pending_recovery(private: Path):
    journal = safe_path(private / "transaction.json")
    if not journal.exists():
        return False
    try:
        value = read_json(journal, limit=2 * 1024 * 1024)
        return not isinstance(value, dict) or value.get("phase") not in {"completed", "rolled_back", "aborted"}
    except (ValueError, OSError):
        return True


def _current_metadata(root: Path, private: Path, recovering: bool):
    if recovering:
        try:
            value = read_json(private / "installed-version.json")
            if (isinstance(value, dict) and value.get("format") == 1 and value.get("update_protocol") == 1
                    and isinstance(value.get("version"), str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", value["version"])
                    and isinstance(value.get("components"), dict) and set(value["components"]) == {"astrbot", "qwenpaw", "napcat"}):
                return value
        except (ValueError, OSError):
            pass
    try:
        return metadata(root)
    except (ValueError, OSError):
        if recovering:
            return {"version": "unknown"}
        raise


def _refresh_host_if_reviewed(root: Path, project: str, context: str, private: Path):
    """Repair a post-commit/pre-helper-refresh interruption without trusting edits."""
    if _pending_recovery(private):
        return False
    from .controller import Controller
    try:
        Controller(root, project, context, private, lambda event: None).ensure_baseline()
    except Exception as error:
        if getattr(error, "code", None) in {"release_manifest_required", "local_source_modified"}:
            return False
        _error("helper_refresh_failed", "The installed recovery helper could not be safely refreshed.")
    _snapshot_host(root, private)
    return True


def _local_context(context: str):
    if not isinstance(context, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", context):
        _error("invalid_context", "Select a valid local Docker context.")
    environment = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"):
        environment.pop(key, None)
    try:
        result = subprocess.run(
            ["docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False, env=environment, shell=False,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        endpoint = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= 4096 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        endpoint = None
    local_pipe = isinstance(endpoint, str) and re.fullmatch(r"npipe:/{2,4}\./pipe/[A-Za-z0-9_.-]+", endpoint, re.I)
    local_unix = isinstance(endpoint, str) and endpoint.startswith("unix:///") and not any(
        char in endpoint for char in "\r\n?#%"
    )
    if not local_pipe and not local_unix:
        _error("remote_context", "Only a local Docker pipe or Unix socket is supported.")


def _prepare(root: Path, project: str, context: str, *, context_checker=None):
    root = safe_path(root)
    if not root.is_dir() or root == Path(root.anchor):
        _error("invalid_root", "Choose an existing dedicated bundle source directory.")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", project):
        _error("invalid_project", "The Docker Compose project identifier is invalid.")
    expected_project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(root).lower().encode("utf-8")).hexdigest()[:10]
    if project != expected_project:
        _error("invalid_project", "The updater can only manage this bundle's derived Compose project.")
    runtime = safe_path(root / "runtime")
    update_root = safe_path(runtime / "updates")
    ipc = safe_path(update_root / "ipc")
    private = safe_path(update_root / "private")
    recovering = _pending_recovery(private)
    current = _current_metadata(root, private, recovering)
    if not recovering:
        for relative in ("deploy/compose.bundle.yaml", "deploy/prepare.py", "scripts/run_bundle_updater.py", "bundle_updater/controller.py"):
            target = safe_path(root / relative)
            if not target.is_file():
                _error("incomplete_bundle", "The bundle source is missing required updater files.")
    marker = read_json(runtime / ".combined-setup.json", limit=4096)
    if not isinstance(marker, dict) or type(marker.get("format")) is not int or marker["format"] != 1 or marker.get("mode") != "fresh":
        _error("unsupported_mode", "Automatic updates require an isolated fresh bundle installation.")
    env_path = safe_path(runtime / ".env")
    if not env_path.is_file() or env_path.stat().st_size > 1024 * 1024 or env_path.stat().st_nlink > 1:
        _error("invalid_runtime", "The private runtime configuration is missing or invalid.")
    env = parse_env(env_path)
    if not env.get("PROJECT_SOURCE") or safe_path(Path(env["PROJECT_SOURCE"])) != root:
        _error("invalid_runtime", "The runtime configuration does not belong to this source directory.")
    token = env.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", "")
    if len(token) < 32 or len(token) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        _error("invalid_runtime_token", "The private runtime authentication token is invalid.")
    (context_checker or _local_context)(context)
    for directory, mode in ((update_root, 0o711), (ipc, 0o1777), (private, 0o700)):
        directory.mkdir(parents=True, exist_ok=True, mode=mode)
        safe_path(directory)
        if not directory.is_dir():
            _error("invalid_update_directory", "The local update storage is invalid.")
        directory.chmod(mode)
    _protect_private(private)
    return root, ipc, private, token, current


def _spawn(root: Path, project: str, context: str, build_qwenpaw: bool, nonce: str):
    stable = safe_path(root / "runtime" / "updates" / "private" / "host-entry.py")
    entry = stable if stable.is_file() else root / "scripts" / "run_bundle_updater.py"
    command = [sys.executable, str(entry), "--serve",
               "--root", str(root), "--project", project, "--context", context, "--instance-id", nonce]
    if build_qwenpaw:
        command.append("--build-qwenpaw")
    environment = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"):
        environment.pop(key, None)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, shell=False, env=environment, **kwargs)


def _status(ipc: Path, token: str, *, clock=time.time):
    try:
        state = verify(read_json(ipc / "status.json"), token)
        heartbeat = state.get("heartbeat") if isinstance(state, dict) else None
        if (isinstance(heartbeat, bool) or not isinstance(heartbeat, (float, int))
                or not math.isfinite(heartbeat) or not 0 <= clock() - heartbeat <= 20):
            return None
        return state
    except (ValueError, TypeError, OSError):
        return None


def _running(private: Path) -> bool:
    try:
        with FileLock(private / "daemon.lock"):
            return False
    except BlockingIOError:
        return True


def _stop_existing(ipc: Path, private: Path, token: str, *, timeout=30, clock=time.time, sleep=time.sleep):
    if not _running(private):
        return {"running": False, "stopped": True}
    state = _status(ipc, token, clock=clock)
    if state is None or state.get("active") is True:
        _error("updater_busy", "The updater is busy or still recovering. Wait before stopping it.")
    identifier = str(uuid.uuid4())
    atomic_json(private / "stop-request.json", sign({"id": identifier, "action": "stop", "created": clock()}, token))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _running(private):
            return {"running": False, "stopped": True}
        result = None
        try:
            result = verify(read_json(private / "stop-result.json"), token)
        except (OSError, ValueError, TypeError):
            pass
        if isinstance(result, dict) and result.get("id") == identifier and result.get("accepted") is False:
            _error("updater_busy", "The updater started a task and could not stop safely.")
        sleep(0.1)
    _error("stop_timeout", "The updater did not stop in time. No process was terminated.")


def ensure_daemon(root: Path, project: str, context: str, build_qwenpaw: bool = False) -> dict:
    root, ipc, private, token, _ = _prepare(root, project, context)
    mode = {"format": 1, "mode": "fresh", "project": project, "context": context, "build_qwenpaw": bool(build_qwenpaw)}
    with FileLock(private / "ensure.lock"):
        alive = _running(private)
        if alive:
            state = _status(ipc, token)
            if state is None or state.get("active") is True or state.get("recovery_required") is True:
                _error("updater_busy", "The updater is busy or requires recovery. The launcher did not start services.")
            try:
                previous_mode = read_json(private / "mode.json")
            except (OSError, ValueError):
                previous_mode = None
            if previous_mode == mode:
                return {"running": True, "ready": True}
            _stop_existing(ipc, private, token)
        atomic_json(private / "mode.json", mode)
        if not (private / "host-entry.py").exists():
            if _pending_recovery(private):
                _error("manual_recovery_required", "Interrupted updates need the existing private recovery helper.")
            _snapshot_host(root, private)
        elif not _pending_recovery(private):
            _refresh_host_if_reviewed(root, project, context, private)
        nonce = secrets.token_hex(16)
        _spawn(root, project, context, build_qwenpaw, nonce)
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            state = _status(ipc, token)
            if state is not None and state.get("daemon_nonce") == nonce and _running(private):
                if state.get("active") is not True:
                    if state.get("recovery_required") is True or state.get("startup_failed") is True:
                        _error("recovery_required", "Update recovery did not finish. The launcher did not start services.")
                    return {"running": True, "ready": True}
            time.sleep(0.1)
        _error("updater_start_timeout", "The updater did not become ready. Check its private diagnostic record.")


def stop_daemon(root: Path, project: str, context: str) -> dict:
    _, ipc, private, token, _ = _prepare(root, project, context)
    with FileLock(private / "ensure.lock"):
        return _stop_existing(ipc, private, token)


class UpdaterDaemon:
    def __init__(self, root: Path, project: str, context: str, *, build_qwenpaw=False,
                 controller_factory=None, release_checker=None, clock=time.time,
                 context_checker=None, daemon_nonce=None):
        self.root, self.ipc, self.private, self.token, current = _prepare(root, project, context, context_checker=context_checker)
        self.project, self.context = project, context
        self.build_qwenpaw = bool(build_qwenpaw)
        self.clock = clock
        self.controller_factory = controller_factory
        self.release_checker = release_checker or check_for_update
        self.controller = None
        self.worker = None
        self.ready = False
        self.exiting = False
        self.restart = False
        self.applying_controller = False
        self.guard = threading.RLock()
        self.daemon_nonce = daemon_nonce or secrets.token_hex(16)
        self.state = {"current_version": current["version"], "phase": "idle", "active": False,
                      "checked": False, "recovery_required": False, "update_capable": True,
                      "daemon_nonce": self.daemon_nonce, "error": None}
        self.recent = {}
        try:
            previous = verify(read_json(self.ipc / "status.json"), self.token)
            if isinstance(previous, dict) and previous.get("phase") in {"completed", "rolled_back", "failed"}:
                self.state.update(phase=previous["phase"], recovery_required=previous.get("recovery_required") is True)
        except (ValueError, TypeError, OSError):
            pass
        try:
            recent = read_json(self.private / "recent-requests.json", limit=256 * 1024)
            if isinstance(recent, dict):
                self.recent = {key: value for key, value in recent.items() if isinstance(key, str) and UUID_RE.fullmatch(key)
                               and type(value) in (int, float) and math.isfinite(value) and self.clock() - value < 600}
        except (ValueError, TypeError, OSError):
            pass
        self.candidate = None
        try:
            stored = read_json(self.private / "candidate.json", limit=5 * 1024 * 1024)
            if self._valid_candidate_record(stored):
                self.candidate = stored
        except (ValueError, TypeError, OSError):
            pass

    def _log(self, code, error=None):
        try:
            target = safe_path(self.private / "updater.log")
            if target.exists() and (not target.is_file() or target.stat().st_nlink > 1 or target.stat().st_size > 2 * 1024 * 1024):
                return
            record = {"time": self.clock(), "code": code, "type": type(error).__name__ if error else None}
            with target.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, sort_keys=True) + "\n")
            target.chmod(0o600)
        except (ValueError, OSError):
            pass

    def _publish(self, **changes):
        with self.guard:
            self.state.update(changes)
            self.state["heartbeat"] = self.clock()
            atomic_json(self.ipc / "status.json", sign(dict(self.state), self.token), mode=0o644)

    def _report(self, event, *args, **kwargs):
        step = event.get("phase", event.get("step", "")) if isinstance(event, dict) else str(event)
        phase = "restarting" if step in {"restarting", "restart", "starting", "health", "health_check", "verifying_health"} else "updating"
        known = {"preflight", "preparing", "staged", "pull", "downloading_images", "building_gateway", "backup",
                 "backing_up", "backed_up", "stop", "stopping", "stopped", "install", "installing", "start",
                 "starting", "restart", "restarting", "checking", "health", "health_check", "verifying_health",
                 "rollback", "rolling_back", "rollback_failed", "recover", "restore", "snapshot", "verify", "extract"}
        self._publish(phase=phase, active=True, step=step if step in known else "updating")

    def _make_controller(self):
        factory = self.controller_factory
        if factory is None:
            from .controller import Controller
            factory = Controller
        return factory(self.root, self.project, self.context, self.private, self._report)

    def initialize(self, *, background=False):
        def recover():
            previous_phase = self.state["phase"]
            self._publish(active=True, phase="updating")
            try:
                self.controller = self._make_controller()
                result = self.controller.recover()
                baseline_error = None
                if result is None or result.get("recovery_required") is not True:
                    enroll = getattr(self.controller, "ensure_baseline", None)
                    if enroll is not None:
                        try:
                            enroll()
                        except Exception as error:
                            if getattr(error, "code", None) not in {"release_manifest_required", "local_source_modified"}:
                                raise
                            baseline_error = _safe_error(error)
                if result is None:
                    current = metadata(self.root)
                    atomic_json(self.private / "installed-version.json", current)
                    self.state["current_version"] = current["version"]
                    self._publish(active=False, phase=previous_phase, recovery_required=False)
                else:
                    self._result(result)
                if baseline_error:
                    self._publish(active=False, update_capable=False, error=baseline_error,
                                  phase="idle" if previous_phase == "idle" else self.state["phase"])
                self.ready = True
            except Exception as error:
                self._log("startup_recovery_failed", error)
                self._publish(active=False, phase="failed", recovery_required=True, startup_failed=True,
                              update_capable=False, error="manual_recovery_required")
                self.ready = True
        if background:
            self.worker = threading.Thread(target=recover, name="bundle-recovery", daemon=True)
            self.worker.start()
        else:
            recover()

    def _valid_candidate_record(self, stored):
        if not isinstance(stored, dict) or set(stored) != {"id", "checked_at", "candidate"}:
            return False
        identifier, stamp, candidate = stored["id"], stored["checked_at"], stored["candidate"]
        if (not isinstance(identifier, str) or not CANDIDATE_RE.fullmatch(identifier)
                or type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not 0 <= self.clock() - stamp <= CANDIDATE_AGE or not isinstance(candidate, dict)):
            return False
        path = candidate.get("archive_path")
        if not isinstance(path, str):
            return False
        try:
            safe_path(Path(path)).relative_to(safe_path(self.private / "cache"))
        except (ValueError, OSError):
            return False
        return True

    def _public_candidate(self, stored):
        candidate = stored["candidate"]
        return {"id": stored["id"], "version": candidate["version"], "components": candidate["components"],
                "notes": str(candidate.get("notes", ""))[:8000],
                "html_url": "https://github.com/misaki-mei-yue/astrbot-qwenpaw/releases/tag/v" + candidate["version"]}

    def _check(self):
        result = self.release_checker(self.state["current_version"], self.private / "cache")
        if result is None:
            self.candidate = None
            safe_path(self.private / "candidate.json").unlink(missing_ok=True)
            self._publish(phase="idle", active=False, checked=True, candidate=None)
            return
        stored = {"id": secrets.token_hex(16), "checked_at": self.clock(), "candidate": result}
        if not self._valid_candidate_record(stored):
            _error("invalid_candidate", "The release checker returned an invalid candidate.")
        # The host-only private record includes source location and checksums;
        # no such value is copied into the signed public status document.
        atomic_json(self.private / "candidate.json", stored)
        self.candidate = stored
        self._publish(phase="available", active=False, checked=True, candidate=self._public_candidate(stored))

    def _result(self, result):
        if not isinstance(result, dict) or result.get("state") not in {"completed", "rolled_back", "failed"}:
            _error("invalid_controller_result", "The update controller returned an invalid result.")
        changes = {"phase": result["state"], "active": False,
                   "recovery_required": result.get("recovery_required") is True,
                   "error": _safe_error(result.get("error")) if result.get("error") else None}
        try:
            if changes["recovery_required"]:
                changes["current_version"] = self.state["current_version"]
            else:
                current = metadata(self.root)
                atomic_json(self.private / "installed-version.json", current)
                changes["current_version"] = current["version"]
        except (ValueError, OSError):
            changes["recovery_required"] = True
            changes["phase"] = "failed"
        self._publish(**changes)

    def _apply(self, identifier):
        if self.state.get("recovery_required"):
            _error("recovery_required", "The previous update must be recovered first.")
        if self.state.get("update_capable") is not True:
            _error(self.state.get("error") or "update_failed", "This source installation cannot be updated automatically.")
        if not self._valid_candidate_record(self.candidate) or identifier != self.candidate["id"]:
            _error("expired_candidate", "The update candidate expired. Check for updates again.")
        candidate = self.candidate["candidate"]
        archive = safe_path(Path(candidate["archive_path"]))
        expected = candidate.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            _error("invalid_candidate", "The private update candidate has an invalid checksum.")
        if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
            _error("candidate_changed", "The downloaded update changed. Check for updates again.")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            _error("candidate_changed", "The downloaded update changed. Check for updates again.")
        validated = validate_archive(archive, candidate["version"])
        if validated["components"] != candidate.get("components"):
            _error("candidate_changed", "The downloaded update metadata changed.")
        self.applying_controller = True
        result = self.controller.apply({**candidate, "archive_sha256": expected})
        self._result(result)
        if result.get("state") == "completed" and not self.state.get("recovery_required"):
            self.candidate = None
            safe_path(self.private / "candidate.json").unlink(missing_ok=True)
            self._publish(candidate=None)
            try:
                _snapshot_host(self.root, self.private)
                self.restart = True
            except Exception as error:
                self._log("helper_refresh_failed", error)
                self._publish(update_capable=False, error="helper_refresh_failed")
        self.applying_controller = False

    def _job(self, request):
        try:
            if request["action"] == "check":
                self._check()
            else:
                self._apply(request["candidate_id"])
        except Exception as error:
            self._log(error.code if isinstance(error, ReleaseError) else "update_job_failed", error)
            self._publish(phase="failed", active=False,
                          recovery_required=self.state.get("recovery_required") is True or self.applying_controller,
                          error=_safe_error(error))
            self.applying_controller = False

    def _valid_request(self, payload, identifier, *, stop=False):
        allowed = {"id", "action", "created"}
        if isinstance(payload, dict) and payload.get("action") == "apply" and not stop:
            allowed.add("candidate_id")
        if not isinstance(payload, dict) or set(payload) != allowed or payload.get("id") != identifier:
            return False
        if not UUID_RE.fullmatch(identifier):
            return False
        stamp = payload["created"]
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not -5 <= self.clock() - stamp <= REQUEST_AGE:
            return False
        if payload["action"] not in ({"stop"} if stop else {"check", "apply"}):
            return False
        return payload["action"] != "apply" or isinstance(payload.get("candidate_id"), str) and CANDIDATE_RE.fullmatch(payload["candidate_id"]) is not None

    def _consume_stop(self):
        path = self.private / "stop-request.json"
        if not path.exists():
            return
        try:
            payload = verify(read_json(path, limit=4096), self.token)
            identifier = payload.get("id", "") if isinstance(payload, dict) else ""
            if not self._valid_request(payload, identifier, stop=True):
                raise ValueError("Invalid stop request.")
            accepted = self.ready and self.state.get("active") is not True and not (self.worker and self.worker.is_alive())
            atomic_json(self.private / "stop-result.json", sign({"id": identifier, "accepted": accepted}, self.token))
            self.exiting = accepted
        except (ValueError, TypeError, OSError) as error:
            self._log("invalid_stop_request", error)
        finally:
            try:
                safe_path(path).unlink(missing_ok=True)
            except (ValueError, OSError):
                pass

    def run_once(self):
        self._consume_stop()
        if self.exiting:
            return
        for path in sorted(self.ipc.glob("request-*.json"))[:64]:
            if self.restart or self.exiting:
                break
            match = re.fullmatch(r"request-([0-9a-f-]{36})\.json", path.name)
            if not match:
                continue
            try:
                payload = verify(read_json(path, limit=4096), self.token)
                identifier = match.group(1)
                if not self._valid_request(payload, identifier) or identifier in self.recent:
                    raise ValueError("Invalid or replayed update request.")
                self.recent[identifier] = self.clock()
                self.recent = dict(sorted(((key, stamp) for key, stamp in self.recent.items() if self.clock() - stamp < 600),
                                          key=lambda item: item[1])[-2048:])
                atomic_json(self.private / "recent-requests.json", self.recent)
                if not self.ready or self.state.get("active") is True or self.worker and self.worker.is_alive():
                    self._log("busy_request_rejected")
                    continue
                if payload["action"] == "apply" and self.state.get("recovery_required"):
                    self._log("recovery_required")
                    continue
                self._publish(request_id=identifier, active=True, checked=False,
                              phase="checking" if payload["action"] == "check" else "updating",
                              error=None if self.state.get("update_capable") else self.state.get("error"))
                self.worker = threading.Thread(target=self._job, args=(payload,), name="bundle-update", daemon=True)
                self.worker.start()
            except (ValueError, TypeError, OSError) as error:
                self._log("invalid_request", error)
            finally:
                try:
                    safe_path(path).unlink(missing_ok=True)
                except (ValueError, OSError):
                    pass

    def serve(self):
        with FileLock(self.private / "daemon.lock"):
            self.initialize(background=True)
            next_heartbeat = 0.0
            while not self.exiting and not self.restart:
                self.run_once()
                if time.monotonic() >= next_heartbeat:
                    self._publish()
                    next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
                time.sleep(0.2)
            if self.worker is not None:
                self.worker.join()
            with self.guard:
                self.state["heartbeat"] = 0
                atomic_json(self.ipc / "status.json", sign(self.state, self.token), mode=0o644)
        if self.restart:
            # The process lifetime lock has been released. The successor imports
            # newly installed code and preserves the completed status on startup.
            _spawn(self.root, self.project, self.context, self.build_qwenpaw, secrets.token_hex(16))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--context", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ensure", action="store_true")
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--stop", action="store_true")
    parser.add_argument("--build-qwenpaw", action="store_true")
    parser.add_argument("--instance-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.instance_id is not None and not CANDIDATE_RE.fullmatch(args.instance_id):
            _error("invalid_instance", "The updater instance identifier is invalid.")
        if args.ensure:
            ensure_daemon(args.root, args.project, args.context, args.build_qwenpaw)
        elif args.stop:
            stop_daemon(args.root, args.project, args.context)
        else:
            UpdaterDaemon(args.root, args.project, args.context, build_qwenpaw=args.build_qwenpaw,
                          daemon_nonce=args.instance_id).serve()
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, ReleaseError) else "The local updater could not complete this operation safely."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
