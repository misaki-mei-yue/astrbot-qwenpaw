"""Exercise genuine AstrBot 4.25.1 core in fresh, disposable test storage.

No user's data, credentials, QQ/WeChat login, external model or dashboard is
used. Only platform transport and QwenPaw HTTP are loopback test boundaries.
Requires an independently installed AstrBot runtime dependency environment.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import uuid

ASTRBOT_COMMIT = "d609f23b7136e30adf38929bac6402a581148ffc"
ASTRBOT_FRAMEWORK_SHA256 = "82bdde023cb497eccb91ad19b4720d5ee6642f623ac7cb2f693562a45b094154"


def framework_hash(source: Path) -> str:
    """Fingerprint the exact tag's framework tree, excluding Python caches."""
    digest = hashlib.sha256()
    files = sorted((path for path in (source / "astrbot").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts),
                   key=lambda path: path.relative_to(source).as_posix())
    for path in files:
        if path.is_symlink():
            raise ValueError("Tagged framework source must not contain symlinks")
        digest.update(path.relative_to(source).as_posix().encode() + b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


PROBE_PLUGIN = '''from astrbot.api import llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star

class RuntimeProbe(Star):
    def __init__(self, context):
        super().__init__(context)
        self.calls = 0

    @filter.command("bridge_test_echo")
    async def echo(self, event: AstrMessageEvent):
        yield event.plain_result("original-command-preserved")

    @llm_tool(name="bridge_test_lookup")
    async def lookup(self, event: AstrMessageEvent, query: str) -> str:
        """Return a deterministic harmless test result.

        Args:
            query (str): Text to return.
        """
        self.calls += 1
        return "runtime-tool:" + query
'''


def loopback_guard():
    """Deny outbound non-loopback connections and bind listeners to loopback."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_bind = socket.socket.bind
    original_getaddrinfo = socket.getaddrinfo

    def check(address):
        if isinstance(address, tuple):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed = address[0] == "localhost"
            if not allowed:
                raise OSError("Runtime test forbids non-loopback connections")

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def bind(sock, address):
        if isinstance(address, tuple) and address[0] in ("", "0.0.0.0", "::"):
            address = (("::1" if sock.family == socket.AF_INET6 else "127.0.0.1"), *address[1:])
        check(address)
        return original_bind(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host:
            check((host.decode() if isinstance(host, bytes) else host, 0))
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.bind = bind
    socket.getaddrinfo = getaddrinfo


async def exercise(source: Path, project: Path, run_root: Path) -> dict:
    from aiohttp import ClientSession, web
    from astrbot.core import LogBroker, astrbot_config, db_helper, html_renderer
    import astrbot.core.core_lifecycle as lifecycle_module
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform import Platform
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.message.components import Plain
    from astrbot.core.star.star import star_registry

    assert lifecycle_module.VERSION == "4.25.1", "Only AstrBot 4.25.1 is verified"
    astrbot_config["platform"] = []
    astrbot_config["provider"] = []
    astrbot_config["provider_sources"] = []
    astrbot_config["disable_metrics"] = True
    astrbot_config["provider_settings"]["enable"] = False
    astrbot_config["platform_settings"]["segmented_reply"]["enable"] = False
    astrbot_config["platform_settings"]["reply_with_mention"] = False
    astrbot_config["platform_settings"]["reply_with_quote"] = False
    astrbot_config["admins_id"] = ["runtime-test-owner"]
    astrbot_config.save_config()

    async def no_external_metadata_refresh():
        return None

    lifecycle_module.update_llm_metadata = no_external_metadata_refresh
    # Avoid even a attempted public metadata request during renderer startup.
    html_renderer.network_strategy.BASE_RENDER_URL = "http://127.0.0.1:9/text2img"
    html_renderer.network_strategy.endpoints = ["http://127.0.0.1:9/text2img"]
    loopback_guard()
    checks = []
    qwen_inputs = []
    qwen_errors = []
    token = uuid.uuid4().hex + uuid.uuid4().hex
    runtime_token = uuid.uuid4().hex + uuid.uuid4().hex
    os.environ.update({
        "BRIDGE_TOKEN": token,
        "QWENPAW_RUNTIME_INTERNAL_TOKEN": runtime_token,
        "OWNER_USER_IDS": "",
        "TOOL_ALLOWLIST": "",
        "BRIDGE_STATE_PATH": str(run_root / "data/plugin_data/bridge/bridge.sqlite3"),
        "BRIDGE_FILES_ROOT": str(run_root / "shared"),
        "BRIDGE_SOURCE_ROOTS": str(run_root / "data/temp"),
    })

    async def fake_chat(request):
        body = await request.json()
        qwen_inputs.append(body)
        if request.headers.get("X-QwenPaw-Runtime-Token") != runtime_token:
            return web.json_response({"error": "unauthorized"}, status=401)
        payload = {"session_id": body["session_id"], "user_id": body["user_id"],
                   "turn_id": body["request_context"]["astrbot_bridge_turn_id"]}
        try:
            async with ClientSession(headers={"Authorization": "Bearer " + token}) as client:
                async with client.post("http://127.0.0.1:9186/v1/tools/list", json=payload) as response:
                    assert response.status == 200
                    listing = await response.json()
                assert [item["name"] for item in listing["tools"]] == ["bridge_test_lookup"]
                text = body["input"][0]["content"][0]["text"]
                call = {**payload, "call_id": "single-runtime-call", "tool_name": "bridge_test_lookup", "arguments": {"query": text}}
                async with client.post("http://127.0.0.1:9186/v1/tools/call", json=call) as response:
                    assert response.status == 200
                    result = await response.json()
                assert result["isError"] is False, result
                async with client.post("http://127.0.0.1:9186/v1/tools/call", json=call) as response:
                    assert response.status == 200 and await response.json() == result
            message = {"object": "message", "status": "completed", "type": "message", "role": "assistant", "content": result["content"]}
            events = [message, {"object": "response", "status": "completed", "output": [message]}]
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for event in events:
                await response.write(("data: " + json.dumps(event) + "\n\n").encode())
            await response.write_eof()
            return response
        except Exception as exc:
            qwen_errors.append(type(exc).__name__ + ": " + str(exc))
            return web.json_response({"error": "test_contract_failed"}, status=500)

    app = web.Application()
    app.router.add_post("/api/agents/default/console/chat", fake_chat)
    app.router.add_get("/api/approval/list", lambda request: web.json_response([]))

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    os.environ["QWENPAW_BASE_URL"] = "http://127.0.0.1:" + str(port)
    core = lifecycle_module.AstrBotCoreLifecycle(LogBroker(), db_helper)

    class ProbeEvent(AstrMessageEvent):
        def __init__(self, text, message_id, user="runtime-test-owner"):
            msg = AstrBotMessage()
            msg.type = MessageType.FRIEND_MESSAGE
            msg.self_id = "runtime-test-bot"
            msg.message_id = message_id
            msg.session_id = user
            msg.sender = MessageMember(user, "runtime-test-user")
            msg.message = [Plain(text)]
            msg.message_str = text
            msg.raw_message = {}
            super().__init__(text, msg, PlatformMetadata("aiocqhttp", "Isolated transport", "runtime-test-qq"), user)
            self.sent = []

        async def send(self, message):
            self.sent.append(message)
            await super().send(message)

    class ProbePlatform(Platform):
        def __init__(self):
            super().__init__({}, core.event_queue)
            self.sent = []

        def meta(self):
            return PlatformMetadata("aiocqhttp", "Isolated transport", "runtime-test-qq")

        async def run(self):
            return None

        async def send_by_session(self, session, message_chain):
            self.sent.append((str(session), message_chain))

    def text_of(event):
        return "".join(part.text for chain in event.sent for part in chain.chain if isinstance(part, Plain))

    initialized = False
    try:
        await core.initialize()
        initialized = True
        assert core.plugin_manager.failed_plugin_dict == {}, core.plugin_manager.failed_plugin_info
        bridge = next(plugin.star_cls for plugin in star_registry if plugin.root_dir_name == "astrbot_plugin_qwenpaw_bridge")
        probe = next(plugin.star_cls for plugin in star_registry if plugin.root_dir_name == "astrbot_plugin_runtime_probe")
        assert bridge.owners == {"runtime-test-owner"}
        assert bridge.tool_allowlist == {"bridge_test_lookup"}
        checks.append("genuine_plugin_loader_and_dashboard_config")
        transport = ProbePlatform()
        core.platform_manager.platform_insts.append(transport)
        scheduler = core.pipeline_scheduler_mapping["default"]
        command = ProbeEvent("/bridge_test_echo", "runtime-command")
        await scheduler.execute(command)
        assert text_of(command) == "original-command-preserved", text_of(command)
        assert not qwen_inputs
        checks.append("ordinary_plugin_command_preserved")
        whoami = ProbeEvent("/paw whoami", "runtime-whoami")
        await scheduler.execute(whoami)
        assert "runtime-test-owner" in text_of(whoami), text_of(whoami)
        checks.append("genuine_paw_command_registration")
        event = ProbeEvent("hello-runtime", "runtime-chat")
        await scheduler.execute(event)
        assert not qwen_errors, qwen_errors
        assert text_of(event) == "runtime-tool:hello-runtime", text_of(event)
        assert probe.calls == 1
        assert len(qwen_inputs) == 1
        checks.extend(["genuine_function_tool_executor_and_hooks", "tool_callback_idempotency", "pipeline_bridge_reply_once"])
        duplicate = ProbeEvent("hello-runtime", "runtime-chat")
        await scheduler.execute(duplicate)
        assert probe.calls == 1 and len(qwen_inputs) == 1
        checks.append("platform_message_replay_deduplicated")
        denied = ProbeEvent("hello-runtime", "runtime-denied", user="runtime-test-outsider")
        await scheduler.execute(denied)
        assert "owner" in text_of(denied) and len(qwen_inputs) == 1
        checks.append("nonowner_denied")
        route = next(bridge._store.get_session(body["session_id"]) for body in qwen_inputs)
        async with ClientSession(headers={"Authorization": "Bearer " + token}) as client:
            payload = {"delivery_id": "runtime-delivery", "session_id": route["session_id"], "user_id": route["user_id"], "content": [{"type": "text", "text": "proactive-runtime"}]}
            async with client.post("http://127.0.0.1:9186/v1/deliver", json=payload) as response:
                assert response.status == 200 and (await response.json())["accepted"]
            async with client.post("http://127.0.0.1:9186/v1/deliver", json=payload) as response:
                assert response.status == 200 and (await response.json())["accepted"]
        assert len(transport.sent) == 1 and transport.sent[0][0] == route["origin"]
        checks.append("genuine_context_proactive_route_and_deduplication")
    finally:
        if initialized:
            await core.stop()
            assert bridge._runner is None and bridge._client is None
            assert bridge._store is None and bridge._approval_task is None
            assert not bridge._active and not bridge._chat_locks and not bridge._delivery_locks
            checks.append("genuine_bridge_lifecycle_cleanup")
        await runner.cleanup()
        with contextlib.suppress(Exception):
            await html_renderer.network_strategy.terminate()
        with contextlib.suppress(Exception):
            await db_helper.engine.dispose()

    packages = {name: importlib.metadata.version(name) for name in ("aiohttp", "jsonschema", "mcp", "pydantic", "sqlmodel", "openai")}
    source_hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in (
        "astrbot/core/core_lifecycle.py", "astrbot/core/star/star_manager.py",
        "astrbot/core/star/context.py", "astrbot/core/astr_agent_tool_exec.py",
        "astrbot/core/platform/astr_message_event.py",
    )}
    return {"astrbot_version": "4.25.1", "astrbot_commit": ASTRBOT_COMMIT,
            "framework_tree_sha256": framework_hash(source), "python_version": sys.version.split()[0], "checks": checks, "check_count": len(checks), "dependencies": packages,
            "source_sha256": source_hashes,
            "scope": "Genuine AstrBot core lifecycle, plugin loader, command filters, pipeline, tool executor and hooks, native messages, Context proactive route and bridge lifecycle cleanup. Only platform transport and QwenPaw HTTP responses are local test boundaries. No real QQ/WeChat, external model, browser, production data or dashboard UI is exercised."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-source", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.report:
        args.report = args.report.absolute()
    source = args.astrbot_source.resolve(strict=True)
    assert (source / "astrbot/core/core_lifecycle.py").is_file()
    if framework_hash(source) != ASTRBOT_FRAMEWORK_SHA256:
        parser.error("Expected an unmodified git archive of AstrBot v4.25.1 commit " + ASTRBOT_COMMIT)
    project = Path(__file__).resolve().parents[1]
    # Each invocation gets a brand-new root; source/data is never copied or read.
    run_root = Path(tempfile.mkdtemp(prefix="astrbot-combined-runtime-")).resolve()
    for name in ("data/plugins", "data/config", "data/temp", "shared"):
        (run_root / name).mkdir(parents=True, exist_ok=True)
    for name in ("data", "data/plugins"):
        (run_root / name / "__init__.py").write_text("", encoding="utf-8")
    shutil.copytree(project / "astrbot_plugin_qwenpaw_bridge", run_root / "data/plugins/astrbot_plugin_qwenpaw_bridge", ignore=shutil.ignore_patterns("__pycache__"))
    probe = run_root / "data/plugins/astrbot_plugin_runtime_probe"
    probe.mkdir()
    (probe / "main.py").write_text(PROBE_PLUGIN, encoding="utf-8")
    (probe / "metadata.yaml").write_text("name: astrbot_plugin_runtime_probe\nauthor: runtime_test\ndesc: Isolated runtime verification\nversion: 1.0.0\n", encoding="utf-8")
    config = {"owner_user_ids": ["runtime-test-owner"], "tool_allowlist": ["bridge_test_lookup"], "files_root": str(run_root / "shared"), "source_roots": [str(run_root / "data/temp")]}
    (run_root / "data/config/astrbot_plugin_qwenpaw_bridge_config.json").write_text(json.dumps(config), encoding="utf-8")
    os.environ["ASTRBOT_ROOT"] = str(run_root)
    os.environ.pop("ASTRBOT_DESKTOP_RUNTIME", None)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.chdir(run_root)
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(run_root))
    report = asyncio.run(exercise(source, project, run_root))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
