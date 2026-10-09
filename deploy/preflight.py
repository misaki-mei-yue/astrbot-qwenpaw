"""Read-only Linux capacity and Docker checks. No install, upgrade, or restart."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess


def command(args: list[str]) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args[0]} {args[1]}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing-container", default="")
    parser.add_argument("--disk-path", type=Path, default=Path("/opt"))
    args = parser.parse_args()
    if platform.system() != "Linux":
        print("STOP: Run preflight on the Linux deployment server.")
        return 2
    if not shutil.which("docker"):
        print("STOP: Docker is missing. Install Docker Engine and Compose first.")
        return 2
    try:
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total_gib = int(memory["MemTotal"].split()[0]) / 1024**2
        free_gib = int(memory["MemAvailable"].split()[0]) / 1024**2
        disk_gib = shutil.disk_usage(args.disk_path).free / 1024**3
        print(f"CPU: {os.cpu_count()} logical cores; RAM: {total_gib:.1f} GiB; available: {free_gib:.1f} GiB.")
        print(f"Free disk: {disk_gib:.1f} GiB; architecture: {platform.machine()}.")
        print("Docker Engine:", command(["docker", "version", "--format", "{{.Server.Version}}"] ))
        print("Compose:", command(["docker", "compose", "version", "--short"]))
        if total_gib < 3.5:
            print("CAPACITY: Below the 4 GB starting estimate. Prepare files now; add memory before running all services and browser tasks.")
        elif total_gib < 7:
            print("CAPACITY: 8 GB gives more room for shared services and browser tasks; this is an estimate, not a measured minimum.")
        if disk_gib < 8:
            print("CAPACITY: Less than 8 GiB free. The full desktop/browser image build needs more disk headroom.")
        for port in (6185, 8088, 6099):
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    print(f"PORT: 127.0.0.1:{port} is in use; review your selected management binding.")
        if args.existing_container:
            state = command(["docker", "inspect", "--format", "{{json .State}}", args.existing_container])
            state = json.loads(state)
            print("Existing AstrBot:", state.get("Status"), "OOMKilled:", state.get("OOMKilled"))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"STOP: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
