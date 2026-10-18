"""First-start AstrBot channel bootstrap via QwenPaw 2.2.1's native API.

The launcher pipes this source into the running qwenpaw container's Python.
Only a missing or empty channel config is enabled. Existing disabled channels
remain disabled. Credentials come from process env and are never printed.
No model calls, delivery, approval decisions, or local state-file writes.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


BASE = "http://127.0.0.1:8088"
CHANNEL_PATH = "/api/agents/default/config/channels/astrbot"
TIMEOUT_SECONDS = 8
READY_TIMEOUT_SECONDS = 60
RELOAD_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request_json(path, token, method="GET", payload=None):
    encoded = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=encoded, method=method,
        headers={"X-QwenPaw-Runtime-Token": token, "Content-Type": "application/json"},
    )
    try:
        with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return status, None
        try:
            return status, json.loads(raw)
        except (ValueError, UnicodeError):
            return status, None
    except urllib.error.HTTPError as error:
        status = int(error.code)
        error.close()
        return status, None
    except Exception:
        return 0, None


def matches_existing_keys(expected, actual):
    """Native response models can add default fields not in the PUT body."""
    return isinstance(actual, dict) and all(
        key in actual and actual[key] == value for key, value in expected.items()
    )


def bootstrap():
    result = {"passed": False, "changed": False, "skipped": False,
              "existing_config_preserved": False, "channel_healthy": False,
              "statuses": {}}
    token = os.environ.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", "")
    if len(token) < 32 or os.environ.get("QWENPAW_AGENT_ID", "default") != "default":
        result["precondition_failed"] = True
        return result

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        status, health = request_json("/api/healthz", token)
        result["statuses"]["startup_health"] = status
        if status == 200 and isinstance(health, dict) and health.get("status") == "ok" and isinstance(health.get("agents_loaded"), list) and "default" in health["agents_loaded"]:
            break
        if status in (401, 403) or time.monotonic() >= deadline:
            return result
        time.sleep(1)

    status, version = request_json("/api/version", token)
    result["statuses"]["version"] = status
    if status != 200 or not isinstance(version, dict) or version.get("version") != "2.2.1":
        result["unsupported_version"] = True
        return result

    status, types = request_json("/api/agents/default/config/channels/types", token)
    result["statuses"]["channel_types"] = status
    if status != 200 or not isinstance(types, list) or "astrbot" not in types:
        return result

    status, existing = request_json(CHANNEL_PATH, token)
    result["statuses"]["config_read"] = status
    if status == 404:
        existing = {}
    elif status != 200 or not isinstance(existing, dict):
        return result

    desired = existing
    if not existing:
        # Recheck before replacing an absent/empty config. There is no native
        # compare-and-swap API; first-start launcher runs this before its UI.
        status, current = request_json(CHANNEL_PATH, token)
        result["statuses"]["config_recheck"] = status
        if status == 404:
            current = {}
        elif status != 200 or not isinstance(current, dict):
            return result
        if current:
            existing = current
            desired = current
        else:
            desired = {"enabled": True}
            status, saved = request_json(CHANNEL_PATH, token, "PUT", desired)
            result["statuses"]["config_write"] = status
            result["changed"] = status == 200
            if status != 200 or not matches_existing_keys(desired, saved):
                # Do not automatically retry a PUT with an uncertain outcome.
                return result

    result["existing_config_preserved"] = True
    if existing and existing.get("enabled") is not True:
        # Existing explicit disabled/custom configuration belongs to the user.
        result.update(passed=True, skipped=True, existing_disabled_preserved=True)
        return result

    deadline = time.monotonic() + RELOAD_TIMEOUT_SECONDS
    while True:
        status, health = request_json(CHANNEL_PATH + "/health", token)
        result["statuses"]["channel_health"] = status
        if status == 200 and isinstance(health, dict) and health.get("channel") == "astrbot" and health.get("status") == "healthy":
            result["channel_healthy"] = True
            break
        if status in (401, 403) or time.monotonic() >= deadline:
            return result
        time.sleep(1)

    status, final = request_json(CHANNEL_PATH, token)
    result["statuses"]["config_final"] = status
    result["existing_config_preserved"] = status == 200 and matches_existing_keys(desired, final)
    result["passed"] = result["existing_config_preserved"] and result["channel_healthy"]
    return result


def main():
    try:
        result = bootstrap()
    except Exception:
        result = {"passed": False, "script_error": True}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
