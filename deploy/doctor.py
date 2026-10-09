"""Read-only, redacted runtime checks for AstrBot x QwenPaw deployments.

The structured report contains only fixed labels, booleans, counts and expected
versions. No logs, complete container environment, chat records, account IDs,
credentials, platform messages, model calls or configuration writes are used.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Callable
import urllib.error
import urllib.request

EXPECTED_IMAGES = {
    "astrbot": "soulter/astrbot:v4.25.1",
    "qwenpaw": "bot-combined/qwenpaw:2.2.1-local",
    "napcat": "mlikiowa/napcat-docker:v4.18.33",
    "console": "caddy:2.11.7-alpine",
}
EXPECTED_VERSIONS = {"astrbot": "4.25.1", "qwenpaw": "2.2.1"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_OUTPUT = 1024 * 1024

# Select fields in Docker's process rather than retrieving its complete Config.
# Shared source paths and network names are compared in memory, never reported.
INSPECT_FORMAT = (
    '{"running":{{json .State.Running}},"oom_killed":{{json .State.OOMKilled}},'
    '"restart_count":{{json .RestartCount}},"image":{{json .Config.Image}},'
    '"host_network":{{if eq .HostConfig.NetworkMode "host"}}true{{else}}false{{end}},'
    '"ports":{{json .HostConfig.PortBindings}},'
    '"mounts":[{{range .Mounts}}{{if eq .Destination "/bridge-files"}}'
    '{"source":{{json .Source}},"rw":{{json .RW}},"type":{{json .Type}}}'
    '{{end}}{{end}}],"networks":{'
    '{{$first := true}}{{range $name, $net := .NetworkSettings.Networks}}'
    '{{if not $first}},{{end}}{{$first = false}}{{json $name}}:'
    '{"aliases":{{json $net.Aliases}}}{{end}}}}'
)

# Only stdlib code executes in application containers. Tokens remain there;
# requests use fixed private addresses, disable proxies and never follow redirects.
CONTAINER_PROBE = r'''
import ast, json, os, re, stat, sys, urllib.error, urllib.request
from pathlib import Path

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def read_json(path):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1048576:
            return {}
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def items(config, key, env):
    override = os.environ.get(env, '').strip()
    value = override if override else config.get(key, [])
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in value if str(x).strip()}

def version(path, variable):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1048576:
            return ''
        for node in ast.parse(raw).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
                return node.value.value if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) else ''
    except Exception:
        pass
    return ''

def get(url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=4) as response:
            status = response.status
            payload = response.read(65537)
        if len(payload) <= 65536:
            try:
                data = json.loads(payload)
            except Exception:
                data = {}
        else:
            data = {}
        return status, data if isinstance(data, dict) else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}

def common():
    path = Path('/bridge-files')
    exists = path.is_dir() and not path.is_symlink()
    return {
        'probe_ok': True,
        'shared_exists': exists,
        'shared_readable': exists and os.access(path, os.R_OK | os.X_OK),
        'shared_writable_indicated': exists and os.access(path, os.W_OK | os.X_OK),
        'shared_world_access': bool(path.stat().st_mode & 7) if exists else False,
        'uid_is_root': os.geteuid() == 0,
    }

def astrbot():
    result = common()
    config = read_json('/AstrBot/data/config/astrbot_plugin_qwenpaw_bridge_config.json')
    owners = items(config, 'owner_user_ids', 'OWNER_USER_IDS') - {'*'}
    tools = items(config, 'tool_allowlist', 'TOOL_ALLOWLIST')
    result.update({
        'plugin_present': Path('/AstrBot/data/plugins/astrbot_plugin_qwenpaw_bridge/main.py').is_file(),
        'config_file_present': Path('/AstrBot/data/config/astrbot_plugin_qwenpaw_bridge_config.json').is_file(),
        'owner_count': len(owners), 'tool_count': len(tools - {'*'}),
        'all_tools_enabled': '*' in tools,
        'version_matches': version('/AstrBot/astrbot/core/config/default.py', 'VERSION') == '4.25.1',
    })
    token = os.environ.get('BRIDGE_TOKEN', config.get('bridge_token', ''))
    runtime = os.environ.get('QWENPAW_RUNTIME_INTERNAL_TOKEN', config.get('runtime_token', ''))
    result['bridge_token_configured'] = isinstance(token, str) and len(token) >= 32
    result['runtime_token_configured'] = isinstance(runtime, str) and len(runtime) >= 32
    result['qwen_url_expected'] = os.environ.get('QWENPAW_BASE_URL', config.get('qwenpaw_url', 'http://qwenpaw:8088')) == 'http://qwenpaw:8088'
    unauth, _ = get('http://127.0.0.1:9186/health')
    auth, body = get('http://127.0.0.1:9186/health', {'Authorization': 'Bearer ' + token}) if result['bridge_token_configured'] else (0, {})
    result.update(gateway_unauthorized_status=unauth, gateway_authorized_status=auth, gateway_ready=body.get('ready') is True)
    qstatus, qbody = get('http://qwenpaw:8088/api/healthz', {'X-QwenPaw-Runtime-Token': runtime}) if result['runtime_token_configured'] else (0, {})
    agent_id = os.environ.get('QWENPAW_AGENT_ID', config.get('agent_id', 'default'))
    loaded = qbody.get('agents_loaded')
    result.update(qwen_cross_status=qstatus, qwen_cross_ready=qbody.get('status') == 'ok' and isinstance(loaded, list) and agent_id in loaded)
    return result

def qwenpaw():
    result = common()
    root = Path('/app/working')
    config = read_json(root / 'config.json')
    profiles = config.get('agents', {}).get('profiles', {}) if isinstance(config.get('agents'), dict) else {}
    if not isinstance(profiles, dict):
        profiles = {}
    agent_id = os.environ.get('QWENPAW_AGENT_ID', 'default')
    ref = profiles.get(agent_id, {}) if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', agent_id) else {}
    profile = {}
    if isinstance(ref, dict):
        workspace = Path(ref.get('workspace_dir', str(root / 'workspaces' / agent_id)))
        try:
            if workspace.resolve().is_relative_to(root.resolve()) and not workspace.is_symlink():
                profile = read_json(workspace / 'agent.json')
        except Exception:
            pass
    channels = profile.get('channels', {})
    if not isinstance(channels, dict):
        channels = {}
    bridge_channel = channels.get('astrbot', {})
    enabled_channels = os.environ.get('QWENPAW_ENABLED_CHANNELS', '').split(',')
    result.update({
        'plugin_present': (root / 'plugins/qwenpaw_plugin_astrbot_bridge/plugin.json').is_file(),
        'config_file_present': (root / 'config.json').is_file(),
        'profile_count': len(profiles),
        'target_profile_configured': bool(profile),
        'channel_enabled_count': sum(isinstance(v, dict) and v.get('enabled') is True for v in channels.values()),
        'astrbot_channel_enabled': isinstance(bridge_channel, dict) and bridge_channel.get('enabled') is True,
        'astrbot_channel_requested': 'astrbot' in enabled_channels,
        'console_channel_requested': 'console' in enabled_channels,
        'callback_url_expected': os.environ.get('ASTRBOT_BRIDGE_URL', 'http://astrbot:9186') == 'http://astrbot:9186',
        'version_matches': version('/app/src/qwenpaw/__version__.py', '__version__') == '2.2.1',
    })
    token = os.environ.get('QWENPAW_RUNTIME_INTERNAL_TOKEN', '')
    bridge = os.environ.get('BRIDGE_TOKEN', '')
    result['runtime_token_configured'] = len(token) >= 32
    result['bridge_token_configured'] = len(bridge) >= 32
    unauth, _ = get('http://127.0.0.1:8088/api/version')
    auth, body = get('http://127.0.0.1:8088/api/healthz', {'X-QwenPaw-Runtime-Token': token}) if result['runtime_token_configured'] else (0, {})
    ver_status, ver_body = get('http://127.0.0.1:8088/api/version', {'X-QwenPaw-Runtime-Token': token}) if result['runtime_token_configured'] else (0, {})
    loaded = body.get('agents_loaded', [])
    result.update(runtime_unauthorized_status=unauth, runtime_authorized_status=auth,
                  runtime_ready=body.get('status') == 'ok' and isinstance(loaded, list) and agent_id in loaded, agents_loaded_count=len(loaded) if isinstance(loaded, list) else 0,
                  runtime_version_status=ver_status, runtime_version_matches=ver_body.get('version') == '2.2.1')
    auth, body = get('http://astrbot:9186/health', {'Authorization': 'Bearer ' + bridge}) if result['bridge_token_configured'] else (0, {})
    result.update(gateway_cross_status=auth, gateway_cross_ready=body.get('ready') is True)
    return result

if __name__ == '__main__':
    try:
        answer = {'astrbot': astrbot, 'qwenpaw': qwenpaw}[sys.argv[1]]()
    except Exception:
        answer = {'probe_ok': False}
    print(json.dumps(answer, separators=(',', ':')))
'''

NAPCAT_PROBE = r'''
exists=false; readable=false; writable=false
if [ -d /bridge-files ] && [ ! -L /bridge-files ]; then exists=true; fi
if [ "$exists" = true ] && [ -r /bridge-files ] && [ -x /bridge-files ]; then readable=true; fi
if [ "$exists" = true ] && [ -w /bridge-files ]; then writable=true; fi
printf '{"probe_ok":true,"shared_exists":%s,"shared_readable":%s,"shared_writable_indicated":%s}\n' "$exists" "$readable" "$writable"
'''

BOOL_PROBE_FIELDS = frozenset((
    "probe_ok", "shared_exists", "shared_readable", "shared_writable_indicated", "shared_world_access", "uid_is_root",
    "plugin_present", "config_file_present", "all_tools_enabled", "version_matches", "bridge_token_configured", "runtime_token_configured",
    "qwen_url_expected", "gateway_ready", "qwen_cross_ready", "target_profile_configured", "astrbot_channel_enabled", "astrbot_channel_requested",
    "console_channel_requested", "callback_url_expected", "runtime_ready", "runtime_version_matches", "gateway_cross_ready",
))
INT_PROBE_FIELDS = frozenset((
    "owner_count", "tool_count", "profile_count", "channel_enabled_count", "agents_loaded_count", "gateway_unauthorized_status",
    "gateway_authorized_status", "qwen_cross_status", "runtime_unauthorized_status", "runtime_authorized_status", "runtime_version_status", "gateway_cross_status",
))


class CommandUnavailable(RuntimeError):
    pass


def command(args: list[str]) -> str:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CommandUnavailable("command_unavailable") from None
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        raise CommandUnavailable("command_unavailable")
    return result.stdout.strip()


def parse_json(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw) > MAX_OUTPUT:
        raise CommandUnavailable("invalid_probe_output")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise CommandUnavailable("invalid_probe_output") from None
    if not isinstance(value, dict):
        raise CommandUnavailable("invalid_probe_output")
    return value


def sanitize_probe(value: dict) -> dict:
    return {
        **{key: value[key] for key in BOOL_PROBE_FIELDS if type(value.get(key)) is bool},
        **{key: value[key] for key in INT_PROBE_FIELDS if type(value.get(key)) is int and 0 <= value[key] <= 100000},
    }


def sanitize_snapshot(value: dict) -> dict:
    """Keep Docker metadata useful for comparisons; never expose it directly."""
    result = {key: value[key] for key in ("running", "oom_killed", "host_network") if type(value.get(key)) is bool}
    result["restart_count"] = value.get("restart_count") if type(value.get("restart_count")) is int else 0
    result["image"] = value.get("image") if isinstance(value.get("image"), str) else ""
    result["networks"] = {}
    networks = value.get("networks")
    if isinstance(networks, dict):
        for name, info in networks.items():
            if isinstance(info, dict) and isinstance(info.get("aliases"), list):
                result["networks"][name] = {"aliases": [alias for alias in info["aliases"] if isinstance(alias, str)]}
    result["mounts"] = []
    if isinstance(value.get("mounts"), list):
        for mount in value["mounts"]:
            if isinstance(mount, dict) and isinstance(mount.get("source"), str) and type(mount.get("rw")) is bool:
                result["mounts"].append({"source": mount["source"], "rw": mount["rw"], "type": mount.get("type") if isinstance(mount.get("type"), str) else ""})
    result["ports"] = {}
    ports = value.get("ports")
    result["port_metadata_valid"] = "ports" in value and (ports is None or isinstance(ports, dict))
    if isinstance(ports, dict):
        for port, bindings in ports.items():
            if not isinstance(port, str) or not re.fullmatch(r"[0-9]+/(tcp|udp|sctp)", port):
                result["port_metadata_valid"] = False
            if bindings is None:
                continue
            if isinstance(bindings, list):
                result["ports"][port] = []
                for binding in bindings:
                    if not isinstance(binding, dict) or not isinstance(binding.get("HostIp"), str) or not isinstance(binding.get("HostPort"), str):
                        result["port_metadata_valid"] = False
                        continue
                    result["ports"][port].append({"HostIp": binding["HostIp"], "HostPort": binding["HostPort"]})
            else:
                result["port_metadata_valid"] = False
    return result


def host_http_status(port: int, path: str) -> int:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, ValueError):
        return 0


def management_bindings(snapshot: dict, role: str) -> tuple[bool, bool, list[int]]:
    ports = snapshot.get("ports") or {}
    if not isinstance(ports, dict):
        return False, False, []
    wanted = {"astrbot": "6185/tcp", "console": "8088/tcp", "napcat": "6099/tcp"}.get(role)
    private = {"astrbot": {"6199/tcp", "9186/tcp"}, "qwenpaw": {"8088/tcp"}, "napcat": {"3000/tcp", "3001/tcp"}}.get(role, set())
    safe = not snapshot.get("host_network", True) and snapshot.get("port_metadata_valid") is True
    unpublished, local = True, []
    for container_port, bindings in ports.items():
        if bindings and container_port in private:
            unpublished = False
        for binding in bindings or []:
            if not isinstance(binding, dict):
                safe = False
                continue
            if binding.get("HostIp") not in ("127.0.0.1", "::1"):
                safe = False
            host_port = binding.get("HostPort", "")
            if container_port == wanted and binding.get("HostIp") == "127.0.0.1" and isinstance(host_port, str) and host_port.isdecimal() and 1 <= int(host_port) <= 65535:
                local.append(int(host_port))
    return safe, unpublished, local


def run_doctor(*, mode: str = "fresh", containers: dict[str, str] | None = None, network: str = "bot-combined-network", runner: Callable = command, http_probe: Callable = host_http_status) -> dict:
    names = {"astrbot": "astrbot-weixin" if mode == "existing" else "bot-astrbot", "qwenpaw": "bot-qwenpaw", "napcat": "bot-napcat", "console": "bot-qwenpaw-console"}
    names.update(containers or {})
    if mode not in ("fresh", "existing") or set(names) != set(EXPECTED_IMAGES) or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in (*names.values(), network)):
        raise ValueError("Use valid Docker names and fresh/existing mode.")
    checks = []

    def add(key: str, status: str, **details):
        checks.append({"id": key, "status": status, "details": details})

    snapshots, probes = {}, {}
    for role, name in names.items():
        try:
            snap = sanitize_snapshot(parse_json(runner(["docker", "inspect", "--format", INSPECT_FORMAT, name])))
        except (CommandUnavailable, OSError, ValueError):
            add(f"{role}.container", "fail", available=False)
            continue
        snapshots[role] = snap
        running, oom = snap.get("running") is True, snap.get("oom_killed") is True
        restart_count = snap.get("restart_count")
        restart_count = restart_count if type(restart_count) is int and 0 <= restart_count <= 100000 else 0
        add(f"{role}.container", "ok" if running and not oom else "fail", available=True, running=running, oom_killed=oom, restart_count=restart_count)
        image = snap.get("image")
        matches = isinstance(image, str) and (image == EXPECTED_IMAGES[role] or image.endswith("/" + EXPECTED_IMAGES[role]))
        add(f"{role}.fixed_image", "ok" if matches else "warn", expected_image=EXPECTED_IMAGES[role], image_ref_matches=matches)
        safe, unpublished, local_ports = management_bindings(snap, role)
        add(f"{role}.port_isolation", "ok" if safe and unpublished else "fail", management_bindings_loopback=safe, protocol_ports_unpublished=unpublished)
        if role in ("console", "napcat", "astrbot"):
            path = {"console": "/api/version", "napcat": "/webui/", "astrbot": "/"}[role]
            statuses = [http_probe(port, path) for port in local_ports] if running else []
            statuses = [x for x in statuses if type(x) is int and 0 <= x <= 599]
            reachable = any(x == 200 for x in statuses)
            optional = role == "astrbot" and mode == "existing" and not local_ports
            add(f"{role}.loopback_entry", "skip" if optional else "ok" if reachable else "warn", binding_present=bool(local_ports), http_200=reachable)
        if role in ("astrbot", "qwenpaw", "napcat") and running:
            args = ["docker", "exec", name, "sh", "-c", NAPCAT_PROBE] if role == "napcat" else ["docker", "exec", name, "python", "-c", CONTAINER_PROBE, role]
            try:
                probe = sanitize_probe(parse_json(runner(args)))
            except (CommandUnavailable, OSError, ValueError):
                probe = {}
            probes[role] = probe
            add(f"{role}.readonly_probe", "ok" if probe.get("probe_ok") else "warn", probe_completed=probe.get("probe_ok", False))

    participants = tuple(EXPECTED_IMAGES)
    shared = all(network in (snapshots.get(role, {}).get("networks") or {}) for role in participants)
    aliases_ok = True
    for role, alias in (("astrbot", "astrbot"), ("qwenpaw", "qwenpaw")):
        info = (snapshots.get(role, {}).get("networks") or {}).get(network, {})
        aliases_ok = aliases_ok and isinstance(info, dict) and alias in (info.get("aliases") or [])
    try:
        external_egress = json.loads(runner(["docker", "network", "inspect", "--format", "{{json .Internal}}", network])) is False
    except (CommandUnavailable, ValueError, OSError):
        external_egress = False
    add("network.shared", "ok" if shared and aliases_ok and external_egress else "fail", all_containers_share_network=shared, required_aliases_present=aliases_ok, network_allows_egress=external_egress)

    sources, mount_modes = [], []
    for role in ("astrbot", "qwenpaw", "napcat"):
        mounts = snapshots.get(role, {}).get("mounts") or []
        mount = mounts[0] if isinstance(mounts, list) and len(mounts) == 1 and isinstance(mounts[0], dict) else {}
        source = mount.get("source")
        sources.append(source if isinstance(source, str) and source else None)
        expected_write = role != "napcat"
        declared = mount.get("rw") is expected_write and mount.get("type") == "bind"
        mount_modes.append(declared)
        probe = probes.get(role, {})
        accessible = probe.get("shared_exists") is True and probe.get("shared_readable") is True
        if role != "napcat":
            accessible = accessible and probe.get("shared_writable_indicated") is True
        else:
            accessible = accessible and probe.get("shared_writable_indicated") is False
        add(f"{role}.shared_directory", "ok" if declared and accessible and not probe.get("shared_world_access") else "warn", expected_write=expected_write, declared_mount_mode_matches=declared, directory_access_indicated=accessible, world_access=probe.get("shared_world_access", False))
    same_source = all(sources) and len(set(sources)) == 1
    add("media.shared_mount", "ok" if same_source and all(mount_modes) else "fail", same_bind_source=bool(same_source), rw_rw_ro_declared=all(mount_modes), write_test_performed=False)

    for role in ("astrbot", "qwenpaw"):
        probe = probes.get(role, {})
        version_ok = probe.get("version_matches") is True
        add(f"{role}.application_version", "ok" if version_ok else "warn", expected_version=EXPECTED_VERSIONS[role], version_matches=version_ok)
        add(f"{role}.plugin_present", "ok" if probe.get("plugin_present") else "warn", present=probe.get("plugin_present", False))
    ab, qp = probes.get("astrbot", {}), probes.get("qwenpaw", {})
    gateway_ok = ab.get("gateway_authorized_status") == 200 and ab.get("gateway_ready") is True and ab.get("gateway_unauthorized_status") == 401
    add("gateway.health_auth", "ok" if gateway_ok else "fail", ready=ab.get("gateway_ready", False), authenticated_200=ab.get("gateway_authorized_status") == 200, unauthenticated_401=ab.get("gateway_unauthorized_status") == 401, token_configured=ab.get("bridge_token_configured", False))
    runtime_ok = qp.get("runtime_authorized_status") == 200 and qp.get("runtime_ready") is True and qp.get("runtime_unauthorized_status") == 401 and qp.get("runtime_version_matches") is True
    add("qwenpaw.runtime_health_auth", "ok" if runtime_ok else "fail", ready=qp.get("runtime_ready", False), authenticated_200=qp.get("runtime_authorized_status") == 200, unauthenticated_401=qp.get("runtime_unauthorized_status") == 401, version_matches=qp.get("runtime_version_matches", False), agents_loaded_count=qp.get("agents_loaded_count", 0), token_configured=qp.get("runtime_token_configured", False))
    cross_ok = ab.get("qwen_cross_status") == 200 and ab.get("qwen_cross_ready") is True and qp.get("gateway_cross_status") == 200 and qp.get("gateway_cross_ready") is True and ab.get("qwen_url_expected") is True and qp.get("callback_url_expected") is True
    add("bridge.internal_http", "ok" if cross_ok else "fail", astrbot_to_qwen_ready=ab.get("qwen_cross_ready", False), qwen_to_gateway_ready=qp.get("gateway_cross_ready", False), qwen_url_expected=ab.get("qwen_url_expected", False), callback_url_expected=qp.get("callback_url_expected", False))
    owner_count, tool_count = ab.get("owner_count", 0), ab.get("tool_count", 0)
    add("bridge.owner_configuration", "ok" if owner_count else "warn", configured=owner_count > 0, owner_count=owner_count)
    add("bridge.tool_configuration", "ok" if tool_count or ab.get("all_tools_enabled") else "warn", configured=bool(tool_count or ab.get("all_tools_enabled")), tool_count=tool_count, all_tools_enabled=ab.get("all_tools_enabled", False))
    channel_ok = qp.get("astrbot_channel_enabled") is True and qp.get("astrbot_channel_requested") is True
    add("qwenpaw.channel_configuration", "ok" if channel_ok else "warn", astrbot_enabled=qp.get("astrbot_channel_enabled", False), astrbot_requested=qp.get("astrbot_channel_requested", False), console_requested=qp.get("console_channel_requested", False), channel_enabled_count=qp.get("channel_enabled_count", 0), target_profile_configured=qp.get("target_profile_configured", False), profile_count=qp.get("profile_count", 0))
    return {"schema_version": 1, "read_only": True, "summary": {status: sum(c["status"] == status for c in checks) for status in ("ok", "warn", "fail", "skip")}, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fresh", "existing"), default="fresh")
    parser.add_argument("--network", default="bot-combined-network")
    for role in EXPECTED_IMAGES:
        parser.add_argument("--" + role + "-container")
    args = parser.parse_args()
    overrides = {role: getattr(args, role + "_container") for role in EXPECTED_IMAGES if getattr(args, role + "_container")}
    try:
        report = run_doctor(mode=args.mode, containers=overrides, network=args.network)
    except ValueError:
        print(json.dumps({"schema_version": 1, "read_only": True, "error": "invalid_arguments"}))
        return 2
    print(json.dumps(report, indent=2))
    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
