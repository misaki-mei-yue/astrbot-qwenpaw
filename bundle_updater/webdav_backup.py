"""Host-only encrypted WebDAV configuration, daily scheduling and copy journal.

The caller owns the bundle operation lock. This module never accesses Docker or
places credentials in public IPC state. Remote deletion is limited to receipts
created by this installation for the unchanged target identity.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .common import atomic_json, read_json, safe_path

ID = re.compile(r"^[0-9a-f]{32}$")
AAD = b"astrbot-qwenpaw-webdav-host-v1:"
TRANSIENT = {"timeout", "connection", "remote_unavailable"}
REMOTE_ERRORS = TRANSIENT | {"authentication", "permission", "redirect", "tls", "remote_invalid",
    "remote_size", "path_invalid", "url_invalid", "credentials_invalid", "source_invalid", "conflict",
    "storage_full", "unsupported", "cleanup_failed"}


class WebDAVBackupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise WebDAVBackupError(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def _bounded_text(value, limit, required=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit or (required and not value):
        _fail("webdav_config_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("webdav_config_invalid")
    return value


class WebDAVBackups:
    def __init__(self, private: Path, controller, report=None, client_factory=None, clock=time.time):
        self.private = safe_path(Path(private) / "webdav")
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private.chmod(0o700)
        self.controller, self.report, self.clock = controller, report, clock
        if client_factory is None:
            from .webdav_transport import WebDAVClient
            client_factory = WebDAVClient
        self.client_factory = client_factory
        self._sleep = time.sleep
        self.key = self._key()
        self._settings_pending = False
        self._recover_settings()
        self.config = self._load("config", None)
        if self.config is None:
            self.config = {"enabled": False, "time": "03:00", "utc_offset_minutes": 480, "keep_last": 7,
                           "password": "", "destinations": [], "namespace": "astrbot-qwenpaw/" + uuid.uuid4().hex}
            self._save("config", self.config)
        self.state = self._load("state", {"next_run": None, "history": [], "receipts": {}, "local": [], "test_result": None})
        self._validate_loaded()
        # A claimed schedule is advanced before work. A daemon restart may offer
        # retry of the existing archive but must never silently export twice.
        if self.state["history"] and self.state["history"][0]["status"] == "running":
            run = self.state["history"][0]
            run.update(status="interrupted", finished_at=self.clock(), error="webdav_interrupted")
            for target in run["targets"]:
                if target["status"] in {"pending", "uploading"}:
                    target.update(status="failed", error="webdav_interrupted")
            self._persist()

    def _key(self):
        path = safe_path(self.private / "key")
        if not path.exists():
            # Losing a key must not silently replace it and obscure existing data.
            if any((self.private / (name + ".json")).exists() for name in ("config", "state", "settings-update")):
                _fail("webdav_key_missing")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                path.chmod(0o600)
            except FileExistsError:
                pass
        info = path.stat()
        if not path.is_file() or info.st_nlink != 1 or info.st_size != 32:
            _fail("webdav_key_invalid")
        return path.read_bytes()

    def _load(self, name, default):
        path = self.private / (name + ".json")
        if not path.exists():
            return copy.deepcopy(default)
        try:
            envelope = read_json(path, limit=4 * 1024 * 1024)
            if set(envelope) != {"format", "sealed"} or envelope["format"] != 1:
                raise ValueError()
            raw = base64.b64decode(envelope["sealed"], validate=True)
            return json.loads(AESGCM(self.key).decrypt(raw[:12], raw[12:], AAD + name.encode("ascii")))
        except Exception:
            _fail("webdav_state_invalid")

    def _save(self, name, value):
        nonce = os.urandom(12)
        raw = nonce + AESGCM(self.key).encrypt(nonce, _canonical(value), AAD + name.encode("ascii"))
        atomic_json(self.private / (name + ".json"), {"format": 1, "sealed": base64.b64encode(raw).decode("ascii")})

    def _validate_loaded(self, config=None, state=None):
        config = self.config if config is None else config
        state = self.state if state is None else state
        try:
            if (not isinstance(config, dict) or set(config) != {"enabled", "time", "utc_offset_minutes", "keep_last", "password", "destinations", "namespace"}
                    or not re.fullmatch(r"astrbot-qwenpaw/[0-9a-f]{32}", config["namespace"])):
                raise ValueError()
            if not isinstance(state, dict) or set(state) != {"next_run", "history", "receipts", "local", "test_result"}:
                raise ValueError()
            if not isinstance(state["history"], list) or len(state["history"]) > 20:
                raise ValueError()
            if not isinstance(state["receipts"], dict) or not isinstance(state["local"], list):
                raise ValueError()
            if len(state["local"]) > 10000:
                raise ValueError()
            next_run = state["next_run"]
            if (next_run is not None and (type(next_run) not in (int, float) or not math.isfinite(next_run) or next_run < 0)
                    or config["enabled"] != (next_run is not None)):
                raise ValueError()
            if self._normalized(config, config) != config:
                raise ValueError()
        except Exception:
            _fail("webdav_state_invalid")

    def _recover_settings(self):
        """Finish a validated save intent before reading or changing live state."""
        path = safe_path(self.private / "settings-update.json")
        if not path.exists():
            self._settings_pending = False
            return
        self._settings_pending = True
        intent = self._load("settings-update", None)
        if (not isinstance(intent, dict) or set(intent) != {"format", "config", "state"}
                or type(intent["format"]) is not int or intent["format"] != 1
                or not isinstance(intent["config"], dict) or not isinstance(intent["state"], dict)):
            _fail("webdav_state_invalid")
        self._validate_loaded(intent["config"], intent["state"])
        try:
            self._save("state", intent["state"])
            self._save("config", intent["config"])
            path.unlink()
        except Exception:
            _fail("webdav_settings_pending")
        self.config, self.state = intent["config"], intent["state"]
        self._settings_pending = False

    def _stable(self):
        if self._settings_pending:
            _fail("webdav_settings_pending")

    def _persist(self):
        self._stable()
        self._save("state", self.state)

    def _emit(self, phase):
        self._persist()
        if self.report:
            self.report({"phase": phase})

    @staticmethod
    def _identity(target):
        return hashlib.sha256(_canonical([target["url"], target["username"]])).hexdigest()

    def _normalized(self, value, previous):
        if not isinstance(value, dict):
            _fail("webdav_config_invalid")
        enabled, at = value.get("enabled", False), value.get("time", "03:00")
        offset, keep = value.get("utc_offset_minutes", 480), value.get("keep_last", 7)
        if type(enabled) is not bool or not isinstance(at, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at):
            _fail("webdav_config_invalid")
        if type(offset) is not int or not -720 <= offset <= 840 or type(keep) is not int or not 1 <= keep <= 100:
            _fail("webdav_config_invalid")
        password = value.get("password", "")
        if not isinstance(password, str):
            _fail("webdav_config_invalid")
        password = password or previous.get("password", "")
        if password and (len(password) < 12 or len(password.encode("utf-8")) > 1024):
            _fail("invalid_password")
        rows = value.get("destinations", [])
        if not isinstance(rows, list) or len(rows) > 10:
            _fail("webdav_config_invalid")
        old = {row["id"]: row for row in previous.get("destinations", [])}
        targets, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                _fail("webdav_config_invalid")
            target_id = row.get("id") or uuid.uuid4().hex
            if not isinstance(target_id, str) or not ID.fullmatch(target_id) or target_id in seen:
                _fail("webdav_config_invalid")
            seen.add(target_id)
            name = _bounded_text(row.get("name", ""), 120, True)
            url = _bounded_text(row.get("url", ""), 2048, True)
            username = _bounded_text(row.get("username", ""), 512, True)
            secret = _bounded_text(row.get("password", ""), 2048)
            try:
                from .webdav_transport import validate_target_url
                url = validate_target_url(url)
                if len(url.encode("utf-8")) > 2048:
                    raise ValueError()
            except ValueError:
                _fail("webdav_url_invalid")
            if ":" in username:
                _fail("webdav_config_invalid")
            active = row.get("enabled", True)
            if type(active) is not bool:
                _fail("webdav_config_invalid")
            target = {"id": target_id, "name": name, "url": url, "username": username, "enabled": active, "password": secret}
            prior = old.get(target_id)
            if not secret and prior and self._identity(prior) == self._identity(target):
                target["password"] = prior["password"]
            if not target["password"]:
                _fail("webdav_credentials_required")
            targets.append(target)
        if enabled and (not password or not any(row["enabled"] for row in targets)):
            _fail("webdav_setup_required")
        return {"enabled": enabled, "time": at, "utc_offset_minutes": offset, "keep_last": keep,
                "password": password, "destinations": targets, "namespace": previous["namespace"]}

    def save(self, value):
        if self._settings_pending:
            self._recover_settings()
        updated = self._normalized(value, self.config)
        state = copy.deepcopy(self.state)
        previous = self.config
        old = {row["id"]: row for row in previous["destinations"]}
        # Clear receipts and retry identities before changing the saved target.
        # A crash here only leaves harmless old remote files; it cannot cause
        # deletion in a different account on restart.
        for row in updated["destinations"]:
            if row["id"] in old and self._identity(row) != self._identity(old[row["id"]]):
                state["receipts"].pop(row["id"], None)
        remaining = {row["id"] for row in updated["destinations"]}
        state["receipts"] = {key: val for key, val in state["receipts"].items() if key in remaining}
        changed_schedule = any(updated[key] != previous[key] for key in ("enabled", "time", "utc_offset_minutes"))
        if not updated["enabled"]:
            state["next_run"] = None
        elif changed_schedule or state["next_run"] is None:
            state["next_run"] = self._next(updated)
        state["test_result"] = None
        self._validate_loaded(updated, state)
        self._save("settings-update", {"format": 1, "config": updated, "state": state})
        self._settings_pending = True
        self._recover_settings()

    def pause(self):
        """Explicitly disable the schedule after an imported restore."""
        if self._settings_pending:
            self._recover_settings()
        updated = copy.deepcopy(self.config)
        updated["enabled"] = False
        self.save(updated)

    def _next(self, config=None):
        config = config or self.config
        tz = timezone(timedelta(minutes=config["utc_offset_minutes"]))
        now = datetime.fromtimestamp(self.clock(), tz)
        hour, minute = map(int, config["time"].split(":"))
        result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if result <= now:
            result += timedelta(days=1)
        return result.timestamp()

    def due(self):
        return bool(not self._settings_pending and self.config["enabled"] and self.state["next_run"] is not None and self.clock() >= self.state["next_run"])

    @staticmethod
    def _public_run(run):
        result = {key: copy.deepcopy(value) for key, value in run.items() if key not in {"identities"}}
        return result

    def public(self):
        config = {key: copy.deepcopy(value) for key, value in self.config.items() if key not in {"password", "destinations"}}
        config["password_set"] = bool(self.config["password"])
        config["destinations"] = [{**{key: value for key, value in row.items() if key != "password"},
                                   "password_set": bool(row["password"])} for row in self.config["destinations"]]
        history = [self._public_run(row) for row in self.state["history"]]
        return {"config": config, "next_run": self.state["next_run"], "last_run": history[0] if history else None,
                "test_result": copy.deepcopy(self.state["test_result"]), "history": history}

    def _target(self, target_id):
        row = next((row for row in self.config["destinations"] if row["id"] == target_id), None)
        if row is None:
            _fail("webdav_target_unknown")
        return row

    @staticmethod
    def _error(error):
        code = getattr(error, "code", "")
        return code if code in REMOTE_ERRORS else "webdav_operation_failed"

    def test(self, target_id):
        self._stable()
        target = self._target(target_id)
        try:
            self.client_factory(target).probe(self.config["namespace"])
            result = {"target_id": target_id, "ok": True}
        except Exception as error:
            result = {"target_id": target_id, "ok": False, "error": self._error(error)}
        self.state["test_result"] = result
        self._persist()
        return copy.deepcopy(result)

    def _archive(self, archive_id):
        try:
            return self.controller.get_archive(archive_id)
        except Exception:
            _fail("webdav_archive_unavailable")

    def run(self, trigger="manual"):
        self._stable()
        if trigger not in {"manual", "scheduled"}:
            _fail("webdav_trigger_invalid")
        if trigger == "scheduled" and not self.due():
            _fail("webdav_not_due")
        targets = [row for row in self.config["destinations"] if row["enabled"]]
        if not targets or not self.config["password"]:
            _fail("webdav_setup_required")
        run = {"id": uuid.uuid4().hex, "trigger": trigger, "status": "running", "started_at": self.clock(),
               "finished_at": None, "backup_id": None, "identities": {row["id"]: self._identity(row) for row in targets},
               "targets": [{"id": row["id"], "name": row["name"], "status": "pending", "attempts": 0} for row in targets]}
        self.state["history"].insert(0, run)
        del self.state["history"][20:]
        if self.due():
            self.state["next_run"] = self._next()
        self._emit("exporting")
        try:
            exported = self.controller.export(self.config["password"], "server")
            archive_id = exported["id"]
            if not isinstance(archive_id, str) or not ID.fullmatch(archive_id):
                raise ValueError()
            run["backup_id"] = archive_id
            self.state["local"].append({"id": archive_id, "complete": False})
            self._persist()
            path = self._archive(archive_id)
        except Exception:
            run.update(status="failed", finished_at=self.clock(), error="webdav_export_failed")
            for row in run["targets"]:
                row.update(status="failed", error="webdav_export_failed")
            self._emit("failed")
            return self._public_run(run)
        return self._uploads(run, path, targets)

    def retry(self, run_id):
        self._stable()
        if not self.state["history"] or self.state["history"][0]["id"] != run_id:
            _fail("webdav_run_unknown")
        run = self.state["history"][0]
        if run["status"] not in {"failed", "partial", "interrupted"} or not run["backup_id"]:
            _fail("webdav_retry_unavailable")
        path = self._archive(run["backup_id"])
        targets = []
        for result in run["targets"]:
            if result["status"] == "completed":
                continue
            target = self._target(result["id"])
            if self._identity(target) != run["identities"][target["id"]]:
                _fail("webdav_target_changed")
            if target["enabled"]:
                targets.append(target)
        if not targets:
            _fail("webdav_retry_unavailable")
        run.update(status="running", finished_at=None)
        run.pop("error", None)
        self._emit("uploading")
        return self._uploads(run, path, targets)

    def _uploads(self, run, path, targets):
        filename = run["backup_id"] + ".aqbackup"
        for target in targets:
            result = next(row for row in run["targets"] if row["id"] == target["id"])
            result.update(status="uploading", attempts=0)
            result.pop("error", None)
            self._emit("uploading")
            for attempt in range(3):
                result["attempts"] = attempt + 1
                try:
                    client = self.client_factory(target)
                    client.upload(path, self.config["namespace"], filename)
                    result["status"] = "completed"
                    receipts = self.state["receipts"].setdefault(target["id"], [])
                    if filename not in receipts:
                        receipts.append(filename)
                    self._persist()
                    self._remote_retention(client, target["id"])
                    break
                except Exception as error:
                    code = self._error(error)
                    result.update(status="failed", error=code)
                    self._persist()
                    if code not in TRANSIENT or attempt == 2:
                        break
                    self._sleep(2 ** attempt)
        statuses = [row["status"] for row in run["targets"]]
        complete = all(status == "completed" for status in statuses)
        run.update(status="completed" if complete else "partial" if "completed" in statuses else "failed", finished_at=self.clock())
        owned = next((row for row in self.state["local"] if row["id"] == run["backup_id"]), None)
        if owned is not None:
            owned["complete"] = complete
        self._persist()
        self._local_retention()
        self._emit(run["status"])
        return self._public_run(run)

    def _remote_retention(self, client, target_id):
        receipts = self.state["receipts"][target_id]
        while len(receipts) > self.config["keep_last"]:
            filename = receipts[0]
            if not re.fullmatch(r"[0-9a-f]{32}\.aqbackup", filename):
                _fail("webdav_state_invalid")
            try:
                client.delete(self.config["namespace"], filename)
            except Exception:
                # Retention failure must not turn a verified upload into failure.
                # Keep the receipt so a later successful upload can try again.
                return
            receipts.pop(0)
            self._persist()

    def _local_retention(self):
        # Called only after the new archive passed get_archive integrity checks.
        # Keep both a recent successfully replicated set and a recent unreplicated
        # set; a long outage must not permanently block future backup creation.
        completed = [row for row in self.state["local"] if row["complete"]]
        pending = [row for row in self.state["local"] if not row["complete"]]
        expired = completed[:-self.config["keep_last"]] + pending[:-self.config["keep_last"]]
        for row in expired:
            try:
                self.controller.delete_archive(row["id"])
            except Exception:
                continue
            self.state["local"].remove(row)
            self._persist()
