"""Run genuine QwenPaw 2.2.1 against controlled loopback test boundaries.

Run with an independently installed QwenPaw Python environment. This script
does not install anything, load existing configurations, call a paid model,
or connect to QQ/WeChat. It leaves its fresh test files and logs for review.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

QWENPAW_COMMIT = "cae5773707b26ab2fd00903f84b712387894b256"
SESSION = "ab_" + "a" * 32
USER = "runtime-test-owner"
TURN = "c" * 32
MARKER = "orchid741"
MEMORY_TEXT = "# Runtime validation\nThe remembered test marker is orchid741.\n"

# Instrumentation only constrains the test process's network boundary. All
# QwenPaw, AgentScope, ReMe, hook and tool implementations remain unchanged.
LAUNCHER = '''import faulthandler, ipaddress, runpy, socket
faulthandler.dump_traceback_later(60, repeat=True)
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_getaddrinfo = socket.getaddrinfo
def check(address):
    if isinstance(address, tuple):
        host = address[0]
        if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
            raise OSError('Runtime test forbids non-loopback connections')
def connect(sock, address):
    check(address)
    return original_connect(sock, address)
def connect_ex(sock, address):
    check(address)
    return original_connect_ex(sock, address)
def getaddrinfo(host, *args, **kwargs):
    if host not in ('localhost', None):
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise OSError('Runtime test forbids non-loopback DNS')
    return original_getaddrinfo(host, *args, **kwargs)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
runpy.run_module('qwenpaw', run_name='__main__')
'''


def framework_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py"), key=lambda p: p.relative_to(root).as_posix()):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError("Framework source must not contain symlinks")
        digest.update(path.relative_to(root).as_posix().encode() + b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def tool_outputs(events: list[dict]) -> str:
    return json.dumps([event for event in events if event.get("object") == "message"
                       and event.get("role") == "tool" and event.get("status") == "completed"],
                      ensure_ascii=False)


class RuntimeCheck:
    def __init__(self, artifacts: Path, project: Path, upstream: Path | None):
        self.root, self.project = artifacts, project
        self.port = free_port()
        self.runtime_token, self.bridge_token = uuid4().hex, uuid4().hex
        self.calls, self.deliveries, self.model_requests = [], [], []
        self.approvals = []
        self.proc = None
        self.log = None
        self.working = artifacts / "working"
        self.file_path = self.working / "workspaces/default/runtime-file.txt"
        import qwenpaw
        package_root = Path(qwenpaw.__file__).parent
        self.report = {
            "test_only": True, "qwenpaw_version": importlib.metadata.version("qwenpaw"),
            "expected_source_commit": QWENPAW_COMMIT,
            "installed_framework_sha256": framework_hash(package_root),
            "versions": {name: importlib.metadata.version(name)
                         for name in ("agentscope", "reme-ai", "aiohttp", "fastapi", "playwright")},
            "started_at": datetime.now(timezone.utc).isoformat(),
            "network_boundary": "loopback only; controlled model and callback servers",
            "limitations": ["No real QQ/WeChat transport or paid model calls",
                            "Automatic memory extraction/dream and vector embedding quality are not tested"],
        }
        if self.report["qwenpaw_version"] != "2.2.1":
            raise ValueError("Install the pinned QwenPaw 2.2.1 test environment first")
        if upstream:
            commit = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
            if commit != QWENPAW_COMMIT:
                raise ValueError("Upstream checkout is not the pinned QwenPaw commit")
            source_hash = framework_hash(upstream / "src/qwenpaw")
            if source_hash != self.report["installed_framework_sha256"]:
                raise ValueError("Installed QwenPaw framework differs from the pinned source")
            self.report.update(source_commit=commit, source_verified=True)
        else:
            self.report["source_verified"] = False

    def api(self, path, method="GET", body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode()
        request = Request(f"http://127.0.0.1:{self.port}" + path, data=data, method=method,
                          headers={"X-QwenPaw-Runtime-Token": self.runtime_token,
                                   "Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def handler(self):
        test = self

        class LocalApi(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, data):
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/v1/models":
                    self.reply(200, {"object": "list", "data": [{"id": "local-runtime-test", "object": "model"}]})
                else:
                    self.reply(404, {"error": "Local test API only"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path.startswith("/v1/tools/") or self.path == "/v1/deliver":
                    if self.headers.get("Authorization") != "Bearer " + test.bridge_token:
                        self.reply(401, {"error": "Test bridge token mismatch"}); return
                    if body.get("session_id") != SESSION or body.get("user_id") != USER:
                        self.reply(403, {"error": "Test route mismatch"}); return
                    if self.path.startswith("/v1/tools/") and body.get("turn_id") != TURN:
                        self.reply(409, {"error": "Test turn mismatch"}); return
                    if self.path == "/v1/deliver":
                        test.deliveries.append(body)
                        self.reply(200, {"accepted": True}); return
                    test.calls.append(body)
                    if self.path == "/v1/tools/list":
                        self.reply(200, {"tools": [{"name": "echo", "description": "Local test echo",
                                                   "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}]})
                    else:
                        self.reply(200, {"result": {"text": "ECHO_RUNTIME_OK"}})
                    return
                if self.path != "/v1/chat/completions":
                    self.reply(404, {"error": "Local test API only"}); return
                test.model_requests.append(body)
                messages = body.get("messages", [])
                user_index = next((i for i in range(len(messages)-1, -1, -1)
                                   if messages[i].get("role") == "user"), -1)
                content = messages[user_index].get("content", "") if user_index >= 0 else ""
                if isinstance(content, list):
                    content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
                content = str(content)
                already_called = any(item.get("role") == "tool" for item in messages[user_index+1:])
                function = None
                if not already_called:
                    cases = {
                        "RUNTIME_LIST_TOOLS": ("astrbot_list_tools", {}),
                        "RUNTIME_CALL_TOOL": ("astrbot_call_tool", {"tool_name": "echo", "arguments": {"text": "runtime"}}),
                        "RUNTIME_WRITE_FILE": ("write_file", {"file_path": str(test.file_path), "content": "NATIVE_FILE_OK\n"}),
                        "RUNTIME_READ_FILE": ("read_file", {"file_path": str(test.file_path)}),
                        "RUNTIME_MEMORY_SEARCH": ("memory_search", {"query": MARKER, "max_results": 3}),
                    }
                    for marker, (name, arguments) in cases.items():
                        if marker in content:
                            function = {"name": name, "arguments": json.dumps(arguments)}
                            break
                answer = "RUNTIME_TOOL_OK" if already_called else "RUNTIME_CHAT_OK"
                if "RUNTIME_CRON" in content:
                    answer = "RUNTIME_CRON_OK"
                chunk_id = "chatcmpl-" + uuid4().hex
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    def emit(delta, finish=None):
                        chunk = {"id": chunk_id, "object": "chat.completion.chunk", "created": 1,
                                 "model": "local-runtime-test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode()); self.wfile.flush()
                    emit({"role": "assistant", "content": ""})
                    if function:
                        emit({"tool_calls": [{"index": 0, "id": "call-" + uuid4().hex,
                                              "type": "function", "function": function}]})
                        emit({}, "tool_calls")
                    else:
                        emit({"content": answer}); emit({}, "stop")
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                    self.close_connection = True
                else:
                    message = {"role": "assistant", "content": answer}
                    if function:
                        message = {"role": "assistant", "content": None,
                                   "tool_calls": [{"id": "call-" + uuid4().hex, "type": "function", "function": function}]}
                    self.reply(200, {"id": chunk_id, "object": "chat.completion", "created": 1,
                                     "model": "local-runtime-test", "choices": [{"index": 0, "message": message,
                                     "finish_reason": "tool_calls" if function else "stop"}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
        return LocalApi

    def initialize(self, gateway):
        self.working.mkdir()
        (self.working / ".telemetry_collected").write_text('{"opted_out":true}', encoding="utf-8")
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
                   "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "HOME"}
        self.env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        self.env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "QWENPAW_WORKING_DIR": str(self.working),
                         "QWENPAW_SECRET_DIR": str(self.root / "secret"), "QWENPAW_BACKUP_DIR": str(self.root / "backups"),
                         "QWENPAW_DISABLE_KEYRING": "true", "QWENPAW_AUTH_ENABLED": "false",
                         "QWENPAW_RUNTIME_INTERNAL_TOKEN": self.runtime_token, "QWENPAW_ENABLED_CHANNELS": "console,astrbot",
                         "QWENPAW_AGENT_ID": "default", "BRIDGE_TOKEN": self.bridge_token,
                         "ASTRBOT_BRIDGE_URL": f"http://127.0.0.1:{gateway.server_port}",
                         "BRIDGE_FILES_ROOT": str(self.root / "bridge-files"), "HF_HUB_OFFLINE": "1",
                         "TRANSFORMERS_OFFLINE": "1", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"})
        (self.root / "launch_runtime.py").write_text(LAUNCHER, encoding="utf-8")
        with (self.root / "init.log").open("wb") as log:
            subprocess.run([sys.executable, str(self.root / "launch_runtime.py"), "init", "--defaults", "--accept-security"],
                           env=self.env, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        shutil.copytree(self.project / "qwenpaw_plugin_astrbot_bridge", self.working / "plugins/qwenpaw_plugin_astrbot_bridge",
                        ignore=shutil.ignore_patterns("__pycache__"))
        agent_path = self.working / "workspaces/default/agent.json"
        config = json.loads(agent_path.read_text(encoding="utf-8"))
        config["channels"]["astrbot"] = {"enabled": True}
        # Avoid unattended extraction/dream tasks; indexing and real search stay on.
        config["running"]["reme_light_memory_config"]["auto_memory_interval"] = None
        config["running"]["reme_light_memory_config"]["dream_cron_enabled"] = False
        agent_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        root_path = self.working / "config.json"
        config = json.loads(root_path.read_text(encoding="utf-8"))
        for agent_id, profile in config["agents"]["profiles"].items():
            if agent_id != "default":
                profile["enabled"] = False
        root_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def start(self, label):
        self.log = (self.root / (label + ".log")).open("wb")
        self.proc = subprocess.Popen([sys.executable, str(self.root / "launch_runtime.py"), "app", "--host", "127.0.0.1",
                                      "--port", str(self.port), "--log-level", "debug"], env=self.env, cwd=self.root,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 240
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("QwenPaw exited during startup; inspect the local test log")
            try:
                health = self.api("/api/healthz", timeout=2)
                if health.get("agents_loaded") == ["default"]:
                    time.sleep(1)
                    return health
            except (HTTPError, URLError, TimeoutError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("QwenPaw startup; inspect the local test log")
            time.sleep(0.5)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(timeout=5)
            self.proc = None
        if self.log:
            self.log.close(); self.log = None

    def chat(self, marker):
        body = {"session_id": SESSION, "user_id": USER, "channel": "astrbot",
                "request_context": {"astrbot_bridge_turn_id": TURN},
                "input": [{"role": "user", "content": [{"type": "text", "text": marker}]}]}
        request = Request(f"http://127.0.0.1:{self.port}/api/agents/default/console/chat",
                          data=json.dumps(body).encode(), method="POST",
                          headers={"X-QwenPaw-Runtime-Token": self.runtime_token, "Content-Type": "application/json"})
        results = queue.Queue()
        def read():
            try:
                with urlopen(request, timeout=90) as response:
                    results.put(response.read().decode())
            except Exception as exc:
                results.put(exc)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        deadline = time.monotonic() + 100
        while results.empty():
            pending = self.api("/api/approval/list?session_id=" + SESSION)["pending_approvals"]
            for approval in pending:
                # Test-only exact approval: never approve arbitrary model operations.
                if approval.get("tool_name") not in ("write_file", "read_file"):
                    raise AssertionError("Unexpected approval in the local runtime test")
                if approval.get("exact_target") != str(self.file_path):
                    raise AssertionError("Test approval target is not the known disposable file")
                decision = self.api("/api/approval/approve", "POST", {"request_id": approval["request_id"],
                                    "session_id": SESSION, "user_id": USER, "scope": "exact"})
                self.approvals.append({"tool_name": approval["tool_name"], "decision": decision.get("status")})
            if time.monotonic() > deadline:
                raise TimeoutError("Local runtime chat did not finish")
            time.sleep(0.25)
        raw = results.get()
        if isinstance(raw, Exception):
            raise raw
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]
        (self.root / (marker.lower() + ".json")).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        assert any(event.get("object") == "response" and event.get("status") == "completed" for event in events)
        return events

    async def client_checks(self):
        sys.path.insert(0, str(self.project / "astrbot_plugin_qwenpaw_bridge"))
        from bridge_core import QwenPawClient
        client = QwenPawClient(f"http://127.0.0.1:{self.port}", "default", self.runtime_token, timeout=90)
        try:
            results = {}
            for marker in ("RUNTIME_LIST_TOOLS", "RUNTIME_CALL_TOOL"):
                parts = await client.chat(SESSION, USER, marker, turn_id=TURN)
                assert parts == [{"type": "text", "text": "RUNTIME_TOOL_OK"}], "Real AssistantCollector lost or leaked output"
                results[marker] = parts
            return results
        finally:
            await client.close()

    def run(self):
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            self.initialize(gateway)
            self.report["health"] = self.start("runtime")
            self.report["plugins"] = self.api("/api/plugins")
            self.report["channel_health"] = self.api("/api/agents/default/config/channels/astrbot/health")
            self.report["bridge_tools"] = [tool for tool in self.api("/api/agents/default/tools") if tool["name"].startswith("astrbot_")]
            self.report["memory_daily_initial"] = self.api("/api/agents/default/workspace/memory?section=daily")
            self.report["memory_digest_initial"] = self.api("/api/agents/default/workspace/memory?section=digest")
            self.report["memory_runtime"] = self.api("/api/agents/default/memory/runtime-status")
            self.report["active_initial"] = self.api("/api/models/active?scope=effective&agent_id=default")
            provider = "local-runtime-test"
            base_url = self.env["ASTRBOT_BRIDGE_URL"] + "/v1"
            self.api("/api/models/custom-providers", "POST", {"id": provider, "name": "Local Runtime Test", "default_base_url": base_url,
                     "chat_model": "OpenAIChatModel", "models": [{"id": provider, "name": "Local Runtime Test", "is_free": True}]})
            self.api("/api/models/" + provider + "/config", "PUT", {"api_key": "local-test-only", "base_url": base_url,
                     "chat_model": "OpenAIChatModel", "auto_discover": False})
            self.api("/api/models/active", "PUT", {"provider_id": provider, "model": provider, "scope": "agent", "agent_id": "default"})
            time.sleep(2)
            self.report["active_configured"] = self.api("/api/models/active?scope=effective&agent_id=default")
            self.report["chat_completed"] = len(self.chat("RUNTIME_HELLO"))
            self.report["real_bridge_client"] = asyncio.run(self.client_checks())
            write = self.chat("RUNTIME_WRITE_FILE")
            # Official write_file deliberately emits a UTF-8 BOM for .txt files.
            assert self.file_path.read_text(encoding="utf-8-sig") == "NATIVE_FILE_OK\n", "Native write_file did not create the test file"
            read = self.chat("RUNTIME_READ_FILE")
            assert "NATIVE_FILE_OK" in tool_outputs(read), "Native read_file did not read the written file"
            self.report["native_files"] = {"write_event_count": len(write), "read_event_count": len(read), "read_back_matches": True}
            memory_path = "/api/agents/default/workspace/memory/runtime-validation.md?section=daily"
            self.report["memory_written"] = self.api(memory_path, "PUT", {"content": MEMORY_TEXT})
            # Official memory reads strip outer whitespace and normalize file decoding.
            assert self.api(memory_path)["content"].replace("\r\n", "\n") == MEMORY_TEXT.strip()
            time.sleep(12)  # Real ReMe filesystem watcher: polling + debounce + index update.
            search = self.chat("RUNTIME_MEMORY_SEARCH")
            assert MARKER in tool_outputs(search) and "runtime-validation.md" in tool_outputs(search), "Real ReMe memory_search did not retrieve the indexed marker"
            self.report["memory_search"] = {"marker_retrieved": True, "event_count": len(search), "backend": "ReMe BM25; no embedding configured"}
            self.report["memory_daily_written"] = self.api("/api/agents/default/workspace/memory?section=daily")
            job = self.api("/api/agents/default/cron/jobs", "POST", {"name": "Local runtime cron", "schedule": {"type": "cron", "cron": "0 0 1 1 *", "timezone": "UTC"},
                           "task_type": "agent", "request": {"input": [{"role": "user", "content": [{"type": "text", "text": "RUNTIME_CRON"}]}]},
                           "dispatch": {"channel": "astrbot", "target": {"session_id": SESSION, "user_id": USER}, "mode": "final", "silent": False},
                           "runtime": {"max_concurrency": 1, "tool_safety": True, "share_session": True, "timeout_seconds": 900}})
            job_id = job["id"]
            self.report["cron_created"] = job
            self.api("/api/agents/default/cron/jobs/" + job_id + "/run", "POST")
            deadline = time.monotonic() + 90
            while not self.deliveries and time.monotonic() < deadline:
                time.sleep(0.25)
            assert len(self.deliveries) == 1 and self.deliveries[0]["content"] == [{"type": "text", "text": "RUNTIME_CRON_OK"}]
            self.report["cron_history"] = self.api("/api/agents/default/cron/jobs/" + job_id + "/history")
            self.report["cron_paused"] = self.api("/api/agents/default/cron/jobs/" + job_id + "/pause", "POST")
            self.report["cron_resumed"] = self.api("/api/agents/default/cron/jobs/" + job_id + "/resume", "POST")
            self.report["tool_disabled"] = self.api("/api/agents/default/tools/astrbot_call_tool/toggle", "PATCH")
            assert self.report["tool_disabled"]["enabled"] is False
            self.stop()
            self.report["health_after_restart"] = self.start("runtime-restart")
            persisted_tools = self.api("/api/agents/default/tools")
            assert next(tool for tool in persisted_tools if tool["name"] == "astrbot_call_tool")["enabled"] is False
            assert self.api(memory_path)["content"].replace("\r\n", "\n") == MEMORY_TEXT.strip()
            persisted_jobs = self.api("/api/agents/default/cron/jobs")
            assert any(item["id"] == job_id and item["enabled"] for item in persisted_jobs)
            search = self.chat("RUNTIME_MEMORY_SEARCH_AFTER_RESTART")
            assert MARKER in tool_outputs(search), "Persisted ReMe index was not searchable after restart"
            self.report["persistence"] = {"memory_file": True, "reme_search": True, "cron_job": True,
                                          "tool_disabled_setting": True, "session_file": any((self.working / "workspaces/default/sessions/astrbot").glob("*.json"))}
            assert len(self.calls) == 2 and all(call["turn_id"] == TURN for call in self.calls)
            assert len(self.report["bridge_tools"]) == 4 and all(tool["enabled"] for tool in self.report["bridge_tools"])
            assert any(plugin.get("id") == "astrbot-bridge" and plugin.get("loaded") for plugin in self.report["plugins"])
            self.report.update(passed=True, gateway_calls=self.calls, deliveries=self.deliveries,
                               exact_test_approvals=self.approvals, model_calls=len(self.model_requests))
        finally:
            self.stop()
            gateway.shutdown(); gateway.server_close()
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            # Remove local file paths from the shareable report; raw events/logs stay local.
            encoded = json.dumps(self.report, ensure_ascii=False, indent=2)
            encoded = encoded.replace(str(self.root).replace("\\", "\\\\"), "<isolated-test-root>")
            (self.root / "report.json").write_text(encoded, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, help="A new directory for disposable test state and evidence")
    parser.add_argument("--upstream-source", type=Path, help="Optional official pinned checkout to verify installed Python sources")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.artifacts_dir:
        root = args.artifacts_dir.resolve()
        root.mkdir(parents=True, exist_ok=False)
    else:
        root = Path(tempfile.mkdtemp(prefix="astrbot-qwenpaw-runtime-"))
    check = RuntimeCheck(root, project, args.upstream_source)
    check.run()
    print(json.dumps({"passed": True, "report": str(root / "report.json"), "paid_model_calls": 0}))


if __name__ == "__main__":
    main()
