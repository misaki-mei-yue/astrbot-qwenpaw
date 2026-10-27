"""Linux full bundle lifecycle; backup helper has no exposed network listener."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bundle_updater.common import safe_path
from deploy.prepare import prepare, parse_env
from scripts.backup_host import helper_python


def is_windows():
    return os.name == "nt"


def checked(command, *, timeout=900, capture=False, input=None):
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("DOCKER_", "COMPOSE_")) and k not in {"PROJECT_SOURCE", "INSTALL_ROOT"}}
    if "--env-file" in command:
        env_path = Path(command[command.index("--env-file") + 1])
        for key in parse_env(safe_path(env_path)):
            environment.pop(key, None)
    result = subprocess.run(command, cwd=ROOT, env=environment, shell=False, check=True, timeout=timeout,
                            input=input, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def compose_args(runtime, project, context, layout):
    command = ["docker", "--context", context, "compose", "--project-directory", str(ROOT),
               "--project-name", project, "--env-file", str(runtime / ".env"), "-f",
               str(ROOT / "deploy" / ("compose.bundle.yaml" if layout == "bundle" else "compose.yaml"))]
    if layout == "linux":
        command += ["-f", str(ROOT / "deploy/compose.manage.yaml")]
    return command


def wait_health(timeout, qwenpaw_only=False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open("http://127.0.0.1:18080/bundle-health", timeout=8) as reply:
                value = json.loads(reply.read(65537))
            ready = (value.get("services", {}).get("qwenpaw", {}).get("ready") is True
                     if qwenpaw_only else value.get("status") == "ready")
            if ready:
                return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise ValueError("Services did not become ready; data and containers were retained.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start/stop the full Linux three-in-one bundle")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--data-dir", type=Path, help="Adopt a previous fresh deploy/setup.sh installation; not add-on mode")
    args = parser.parse_args(argv)
    try:
        if is_windows() or sys.version_info < (3, 10):
            raise ValueError("Use Python 3.10+ on Linux, or start.cmd on Windows.")
        runtime = safe_path(args.data_dir or ROOT / "runtime")
        layout = "linux" if args.data_dir else "bundle"
        # Validate Docker before creating credentials or changing files.
        context = checked(["docker", "context", "show"], capture=True, timeout=20)
        from bundle_updater.daemon import _local_context
        _local_context(context)
        if checked(["docker", "--context", context, "info", "--format", "{{.OSType}}"], capture=True, timeout=20) != "linux":
            raise ValueError("A local Linux Docker engine is required.")
        checked(["docker", "--context", context, "compose", "version"], timeout=20)
        project = "astrbot-qwenpaw-bundle-" + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:10]
        if layout == "linux":
            marker = json.loads(safe_path(runtime / ".combined-setup.json").read_text(encoding="utf-8"))
            env = parse_env(safe_path(runtime / ".env"))
            if (marker.get("format") != 1 or marker.get("mode") != "fresh"
                    or safe_path(Path(env.get("PROJECT_SOURCE", ""))) != ROOT
                    or safe_path(Path(env.get("INSTALL_ROOT", ""))) != runtime):
                raise ValueError("Only a full fresh installation belonging to this source can be adopted.")
            project = env.get("COMPOSE_PROJECT_NAME", "bot-combined")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project):
                raise ValueError("Invalid deployment project name.")
        if args.action == "start":
            # Finish any interrupted data restore before prepare can rewrite target paths.
            from bundle_updater.daemon import _pending_recovery
            if _pending_recovery(runtime / "backups/private"):
                python = helper_python(runtime)
                checked([python, str(ROOT / "scripts/run_bundle_backup.py"), "--ensure", "--root", str(ROOT), "--runtime-root", str(runtime), "--layout", layout, "--project", project, "--context", context])
            if layout == "bundle":
                stable = runtime / "updates/private/host-entry.py"
                if stable.is_file():
                    checked([sys.executable, str(stable), "--ensure", "--root", str(ROOT), "--project", project, "--context", context])
                prepare("fresh", runtime, None)
            python = helper_python(runtime)
            checked([python, str(ROOT / "scripts/run_bundle_backup.py"), "--ensure", "--root", str(ROOT), "--runtime-root", str(runtime), "--layout", layout, "--project", project, "--context", context])
            if layout == "bundle":
                checked([sys.executable, str(ROOT / "scripts/run_bundle_updater.py"), "--ensure", "--root", str(ROOT), "--project", project, "--context", context])
            command = compose_args(runtime, project, context, layout)
            checked(command + ["config", "--quiet"], timeout=60)
            checked(command + ["pull", "--ignore-buildable", "--policy", "missing"], timeout=3600)
            checked(command + ["build", "gateway"] + (["qwenpaw"] if layout == "linux" else []), timeout=3600)
            checked(command + ["up", "-d"], timeout=3600)
            wait_health(600, True)
            checked(command + ["exec", "-T", "qwenpaw", "/app/venv/bin/python", "-"],
                    input=(ROOT / "deploy/bootstrap_qwenpaw.py").read_text(encoding="utf-8"), timeout=240)
            wait_health(600)
            print("Ready: http://localhost:18080/ (VPS: use an SSH port forward to this loopback port).")
        else:
            python = helper_python(runtime)
            checked([python, str(ROOT / "scripts/run_bundle_backup.py"), "--stop", "--root", str(ROOT), "--runtime-root", str(runtime), "--layout", layout, "--project", project, "--context", context], timeout=45)
            if layout == "bundle":
                checked([sys.executable, str(ROOT / "scripts/run_bundle_updater.py"), "--stop", "--root", str(ROOT), "--project", project, "--context", context], timeout=45)
            checked(compose_args(runtime, project, context, layout) + ["stop"], timeout=120)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Operation stopped. Check Docker, installation mode, paths and private backup diagnostics; existing data was retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
