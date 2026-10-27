"""Public WebDAV status projection. Never return stored credential fields."""
import math
import re

from bundle_updater.webdav_protocol import IDENTIFIER


def _string(value, limit=120):
    return value[:limit] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else ""


def _identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else ""


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 10**11 else None


def _error(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,64}", value) else None


def _run(value):
    if not isinstance(value, dict) or not _identifier(value.get("id")):
        return None
    result = {"id": value["id"], "trigger": "scheduled" if value.get("trigger") == "scheduled" else "manual",
              "status": value.get("status") if value.get("status") in {"running", "completed", "partial", "failed", "interrupted"} else "failed",
              "started_at": _stamp(value.get("started_at")), "finished_at": _stamp(value.get("finished_at")),
              "backup_id": _identifier(value.get("backup_id")), "error": _error(value.get("error")), "targets": []}
    targets = value.get("targets", [])
    for item in targets[:10] if isinstance(targets, list) else []:
        if not isinstance(item, dict) or not _identifier(item.get("id")):
            continue
        attempts = item.get("attempts")
        result["targets"].append({"id": item["id"], "name": _string(item.get("name")),
            "status": item.get("status") if item.get("status") in {"pending", "uploading", "completed", "failed"} else "failed",
            "error": _error(item.get("error")), "attempts": attempts if type(attempts) is int and 0 <= attempts <= 100 else 0})
    return result


def public_status(value):
    if not isinstance(value, dict) or not isinstance(value.get("config"), dict):
        return None
    config = value["config"]
    offset, keep = config.get("utc_offset_minutes"), config.get("keep_last")
    result = {"config": {"enabled": config.get("enabled") is True, "time": _string(config.get("time"), 5),
        "utc_offset_minutes": offset if type(offset) is int and -720 <= offset <= 840 else 0,
        "keep_last": keep if type(keep) is int and 1 <= keep <= 100 else 7,
        "password_set": config.get("password_set") is True, "destinations": [],
        "namespace": _string(config.get("namespace"), 100)},
        "next_run": _stamp(value.get("next_run")), "last_run": _run(value.get("last_run")),
        "test_result": None, "history": []}
    targets = config.get("destinations", [])
    for target in targets[:10] if isinstance(targets, list) else []:
        if not isinstance(target, dict) or not _identifier(target.get("id")):
            continue
        result["config"]["destinations"].append({"id": target["id"], "name": _string(target.get("name")),
            "url": _string(target.get("url"), 2048), "username": _string(target.get("username"), 512),
            "enabled": target.get("enabled") is True, "password_set": target.get("password_set") is True})
    test = value.get("test_result")
    if isinstance(test, dict) and _identifier(test.get("target_id")):
        result["test_result"] = {"target_id": test["target_id"], "ok": test.get("ok") is True, "error": _error(test.get("error"))}
    history = value.get("history", [])
    result["history"] = [r for r in (_run(row) for row in (history[:20] if isinstance(history, list) else [])) if r]
    return result
