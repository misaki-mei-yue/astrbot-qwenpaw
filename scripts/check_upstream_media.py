"""Check fixed upstream media models and renderer without starting applications.

Requires Python 3.11-3.13, Pydantic 2, aiohttp, and python-dotenv. AgentScope may be a
local official wheel or an extracted wheel directory. No package installation,
network request, model invocation, application start, or platform login occurs.
Package initializers are bypassed as documented in the emitted scope field.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
from uuid import uuid4

QWEN_COMMIT = "cae5773707b26ab2fd00903f84b712387894b256"
AGENTSCOPE_VERSION = "2.0.7.post1"
AGENTSCOPE_WHEEL_SHA256 = "3fffeb2b6e124c45d7c95a019afc4de73ed6c2a02880804da5b43a61634cc063"
QWEN_HASHES = {
    "schemas.py": "203a2451761d007ecb4b8b0b7f8503e78a5c8dd6dd0109dbc41a964f27d8d6fb",
    "app/channels/renderer.py": "dc48b9528eb23dfb7146e24fe10ea41dbeb4be4fbb15aa0c91e0c2fb37da66c8",
    "runtime/envelope.py": "9fd965f801ce2a4e54a1dd2cd538ce0c6671651537842142d19328c88b865c06",
    "runtime/message_convert.py": "35f570721ac2cbfda566774702e69796b532bbf6542ed56b583c6158641f73d1",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_sha256(path: Path) -> str:
    # Git for Windows may check out the same source with CRLF. Normalize only
    # that transport difference so Linux and Windows check the same content.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@contextmanager
def isolated_working_dir():
    """Prevent constant.py from reading the operator's working .env."""
    temp_parent = Path(os.environ["LOCALAPPDATA"]) / "Temp" if os.name == "nt" and os.environ.get("LOCALAPPDATA") else Path(tempfile.gettempdir())
    empty_working = str(temp_parent / ("upstream-media-" + uuid4().hex))
    require(not Path(empty_working).exists(), "Expected a fresh empty configuration path.")
    previous = os.environ.get("QWENPAW_WORKING_DIR")
    os.environ["QWENPAW_WORKING_DIR"] = empty_working
    try:
        yield empty_working
    finally:
        if previous is None:
            os.environ.pop("QWENPAW_WORKING_DIR", None)
        else:
            os.environ["QWENPAW_WORKING_DIR"] = previous


def check(qwen_checkout: Path, agentscope_path: Path, dotenv_path: Path | None = None, dependency_paths: list[Path] | None = None) -> dict:
    require((3, 11) <= sys.version_info[:2] < (3, 14), "Use Python 3.11-3.13.")
    qwen_checkout = qwen_checkout.resolve()
    source = qwen_checkout / "src/qwenpaw"
    require(source.is_dir(), "Supply the root of a clean fixed QwenPaw source checkout.")
    require(not (qwen_checkout / ".env").exists(), "Refusing a source checkout containing .env.")
    for relative, expected in QWEN_HASHES.items():
        require(source_sha256(source / relative) == expected, f"Fixed source hash mismatch: {relative}")
    commit_verified = False
    if shutil.which("git") and (qwen_checkout / ".git").exists():
        git = subprocess.run(["git", "-C", str(qwen_checkout), "rev-parse", "HEAD"], text=True, capture_output=True, timeout=15)
        require(git.returncode == 0 and git.stdout.strip() == QWEN_COMMIT, "QwenPaw checkout is not the fixed commit.")
        commit_verified = True

    agentscope_path = agentscope_path.resolve()
    is_wheel = agentscope_path.is_file() and zipfile.is_zipfile(agentscope_path)
    require(is_wheel or (agentscope_path / "agentscope/tool/_response.py").is_file(), "Supply the official AgentScope wheel or its extracted directory.")
    if is_wheel:
        require(sha256(agentscope_path) == AGENTSCOPE_WHEEL_SHA256, "AgentScope wheel SHA256 does not match the fixed official artifact.")
    sys.path.insert(0, str(agentscope_path))
    if dotenv_path:
        sys.path.insert(0, str(dotenv_path.resolve()))
    for path in dependency_paths or []:
        sys.path.append(str(path.resolve()))

    import agentscope
    from agentscope.message import DataBlock, TextBlock, ToolResultState, URLSource

    require(agentscope.__version__ == AGENTSCOPE_VERSION, "AgentScope version mismatch.")
    # No class/function substitutes: load the original ToolChunk source while
    # bypassing tool/__init__.py's eager MCP and built-in tool dependencies.
    tool_package = types.ModuleType("agentscope.tool")
    tool_package.__path__ = [str(agentscope_path) + "/agentscope/tool"]
    sys.modules["agentscope.tool"] = tool_package
    response = types.ModuleType("agentscope.tool._response")
    response.__package__ = "agentscope.tool"
    response.__file__ = str(agentscope_path) + "/agentscope/tool/_response.py"
    sys.modules[response.__name__] = response
    if is_wheel:
        with zipfile.ZipFile(agentscope_path) as archive:
            response_source = archive.read("agentscope/tool/_response.py")
    else:
        response_source = (agentscope_path / "agentscope/tool/_response.py").read_bytes()
    exec(compile(response_source, response.__file__, "exec"), response.__dict__)
    tool_chunk = response.ToolChunk

    # event/__init__.py imports FinishedReason from model/__init__.py, whose
    # eager provider imports need the whole framework dependency set. Forward
    # the genuine enum from the unchanged model response module instead.
    model_package = types.ModuleType("agentscope.model")
    model_package.__path__ = [str(agentscope_path) + "/agentscope/model"]
    sys.modules["agentscope.model"] = model_package
    model_package.FinishedReason = importlib.import_module("agentscope.model._model_response").FinishedReason
    events = importlib.import_module("agentscope.event")

    # These namespaces bypass application package initializers only. The
    # schemas, renderer, constants and imported renderer helpers remain real.
    for package in ("qwenpaw", "qwenpaw.app", "qwenpaw.app.channels", "qwenpaw.agents", "qwenpaw.agents.context", "qwenpaw.agents.context.scroll", "qwenpaw.utils", "qwenpaw.runtime", "qwenpaw._compat"):
        module = types.ModuleType(package)
        module.__path__ = [str(source.joinpath(*package.split(".")[1:]))]
        sys.modules[package] = module
    # constant.py reads WORKING_DIR/.env, so point it at a fresh nonexistent
    # temporary path, never the operator's config. No directory is created.
    with isolated_working_dir():
        schema = importlib.import_module("qwenpaw.schemas")
        renderer_module = importlib.import_module("qwenpaw.app.channels.renderer")
        renderer = renderer_module.MessageRenderer()
        envelope_module = importlib.import_module("qwenpaw.runtime.envelope")
        bridge_file = Path(__file__).resolve().parents[1] / "astrbot_plugin_qwenpaw_bridge/bridge_core.py"
        bridge_spec = importlib.util.spec_from_file_location("checked_bridge_core", bridge_file)
        bridge = importlib.util.module_from_spec(bridge_spec)
        sys.modules[bridge_spec.name] = bridge
        bridge_spec.loader.exec_module(bridge)
        records = []
        sid = "ab_" + "a" * 32
        for kind, mime, filename, field in (
            ("file", "text/plain", "result.txt", "file_url"),
            ("image", "image/png", "result.png", "image_url"),
            ("video", "video/mp4", "result.mp4", "video_url"),
            ("audio", "audio/mpeg", "result.mp3", "data"),
        ):
            url = f"file:///bridge-files/{sid}/outbound/{filename}"
            chunk = tool_chunk(state=ToolResultState.SUCCESS, content=[
                DataBlock(source=URLSource(url=url, media_type=mime), name=filename),
                TextBlock(text="File sent successfully."),
            ])
            dumped = chunk.model_dump(mode="json")
            require(dumped["content"][0]["type"] == "data", "Unexpected AgentScope binary block type.")
            require(dumped["content"][0]["source"]["url"] == url, "AgentScope file URL was changed.")
            require(dumped["content"][0]["source"]["media_type"] == mime, "AgentScope MIME type was changed.")
            require(tool_chunk.model_validate_json(chunk.model_dump_json()).content[0].name == filename, "ToolChunk JSON roundtrip failed.")
            message = schema.Message(type=schema.MessageType.FUNCTION_CALL_OUTPUT, role=schema.Role.TOOL, content=[
                schema.DataContent(data={"name": "send_file_to_user", "output": dumped["content"]}),
            ]).completed()
            parts = renderer.message_to_parts(message)
            media_parts = [part for part in parts if part.type.value == kind]
            require(len(media_parts) == 1 and getattr(media_parts[0], field) == url, f"Qwen renderer lost {kind} media or its URL.")
            if kind == "file":
                require(media_parts[0].filename == filename, "Qwen renderer lost filename.")
            message.content[0].data["output"] = json.dumps(dumped["content"])
            json_parts = renderer.message_to_parts(message)
            require(any(part.type.value == kind and getattr(part, field) == url for part in json_parts), f"Qwen renderer lost JSON-string {kind} output.")

            # Exercise actual upstream event classes through the actual
            # envelope, then feed serialized outputs to the bridge collector.
            async def envelope_check():
                envelope = envelope_module.Envelope(sid)
                collector = bridge.AssistantCollector()
                sequence = (
                    events.ToolResultStartEvent(reply_id="reply_test", tool_call_id="call_test", tool_call_name="send_file_to_user"),
                    events.ToolResultDataDeltaEvent(reply_id="reply_test", tool_call_id="call_test", block_id="media_test", media_type=mime, url=url),
                    events.ToolResultEndEvent(reply_id="reply_test", tool_call_id="call_test", state=ToolResultState.SUCCESS),
                    events.TextBlockStartEvent(reply_id="reply_test", block_id="text_test"),
                    events.TextBlockDeltaEvent(reply_id="reply_test", block_id="text_test", delta="Here is your generated file."),
                    events.TextBlockEndEvent(reply_id="reply_test", block_id="text_test"),
                )
                native_kind = None
                for event in sequence:
                    async for output in envelope.translate_event(event):
                        dumped_output = output.model_dump(mode="json")
                        collector.feed(dumped_output)
                        if dumped_output.get("object") == "message" and dumped_output.get("role") == "tool" and dumped_output.get("status") == "completed":
                            content_output = dumped_output["content"][0]["data"]["output"]
                            require(isinstance(content_output, str), "Envelope tool output must be a JSON string.")
                            native_kind = json.loads(content_output)[0]["type"]
                async for output in envelope.finalize():
                    collector.feed(output.model_dump(mode="json"))
                result = collector.result()
                require(any(part.get("type") == "text" and part.get("text") == "Here is your generated file." for part in result), "Collector lost final assistant text snapshot.")
                require(len([part for part in result if part.get("type") == kind and part.get(field) == url]) == 1, f"Actual Envelope-to-collector {kind} media was lost or duplicated.")
                return native_kind

            native_kind = asyncio.run(envelope_check())
            records.append({"kind": kind, "source_type": "data", "envelope_inner_type": native_kind, "output_field": field, "file_url_preserved": True})

        models = (
            schema.FileContent(file_url="file:///bridge-files/example.txt", filename="example.txt"),
            schema.ImageContent(image_url="file:///bridge-files/example.png"),
            schema.AudioContent(data="file:///bridge-files/example.mp3", format="mp3"),
            schema.VideoContent(video_url="file:///bridge-files/example.mp4"),
        )
        for model in models:
            parsed = schema.Message.model_validate({"content": [model.model_dump(mode="json")]})
            require(type(parsed.content[0]) is type(model), "Qwen native content roundtrip lost its model type.")

    return {
        "python": sys.version.split()[0],
        "agentscope": agentscope.__version__,
        "agentscope_wheel_sha256": AGENTSCOPE_WHEEL_SHA256 if is_wheel else None,
        "qwenpaw_commit": QWEN_COMMIT,
        "qwenpaw_commit_verified_by_git": commit_verified,
        "qwen_source_hashes": QWEN_HASHES,
        "qwen_source_hash_format": "SHA256 with CRLF normalized to LF; all other bytes preserved",
        "checked": records,
        "toolchunk_roundtrips": len(records),
        "renderer_cases": len(records) * 2,
        "native_content_roundtrips": len(models),
        "envelope_to_collector_cases": len(records),
        "bridge_core_sha256": sha256(bridge_file),
        "scope": "Real AgentScope event/schema classes, unchanged ToolChunk module, Qwen schemas/renderer/Envelope and bridge AssistantCollector. AgentScope tool/model and Qwen package initializers replaced with namespace packages; model.FinishedReason forwards the genuine enum from model._model_response. No fake classes. Not a full app/runtime, model, browser, QQ or WeChat integration test.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwenpaw-source", type=Path, required=True, help="Clean QwenPaw Git checkout root at the fixed commit.")
    parser.add_argument("--agentscope-path", type=Path, required=True, help="Official 2.0.7.post1 wheel or extracted wheel directory.")
    parser.add_argument("--dotenv-path", type=Path, help="Optional python-dotenv wheel or extracted directory; otherwise use installed dependency.")
    parser.add_argument("--dependency-path", type=Path, action="append", help="Optional extra installed dependency directory, appended to sys.path; repeatable.")
    parser.add_argument("--report", type=Path, help="Optional output JSON report path; not a runtime configuration.")
    args = parser.parse_args()
    try:
        report = check(args.qwenpaw_source, args.agentscope_path, args.dotenv_path, args.dependency_path)
    except (ValueError, ImportError, OSError) as exc:
        parser.exit(2, f"CHECK FAILED: {exc}\n")
    text = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
