"""Read-only diagnostic boundaries, without requiring a Docker daemon."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("deployment_doctor", Path(__file__).parents[1] / "deploy/doctor.py")
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


def application_probe(role):
    result = dict(probe_ok=True, shared_exists=True, shared_readable=True,
                  shared_writable_indicated=role != "napcat", shared_world_access=False,
                  plugin_present=True, config_file_present=True, version_matches=True,
                  bridge_token_configured=True, runtime_token_configured=True)
    if role == "astrbot":
        result.update(owner_count=1, tool_count=2, all_tools_enabled=False,
                      qwen_url_expected=True, gateway_unauthorized_status=401,
                      gateway_authorized_status=200, gateway_ready=True,
                      qwen_cross_status=200, qwen_cross_ready=True)
    if role == "qwenpaw":
        result.update(profile_count=1, target_profile_configured=True,
                      channel_enabled_count=2, astrbot_channel_enabled=True,
                      astrbot_channel_requested=True, console_channel_requested=True,
                      callback_url_expected=True, runtime_unauthorized_status=401,
                      runtime_authorized_status=200, runtime_ready=True,
                      agents_loaded_count=1, runtime_version_status=200,
                      runtime_version_matches=True, gateway_cross_status=200,
                      gateway_cross_ready=True)
    return result


class FakeDocker:
    def __init__(self, existing=False):
        self.calls = []
        self.names = {"astrbot": "astrbot-weixin" if existing else "bot-astrbot",
                      "qwenpaw": "bot-qwenpaw", "napcat": "bot-napcat", "console": "bot-qwenpaw-console"}
        self.snapshots = {}
        self.probes = {role: application_probe(role) for role in ("astrbot", "qwenpaw", "napcat")}
        for role in doctor.EXPECTED_IMAGES:
            admin = {"astrbot": "6185", "napcat": "6099", "console": "8088"}.get(role)
            ports = {admin + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": admin}]} if admin else {}
            self.snapshots[role] = dict(running=True, oom_killed=False, restart_count=0,
                image=doctor.EXPECTED_IMAGES[role], host_network=False, ports=ports,
                mounts=[dict(source="/private/deployment/state/bridge-files", rw=role != "napcat", type="bind")] if role != "console" else [],
                networks={"bot-combined-network": {"aliases": [role]}})
        if existing:
            self.snapshots["astrbot"]["ports"] = {}
        self.internal = False

    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ["docker", "network", "inspect"]:
            return json.dumps(self.internal)
        role = next(key for key, name in self.names.items() if name == args[-1] or (len(args) > 2 and name == args[2]))
        if args[1] == "inspect":
            return json.dumps(self.snapshots[role])
        if args[1] == "exec":
            return json.dumps(self.probes[role])
        raise AssertionError("Unexpected command")


class DoctorTests(unittest.TestCase):
    def check(self, fake=None, **kwargs):
        fake = fake or FakeDocker()
        report = doctor.run_doctor(runner=fake, http_probe=lambda port, path: 200, **kwargs)
        return report, {item["id"]: item for item in report["checks"]}

    def test_healthy_report_and_only_read_commands(self):
        fake = FakeDocker()
        report, checks = self.check(fake)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["warn"], 0)
        self.assertTrue(report["read_only"])
        for check_id in ("gateway.health_auth", "qwenpaw.runtime_health_auth", "bridge.internal_http", "network.shared", "media.shared_mount"):
            self.assertEqual(checks[check_id]["status"], "ok")
        for args in fake.calls:
            self.assertIn(args[1], ("inspect", "network", "exec"))
            if args[1] == "network":
                self.assertEqual(args[2], "inspect")
            if args[1] == "inspect":
                self.assertNotIn(".Config.Env", args[3])
        self.assertFalse(checks["media.shared_mount"]["details"]["write_test_performed"])

    def test_existing_name_and_private_astrbot_entry(self):
        fake = FakeDocker(existing=True)
        report, checks = self.check(fake, mode="existing")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(checks["astrbot.loopback_entry"]["status"], "skip")
        self.assertTrue(any(args[-1] == "astrbot-weixin" for args in fake.calls if args[1] == "inspect"))

    def test_reports_are_allowlisted_and_never_include_private_values(self):
        fake = FakeDocker()
        sensitive = ["secret-token", "private-chat", "qq-123456789", "/private/deployment/state/bridge-files"]
        for probe in fake.probes.values():
            probe.update(BRIDGE_TOKEN=sensitive[0], chat=sensitive[1], owner_id=sensitive[2])
        fake.snapshots["astrbot"].update(env=sensitive, logs=sensitive)
        report, _ = self.check(fake)
        serialized = json.dumps(report)
        for value in sensitive:
            self.assertNotIn(value, serialized)
        for check in report["checks"]:
            for key, value in check["details"].items():
                if key in ("expected_image", "expected_version"):
                    self.assertIn(value, (*doctor.EXPECTED_IMAGES.values(), *doctor.EXPECTED_VERSIONS.values()))
                else:
                    self.assertIn(type(value), (bool, int))

    def test_closed_owner_and_tools_are_clear_without_ids(self):
        fake = FakeDocker()
        fake.probes["astrbot"].update(owner_count=0, tool_count=0)
        _, checks = self.check(fake)
        for name in ("owner", "tool"):
            self.assertEqual(checks[f"bridge.{name}_configuration"]["status"], "warn")
            self.assertFalse(checks[f"bridge.{name}_configuration"]["details"]["configured"])
        fake.probes["astrbot"]["all_tools_enabled"] = True
        _, checks = self.check(fake)
        self.assertEqual(checks["bridge.tool_configuration"]["status"], "ok")

    def test_port_publish_and_host_network_are_detected(self):
        for change in ("public_admin", "gateway", "onebot", "runtime", "host_network"):
            with self.subTest(change=change):
                fake = FakeDocker()
                role = "qwenpaw" if change == "runtime" else "astrbot"
                snap = fake.snapshots[role]
                if change == "public_admin":
                    snap["ports"]["6185/tcp"][0]["HostIp"] = "0.0.0.0"
                elif change == "host_network":
                    snap["host_network"] = True
                else:
                    port = {"gateway": "9186", "onebot": "6199", "runtime": "8088"}[change]
                    snap["ports"][port + "/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": port}]
                _, checks = self.check(fake)
                self.assertEqual(checks[role + ".port_isolation"]["status"], "fail")

    def test_malformed_ports_fail_closed(self):
        for ports in ([], "secret-token", {"6185/tcp": "secret-token"}, {"6185/tcp": [None]}, {"6185/tcp": [{"HostIp": [], "HostPort": "6185"}]}):
            fake = FakeDocker()
            fake.snapshots["astrbot"]["ports"] = ports
            report, checks = self.check(fake)
            self.assertEqual(checks["astrbot.port_isolation"]["status"], "fail")
            self.assertNotIn("secret-token", json.dumps(report))
        fake.snapshots["astrbot"]["ports"] = None
        _, checks = self.check(fake)
        self.assertEqual(checks["astrbot.port_isolation"]["status"], "ok")

    def test_mount_modes_source_and_permissions_are_checked_without_writes(self):
        for change in ("different_source", "napcat_rw", "unreadable", "world_access"):
            with self.subTest(change=change):
                fake = FakeDocker()
                if change == "different_source":
                    fake.snapshots["qwenpaw"]["mounts"][0]["source"] = "/other/private/path"
                if change == "napcat_rw":
                    fake.snapshots["napcat"]["mounts"][0]["rw"] = True
                if change == "unreadable":
                    fake.probes["qwenpaw"]["shared_readable"] = False
                if change == "world_access":
                    fake.probes["qwenpaw"]["shared_world_access"] = True
                _, checks = self.check(fake)
                expected = "fail" if change in ("different_source", "napcat_rw") else "ok"
                self.assertEqual(checks["media.shared_mount"]["status"], expected)
                if change in ("unreadable", "world_access"):
                    self.assertEqual(checks["qwenpaw.shared_directory"]["status"], "warn")

    def test_authentication_startup_and_wrong_config_do_not_pass(self):
        scenarios = (("astrbot", "gateway_unauthorized_status", 200, "gateway.health_auth"),
                     ("qwenpaw", "runtime_authorized_status", 503, "qwenpaw.runtime_health_auth"),
                     ("qwenpaw", "runtime_unauthorized_status", 200, "qwenpaw.runtime_health_auth"),
                     ("astrbot", "qwen_url_expected", False, "bridge.internal_http"),
                     ("qwenpaw", "callback_url_expected", False, "bridge.internal_http"))
        for role, key, value, check_id in scenarios:
            with self.subTest(key=key):
                fake = FakeDocker()
                fake.probes[role][key] = value
                _, checks = self.check(fake)
                self.assertEqual(checks[check_id]["status"], "fail")

    def test_stopped_or_oom_state_does_not_claim_health(self):
        fake = FakeDocker()
        fake.snapshots["astrbot"].update(running=False, oom_killed=True, restart_count=7)
        _, checks = self.check(fake)
        self.assertEqual(checks["astrbot.container"]["status"], "fail")
        self.assertEqual(checks["astrbot.container"]["details"]["restart_count"], 7)
        self.assertEqual(checks["gateway.health_auth"]["status"], "fail")
        self.assertFalse(any(args[1] == "exec" and args[2] == "bot-astrbot" for args in fake.calls))

    def test_missing_daemon_and_malformed_probe_output_are_redacted(self):
        def unavailable(args):
            raise doctor.CommandUnavailable("secret-token private-chat")
        report, checks = self.check(unavailable)
        self.assertGreater(report["summary"]["fail"], 0)
        self.assertNotIn("secret-token", json.dumps(report))
        fake = FakeDocker()
        original = fake.__call__
        def malformed(args):
            if args[1] == "exec":
                return "secret-token private-chat"
            return original(args)
        report, checks = self.check(malformed)
        self.assertNotIn("secret-token", json.dumps(report))
        self.assertEqual(checks["astrbot.readonly_probe"]["status"], "warn")

    def test_invalid_names_are_rejected_before_docker(self):
        for kwargs in ({"containers": {"astrbot": "--privileged"}}, {"network": "bad\nname"}, {"mode": "overwrite"}, {"containers": {"surprise": "name"}}):
            with self.subTest(kwargs=kwargs):
                fake = FakeDocker()
                with self.assertRaises(ValueError):
                    self.check(fake, **kwargs)
                self.assertFalse(fake.calls)

    def test_probe_types_and_bounds_are_strict(self):
        self.assertEqual(doctor.sanitize_probe(dict(probe_ok="true", owner_count=True, tool_count=-1, agents_loaded_count=1.5, profile_count=100001, secret="value")), {})
        self.assertEqual(doctor.sanitize_probe(dict(probe_ok=True, owner_count=2)), dict(probe_ok=True, owner_count=2))

    def test_mirror_refs_and_opaque_wrong_images(self):
        fake = FakeDocker()
        fake.snapshots["astrbot"]["image"] = "m.daocloud.io/docker.io/" + doctor.EXPECTED_IMAGES["astrbot"]
        fake.snapshots["qwenpaw"]["image"] = "secret-token/private:unknown"
        report, checks = self.check(fake)
        self.assertEqual(checks["astrbot.fixed_image"]["status"], "ok")
        self.assertEqual(checks["qwenpaw.fixed_image"]["status"], "warn")
        self.assertNotIn("secret-token", json.dumps(report))

    def test_subprocess_errors_never_return_stderr_or_original_exception(self):
        outputs = (subprocess.CompletedProcess([], 1, "secret-token", "private-chat"),
                   subprocess.CompletedProcess([], 0, "x" * (doctor.MAX_OUTPUT + 1), "private-chat"))
        for result in outputs:
            with patch.object(doctor.subprocess, "run", return_value=result):
                with self.assertRaises(doctor.CommandUnavailable) as caught:
                    doctor.command(["docker", "inspect"])
                self.assertEqual(str(caught.exception), "command_unavailable")
        with patch.object(doctor.subprocess, "run", side_effect=subprocess.TimeoutExpired("secret-token", 30)):
            with self.assertRaises(doctor.CommandUnavailable) as caught:
                doctor.command(["docker", "inspect"])
            self.assertEqual(str(caught.exception), "command_unavailable")

    def test_real_container_probe_filters_config_before_returning(self):
        namespace = {"__name__": "doctor_probe_test"}
        exec(compile(doctor.CONTAINER_PROBE, "<container-probe>", "exec"), namespace)
        sensitive = dict(owner_user_ids=["qq-123456789", "wx-private-user"],
                         tool_allowlist=["read_file", "unified_browser"], bridge_token="a" * 64, runtime_token="b" * 64)
        namespace["common"] = lambda: dict(probe_ok=True)
        namespace["read_json"] = lambda path: sensitive
        namespace["version"] = lambda path, variable: "4.25.1"
        seen_requests = []
        def get(url, headers=None):
            seen_requests.append((url, headers))
            if not headers:
                return 401, {}
            return 200, {"ready": True, "status": "ok", "agents_loaded": ["default"], "chat": "private-chat"}
        namespace["get"] = get
        with patch.dict(namespace["os"].environ, {}, clear=True):
            probe = namespace["astrbot"]()
        self.assertEqual(probe["owner_count"], 2)
        self.assertEqual(probe["tool_count"], 2)
        self.assertTrue(probe["qwen_cross_ready"])
        for value in ("qq-123456789", "wx-private-user", "private-chat", "a" * 64, "b" * 64):
            self.assertNotIn(value, json.dumps(probe))
        self.assertEqual({url for url, _ in seen_requests}, {"http://127.0.0.1:9186/health", "http://qwenpaw:8088/api/healthz"})
        with patch.dict(namespace["os"].environ, {"OWNER_USER_IDS": "", "TOOL_ALLOWLIST": ""}, clear=True):
            self.assertEqual(namespace["astrbot"]()["owner_count"], 2)
            self.assertEqual(namespace["astrbot"]()["tool_count"], 2)
        with patch.dict(namespace["os"].environ, {"OWNER_USER_IDS": "override-owner", "TOOL_ALLOWLIST": "*"}, clear=True):
            changed = namespace["astrbot"]()
            self.assertEqual(changed["owner_count"], 1)
            self.assertEqual(changed["tool_count"], 0)
            self.assertTrue(changed["all_tools_enabled"])


if __name__ == "__main__":
    unittest.main()
