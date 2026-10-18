"""Host-only, recoverable bundle updates. Never mount this module into the gateway.

The optional runner is a subprocess.run-compatible callable accepting args, cwd,
env, input and timeout, returning returncode/stdout/stderr. No downloaded Python
module is imported. A failed rollback blocks further updates for manual recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
from typing import Callable
import urllib.request
import uuid

from .releases import extract_verified_archive, validate_archive


SERVICES = ("astrbot", "qwenpaw", "napcat", "gateway")
CORE = SERVICES[:3]
TERMINAL = {"completed", "rolled_back", "aborted"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
MANIFEST = "RELEASE-MANIFEST.json"
MAX_JSON = 2 * 1024 * 1024

# Executed by the existing QwenPaw container Python. Read-only API requests only:
# no prompts, task creation, scheduler mutation, messages or model invocation.
TASK_GUARD = r'''import json,os,re,time,urllib.request,urllib.parse
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k): return None
def inspect_jobs():
    token=os.environ.get('QWENPAW_RUNTIME_INTERNAL_TOKEN','')
    if len(token)<32: return {'allowed':False,'reason':'task_check_unavailable'}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    deadline=time.monotonic()+30
    def get(path):
        remaining=deadline-time.monotonic()
        if remaining<=0: raise ValueError()
        request=urllib.request.Request('http://127.0.0.1:8088'+path,headers={'X-QwenPaw-Runtime-Token':token})
        with opener.open(request,timeout=min(8,remaining)) as response:
            raw=response.read(2097153)
        if len(raw)>2097152: raise ValueError()
        return json.loads(raw)
    health=get('/api/healthz')
    agents=health.get('agents_loaded')
    if health.get('status')!='ok' or not isinstance(agents,list) or not 1<=len(agents)<=128: raise ValueError()
    if any(not isinstance(a,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',a) for a in agents): raise ValueError()
    if len(set(agents))!=len(agents): raise ValueError()
    for agent in agents:
        jobs=get('/api/agents/'+urllib.parse.quote(agent,safe='')+'/cron/jobs')
        if not isinstance(jobs,list) or len(jobs)>1000: raise ValueError()
        for job in jobs:
            if not isinstance(job,dict) or type(job.get('enabled')) is not bool: raise ValueError()
            if job['enabled']: return {'allowed':False,'reason':'scheduled_jobs_enabled'}
    return {'allowed':True}
try: result=inspect_jobs()
except Exception: result={'allowed':False,'reason':'task_check_unavailable'}
print(json.dumps(result,separators=(',',':')))
'''


class UpdateError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _linked(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe(path: Path, *, file=False):
    """Reject all link/reparse ancestors, nonregular leaves and hardlinked files."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise UpdateError("unsafe_path")
        if part == path and file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise UpdateError("unsafe_file")


def _relative(value: str) -> Path:
    if not isinstance(value, str) or "\\" in value or ":" in value or "\0" in value:
        raise UpdateError("invalid_managed_path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in {"", ".", ".."} for p in value.split("/")):
        raise UpdateError("invalid_managed_path")
    if path.parts[0].casefold() in {"runtime", ".git"}:
        raise UpdateError("invalid_managed_path")
    return Path(*path.parts)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_json(path: Path):
    _safe(path, file=True)
    if path.stat().st_size > MAX_JSON:
        raise UpdateError("invalid_private_state")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise UpdateError("invalid_private_state") from None
    return value


def _atomic(path: Path, data: bytes, *, mode=0o600):
    _safe(path, file=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path.parent)
    temp = path.with_name(path.name + ".update-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(mode)
        _safe(path, file=True)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _write_json(path: Path, value):
    _atomic(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _digest(path: Path) -> str:
    _safe(path, file=True)
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def _tree_files(root: Path):
    _safe(root)
    if not root.is_dir():
        raise UpdateError("missing_state_directory")
    for item in sorted(root.iterdir()):
        _safe(item)
        info = item.lstat()
        if stat.S_ISDIR(info.st_mode):
            yield from _tree_files(item)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            yield item
        else:
            raise UpdateError("unsafe_backup_entry")


def _copy_file(source: Path, target: Path):
    _safe(source, file=True)
    _safe(target, file=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".update-" + uuid.uuid4().hex)
    try:
        with source.open("rb") as origin, temp.open("xb") as destination:
            while block := origin.read(1024 * 1024):
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        temp.chmod(stat.S_IMODE(source.stat().st_mode))
        _safe(target, file=True)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _copy_tree(source: Path, target: Path):
    """Source must be quiesced. Every leaf is checked again during copying."""
    target.mkdir(parents=True, exist_ok=False)
    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        _safe(current)
        destination = target / current.relative_to(source)
        destination.mkdir(parents=True, exist_ok=True)
        for name in names:
            _safe(current / name)
            (destination / name).mkdir(exist_ok=True)
        for name in files:
            origin = current / name
            _safe(origin, file=True)
            _copy_file(origin, destination / name)


def _purge(path: Path):
    """Remove a checked subtree without following candidate-created symlinks."""
    _safe(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if _linked(info):
        if stat.S_ISDIR(info.st_mode):
            path.rmdir()  # Windows junction: remove the junction, never its target.
        else:
            path.unlink()
    elif stat.S_ISDIR(info.st_mode):
        for entry in path.iterdir():
            _purge(entry)
        path.rmdir()
    elif stat.S_ISREG(info.st_mode):
        path.unlink()
    else:
        raise UpdateError("unsafe_restore_entry")


class Controller:
    def __init__(self, root: Path, project: str, context: str, private_dir: Path,
                 report: Callable[[dict], None], runner=None):
        self.root = Path(os.path.abspath(root))
        self.private = Path(os.path.abspath(private_dir))
        if not NAME.fullmatch(project) or not NAME.fullmatch(context):
            raise UpdateError("invalid_docker_scope")
        expected = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(self.root).lower().encode()).hexdigest()[:10]
        if project != expected:
            raise UpdateError("project_scope_mismatch")
        if not _within(self.private, self.root / "runtime" / "updates" / "private"):
            raise UpdateError("invalid_private_directory")
        _safe(self.root)
        _safe(self.private)
        if self.root == Path(self.root.anchor) or not self.root.is_dir():
            raise UpdateError("invalid_bundle_directory")
        self.runtime = self.root / "runtime"
        self.project, self.context = project, context
        self.report = report
        self.runner = runner
        self.docker = shutil.which("docker.exe" if os.name == "nt" else "docker") or "docker"
        self.private.mkdir(parents=True, exist_ok=True)
        self.private.chmod(0o700)
        self.journal_path = self.private / "transaction.json"
        self.baseline_path = self.private / "installed-files.json"
        self.journal = None

    def _emit(self, phase: str, **extra):
        # Caller callbacks cannot derail an in-progress recovery transaction.
        payload = {"phase": phase, **extra}
        if self.journal:
            payload.update(transaction_id=self.journal["transaction_id"], version=self.journal["version"])
        try:
            self.report(payload)
        except Exception:
            pass

    def _checkpoint(self, phase: str, **extra):
        self.journal.update(phase=phase, **extra)
        _write_json(self.journal_path, self.journal)
        self._emit(phase)

    def _load_journal(self):
        if not self.journal_path.exists():
            return None
        data = _read_json(self.journal_path)
        if not isinstance(data, dict) or data.get("format") != 1:
            raise UpdateError("invalid_journal")
        if not re.fullmatch(r"[0-9a-f]{32}", str(data.get("transaction_id", ""))):
            raise UpdateError("invalid_journal")
        if data.get("project") != self.project or data.get("context") != self.context:
            raise UpdateError("journal_scope_mismatch")
        if data.get("phase") not in TERMINAL | {"preparing", "staged", "stopping", "stopped", "backing_up", "backed_up", "installing", "starting", "checking", "rolling_back", "rollback_failed"}:
            raise UpdateError("invalid_journal")
        self.journal = data
        return data

    def _baseline(self):
        if self.baseline_path.exists():
            value = _read_json(self.baseline_path)
            if not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("files"), dict):
                raise UpdateError("invalid_baseline")
            for name, digest in value["files"].items():
                _relative(name)
                if not isinstance(digest, str) or not SHA.fullmatch(digest):
                    raise UpdateError("invalid_baseline")
            return value
        # A distributed manifest defines ownership, not all files found in a
        # directory. Private/custom scripts may coexist with a released install.
        manifest_path = self.root / MANIFEST
        if manifest_path.exists():
            manifest = _read_json(manifest_path)
            if not isinstance(manifest, dict) or manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
                raise UpdateError("invalid_install_manifest")
            files = {}
            for row in manifest["files"]:
                if not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}:
                    raise UpdateError("invalid_install_manifest")
                name, digest = row["path"], row["sha256"]
                relative = _relative(name)
                if name == MANIFEST or name in files or not isinstance(digest, str) or not SHA.fullmatch(digest) or type(row["size"]) is not int:
                    raise UpdateError("invalid_install_manifest")
                path = self.root / relative
                if not path.is_file() or path.stat().st_size != row["size"] or _digest(path) != digest:
                    raise UpdateError("local_source_modified")
                files[name] = digest
            if not {"README.md", "LICENSE"} <= set(files) or len(files) > 2000:
                raise UpdateError("invalid_install_manifest")
            files[MANIFEST] = _digest(manifest_path)
        else:
            # A source checkout can contain private/untracked scripts. Scanning
            # it cannot establish which files this installer owns or may delete.
            raise UpdateError("release_manifest_required")
        value = {"format": 1, "files": files}
        _write_json(self.baseline_path, value)
        return value

    def ensure_baseline(self):
        """Enroll current public files once; reject modification on subsequent uses."""
        data = self._baseline()
        for name, expected in data["files"].items():
            path = self.root / _relative(name)
            if not path.is_file() or _digest(path) != expected:
                raise UpdateError("local_source_modified")
        return data

    def _env(self, source: Path):
        values = dict(os.environ)
        for key in list(values):
            if key.startswith(("DOCKER_", "COMPOSE_")):
                del values[key]
        values.update(PROJECT_SOURCE=source.as_posix(), INSTALL_ROOT=self.runtime.as_posix())
        return values

    def _run(self, arguments, *, phase="docker", source=None, input=None, timeout=180):
        source = source or self.root
        args = [self.docker, "--context", self.context, *arguments]
        try:
            kwargs = {"cwd": self.root, "env": self._env(source), "input": input, "timeout": timeout}
            if self.runner is None:
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
            else:
                result = self.runner(args, **kwargs)
            get = result.get if isinstance(result, dict) else lambda key, default=None: getattr(result, key, default)
            output, error = get("stdout", b""), get("stderr", b"")
            output = output.encode() if isinstance(output, str) else output
            error = error.encode() if isinstance(error, str) else error
            # Diagnostics remain under private, which is never mounted or served.
            logfile = self.private / ("docker-" + uuid.uuid4().hex + ".log")
            _atomic(logfile, (output or b"") + b"\n" + (error or b""))
            if get("returncode", 1) != 0:
                raise UpdateError(phase + "_failed")
            if phase in {"context", "engine", "containers", "image_snapshot", "stop_verify"} and len(output or b"") > MAX_JSON:
                raise UpdateError("docker_response_too_large")
            return (output or b"")[:MAX_JSON].decode("utf-8", errors="replace").strip()
        except UpdateError:
            raise
        except Exception:
            raise UpdateError(phase + "_failed") from None

    def _compose(self, *args, source=None, override=None, **kwargs):
        source = source or self.root
        arguments = ["compose", "--project-directory", str(self.root), "--project-name", self.project,
                     "--env-file", str(self.runtime / ".env"), "-f", str(source / "deploy" / "compose.bundle.yaml")]
        if override is not None:
            arguments += ["-f", str(override)]
        return self._run([*arguments, *args], source=source, **kwargs)

    def _preflight(self):
        for path in (self.runtime / ".env", self.runtime / ".combined-setup.json", self.root / "deploy" / "compose.bundle.yaml"):
            _safe(path, file=True)
            if not path.is_file():
                raise UpdateError("bundle_not_prepared")
        marker = _read_json(self.runtime / ".combined-setup.json")
        if not isinstance(marker, dict) or marker.get("format") != 1 or marker.get("mode") != "fresh":
            raise UpdateError("unsupported_deployment")
        mode = self.private / "mode.json"
        if not mode.is_file():
            raise UpdateError("deployment_mode_unknown")
        config = _read_json(mode)
        if config.get("format") != 1 or config.get("build_qwenpaw") is not False:
            raise UpdateError("source_build_unsupported")
        endpoint = json.loads(self._run(["context", "inspect", self.context, "--format", "{{json .Endpoints.docker.Host}}"], phase="context"))
        local_pipe = isinstance(endpoint, str) and re.fullmatch(r"npipe:/{2,4}\./pipe/[A-Za-z0-9_.-]+", endpoint, re.I)
        local_unix = isinstance(endpoint, str) and endpoint.startswith("unix:///") and not any(char in endpoint for char in "\r\n?#%")
        if not local_pipe and not local_unix:
            raise UpdateError("remote_engine_forbidden")
        if self._run(["info", "--format", "{{.OSType}}"], phase="engine") != "linux":
            raise UpdateError("linux_engine_required")
        self._compose("config", "--quiet", phase="configuration")

    def _task_gate(self):
        try:
            raw = self._compose("exec", "-T", "qwenpaw", "/app/venv/bin/python", "-",
                                input=TASK_GUARD.encode(), phase="task_check", timeout=40)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            if value == {"allowed": True}:
                return
            if value == {"allowed": False, "reason": "scheduled_jobs_enabled"}:
                raise UpdateError("scheduled_jobs_enabled")
        except UpdateError as error:
            if error.code == "scheduled_jobs_enabled":
                raise
        except Exception:
            pass
        raise UpdateError("task_check_unavailable")

    def _container(self, service, *, required=True):
        # Container lookup and stop must still work after an interrupted partial
        # source installation, without parsing a potentially incomplete Compose.
        values = self._run(["ps", "--all", "--quiet", "--filter", "label=com.docker.compose.project=" + self.project,
                            "--filter", "label=com.docker.compose.service=" + service], phase="containers").split()
        if not values and not required:
            return None
        if len(values) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", values[0]):
            raise UpdateError("container_not_found")
        return values[0]

    def _old_images(self):
        images = {}
        for service in SERVICES:
            container = self._container(service)
            raw = self._run(["inspect", "--format", '{{json .Image}}', container], phase="image_snapshot")
            image = json.loads(raw)
            if not isinstance(image, str) or not IMAGE.fullmatch(image):
                raise UpdateError("invalid_image_snapshot")
            images[service] = image
            tag = "bot-combined-update-backup:" + self.journal["transaction_id"] + "-" + service
            self._run(["image", "tag", image, tag], phase="preserve_image")
        return images

    def _stop_core(self):
        containers = [value for role in CORE if (value := self._container(role, required=False))]
        if containers:
            self._run(["stop", "--time", "45", *containers], phase="stop", timeout=240)
        for service in CORE:
            container = self._container(service, required=False)
            if container and self._run(["inspect", "--format", "{{json .State.Running}}", container], phase="stop_verify") != "false":
                raise UpdateError("services_not_stopped")

    def _backup(self, transaction: Path, baseline):
        backup = transaction / "backup"
        backup.mkdir()
        files = list(_tree_files(self.runtime / "state"))
        required = sum(p.stat().st_size for p in files) * 2 + sum((self.root / _relative(p)).stat().st_size for p in baseline["files"]) * 3 + 64 * 1024 * 1024
        if shutil.disk_usage(self.root).free < required:
            raise UpdateError("insufficient_backup_space")
        _copy_tree(self.runtime / "state", backup / "state")
        state_hashes = {}
        for source in files:
            name = source.relative_to(self.runtime / "state").as_posix()
            original_hash = _digest(source)
            if _digest(backup / "state" / name) != original_hash:
                raise UpdateError("backup_verification_failed")
            state_hashes[name] = original_hash
        _write_json(backup / "state-files.json", state_hashes)
        for name in (".env", ".combined-setup.json"):
            _atomic(backup / name, (self.runtime / name).read_bytes())
        for name, digest in baseline["files"].items():
            source = self.root / _relative(name)
            if _digest(source) != digest:
                raise UpdateError("local_source_modified")
            _copy_file(source, backup / "source" / _relative(name))
        _write_json(backup / "baseline.json", baseline)
        self._checkpoint("backed_up", backup_complete=True)

    def _managed(self, stage: Path, verified):
        result = {row["path"]: row["sha256"] for row in verified["manifest"]["files"]}
        result[MANIFEST] = _digest(stage / MANIFEST)
        for name, expected in result.items():
            if _digest(stage / _relative(name)) != expected:
                raise UpdateError("staging_modified")
        return result

    def _install(self, stage: Path, old, new):
        self._validate_destinations(old, new)
        self._checkpoint("installing", created_files=sorted(set(new) - set(old)))
        for name in sorted(new):
            _copy_file(stage / _relative(name), self.root / _relative(name))
        for name in sorted(set(old) - set(new)):
            destination = self.root / _relative(name)
            _safe(destination, file=True)
            destination.unlink(missing_ok=True)

    def _validate_destinations(self, old, new):
        for name in new:
            destination = self.root / _relative(name)
            _safe(destination, file=True)
            if destination.exists() and name not in old:
                raise UpdateError("unmanaged_source_collision")

    def _wait_ready(self, timeout=600):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with opener.open("http://127.0.0.1:18080/bundle-health", timeout=8) as reply:
                    raw = reply.read(65537)
                    value = json.loads(raw) if len(raw) <= 65536 else {}
                if value.get("status") == "ready" and all(value.get("services", {}).get(role, {}).get("ready") is True for role in CORE):
                    return
            except Exception:
                pass
            time.sleep(2)
        raise UpdateError("health_check_failed")

    def _bridge_check(self):
        script = b'''import json,os,urllib.request
o=urllib.request.build_opener(urllib.request.ProxyHandler({}))
r=urllib.request.Request('http://127.0.0.1:9186/health',headers={'Authorization':'Bearer '+os.environ['BRIDGE_TOKEN']})
with o.open(r,timeout=8) as x: assert json.loads(x.read(65536)).get('ready') is True
'''
        self._compose("exec", "-T", "astrbot", "python", "-", phase="bridge_health", input=script, timeout=30)

    def _override(self, transaction: Path):
        images = self.journal.get("old_images", {})
        if set(images) != set(SERVICES) or any(not isinstance(v, str) or not IMAGE.fullmatch(v) for v in images.values()):
            raise UpdateError("invalid_image_snapshot")
        override = transaction / "rollback-compose.json"
        _write_json(override, {"services": {name: {"image": image, "pull_policy": "never"} for name, image in images.items()}})
        return override

    def _result(self, state, error=None, *, recovery_required=False):
        result = {"state": state, "phase": self.journal["phase"] if self.journal else state,
                  "recovery_required": recovery_required}
        if self.journal:
            result.update(transaction_id=self.journal["transaction_id"], version=self.journal["version"])
        if error:
            result["error"] = error
        return result

    def _rollback(self, original_error):
        transaction = self.private / self.journal["transaction_id"]
        try:
            self._checkpoint("rolling_back", error=original_error)
            self._stop_core()  # Never replace a database while a candidate can write it.
            if self.journal.get("backup_complete"):
                backup = transaction / "backup"
                baseline = _read_json(backup / "baseline.json")
                state_hashes = _read_json(backup / "state-files.json")
                backup_files = list(_tree_files(backup / "state"))
                if not isinstance(state_hashes, dict) or {p.relative_to(backup / "state").as_posix() for p in backup_files} != set(state_hashes):
                    raise UpdateError("backup_state_corrupt")
                for path in backup_files:
                    if _digest(path) != state_hashes[path.relative_to(backup / "state").as_posix()]:
                        raise UpdateError("backup_state_corrupt")
                # Validate the entire source snapshot before replacing any data.
                for name, expected in baseline["files"].items():
                    if _digest(backup / "source" / _relative(name)) != expected:
                        raise UpdateError("backup_source_corrupt")
                # Candidate-created files must not survive an incompatible migration.
                _purge(self.runtime / "state")
                _copy_tree(backup / "state", self.runtime / "state")
                for name in (".env", ".combined-setup.json"):
                    _atomic(self.runtime / name, (backup / name).read_bytes())
                created = self.journal.get("created_files", [])
                for name in set(created) - set(baseline["files"]):
                    destination = self.root / _relative(name)
                    _safe(destination, file=True)
                    destination.unlink(missing_ok=True)
                for name, expected in baseline["files"].items():
                    source = backup / "source" / _relative(name)
                    if _digest(source) != expected:
                        raise UpdateError("backup_source_corrupt")
                    _copy_file(source, self.root / _relative(name))
                _write_json(self.baseline_path, baseline)
            override = self._override(transaction)
            self._compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", *SERVICES,
                          override=override, phase="rollback_start", timeout=600)
            self._wait_ready()
            self._bridge_check()
            self._checkpoint("rolled_back", error=original_error)
            return self._result("rolled_back", original_error)
        except Exception:
            try:
                self._checkpoint("rollback_failed", error="manual_recovery_required")
            except Exception:
                self._emit("rollback_failed", error="manual_recovery_required")
            return self._result("failed", "manual_recovery_required", recovery_required=True)

    def recover(self):
        try:
            data = self._load_journal()
            if not data or data["phase"] in TERMINAL:
                return None
            if data["phase"] == "rollback_failed":
                return self._result("failed", "manual_recovery_required", recovery_required=True)
            if data["phase"] in {"preparing", "staged"}:
                self._checkpoint("aborted", error="interrupted_before_stop")
                return self._result("failed", "interrupted_before_stop")
            return self._rollback("interrupted_update")
        except Exception:
            return {"state": "failed", "phase": "rollback_failed", "error": "invalid_recovery_state", "recovery_required": True}

    def apply(self, candidate: dict) -> dict:
        try:
            active = self._load_journal()
            if active and active["phase"] not in TERMINAL:
                return self._result("failed", "recovery_required", recovery_required=True)
            self.journal = None
            self._preflight()
            baseline = self.ensure_baseline()
            self._task_gate()
            if not isinstance(candidate, dict) or not isinstance(candidate.get("version"), str):
                raise UpdateError("invalid_candidate")
            archive = Path(os.path.abspath(candidate.get("archive_path", "")))
            if not _within(archive, self.private) or not isinstance(candidate.get("sha256"), str) or not SHA.fullmatch(candidate["sha256"]):
                raise UpdateError("invalid_candidate")
            if _digest(archive) != candidate["sha256"]:
                raise UpdateError("candidate_checksum_changed")
            verified = validate_archive(archive, candidate["version"])
            tid = uuid.uuid4().hex
            transaction = self.private / tid
            transaction.mkdir(mode=0o700)
            self.journal = {"format": 1, "transaction_id": tid, "version": candidate["version"],
                            "project": self.project, "context": self.context,
                            "phase": "preparing", "backup_complete": False}
            self._checkpoint("preparing")
            # Snapshot old gateway image before the candidate build reuses its tag.
            self.journal["old_images"] = self._old_images()
            self._checkpoint("preparing")
            stage = transaction / "stage"
            verified = extract_verified_archive(archive, stage, candidate["version"])
            managed = self._managed(stage, verified)
            self._validate_destinations(baseline["files"], managed)
            self._checkpoint("preparing", candidate_files=managed)
            self._compose("config", "--quiet", source=stage, phase="candidate_configuration")
            self._emit("downloading_images")
            self._compose("pull", "--ignore-buildable", "--policy", "missing", source=stage, phase="pull", timeout=3600)
            self._emit("building_gateway")
            self._compose("build", "gateway", source=stage, phase="build", timeout=3600)
            # Detect unsafe state and insufficient disk before stopping any service.
            state_files = list(_tree_files(self.runtime / "state"))
            needed = sum(p.stat().st_size for p in state_files) * 2 + 64 * 1024 * 1024
            if shutil.disk_usage(self.root).free < needed:
                raise UpdateError("insufficient_backup_space")
            self.ensure_baseline()
            # Pulls/builds can take minutes. Recheck after staging so a scheduler
            # re-enabled in that interval does not slip into the backup/start.
            self._task_gate()
            self._checkpoint("staged")
            self._checkpoint("stopping")
            self._stop_core()
            self._checkpoint("stopped")
            self._checkpoint("backing_up")
            self._backup(transaction, baseline)
            self._checkpoint("installing")
            self._install(stage, baseline["files"], managed)
            self._checkpoint("starting")
            self._compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", *SERVICES,
                          phase="candidate_start", timeout=600)
            self._checkpoint("checking")
            self._wait_ready()
            script = self.root / "deploy" / "bootstrap_qwenpaw.py"
            _safe(script, file=True)
            self._compose("exec", "-T", "qwenpaw", "/app/venv/bin/python", "-", input=script.read_bytes(), phase="bootstrap", timeout=240)
            self._bridge_check()
            _write_json(self.baseline_path, {"format": 1, "files": managed})
            self._checkpoint("completed")
            return self._result("completed")
        except Exception as error:
            code = error.code if isinstance(error, UpdateError) else "update_failed"
            if code in {"invalid_journal", "journal_scope_mismatch", "invalid_private_state"} and self.journal_path.exists():
                return self._result("failed", "invalid_recovery_state", recovery_required=True)
            if self.journal and self.journal.get("phase") not in {"preparing", "staged"} | TERMINAL:
                return self._rollback(code)
            if self.journal:
                try:
                    self._checkpoint("aborted", error=code)
                except Exception:
                    return self._result("failed", "journal_write_failed", recovery_required=True)
            return self._result("failed", code)
