"""Host-only portable backups. The gateway shares ciphertext and signed mailboxes only."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

from deploy.prepare import parse_env
from .common import FileLock, atomic_json, metadata, read_json, safe_path, sign, verify
from .daemon import _local_context, _protect_private

ID = re.compile(r"[0-9a-f]{32}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
PHASES = {"idle", "exporting", "inspecting", "ready_to_restore", "restoring", "completed", "failed", "rolled_back"}


def prepare(root, project, context, runtime_root=None, layout="bundle", *, check_context=True):
    root = safe_path(root)
    runtime = safe_path(runtime_root or root / "runtime")
    if layout not in {"bundle", "linux"} or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", project):
        raise ValueError("Invalid backup deployment.")
    if layout == "bundle" and (runtime != root / "runtime" or project != "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:10]):
        raise ValueError("The backup helper does not belong to this installation.")
    marker = read_json(runtime / ".combined-setup.json", 4096)
    env = parse_env(safe_path(runtime / ".env"))
    if marker.get("format") != 1 or marker.get("mode") != "fresh":
        raise ValueError("Portable backups require a full fresh installation, not an external AstrBot add-on.")
    if safe_path(Path(env.get("PROJECT_SOURCE", ""))) != root or safe_path(Path(env.get("INSTALL_ROOT", ""))) != runtime:
        raise ValueError("The private configuration belongs to another installation.")
    if layout == "linux" and env.get("COMPOSE_PROJECT_NAME", "bot-combined") != project:
        raise ValueError("The Compose project does not match this installation.")
    token = env.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
        raise ValueError("Invalid private runtime token.")
    if check_context:
        _local_context(context)
    base = safe_path(runtime / "backups")
    ipc, private = base / "ipc", base / "private"
    for path, mode in ((base, 0o711), (ipc, 0o1777), (private, 0o700),
                       (runtime / "updates", 0o711), (runtime / "updates/private", 0o700)):
        safe_path(path).mkdir(parents=True, exist_ok=True, mode=mode)
        path.chmod(mode)
    _protect_private(private)
    _protect_private(runtime / "updates/private")
    for name in ("uploads", "transfers"):
        path = safe_path(ipc / name)
        path.mkdir(exist_ok=True, mode=0o1777)
        path.chmod(0o1777)
    return root, runtime, ipc, private, token


def status(ipc, token):
    try:
        value = verify(read_json(ipc / "status.json", 262144), token)
        stamp = value.get("heartbeat")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 20:
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


class BackupDaemon:
    def __init__(self, root, project, context, runtime_root=None, layout="bundle", *, controller_factory=None, check_context=True, instance=None):
        self.root, self.runtime, self.ipc, self.private, self.token = prepare(root, project, context, runtime_root, layout, check_context=check_context)
        self.project, self.context, self.layout = project, context, layout
        self.operation_lock = self.runtime / "updates/private/operation.lock"
        self.guard = threading.RLock()
        self.worker = None
        self.exit_requested = False
        self.state = {"phase": "idle", "active": True, "ready": False, "backups": [], "instance": instance or secrets.token_hex(16)}
        self.recent = {}
        try:
            self.recent = read_json(self.private / "recent.json", 262144)
        except (OSError, ValueError):
            pass
        if not isinstance(self.recent, dict):
            self.recent = {}
        self.factory = controller_factory
        self.controller = None

    def publish(self, **changes):
        with self.guard:
            self.state.update(changes)
            self.state["heartbeat"] = time.time()
            atomic_json(self.ipc / "status.json", sign(dict(self.state), self.token), 0o644)

    def report(self, event, **kwargs):
        step = event.get("phase", event.get("step", "")) if isinstance(event, dict) else str(event)
        # Do not publish paths, Docker output, filenames, environment, or exception text.
        allowed = {"preflight", "stopping", "stopped", "snapshot", "backing_up", "encrypting", "decrypting", "validating", "restoring", "starting", "checking", "rolling_back", "completed"}
        self.publish(step=step if step in allowed else "checking")

    def make_controller(self):
        factory = self.factory
        if factory is None:
            from .backup_controller import BackupController
            factory = BackupController
        return factory(self.root, self.project, self.context, self.private, self.report,
                       runtime_root=self.runtime, layout=self.layout)

    def refresh_token(self):
        self.token = parse_env(safe_path(self.runtime / ".env"))["QWENPAW_RUNTIME_INTERNAL_TOKEN"]

    def initialize(self):
        restart_updater = False
        try:
            from .daemon import _pending_recovery
            if _pending_recovery(self.private):
                self._updater_stop()
                restart_updater = True
            with FileLock(self.operation_lock):
                self.controller = self.make_controller()
                result = self.controller.recover()
                self.refresh_token()
                if result is not None:
                    self.state.update(phase=result["state"], recovery_required=result.get("recovery_required") is True)
                catalog = self.controller.list_backups()
                for item in catalog[:100]:
                    if not (self.ipc / "transfers" / (item["id"] + ".aqbackup")).exists():
                        self.transfer({**item, "archive_path": str(self.controller.get_archive(item["id"]))})
                self.publish(active=False, ready=True, backups=self.public_catalog())
        except Exception as error:
            self.fail(error, recovery=True)
            self.publish(ready=True)
        finally:
            if restart_updater and not self.state.get("recovery_required"):
                try:
                    self._updater_start()
                except Exception:
                    self.publish(error="updater_restart_failed")

    def public_catalog(self):
        return [{key: item.get(key) for key in ("id", "filename", "size", "created_at", "version")}
                for item in self.controller.list_backups()][:100]

    def fail(self, error, *, recovery=False):
        code = getattr(error, "code", "backup_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "backup_failed"
        # Fixed codes only, even in private diagnostics: never persist a passphrase.
        atomic_json(self.private / "last-error.json", {"time": time.time(), "code": code})
        self.publish(phase="failed", active=False, error=code, recovery_required=recovery or code in {"recovery_required", "manual_recovery_required"})

    def transfer(self, entry):
        identifier = entry["id"]
        if not ID.fullmatch(identifier):
            raise ValueError("Invalid backup identifier.")
        source = safe_path(Path(entry["archive_path"]))
        source.relative_to(self.private)
        destination = safe_path(self.ipc / "transfers" / (identifier + ".aqbackup"))
        if destination.exists():
            if not destination.is_file() or destination.stat().st_nlink != 1:
                raise ValueError("Invalid transfer file.")
            destination.unlink()
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        destination.chmod(0o644)

    def _updater_stop(self):
        if self.layout == "bundle":
            from .daemon import stop_daemon
            stop_daemon(self.root, self.project, self.context)

    def _updater_start(self):
        if self.layout == "bundle":
            from .daemon import ensure_daemon
            ensure_daemon(self.root, self.project, self.context)

    def job(self, request):
        restart_updater = False
        try:
            with FileLock(self.operation_lock):
                from .daemon import _pending_recovery
                if _pending_recovery(self.runtime / "updates/private"):
                    raise ValueError("Finish update recovery before backups.")
                action = request["action"]
                if action == "export":
                    from .backup_archive import open_secret
                    password = open_secret(request["secret"], self.token, request["id"] + ":export")
                    entry = self.controller.export(password, request["destination"])
                    password = None
                    self.transfer(entry)
                    self.publish(phase="completed", active=False, backups=self.public_catalog(),
                                 result={"kind": "export", "backup_id": entry["id"], "filename": entry["filename"]})
                elif action == "inspect":
                    from .backup_archive import open_secret
                    password = open_secret(request["secret"], self.token, request["id"] + ":inspect")
                    archive = safe_path(self.ipc / "uploads" / (request["upload_id"] + ".aqbackup"))
                    if not archive.is_file() or archive.stat().st_nlink != 1:
                        raise ValueError("Upload is unavailable.")
                    incoming = safe_path(self.private / "uploads")
                    incoming.mkdir(exist_ok=True, mode=0o700)
                    private_archive = incoming / (request["upload_id"] + ".aqbackup")
                    try:
                        with archive.open("rb") as source, private_archive.open("xb") as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
                        private_archive.chmod(0o600)
                        plan = self.controller.inspect(private_archive, password)
                    finally:
                        password = None
                        archive.unlink(missing_ok=True)
                        private_archive.unlink(missing_ok=True)
                    self.publish(phase="ready_to_restore", active=False, plan={
                        "plan_id": plan["plan_id"], "version": plan["bundle_version"], "components": plan["components"],
                        "files": plan["file_count"], "bytes": plan["total_size"], "target_has_data": plan["requires_overwrite"],
                        "warnings": plan["warnings"], "path_check": "passed"}, result=None)
                elif action == "restore":
                    self._updater_stop()
                    restart_updater = True
                    result = self.controller.restore(request["plan_id"], confirm_source_stopped=request["confirm_source_stopped"], overwrite=request["overwrite"])
                    self.refresh_token()
                    self.publish(phase=result["state"], active=False, plan=None,
                                 recovery_required=result.get("recovery_required") is True,
                                 error=result.get("error"), result={"kind": "restore"})
        except Exception as error:
            from .daemon import _pending_recovery
            self.fail(error, recovery=_pending_recovery(self.private))
        finally:
            if restart_updater:
                try:
                    self.refresh_token()
                    if not self.state.get("recovery_required"):
                        self._updater_start()
                except Exception:
                    self.publish(error="updater_restart_failed")

    def valid(self, value, identifier):
        if not isinstance(value, dict) or value.get("id") != identifier or not UUID.fullmatch(identifier):
            return False
        stamp = value.get("created")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not -5 <= time.time() - stamp <= 120:
            return False
        action = value.get("action")
        keys = {"id", "action", "created"}
        if action == "export":
            keys |= {"secret", "destination"}
            valid = value.get("destination") in {"server", "download"}
        elif action == "inspect":
            keys |= {"secret", "upload_id"}
            valid = isinstance(value.get("upload_id"), str) and bool(ID.fullmatch(value["upload_id"]))
        elif action == "restore":
            keys |= {"plan_id", "confirm_source_stopped", "overwrite"}
            valid = isinstance(value.get("plan_id"), str) and bool(ID.fullmatch(value["plan_id"])) and value.get("confirm_source_stopped") is True and type(value.get("overwrite")) is bool
        else:
            return False
        return set(value) == keys and valid and ("secret" not in keys or isinstance(value.get("secret"), str) and len(value["secret"]) <= 4096)

    def run_once(self):
        stop = self.private / "stop.json"
        if stop.exists():
            try:
                request = verify(read_json(stop, 4096), self.token)
                accepted = not self.state.get("active") and not (self.worker and self.worker.is_alive())
                if set(request) == {"id", "created"} and UUID.fullmatch(request["id"]) and -5 <= time.time() - request["created"] <= 120:
                    atomic_json(self.private / "stop-result.json", sign({"id": request["id"], "accepted": accepted}, self.token))
                    self.exit_requested = accepted
            except (OSError, ValueError, TypeError, KeyError):
                pass
            finally:
                safe_path(stop).unlink(missing_ok=True)
        for path in sorted(self.ipc.glob("request-*.json"))[:64]:
            try:
                identifier = path.name[8:-5]
                request = verify(read_json(path, 8192), self.token)
                if not self.valid(request, identifier) or identifier in self.recent:
                    continue
                self.recent = {k: v for k, v in self.recent.items() if type(v) in (int, float) and time.time() - v < 600}
                self.recent[identifier] = time.time()
                atomic_json(self.private / "recent.json", self.recent)
                if self.state.get("active") or self.state.get("recovery_required") or self.worker and self.worker.is_alive():
                    continue
                phase = {"export": "exporting", "inspect": "inspecting", "restore": "restoring"}[request["action"]]
                self.publish(phase=phase, active=True, error=None, result=None, request_id=identifier)
                self.worker = threading.Thread(target=self.job, args=(request,), daemon=True)
                self.worker.start()
            except (OSError, ValueError, TypeError):
                pass
            finally:
                safe_path(path).unlink(missing_ok=True)

    def serve(self):
        with FileLock(self.private / "daemon.lock"):
            self.publish()
            self.initialize()
            cleanup_at = 0
            while not self.exit_requested:
                self.run_once()
                if self.controller is not None and time.monotonic() >= cleanup_at and not self.state.get("active"):
                    try:
                        with FileLock(self.operation_lock):
                            self.controller.cleanup()
                            for item in (self.ipc / "uploads").glob("*.aqbackup"):
                                if time.time() - safe_path(item).stat().st_mtime > 3600:
                                    item.unlink()
                    except (BlockingIOError, OSError, ValueError):
                        pass
                    cleanup_at = time.monotonic() + 60
                self.publish()
                time.sleep(1)
            self.publish(heartbeat=0, ready=False)


def running(private):
    try:
        with FileLock(private / "daemon.lock"):
            return False
    except BlockingIOError:
        return True


def lifecycle(root, project, context, runtime_root=None, layout="bundle", *, stop=False):
    root, runtime, ipc, private, token = prepare(root, project, context, runtime_root, layout)
    if stop:
        if not running(private):
            return
        current = status(ipc, token)
        if current is None or current.get("active"):
            raise ValueError("A backup or restore is running. Wait before stopping the installation.")
        identifier = str(uuid.uuid4())
        atomic_json(private / "stop.json", sign({"id": identifier, "created": time.time()}, token))
        for _ in range(35):
            if not running(private):
                return
            time.sleep(1)
        raise ValueError("The backup service could not stop safely.")
    instance = None
    if not running(private):
        instance = secrets.token_hex(16)
        command = [sys.executable, str(root / "scripts/run_bundle_backup.py"), "--serve", "--root", str(root),
                   "--runtime-root", str(runtime), "--project", project, "--context", context, "--layout", layout, "--instance", instance]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, shell=False, **options)
    for _ in range(900):
        token = parse_env(safe_path(runtime / ".env"))["QWENPAW_RUNTIME_INTERNAL_TOKEN"]
        current = status(ipc, token)
        if current and current.get("ready") and not current.get("active") and (instance is None or current.get("instance") == instance):
            if current.get("recovery_required"):
                raise ValueError("Restore recovery needs attention. Preserve private backup storage.")
            return
        time.sleep(1)
    raise ValueError("The backup service did not become ready.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local encrypted backup helper")
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ("ensure", "serve", "stop"):
        actions.add_argument("--" + action, action="store_true")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--layout", choices=("bundle", "linux"), default="bundle")
    parser.add_argument("--instance", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.serve:
            if args.instance is not None and not ID.fullmatch(args.instance):
                raise ValueError("Invalid helper instance.")
            BackupDaemon(args.root, args.project, args.context, args.runtime_root, args.layout, instance=args.instance).serve()
        else:
            lifecycle(args.root, args.project, args.context, args.runtime_root, args.layout, stop=args.stop)
        return 0
    except Exception:
        print("Backup helper could not complete safely. Check installation mode and private backup diagnostics.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
