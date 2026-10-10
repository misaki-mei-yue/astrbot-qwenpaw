"""Exercise managed agents against genuine QwenPaw 2.2.1 without any model.

Run in the already-installed QwenPaw image with --network none, mount a pure
code snapshot (both plugin folders and this script) read-only at /checks, and
mount an EMPTY disposable E-drive folder at /artifacts. Never mount the
project's runtime/working directory. This probe
adds read-only diagnostic routes to its own subprocess, not production.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PRIVATE = "abp_" + "1" * 32
OTHER = "abp_" + "2" * 32
GROUP = "abg_" + "3" * 32
ALLOWED = {PRIVATE, OTHER, GROUP}
SESSION = "ab_" + "4" * 32


def serve(port):
    """Test-only native memory/registration diagnostics, with no model path."""
    import uvicorn
    from fastapi import HTTPException, Request as FastRequest
    from fastapi.routing import APIRoute
    from qwenpaw.app._app import app
    from qwenpaw.plugins.registry import PluginRegistry

    async def diagnostics(request: FastRequest):
        body = await request.json()
        agent_id = body.get("agent_id")
        if agent_id not in ALLOWED:
            raise HTTPException(400, "Only this probe's fixture identities are accepted")
        workspace = await app.state.multi_agent_manager.get_agent(agent_id)
        registrations = PluginRegistry().get_middleware_factories()
        bridge = [item for item in registrations if item.plugin_id == "astrbot-bridge"]
        if body.get("operation") == "registrations":
            checks = []
            for registration in bridge:
                ctx = SimpleNamespace(agent_id=agent_id, workspace_dir=workspace.workspace_dir, session_id=SESSION)
                guard = registration.factory(ctx, None)
                permitted = guard.validate("read_file", {"file_path": "probe-own.txt"})
                denied = []
                for name, arguments in (
                    ("read_file", {"file_path": str(Path(workspace.workspace_dir).parent / "default" / "PROFILE.md")}),
                    ("execute_shell_command", {"command": "true"}),
                    ("browser", {"code": "pass"}),
                    ("web_fetch", {"url": "http://127.0.0.1/"}),
                ):
                    try:
                        guard.validate(name, arguments)
                    except ValueError:
                        denied.append(name)
                checks.append({"plugin": registration.plugin_id, "priority": registration.priority,
                               "factory": registration.factory.__name__, "guard": type(guard).__name__,
                               "own_path_allowed": permitted["file_path"].endswith("/probe-own.txt"),
                               "denied": denied})
            return {"middlewares": checks}
        if body.get("operation") == "search":
            query = body.get("query")
            if query not in {"orchid741", "coffee852", "meeting963", "peony741"}:
                raise HTTPException(400, "Only synthetic fixture queries are accepted")
            result = await workspace.memory_manager.memory_search(query, max_results=5)
            blocks = getattr(result, "content", [])
            return {"text": "\n".join(str(item.get("text", "") if isinstance(item, dict)
                                         else getattr(item, "text", "")) for item in blocks)}
        raise HTTPException(400, "Unknown probe operation")

    # Put the probe before the Console's final catch-all static route.
    # Future annotations cannot resolve this function-local import by name.
    diagnostics.__annotations__["request"] = FastRequest
    app.router.routes.insert(0, APIRoute("/api/__personal_probe", diagnostics, methods=["POST"]))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


class PersonalRuntimeCheck:
    def __init__(self, root, project):
        self.root, self.project = root, project
        self.working = root / "working"
        self.runtime_token = secrets.token_hex(32)
        self.bridge_token = secrets.token_hex(32)
        self.proc, self.log = None, None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG"}}
        self.env.update({
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
            "QWENPAW_WORKING_DIR": str(self.working), "QWENPAW_SECRET_DIR": str(root / "secret"),
            "QWENPAW_BACKUP_DIR": str(root / "backups"), "QWENPAW_DISABLE_KEYRING": "true",
            "QWENPAW_AUTH_ENABLED": "false", "QWENPAW_RUNTIME_INTERNAL_TOKEN": self.runtime_token,
            "QWENPAW_ENABLED_CHANNELS": "console,astrbot", "QWENPAW_AGENT_ID": "default",
            "BRIDGE_TOKEN": self.bridge_token, "ASTRBOT_BRIDGE_URL": "http://127.0.0.1:9",
            "BRIDGE_FILES_ROOT": str(root / "bridge-files"), "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        })
        self.report = {
            "test_only": True, "qwenpaw_version": importlib.metadata.version("qwenpaw"),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "network_boundary": "container --network none; loopback API only",
            "model_calls": 0, "real_accounts_loaded": False,
            "checks": [], "limitations": [
                "No actual model, automatic extraction, QQ/WeChat account or browser navigation is exercised.",
                "Middleware registration/factory validation is exercised without a model-generated tool call.",
                "Native file ingestion is asynchronous; successful reindex does not confirm a just-written file is searchable.",
            ],
        }
        if self.report["qwenpaw_version"] != "2.2.1":
            raise RuntimeError("This check requires the pinned QwenPaw 2.2.1 image")

    def api(self, path, method="GET", body=None, timeout=45):
        req = Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "X-QwenPaw-Runtime-Token": self.runtime_token})
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())

    def initialize(self):
        print("Initializing a fresh, disposable QwenPaw workspace.", flush=True)
        self.working.mkdir()
        (self.working / ".telemetry_collected").write_text('{"opted_out":true}', encoding="utf-8")
        with (self.root / "init.log").open("wb") as log:
            subprocess.run([sys.executable, "-m", "qwenpaw", "init", "--defaults", "--accept-security"],
                           cwd=self.root, env=self.env, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=180)
        shutil.copytree(self.project / "qwenpaw_plugin_astrbot_bridge",
                        self.working / "plugins/qwenpaw_plugin_astrbot_bridge",
                        ignore=shutil.ignore_patterns("__pycache__"))
        agent_path = self.working / "workspaces/default/agent.json"
        config = json.loads(agent_path.read_text(encoding="utf-8"))
        config["channels"]["astrbot"] = {"enabled": True}
        config["active_model"] = None
        memory = config["running"]["reme_light_memory_config"]
        memory["auto_memory_interval"] = 5
        memory["dream_cron_enabled"] = False
        agent_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        config_path = self.working / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for agent_id, profile in config["agents"]["profiles"].items():
            if agent_id != "default":
                profile["enabled"] = False
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def start(self, label):
        print(f"Starting native QwenPaw ({label}).", flush=True)
        self.log = (self.root / f"{label}.log").open("wb")
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(self.port)],
                                     cwd=self.root, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Native QwenPaw exited; see {label}.log")
            try:
                value = self.api("/api/healthz", timeout=2)
                if "default" in value.get("agents_loaded", []):
                    return
            except (HTTPError, URLError, TimeoutError):
                pass
            time.sleep(0.5)
        raise TimeoutError(f"Native startup did not finish; see {label}.log")

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
        if self.log:
            self.log.close()
            self.log = None

    def search(self, agent_id, query):
        return self.api("/api/__personal_probe", "POST", {"operation": "search", "agent_id": agent_id, "query": query})["text"]

    async def await_recall(self, agent_id, query, *, present):
        """Observe the actual native watcher, not an assumed fixed delay."""
        started = time.monotonic()
        while time.monotonic() - started < 45:
            result = await asyncio.to_thread(self.search, agent_id, query)
            if (query in result) is present:
                self.report.setdefault("native_index_observations", []).append({
                    "agent_id": agent_id, "query": query, "present": present,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                })
                return
            await asyncio.sleep(1)
        raise AssertionError(f"Native recall did not synchronize within 45 seconds: {query}, present={present}")

    async def checks(self, restart=False):
        sys.path.insert(0, str(self.project))
        from astrbot_plugin_qwenpaw_bridge.bridge_core import QwenPawClient, BridgeError
        from astrbot_plugin_qwenpaw_bridge.personal_agent import PersonalAgentManager
        client = QwenPawClient(f"http://127.0.0.1:{self.port}", "default", self.runtime_token)
        manager = PersonalAgentManager(client)
        try:
            if restart:
                for agent_id, expected in self.saved.items():
                    actual = await manager.memory_list(agent_id)
                    assert actual == expected, f"Restart did not preserve fixture notes for {agent_id}"
                    assert await manager.model_ready(agent_id) is False
                self.report["checks"].append("native_restart_keeps_independent_agents_and_notes")
                return
            print("Provisioning two private agents and one group agent through official REST.", flush=True)
            for agent_id, scope in ((PRIVATE, "private"), (OTHER, "private"), (GROUP, "group")):
                config = await manager.ensure_agent(agent_id, scope)
                assert config["id"] == agent_id
                assert config["workspace_dir"] == str(self.working / "workspaces" / agent_id)
                memory = config["running"]["reme_light_memory_config"]
                assert memory["auto_memory_interval"] == 5
                assert memory["auto_memory_search_config"]["enabled"] is True
                assert memory["memory_search_enabled"] is True
                assert memory["dream_cron_enabled"] is False
                assert await manager.model_ready(agent_id) is False
                guard = await asyncio.to_thread(self.api, "/api/__personal_probe", "POST",
                                               {"operation": "registrations", "agent_id": agent_id})
                entry = next(item for item in guard["middlewares"] if item["factory"] == "workspace_guard_factory")
                assert entry["own_path_allowed"]
                assert set(entry["denied"]) == {"read_file", "execute_shell_command", "browser", "web_fetch"}
                self.report.setdefault("middleware", []).append(entry)
            self.report["checks"].extend(["official_agent_create_update_and_owned_workspaces",
                                           "native_automatic_memory_cadence_and_recall_configuration_persisted",
                                           "model_unconfigured_detected_without_chat",
                                           "official_plugin_middleware_registered_and_enforcing_scope"])
            print("Writing synthetic notes, reindexing, and checking cross-agent recall isolation.", flush=True)
            own = await manager.remember(PRIVATE, "Fixture preference orchid741: unsweetened tea.")
            other = await manager.remember(OTHER, "Fixture preference coffee852: black coffee.")
            group = await manager.remember(GROUP, "Fixture group meeting963: Friday meeting.")
            for agent_id, note in ((PRIVATE, own), (OTHER, other), (GROUP, group)):
                assert note.pop("index_status") == "pending"
                assert "index_warning" not in note
                assert await manager.memory_list(agent_id) == [note]
            await self.await_recall(PRIVATE, "orchid741", present=True)
            await self.await_recall(OTHER, "coffee852", present=True)
            await self.await_recall(GROUP, "meeting963", present=True)
            assert "orchid741" not in await asyncio.to_thread(self.search, OTHER, "orchid741")
            assert "orchid741" not in await asyncio.to_thread(self.search, GROUP, "orchid741")
            try:
                await manager.correct(OTHER, own["id"], "should not overwrite")
            except BridgeError:
                pass
            else:
                raise AssertionError("Cross-agent correction should fail")
            self.report["checks"].append("native_memory_CRUD_and_BM25_recall_do_not_cross_agent_scope")
            fixed = await manager.correct(PRIVATE, own["id"], "Fixture preference peony741: green tea.")
            assert fixed["index_status"] == "pending" and "index_warning" not in fixed
            await self.await_recall(PRIVATE, "peony741", present=True)
            await self.await_recall(PRIVATE, "orchid741", present=False)
            forgotten = await manager.forget(PRIVATE, own["id"])
            assert forgotten["index_status"] == "pending" and "index_warning" not in forgotten
            assert await manager.memory_list(PRIVATE) == []
            await self.await_recall(PRIVATE, "peony741", present=False)
            assert await manager.memory_list(OTHER) == [other]
            assert await manager.memory_list(GROUP) == [group]
            self.saved = {PRIVATE: [], OTHER: [other], GROUP: [group]}
            self.report["checks"].append("native_note_correction_and_forget_eventually_synchronize_search_index")
        finally:
            await client.close()

    def run(self):
        try:
            self.initialize()
            self.start("first-start")
            asyncio.run(self.checks())
            self.stop()
            self.start("restart")
            asyncio.run(self.checks(restart=True))
            self.report["status"] = "passed"
        except Exception as exc:
            self.report["status"] = "failed"
            self.report["failure_type"] = type(exc).__name__
            self.report["failure"] = str(exc).replace(self.runtime_token, "[redacted]").replace(self.bridge_token, "[redacted]")
            raise
        finally:
            self.stop()
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            (self.root / "report.json").write_text(json.dumps(self.report, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
        return
    if args.artifacts is None:
        parser.error("--artifacts must name a new disposable directory")
    root = args.artifacts.resolve()
    root.mkdir(parents=True, exist_ok=False)
    PersonalRuntimeCheck(root, args.project.resolve()).run()
    print("PASS: genuine personal-agent REST, note recall/isolation, middleware registration and restart.", flush=True)


if __name__ == "__main__":
    main()
