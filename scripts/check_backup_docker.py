"""Opt-in isolated Docker backup smoke test; never uses personal application data.

Run with --run --work-root <dedicated-empty-directory>. It starts synthetic
backends on localhost:18081/18082, exercises the real gateway and controller,
then removes only its own containers. Synthetic files remain for inspection.
The test does NOT validate real QQ login, live database migrations or power loss.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bundle_updater.backup_controller import BackupController, BackupError, CORE, STATE_ROOTS
from bundle_updater.common import atomic_json, safe_path, sign
from deploy.prepare import env_value, parse_env

IMAGE = "python:3.12.14-slim"
PASSWORD = "isolated synthetic backup fixture"
BACKEND = r'''import http.server,json,os,threading,time
role=os.environ['ROLE']
ports={'astrbot':[6185,9186],'qwenpaw':[8088],'napcat':[6099]}[role]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  if role=='qwenpaw' and self.headers.get('X-QwenPaw-Runtime-Token')!=os.environ['QWENPAW_RUNTIME_INTERNAL_TOKEN']:
   self.send_response(401); self.end_headers(); return
  if self.path=='/health':
   if self.headers.get('Authorization')!='Bearer '+os.environ['BRIDGE_TOKEN']:
    self.send_response(401); self.end_headers(); return
   body=json.dumps({'ready':True}).encode()
  elif self.path=='/api/healthz': body=json.dumps({'status':'ok','agents_loaded':['default']}).encode()
  else: body=b'<!doctype html><html>synthetic native service</html>'
  self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
for port in ports:
 threading.Thread(target=http.server.ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever,daemon=True).start()
while True: time.sleep(60)
'''


def run(command, *, cwd=ROOT, capture=True, check=True, timeout=600):
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("DOCKER_", "COMPOSE_"))}
    environment.pop("PROJECT_SOURCE", None)
    environment.pop("INSTALL_ROOT", None)
    result = subprocess.run(command, cwd=cwd, env=environment, shell=False, check=check, timeout=timeout,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None,
                            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    return result.stdout.decode("utf-8").strip() if capture and result.stdout else ""


def request(port, path, *, body=None, headers=None, host="localhost", expected=200):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request("http://localhost:" + str(port) + path, data=body,
                                 headers={"Host": host + ":" + str(port), **(headers or {})})
    try:
        with opener.open(req, timeout=15) as response:
            code, payload = response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        code, payload = error.code, error.read(65536)
    if code != expected:
        raise AssertionError("Unexpected fixture HTTP status: " + str(code) + " (expected " + str(expected) + ")")
    return payload


class SmokeController(BackupController):
    def __init__(self, *args, port, **kwargs):
        self.port = port
        self.health_failures = 0
        super().__init__(*args, **kwargs)

    def _wait_ready(self, timeout=120):
        if self.health_failures:
            self.health_failures -= 1
            raise BackupError("health_check_failed")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if json.loads(request(self.port, "/bundle-health"))["status"] == "ready":
                    return
            except (OSError, ValueError, AssertionError):
                pass
            time.sleep(1)
        raise AssertionError("Synthetic services did not become ready.")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def fixture(root, port, context, gateway_image):
    root.mkdir()
    runtime = root / "runtime"
    for suffix in STATE_ROOTS:
        (runtime / "state" / suffix).mkdir(parents=True, mode=0o700, exist_ok=True)
    for path in (runtime / "updates/ipc", runtime / "backups/ipc", runtime / "backups/ipc/transfers", runtime / "backups/ipc/uploads"):
        path.mkdir(parents=True, mode=0o1777, exist_ok=True)
        path.chmod(0o1777)
    for path in (root / "astrbot_plugin_qwenpaw_bridge", root / "qwenpaw_plugin_astrbot_bridge"):
        path.mkdir()
    project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:10]
    config = {"PROJECT_SOURCE": root.as_posix(), "INSTALL_ROOT": runtime.as_posix(), "COMPOSE_PROJECT_NAME": project,
              "BRIDGE_NETWORK": project + "-network", "QWENPAW_AGENT_ID": "default", "QWENPAW_AUTH_ENABLED": "false",
              "QWENPAW_RUNTIME_INTERNAL_TOKEN": secrets.token_urlsafe(32), "ONEBOT_TOKEN": secrets.token_urlsafe(32),
              "BRIDGE_TOKEN": secrets.token_urlsafe(32), "QWENPAW_HTTP_PROXY": "http://synthetic-" + root.name + ":10808"}
    write(runtime / ".env", "\n".join(k + "=" + env_value(v) for k, v in config.items()) + "\n")
    write(runtime / ".combined-setup.json", '{"format":1,"mode":"fresh"}\n')
    write(root / "bundle-version.json", (ROOT / "bundle-version.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(runtime / "state/astrbot/chat.db")) as connection:
        connection.execute("create table chat (text text)")
        connection.execute("insert into chat values (?)", (root.name + " synthetic chat",))
        connection.commit()
    write(runtime / "state/qwenpaw/working/memory.md", root.name + " synthetic memory\n")
    write(runtime / "state/qwenpaw/secret/secret.json", '{"provider":"synthetic-only-no-real-key"}\n')
    write(runtime / "state/napcat/qq/login", root.name + " synthetic login\n")
    controller = SmokeController(root, project, context, runtime / "backups/private", lambda _: None, port=port)
    services = {}
    for role in (*CORE, "gateway"):
        mounts = [{"type": "bind", "source": path.as_posix(), "target": target, "read_only": not rw}
                  for target, (path, rw) in controller._expected_mounts()[role].items()]
        env = {"ROLE": role, "QWENPAW_RUNTIME_INTERNAL_TOKEN": "${QWENPAW_RUNTIME_INTERNAL_TOKEN}", "BRIDGE_TOKEN": "${BRIDGE_TOKEN}"}
        row = {"image": gateway_image if role == "gateway" else IMAGE, "init": True,
               "environment": env, "volumes": mounts, "restart": "no"}
        if role == "gateway":
            env.update(BUNDLE_PORT=str(port), BUNDLE_UPDATE_IPC="/bundle-updates", BUNDLE_BACKUP_IPC="/bundle-backups")
            row.update(ports=["127.0.0.1:" + str(port) + ":" + str(port)], read_only=True, tmpfs=["/tmp"], cap_drop=["ALL"])
        else:
            row["command"] = ["python", "-c", BACKEND]
        services[role] = row
    write(root / "deploy/compose.bundle.yaml", json.dumps({"services": services}, indent=2))
    return controller


def publish_fixture_status(controller, backups=None):
    token = parse_env(controller.runtime / ".env")["QWENPAW_RUNTIME_INTERNAL_TOKEN"]
    value = {"phase": "idle", "ready": True, "active": False, "heartbeat": time.time(), "backups": backups or []}
    atomic_json(controller.runtime / "backups/ipc/status.json", sign(value, token), 0o644)


def verify_csrf(controller):
    publish_fixture_status(controller)
    state = json.loads(request(controller.port, "/bundle-backup"))
    assert state["enabled"] is True
    assert isinstance(state.get("csrf_token"), str)
    request(controller.port, "/bundle-backup/export", body=b'{}', headers={"Content-Type": "application/json"}, expected=403)
    request(controller.port, "/bundle-backup/export", body=b'{}',
            headers={"Content-Type": "application/json", "Origin": "http://outside.invalid", "X-Bundle-Backup-Token": state["csrf_token"]}, expected=403)
    request(controller.port, "/bundle-backup", host="qwenpaw.localhost", expected=403)
    return state["csrf_token"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Confirm isolated Docker container creation")
    parser.add_argument("--work-root", required=True, type=Path, help="Dedicated empty directory; synthetic files are retained")
    parser.add_argument("--gateway-image", help="Existing image built from this checkout's bundle_gateway/Dockerfile")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("Pass --run to opt in; this test creates its own containers.")
    work = safe_path(Path(os.path.abspath(args.work_root)))
    if work == Path(work.anchor) or (work.exists() and any(work.iterdir())):
        parser.error("--work-root must be a dedicated empty directory.")
    work.mkdir(parents=True, exist_ok=True)
    context = run(["docker", "context", "show"])
    from bundle_updater.daemon import _local_context
    _local_context(context)
    if run(["docker", "--context", context, "info", "--format", "{{.OSType}}"] ) != "linux":
        raise ValueError("A local Linux Docker engine is required.")
    run(["docker", "--context", context, "image", "inspect", IMAGE])
    owned_image = args.gateway_image is None
    image = args.gateway_image or "bot-combined-backup-smoke-gateway:" + uuid.uuid4().hex
    controllers = []
    try:
        if owned_image:
            run(["docker", "--context", context, "build", "--tag", image, "--file", str(ROOT / "bundle_gateway/Dockerfile"), str(ROOT)], capture=False, timeout=1800)
        source = fixture(work / "source", 18081, context, image)
        target = fixture(work / "target", 18082, context, image)
        controllers.extend((source, target))
        for controller in controllers:
            controller._compose("up", "-d", "--no-build", "--pull", "never", timeout=180)
            controller._wait_ready()
            controller._deployment()  # Actual inspect/config mount and scope validation.
            verify_csrf(controller)
        exported = source.export(PASSWORD, "download")
        source._wait_ready()
        transfer = source.runtime / "backups/ipc/transfers" / (exported["id"] + ".aqbackup")
        shutil.copyfile(exported["archive_path"], transfer)
        transfer.chmod(0o644)
        publish_fixture_status(source, source.list_backups())
        token = json.loads(request(source.port, "/bundle-backup"))["csrf_token"]
        downloaded = request(source.port, "/bundle-backup/download/" + exported["id"] + "?token=" + token)
        assert hashlib.sha256(downloaded).hexdigest() == exported["sha256"]
        incoming = target.private / "upload.aqbackup"
        incoming.write_bytes(downloaded)
        plan = target.inspect(incoming, PASSWORD)
        assert plan["requires_overwrite"] is True
        old_csrf = verify_csrf(target)
        source._compose("stop", timeout=120)  # Keep the source off during migration.
        result = target.restore(plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        assert result["state"] == "completed", result
        target._wait_ready()
        new_csrf = verify_csrf(target)
        assert new_csrf != old_csrf
        with closing(sqlite3.connect(target.runtime / "state/astrbot/chat.db")) as connection:
            assert connection.execute("select text from chat").fetchone()[0] == "source synthetic chat"
        assert (target.runtime / "state/qwenpaw/working/memory.md").read_text(encoding="utf-8") == "source synthetic memory\n"
        source_env, target_env = parse_env(source.runtime / ".env"), parse_env(target.runtime / ".env")
        for key in ("BRIDGE_TOKEN", "ONEBOT_TOKEN", "QWENPAW_RUNTIME_INTERNAL_TOKEN"):
            assert source_env[key] == target_env[key]
        assert target_env["INSTALL_ROOT"] == target.runtime.as_posix()
        assert target_env["PROJECT_SOURCE"] == target.root.as_posix()
        assert target_env["QWENPAW_HTTP_PROXY"] == "http://synthetic-target:10808"
        # A real container restart failure must restore the pre-import data and
        # exact old image IDs, not only leave a healthy imported application.
        write(target.runtime / "state/qwenpaw/working/memory.md", "target rollback sentinel\n")
        second_plan = target.inspect(incoming, PASSWORD)
        target.health_failures = 1
        rollback = target.restore(second_plan["plan_id"], confirm_source_stopped=True, overwrite=True)
        assert rollback["state"] == "rolled_back" and not rollback["recovery_required"], rollback
        assert (target.runtime / "state/qwenpaw/working/memory.md").read_text(encoding="utf-8") == "target rollback sentinel\n"
        target._wait_ready()
        verify_csrf(target)
        summary = {"result": "passed", "real_docker": True, "encrypted_export_download": True,
                   "different_root_restore": True, "sqlite_and_memory": True, "source_keys_preserved": True,
                   "target_paths_proxy_rebased": True, "gateway_status_csrf_restart": True, "failed_health_rollback": True,
                   "native_backends": "synthetic", "personal_data_used": False}
        write(work / "result.json", json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary))
        return 0
    finally:
        for controller in reversed(controllers):
            try:
                controller._compose("down", "--remove-orphans", "--timeout", "10", timeout=120)
            except Exception:
                print("Fixture cleanup requires attention for project " + controller.project, file=sys.stderr)
            # The production controller retains rollback image tags. This
            # isolated test owns every transaction under its initially empty root.
            for transaction in (controller.private / "transactions").iterdir():
                if len(transaction.name) == 32 and all(c in "0123456789abcdef" for c in transaction.name):
                    for service in controller.services:
                        run(["docker", "--context", context, "image", "rm", "bot-combined-restore-backup:" + transaction.name + "-" + service], check=False)
        if owned_image:
            run(["docker", "--context", context, "image", "rm", image], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
