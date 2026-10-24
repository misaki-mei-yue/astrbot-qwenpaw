"""Host-only portable data backup and recoverable restore.

No model requests, QQ messages, downloaded code or shell commands are executed.
The caller serializes this controller with both updater and backup host daemons.
Passwords are never persisted; an inspected plan contains decrypted private data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import uuid

from deploy.prepare import SECRET_KEYS, env_value, parse_env
from .controller import (_atomic, _copy_file, _copy_tree, _digest, _purge,
                         _read_json, _safe, _tree_files, _within, _write_json,
                         IMAGE, MAX_JSON, NAME)
from .backup_archive import create_backup, extract_backup

CORE = ("astrbot", "qwenpaw", "napcat")
TERMINAL = {"completed", "rolled_back", "aborted"}
ID = re.compile(r"[0-9a-f]{32}\Z")
PROXY_KEYS = ("QWENPAW_HTTP_PROXY", "QWENPAW_HTTPS_PROXY", "QWENPAW_NO_PROXY")
STATE_ROOTS = ("astrbot", "bridge-files", "qwenpaw/working", "qwenpaw/secret",
               "qwenpaw/backups", "napcat/config", "napcat/qq")


class BackupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _normal(value):
    """Canonicalize only documented local Docker Desktop bind aliases."""
    value = str(value).replace("\\", "/").rstrip("/")
    for prefix in ("/run/desktop/mnt/host/", "/host_mnt/"):
        if value.startswith(prefix):
            tail = value[len(prefix):]
            if re.match(r"[a-zA-Z]/", tail):
                value = tail[0] + ":/" + tail[2:]
    if re.match(r"^[A-Za-z]:/", value):
        value = value.casefold()
    return value


def _inventory(path):
    return {p.relative_to(path).as_posix(): {"size": p.stat().st_size, "sha256": _digest(p)}
            for p in _tree_files(path)}


def _private_json(path):
    # File inventories can be larger than updater's small control JSON limit.
    _safe(path, file=True)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise BackupError("invalid_private_state")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise BackupError("invalid_private_state") from None


def _verify_tree(path, expected):
    if not isinstance(expected, dict) or _inventory(path) != expected:
        raise BackupError("snapshot_verification_failed")


def _sync_directory(source, target):
    """Replace content while keeping every bind-mounted directory inode intact."""
    _safe(source)
    _safe(target)
    target.mkdir(parents=True, exist_ok=True)
    desired = {item.name: item for item in source.iterdir()}
    for item in list(target.iterdir()):
        if item.name not in desired:
            _purge(item)
    for name, origin in desired.items():
        _safe(origin)
        destination = target / name
        if origin.is_dir():
            if destination.exists() and not destination.is_dir():
                _purge(destination)
            # A link created by a failed candidate must never be followed.
            if destination.is_symlink() or (destination.exists() and getattr(destination.lstat(), "st_file_attributes", 0) & 0x400):
                _purge(destination)
            _sync_directory(origin, destination)
        else:
            if destination.exists() and (destination.is_dir() or destination.is_symlink()):
                _purge(destination)
            _copy_file(origin, destination)


class BackupController:
    def __init__(self, root: Path, project: str, context: str, private: Path,
                 report, *, runtime_root: Path | None = None, layout="bundle", runner=None):
        self.root = Path(os.path.abspath(root))
        self.runtime = Path(os.path.abspath(runtime_root or self.root / "runtime"))
        self.private = Path(os.path.abspath(private))
        if layout not in {"bundle", "linux"} or not NAME.fullmatch(project) or not NAME.fullmatch(context):
            raise BackupError("invalid_deployment_scope")
        if layout == "bundle":
            expected = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(self.root).lower().encode()).hexdigest()[:10]
            if project != expected or self.runtime != self.root / "runtime":
                raise BackupError("project_scope_mismatch")
        if self.private != self.runtime / "backups" / "private":
            raise BackupError("invalid_private_directory")
        for path in (self.root, self.runtime, self.private):
            _safe(path)
        if not self.root.is_dir() or self.root == Path(self.root.anchor) or self.runtime == Path(self.runtime.anchor):
            raise BackupError("invalid_install_directory")
        self.private.mkdir(parents=True, exist_ok=True)
        self.private.chmod(0o700)
        for name in ("archives", "plans", "transactions"):
            (self.private / name).mkdir(exist_ok=True, mode=0o700)
        self.layout, self.project, self.context = layout, project, context
        self.report, self.runner = report, runner
        self.docker = shutil.which("docker.exe" if os.name == "nt" else "docker") or "docker"
        self.journal_path = self.private / "transaction.json"
        self.journal = None
        self.services = (*CORE, "gateway") if layout == "bundle" else (*CORE, "console", "gateway")

    def _emit(self, phase, **extra):
        try:
            self.report({"phase": phase, **extra})
        except Exception:
            pass

    def _checkpoint(self, phase, **extra):
        self.journal.update(phase=phase, **extra)
        _write_json(self.journal_path, self.journal)
        self._emit(phase)

    def _env(self):
        values = {k: v for k, v in os.environ.items() if not k.startswith(("DOCKER_", "COMPOSE_"))}
        for key in self._config_env():
            values.pop(key, None)
        values.update(PROJECT_SOURCE=self.root.as_posix(), INSTALL_ROOT=self.runtime.as_posix())
        return values

    def _run(self, arguments, *, phase="docker", input=None, timeout=180):
        args = [self.docker, "--context", self.context, *arguments]
        kwargs = {"cwd": self.root, "env": self._env(), "input": input, "timeout": timeout}
        try:
            if self.runner:
                result = self.runner(args, **kwargs)
            else:
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
            get = result.get if isinstance(result, dict) else lambda k, d=None: getattr(result, k, d)
            output = get("stdout", b"") or b""
            if isinstance(output, str):
                output = output.encode()
            if get("returncode", 1) != 0:
                raise BackupError(phase + "_failed")
            if len(output) > MAX_JSON:
                raise BackupError("docker_response_too_large")
            return output.decode("utf-8", errors="strict").strip()
        except BackupError:
            raise
        except Exception:
            raise BackupError(phase + "_failed") from None

    def _compose(self, *args, override=None, **kwargs):
        files = [self.root / "deploy" / ("compose.bundle.yaml" if self.layout == "bundle" else "compose.yaml")]
        if self.layout == "linux":
            files.append(self.root / "deploy" / "compose.manage.yaml")
        arguments = ["compose", "--project-directory", str(self.root), "--project-name", self.project,
                     "--env-file", str(self.runtime / ".env")]
        for path in files + ([override] if override else []):
            _safe(path, file=True)
            arguments.extend(("-f", str(path)))
        return self._run([*arguments, *args], **kwargs)

    def _metadata(self):
        data = _read_json(self.root / "bundle-version.json")
        if data.get("format") != 1 or not isinstance(data.get("version"), str) or set(data.get("components", {})) != set(CORE):
            raise BackupError("invalid_bundle_metadata")
        if any(not isinstance(v, str) or not v for v in data["components"].values()):
            raise BackupError("invalid_bundle_metadata")
        return data

    def _config_env(self):
        _safe(self.runtime / ".env", file=True)
        try:
            values = parse_env(self.runtime / ".env")
        except Exception:
            raise BackupError("invalid_private_configuration") from None
        if any(len(values.get(key, "")) < 16 for key in SECRET_KEYS):
            raise BackupError("invalid_private_configuration")
        return values

    def _expected_mounts(self):
        runtime, root = self.runtime, self.root
        result = {
            "astrbot": {"/AstrBot/data": (runtime / "state/astrbot", True),
                        "/AstrBot/data/plugins/astrbot_plugin_qwenpaw_bridge": (root / "astrbot_plugin_qwenpaw_bridge", False),
                        "/bridge-files": (runtime / "state/bridge-files", True)},
            "qwenpaw": {"/app/working": (runtime / "state/qwenpaw/working", True),
                        "/app/working.secret": (runtime / "state/qwenpaw/secret", True),
                        "/app/working.backups": (runtime / "state/qwenpaw/backups", True),
                        "/app/working/plugins/qwenpaw_plugin_astrbot_bridge": (root / "qwenpaw_plugin_astrbot_bridge", False),
                        "/bridge-files": (runtime / "state/bridge-files", True)},
            "napcat": {"/app/napcat/config": (runtime / "state/napcat/config", True),
                       "/app/.config/QQ": (runtime / "state/napcat/qq", True),
                       "/bridge-files": (runtime / "state/bridge-files", False)},
            "gateway": {"/bundle-updates": (runtime / "updates/ipc", True),
                        "/bundle-backups": (runtime / "backups/ipc", True)},
            "console": {"/etc/caddy/Caddyfile": (root / "deploy/console.Caddyfile", False)},
        }
        if self.layout == "linux":
            result["gateway"].pop("/bundle-updates")
        return result

    def _mounts(self, service, mounts, *, inspect=False):
        expected = self._expected_mounts()[service]
        seen = set()
        if not isinstance(mounts, list):
            raise BackupError("unrecognized_data_mount")
        for row in mounts:
            if not isinstance(row, dict):
                raise BackupError("unrecognized_data_mount")
            kind = row.get("Type" if inspect else "type")
            target = row.get("Destination" if inspect else "target")
            # Only gateway's disposable /tmp is allowed outside persisted binds.
            if kind == "tmpfs" and service == "gateway" and target == "/tmp":
                continue
            if kind != "bind" or target not in expected or target in seen:
                raise BackupError("unrecognized_data_mount")
            source, writable = expected[target]
            _safe(source)
            actual = row.get("Source" if inspect else "source", "")
            rw = row.get("RW") if inspect else not row.get("read_only", False)
            if _normal(actual) != _normal(source) or rw is not writable:
                raise BackupError("external_data_mount")
            seen.add(target)
        if seen != set(expected):
            raise BackupError("incomplete_data_mounts")

    def _container(self, service):
        raw = self._run(["ps", "--all", "--quiet", "--filter", "label=com.docker.compose.project=" + self.project,
                         "--filter", "label=com.docker.compose.service=" + service], phase="containers")
        ids = raw.split()
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
            raise BackupError("container_not_found")
        return ids[0]

    def _deployment(self):
        self._metadata()
        marker = _read_json(self.runtime / ".combined-setup.json")
        if marker.get("format") != 1 or marker.get("mode") != "fresh":
            raise BackupError("addon_backup_unsupported")
        env = self._config_env()
        if _normal(env.get("INSTALL_ROOT")) != _normal(self.runtime) or _normal(env.get("PROJECT_SOURCE")) != _normal(self.root):
            raise BackupError("configuration_path_mismatch")
        if self.layout == "linux" and env.get("COMPOSE_PROJECT_NAME") != self.project:
            raise BackupError("project_scope_mismatch")
        endpoint = json.loads(self._run(["context", "inspect", self.context, "--format", "{{json .Endpoints.docker.Host}}"], phase="context"))
        local_pipe = isinstance(endpoint, str) and re.fullmatch(r"npipe:/{2,4}\./pipe/[A-Za-z0-9_.-]+", endpoint, re.I)
        local_unix = isinstance(endpoint, str) and endpoint.startswith("unix:///") and not any(c in endpoint for c in "\r\n?#%")
        if not local_pipe and not local_unix:
            raise BackupError("remote_engine_forbidden")
        engine = json.loads(self._run(["info", "--format", "{{json .}}"], phase="engine"))
        architecture = engine.get("Architecture")
        if engine.get("OSType") != "linux" or architecture not in {"x86_64", "aarch64", "amd64", "arm64"}:
            raise BackupError("unsupported_docker_engine")
        config = json.loads(self._compose("config", "--format", "json", phase="configuration"))
        if set(config.get("services", {})) != set(self.services):
            raise BackupError("unknown_project_services")
        for service in self.services:
            row = config["services"][service]
            if row.get("volumes_from") or row.get("devices"):
                raise BackupError("external_data_mount")
            self._mounts(service, row.get("volumes", []))
        ids = self._run(["ps", "--all", "--quiet", "--filter", "label=com.docker.compose.project=" + self.project], phase="containers").split()
        if len(ids) != len(self.services) or any(not re.fullmatch(r"[0-9a-f]{12,64}", v) for v in ids):
            raise BackupError("unknown_project_containers")
        containers = json.loads(self._run(["inspect", *ids], phase="containers"))
        if not isinstance(containers, list) or len(containers) != len(ids):
            raise BackupError("invalid_container_state")
        images, running, seen = {}, {}, set()
        for row in containers:
            labels = row.get("Config", {}).get("Labels", {})
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.project") != self.project or service not in self.services or service in seen:
                raise BackupError("container_scope_mismatch")
            self._mounts(service, row.get("Mounts", []), inspect=True)
            image = row.get("Image", "")
            if not IMAGE.fullmatch(image) or type(row.get("State", {}).get("Running")) is not bool:
                raise BackupError("invalid_container_state")
            images[service] = image
            running[service] = row["State"]["Running"]
            seen.add(service)
        if seen != set(self.services):
            raise BackupError("container_scope_mismatch")
        return {"layout": self.layout, "architecture": self._architecture(architecture), "images": images, "running": running}

    @staticmethod
    def _architecture(value):
        return {"x86_64": "amd64", "aarch64": "arm64"}.get(value, value)

    def _stop(self):
        ids = [self._container(service) for service in CORE]
        self._run(["stop", "--time", "45", *ids], phase="stop", timeout=240)
        for value in ids:
            if self._run(["inspect", "--format", "{{json .State.Running}}", value], phase="stop_verify") != "false":
                raise BackupError("services_not_stopped")

    def _restart_original(self):
        running = self.journal["deployment"]["running"]
        ids = [self._container(service) for service in CORE if running[service]]
        if ids:
            self._run(["start", *ids], phase="restart", timeout=240)
            for value in ids:
                if self._run(["inspect", "--format", "{{json .State.Running}}", value], phase="restart_verify") != "true":
                    raise BackupError("restart_failed")

    def _snapshot(self, destination):
        _copy_tree(self.runtime / "state", destination / "state")
        for name in (".env", ".combined-setup.json"):
            _copy_file(self.runtime / name, destination / name)
        hashes = _inventory(destination)
        for name, row in hashes.items():
            if _digest(self.runtime / name) != row["sha256"]:
                raise BackupError("snapshot_verification_failed")
        return hashes

    def _space(self, size, *, multiplier=3):
        if shutil.disk_usage(self.private).free < size * multiplier + 64 * 1024 * 1024:
            raise BackupError("insufficient_backup_space")

    def _active(self):
        journal = self._load_journal()
        if journal and journal["phase"] not in TERMINAL:
            raise BackupError("recovery_required")
        self.journal = None

    def _load_journal(self):
        if not self.journal_path.exists():
            return None
        value = _read_json(self.journal_path)
        phases = TERMINAL | {"preparing", "stopping", "snapshotting", "backed_up", "restarting", "encrypting", "installing", "starting", "checking", "rolling_back", "recovery_failed"}
        if (not isinstance(value, dict) or value.get("format") != 1 or not ID.fullmatch(value.get("transaction_id", ""))
                or value.get("project") != self.project or value.get("context") != self.context
                or value.get("kind") not in {"export", "restore"} or value.get("phase") not in phases):
            raise BackupError("invalid_recovery_state")
        self.journal = value
        return value

    def _new_transaction(self, kind, deployment):
        tid = uuid.uuid4().hex
        transaction = self.private / "transactions" / tid
        transaction.mkdir(mode=0o700)
        self.journal = {"format": 1, "transaction_id": tid, "project": self.project, "context": self.context,
                        "kind": kind, "deployment": deployment, "phase": "preparing", "backup_complete": False}
        _write_json(self.journal_path, self.journal)
        return transaction

    def export(self, password, destination="server"):
        if destination not in {"server", "download"}:
            raise BackupError("invalid_backup_destination")
        # Validate password before stopping anything. Archive module repeats it.
        if not isinstance(password, str) or len(password) < 12 or len(password.encode("utf-8")) > 1024:
            raise BackupError("invalid_password")
        self._active()
        deployment = self._deployment()
        self._space(sum(p.stat().st_size for p in _tree_files(self.runtime / "state")))
        transaction = self._new_transaction("export", deployment)
        archive_id = self.journal["transaction_id"]
        path = self.private / "archives" / (archive_id + ".aqbackup")
        snapshot = transaction / "snapshot"
        try:
            self._checkpoint("stopping")
            self._stop()
            self._checkpoint("snapshotting")
            snapshot.mkdir(mode=0o700)
            hashes = self._snapshot(snapshot)
            _write_json(transaction / "snapshot-hashes.json", hashes)
            self._checkpoint("backed_up", backup_complete=True)
            self._checkpoint("restarting")
            self._restart_original()
            self._checkpoint("encrypting")
            manifest = create_backup(snapshot, path, password, bundle_metadata=self._metadata(),
                                     deployment={k: v for k, v in deployment.items() if k != "running"}, report=lambda p: self._emit(p))
            entry = {"id": archive_id, "filename": path.name, "size": path.stat().st_size, "sha256": _digest(path),
                     "created_at": manifest["created_at"], "version": manifest["bundle"]["version"], "destination": destination}
            catalog = self._catalog()
            catalog.append(entry)
            _write_json(self.private / "catalog.json", {"format": 1, "backups": catalog})
            self._checkpoint("completed")
            try:
                _purge(snapshot)
            except Exception:
                pass  # Private snapshot retention cannot invalidate a committed backup.
            return {**entry, "archive_path": str(path)}
        except Exception as error:
            path.unlink(missing_ok=True)
            code = getattr(error, "code", "backup_failed")
            try:
                self._restart_original()
                self._checkpoint("aborted", error=code)
                if snapshot.exists():
                    _purge(snapshot)
            except Exception:
                self._checkpoint("recovery_failed", error="restart_failed")
                raise BackupError("recovery_required") from None
            raise BackupError(code) from None

    def _catalog(self):
        path = self.private / "catalog.json"
        value = _private_json(path) if path.exists() else {"format": 1, "backups": []}
        if value.get("format") != 1 or not isinstance(value.get("backups"), list) or len(value["backups"]) > 10000:
            raise BackupError("invalid_backup_catalog")
        for entry in value["backups"]:
            if not isinstance(entry, dict) or not ID.fullmatch(entry.get("id", "")) or entry.get("filename") != entry["id"] + ".aqbackup":
                raise BackupError("invalid_backup_catalog")
        return value["backups"]

    def list_backups(self):
        return [dict(row) for row in reversed(self._catalog())]

    def get_archive(self, backup_id):
        if not isinstance(backup_id, str) or not ID.fullmatch(backup_id):
            raise BackupError("unknown_backup")
        row = next((v for v in self._catalog() if v["id"] == backup_id), None)
        if row is None:
            raise BackupError("unknown_backup")
        path = self.private / "archives" / row["filename"]
        if not path.is_file() or path.stat().st_size != row["size"] or _digest(path) != row["sha256"]:
            raise BackupError("backup_archive_changed")
        return path

    def delete_archive(self, backup_id):
        """Remove an owned retained archive while caller holds operation.lock."""
        if not isinstance(backup_id, str) or not ID.fullmatch(backup_id):
            raise BackupError("unknown_backup")
        catalog = self._catalog()
        if not any(row["id"] == backup_id for row in catalog):
            return
        path = self.get_archive(backup_id)
        transfer = self.runtime / "backups/ipc/transfers" / (backup_id + ".aqbackup")
        _safe(transfer)
        if transfer.exists():
            _safe(transfer, file=True)
            if transfer.stat().st_nlink != 1:
                raise BackupError("invalid_backup_catalog")
            transfer.unlink()
        # Commit the catalog first; a crash leaves a harmless unlisted file.
        _write_json(self.private / "catalog.json", {"format": 1, "backups": [r for r in catalog if r["id"] != backup_id]})
        path.unlink()

    def _cleanup_plans(self):
        for path in (self.private / "plans").iterdir():
            _safe(path)
            if not ID.fullmatch(path.name) or not path.is_dir():
                raise BackupError("invalid_restore_plan")
            metadata = path / "plan.json"
            if not metadata.exists() or _private_json(metadata).get("expires_at", 0) <= time.time():
                _purge(path)

    def cleanup(self):
        """Expire decrypted inspection payloads; caller holds the operation lock."""
        self._cleanup_plans()

    def inspect(self, archive_path, password):
        self._active()
        deployment = self._deployment()
        self._cleanup_plans()
        archive = Path(os.path.abspath(archive_path))
        _safe(archive, file=True)
        if not _within(archive, self.private):
            raise BackupError("invalid_archive_path")
        self._space(archive.stat().st_size, multiplier=4)
        plan_id = uuid.uuid4().hex
        plan = self.private / "plans" / plan_id
        plan.mkdir(mode=0o700)
        try:
            manifest = extract_backup(archive, plan / "payload", password, report=lambda p: self._emit(p))
            if manifest.get("format") != 1 or manifest.get("kind") != "astrbot-qwenpaw-backup":
                raise BackupError("invalid_backup_kind")
            if manifest["bundle"]["components"] != self._metadata()["components"]:
                raise BackupError("component_version_mismatch")
            if self._architecture(manifest["deployment"].get("architecture")) != deployment["architecture"]:
                raise BackupError("architecture_mismatch")
            marker = _read_json(plan / "payload/.combined-setup.json")
            if marker.get("format") != 1 or marker.get("mode") != "fresh":
                raise BackupError("addon_backup_unsupported")
            source_env = parse_env(plan / "payload/.env")
            if any(len(source_env.get(key, "")) < 16 for key in SECRET_KEYS):
                raise BackupError("invalid_private_configuration")
            for suffix in STATE_ROOTS:
                if not (plan / "payload/state" / suffix).is_dir():
                    raise BackupError("incomplete_backup_state")
            hashes = _inventory(plan / "payload")
            public = {"plan_id": plan_id, "expires_at": int(time.time()) + 3600,
                      "bundle_version": manifest["bundle"]["version"], "components": manifest["bundle"]["components"],
                      "architecture": deployment["architecture"], "file_count": manifest["file_count"],
                      "total_size": manifest["total_size"], "warnings": ["source_must_be_stopped", "platform_login_may_expire", "scheduled_jobs_resume", "custom_absolute_paths_may_need_review"],
                      "requires_overwrite": any(_tree_files(self.runtime / "state"))}
            _write_json(plan / "plan.json", {"format": 1, "public": public, "expires_at": public["expires_at"],
                       "hashes": hashes, "components": manifest["bundle"]["components"], "architecture": deployment["architecture"]})
            return public
        except Exception as error:
            _purge(plan)
            raise BackupError(getattr(error, "code", "invalid_backup_archive")) from None

    def _rebase_env(self, source):
        values = parse_env(source)
        target = self._config_env()
        for key in PROXY_KEYS:
            if key in target:
                values[key] = target[key]
            else:
                values.pop(key, None)
        for key in ("EXISTING_ASTRBOT_DIR", "EXISTING_ASTRBOT_CONTAINER", "EXISTING_ASTRBOT_PROJECT"):
            values.pop(key, None)
        values.update(INSTALL_ROOT=self.runtime.as_posix(), PROJECT_SOURCE=self.root.as_posix(),
                      COMPOSE_PROJECT_NAME=self.project,
                      BRIDGE_NETWORK=target.get("BRIDGE_NETWORK", self.project + "-network"),
                      QWENPAW_BASE_URL="http://qwenpaw:8088", ASTRBOT_BRIDGE_URL="http://astrbot:9186",
                      BRIDGE_SOURCE_ROOTS="/AstrBot/data/temp", BRIDGE_NAPCAT_HOSTS="napcat")
        encoded = "# Private restored configuration. Do not publish.\n" + "\n".join(k + "=" + env_value(v) for k, v in values.items()) + "\n"
        return encoded.encode("utf-8")

    def _apply_payload(self, payload, *, rebase):
        _sync_directory(payload / "state", self.runtime / "state")
        _atomic(self.runtime / ".env", self._rebase_env(payload / ".env") if rebase else (payload / ".env").read_bytes())
        if rebase:
            _write_json(self.runtime / ".combined-setup.json", {"format": 1, "mode": "fresh"})
        else:
            _copy_file(payload / ".combined-setup.json", self.runtime / ".combined-setup.json")

    def _wait_ready(self, timeout=600):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with opener.open("http://127.0.0.1:18080/bundle-health", timeout=8) as response:
                    value = json.loads(response.read(65537))
                if value.get("status") == "ready" and all(value.get("services", {}).get(s, {}).get("ready") is True for s in CORE):
                    return
            except Exception:
                pass
            time.sleep(2)
        raise BackupError("health_check_failed")

    def _bridge_check(self):
        script = b"import json,os,urllib.request\no=urllib.request.build_opener(urllib.request.ProxyHandler({}))\nr=urllib.request.Request('http://127.0.0.1:9186/health',headers={'Authorization':'Bearer '+os.environ['BRIDGE_TOKEN']})\nwith o.open(r,timeout=8) as x: assert json.loads(x.read(65536)).get('ready') is True\n"
        self._compose("exec", "-T", "astrbot", "python", "-", phase="bridge_health", input=script, timeout=30)

    def _start_restored(self, *, rollback=False):
        override = None
        if rollback:
            images = self.journal["deployment"]["images"]
            if set(images) != set(self.services) or any(not IMAGE.fullmatch(v) for v in images.values()):
                raise BackupError("invalid_image_snapshot")
            override = self.private / "transactions" / self.journal["transaction_id"] / "rollback-compose.json"
            _write_json(override, {"services": {s: {"image": image, "pull_policy": "never"} for s, image in images.items()}})
            running = self.journal["deployment"]["running"]
            if not all(running.values()):
                # Never briefly start a previously stopped scheduler on rollback.
                self._compose("create", "--no-build", "--pull", "never", "--force-recreate", *self.services,
                              override=override, phase="rollback_start", timeout=600)
                active = [s for s in self.services if running[s]]
                if active:
                    self._compose("start", *active, override=override, phase="rollback_start", timeout=240)
                return
        self._compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", *self.services,
                      override=override, phase="rollback_start" if rollback else "restore_start", timeout=600)

    def _result(self, state, error=None, recovery_required=False):
        result = {"state": state, "phase": self.journal["phase"] if self.journal else state,
                  "recovery_required": recovery_required}
        if self.journal:
            result["transaction_id"] = self.journal["transaction_id"]
        if error:
            result["error"] = error
        return result

    def restore(self, plan_id, *, confirm_source_stopped, overwrite):
        self._active()
        if confirm_source_stopped is not True:
            raise BackupError("source_stop_confirmation_required")
        if not isinstance(plan_id, str) or not ID.fullmatch(plan_id):
            raise BackupError("invalid_restore_plan")
        plan = self.private / "plans" / plan_id
        data = _private_json(plan / "plan.json")
        if data.get("format") != 1 or data.get("expires_at", 0) <= time.time():
            if plan.exists():
                _purge(plan)
            raise BackupError("restore_plan_expired")
        deployment = self._deployment()
        if data["components"] != self._metadata()["components"] or data["architecture"] != deployment["architecture"]:
            raise BackupError("restore_plan_incompatible")
        if any(_tree_files(self.runtime / "state")) and overwrite is not True:
            raise BackupError("overwrite_confirmation_required")
        _verify_tree(plan / "payload", data["hashes"])
        size = sum(row["size"] for row in data["hashes"].values())
        self._space(size + sum(p.stat().st_size for p in _tree_files(self.runtime / "state")), multiplier=2)
        transaction = self._new_transaction("restore", deployment)
        # Preserve exact old images against cleanup while the recovery journal exists.
        for service, image in deployment["images"].items():
            self._run(["image", "tag", image, "bot-combined-restore-backup:" + self.journal["transaction_id"] + "-" + service], phase="preserve_image")
        try:
            self._checkpoint("stopping")
            self._stop()
            self._checkpoint("snapshotting")
            snapshot = transaction / "snapshot"
            snapshot.mkdir(mode=0o700)
            hashes = self._snapshot(snapshot)
            _write_json(transaction / "snapshot-hashes.json", hashes)
            self._checkpoint("backed_up", backup_complete=True)
            _verify_tree(plan / "payload", data["hashes"])
            self._checkpoint("installing")
            self._apply_payload(plan / "payload", rebase=True)
            self._checkpoint("starting")
            self._start_restored()
            self._checkpoint("checking")
            self._wait_ready()
            self._bridge_check()
            self._checkpoint("completed")
            try:
                _purge(plan)
            except Exception:
                pass
            return self._result("completed")
        except Exception as error:
            return self._rollback(getattr(error, "code", "restore_failed"))

    def _rollback(self, error):
        try:
            self._checkpoint("rolling_back", error=error)
            self._stop()
            if self.journal.get("backup_complete"):
                transaction = self.private / "transactions" / self.journal["transaction_id"]
                snapshot = transaction / "snapshot"
                _verify_tree(snapshot, _private_json(transaction / "snapshot-hashes.json"))
                self._apply_payload(snapshot, rebase=False)
                self._start_restored(rollback=True)
                running = self.journal["deployment"]["running"]
                for service in self.services:
                    actual = self._run(["inspect", "--format", "{{json .State.Running}}", self._container(service)], phase="rollback_verify")
                    if actual != json.dumps(running[service]):
                        raise BackupError("rollback_state_mismatch")
                if all(running[s] for s in (*CORE, "gateway")):
                    self._wait_ready()
                    self._bridge_check()
            else:
                self._restart_original()
            self._checkpoint("rolled_back", error=error)
            return self._result("rolled_back", error)
        except Exception:
            self._checkpoint("recovery_failed", error="manual_recovery_required")
            return self._result("failed", "manual_recovery_required", True)

    def recover(self):
        try:
            data = self._load_journal()
            if not data or data["phase"] in TERMINAL:
                return None
            # Retry is explicit host startup recovery, never a model/task replay.
            if data["kind"] == "restore":
                return self._rollback("interrupted_restore")
            self._restart_original()
            transaction = self.private / "transactions" / data["transaction_id"]
            if (transaction / "snapshot").exists():
                _purge(transaction / "snapshot")
            if not any(row["id"] == data["transaction_id"] for row in self._catalog()):
                orphan = self.private / "archives" / (data["transaction_id"] + ".aqbackup")
                _safe(orphan, file=True)
                orphan.unlink(missing_ok=True)
            self._checkpoint("aborted", error="interrupted_backup")
            return self._result("rolled_back", "interrupted_backup")
        except Exception:
            return {"state": "failed", "phase": "recovery_failed", "error": "manual_recovery_required", "recovery_required": True}
