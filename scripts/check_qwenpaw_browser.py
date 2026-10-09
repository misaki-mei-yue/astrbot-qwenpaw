"""Exercise fixed QwenPaw's real guest Browser SDK and subprocess worker.

Requires an already installed runtime and an explicit existing Chromium/Edge
executable. It downloads nothing and uses only fresh temporary configuration,
about:blank and a trusted loopback page. It never opens a user browser profile.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading

SOURCE_COMMIT = "cae5773707b26ab2fd00903f84b712387894b256"
# LF-normalized SHA-256 from official v2.2.1. No upstream source is bundled.
SOURCE_SHA256 = {
    "agents/tools/browser.py": "16b92c484489d38e568bd1f2f5772382f5c7f4c01cae486a5959340c3415abab",
    "browser/runtime/engine.py": "87ce2c02223ec87ae2d5d65467a6e1af50a7f4f7aba66246b68d7bd01719d481",
    "browser/runtime/identity.py": "1a38d3ffb065f1d389477dc457ba8442c090b80494273d6eb56b49164d3f52e3",
    "browser/runtime/launch_resolve.py": "cd5f7879d37ea483b4fad3b9c134361c22db276b53698243e970a841beb09bbc",
    "browser/control_link/playwright/adapter.py": "93950f7af09bc969b7d2fad54fedf00e3b53da844e9fecfedd913fe401a79ddb",
    "browser/execution/subprocess_plane.py": "f91b1ccac720f48c7a213504b046a7720f93f447262de8317261d9a8a8d760c1",
    "browser/execution/worker.py": "107302e5626ae9dfcc00f1990b8420d3d1d1964fc458a4f860b34cb9c0e28fe8",
    "browser/sdk/facade.py": "4925f0fc301ecb6f681e3046e2341939cc969b6cba52cad6b0adceb03f92a26f",
    "browser/sdk/page.py": "a4d049de5e10d780f2916c878587c3655f2b0b1e2304578444bd0c44dc6300d9",
    "browser/tool_entrypoint.py": "3d5a3e5fc3d05e844a9839d0bd8f76df6b632a1e8a6de3a1b1f3a504923da552",
}
REQUIRED_CHECKS = (
    "sdk_identity_guest", "sdk_context_incognito", "sdk_blank_snapshot",
    "sdk_local_dom", "sdk_local_click", "worker_tool_success",
    "worker_blank_snapshot", "worker_local_dom", "worker_local_click",
)


class ProbeFailure(RuntimeError):
    """Only a fixed safe failure label is exposed in the report."""


def runtime_info():
    distribution = importlib.metadata.distribution("qwenpaw")
    if distribution.version != "2.2.1":
        raise ProbeFailure("unsupported_qwenpaw_version")
    # Installed wheel/source files are verified before any QwenPaw import.
    package = Path(distribution.locate_file("qwenpaw")).resolve()
    if not package.is_dir():
        raise ProbeFailure("installed_qwenpaw_source_unavailable")
    if (package.parent.parent / ".env").exists():
        # constant.py would import this dotenv. Never consume existing secrets.
        raise ProbeFailure("installation_dotenv_present")
    hashes = {}
    for relative, expected in SOURCE_SHA256.items():
        value = hashlib.sha256((package / relative).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if value != expected:
            raise ProbeFailure("upstream_source_hash_mismatch")
        hashes[relative] = value
    return {
        "upstream": {"version": distribution.version, "reference_commit": SOURCE_COMMIT, "verified_file_sha256": hashes},
        "runtime": {"python_version": platform.python_version(), "os": platform.system(),
                    "playwright_version": importlib.metadata.version("playwright"),
                    "agentscope_version": importlib.metadata.version("agentscope")},
    }


class PageServer(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<html><body><h1>Local isolated probe</h1><button onclick="this.innerText=\'Clicked\'">Probe</button></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def check_browser(url, working, report):
    from qwenpaw.browser.control_link import register_builtin_control_links
    from qwenpaw.browser.runtime.links import registered_links
    from qwenpaw.browser import Browser
    from qwenpaw.browser.tool_entrypoint import run_browser_tool, derive_workspace_id
    from qwenpaw.browser.execution.kernel import get_default_kernel_manager
    from qwenpaw.config.context import current_workspace_dir, current_session_id
    from agentscope.message import ToolResultState

    register_builtin_control_links()
    checks = report["checks"]
    browser = None
    try:
        browser = await Browser.connect(identity="guest")
        checks["sdk_identity_guest"] = browser._engine.session.identity == "guest"
        checks["sdk_context_incognito"] = browser._engine.session.context == "incognito"
        # Read public engine version from the actual launched Playwright process.
        for process in browser._engine.link._procs.values():
            if process.get("browser"):
                report["runtime"]["browser_engine_version"] = process["browser"].version
        page = await browser.open("about:blank")
        checks["sdk_blank_snapshot"] = isinstance((await page.snapshot()).text, str)
        await page.goto(url)
        checks["sdk_local_dom"] = "Local isolated probe" in (await page.snapshot()).text
        await page.get_by_role("button", name="Probe").click()
        checks["sdk_local_click"] = "Clicked" in (await page.snapshot()).text
    finally:
        if browser:
            await browser.close()
        for link in registered_links():
            if getattr(link, "variant", None) == "playwright":
                await link.close_all()

    workspace = working / "workspaces" / "browser-probe"
    workspace.mkdir(parents=True, exist_ok=True)
    wt = current_workspace_dir.set(workspace)
    st = current_session_id.set("isolated-browser-probe")
    code = f'''browser = await Browser.connect(identity="guest")
try:
    page = await browser.open("about:blank")
    print("blank_snapshot=" + str(isinstance((await page.snapshot()).text, str)))
    await page.goto({url!r})
    print("local_dom=" + str("Local isolated probe" in (await page.snapshot()).text))
    await page.get_by_role("button", name="Probe").click()
    print("local_click=" + str("Clicked" in (await page.snapshot()).text))
finally:
    await browser.close()
'''
    try:
        chunk = await run_browser_tool(code)
        blocks = chunk.model_dump(mode="json")["content"]
        joined = "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict))
        checks["worker_tool_success"] = chunk.state == ToolResultState.SUCCESS
        checks["worker_blank_snapshot"] = "blank_snapshot=True" in joined
        checks["worker_local_dom"] = "local_dom=True" in joined
        checks["worker_local_click"] = "local_click=True" in joined
        # Never publish upstream stdout/error details, which can contain paths.
    finally:
        try:
            await get_default_kernel_manager().close_workspace(derive_workspace_id(workspace))
            await get_default_kernel_manager().discard_all_workers()
        finally:
            current_session_id.reset(st)
            current_workspace_dir.reset(wt)


def check(executable: str, *, temp_root: str | None = None) -> dict:
    report = {"schema_version": 1, "status": "failed", "checks": {},
              "scope": {"fresh_guest_contexts": True, "existing_profile_used": False,
                        "external_pages_requested": False, "model_called": False,
                        "platform_messages_sent": False, "target_server_tested": False}}
    env_keys = ("TEMP", "TMP", "QWENPAW_WORKING_DIR", "QWENPAW_SECRET_DIR", "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "QWENPAW_KEYRING_ACCOUNT", "QWENPAW_LOG_LEVEL")
    original = {key: os.environ.get(key) for key in env_keys}
    original_temp = tempfile.tempdir
    captured = io.StringIO()
    try:
        if "qwenpaw" in sys.modules:
            raise ProbeFailure("run_in_fresh_python_process")
        executable_path = Path(executable).expanduser().resolve(strict=True)
        if not executable_path.is_file():
            raise ProbeFailure("browser_executable_not_a_file")
        report.update(runtime_info())
        safe_root = Path(temp_root or tempfile.gettempdir()).expanduser().resolve(strict=True)
        if not safe_root.is_dir():
            raise ProbeFailure("temporary_root_not_a_directory")
        with tempfile.TemporaryDirectory(prefix="qwenpaw-browser-isolated-", dir=safe_root) as folder:
            test_root = Path(folder).resolve()
            if not test_root.is_relative_to(safe_root):
                raise ProbeFailure("temporary_directory_escaped_root")
            working = test_root / "working"
            working.mkdir()
            os.environ.update(TEMP=str(test_root), TMP=str(test_root), QWENPAW_WORKING_DIR=str(working),
                              QWENPAW_SECRET_DIR=str(test_root / "secret"),
                              PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=str(executable_path),
                              QWENPAW_KEYRING_ACCOUNT="isolated-browser-probe", QWENPAW_LOG_LEVEL="warning")
            tempfile.tempdir = str(test_root)
            config = {"browser": {"identity": "guest", "backend": "launch", "engine": "chromium", "headless": "true",
                "executable_path": str(executable_path), "use_system_default": False,
                "args": ["--disable-background-networking", "--disable-component-update", "--no-first-run",
                         "--no-default-browser-check", "--disable-sync", "--disable-extensions", "--no-proxy-server",
                         "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"],
                "user_data_dir": str(test_root / "unused-fresh-profile")}}
            (working / "config.json").write_text(json.dumps(config), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), PageServer)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # Suppress upstream logging; publish only the bounded summary.
                with redirect_stdout(captured), redirect_stderr(captured):
                    asyncio.run(check_browser(f"http://127.0.0.1:{server.server_port}/probe", working, report))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        report["cleanup_disconnected_chrome_warning"] = "close_session failed on chrome" in captured.getvalue()
        report["status"] = "passed" if all(report["checks"].get(key) is True for key in REQUIRED_CHECKS) else "failed"
    except ProbeFailure as exc:
        report["error"] = str(exc)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tempfile.tempdir = original_temp
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", required=True, help="Existing Chromium/Edge executable; no autodownload or default profile")
    parser.add_argument("--temp-root", help="Existing scratch directory (default: OS temporary directory)")
    parser.add_argument("--report", help="Write the redacted JSON report to this file")
    args = parser.parse_args()
    report = check(args.browser_executable, temp_root=args.temp_root)
    serialized = json.dumps(report, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
