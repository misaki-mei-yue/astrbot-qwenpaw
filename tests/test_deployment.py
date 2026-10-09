"""Guard secret preservation, isolation, port exposure, and existing-data safety."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deployment_prepare", ROOT / "deploy/prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def test_read_only_preflight_is_valid_python(self):
        ast.parse((ROOT / "deploy/preflight.py").read_text(encoding="utf-8"))

    def test_credentials_and_napcat_config_survive_second_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            first = prepare.prepare("fresh", root)
            self.assertEqual(first["OWNER_USER_IDS"], "")
            self.assertEqual(first["TOOL_ALLOWLIST"], "")
            self.assertEqual(first["QWENPAW_AGENT_ID"], "default")
            self.assertEqual(len(set(first[key] for key in prepare.SECRET_KEYS)), 3)
            nap = root / "state/napcat/config/onebot11.json"
            original = nap.read_bytes()
            changed = json.loads(original)
            changed["network"]["websocketClients"][0]["heartInterval"] = 45000
            nap.write_text(json.dumps(changed), encoding="utf-8")
            expected = nap.read_bytes()
            second = prepare.prepare("fresh", root)
            self.assertTrue((root / "state/napcat/config/napcat.json").is_file())
            for key in prepare.SECRET_KEYS:
                self.assertGreaterEqual(len(first[key]), 32)
                self.assertEqual(first[key], second[key])
            self.assertEqual(nap.read_bytes(), expected)
            env = prepare.parse_env(root / ".env")
            self.assertEqual(env["BRIDGE_TOKEN"], first["BRIDGE_TOKEN"])
            if os.name == "posix":
                self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)

    def test_existing_installation_and_credentials_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "bot"
            existing.mkdir()
            compose = existing / "compose.yaml"
            compose.write_text("# original private deployment\n", encoding="utf-8")
            old_env = existing / ".env"
            old_env.write_text("ASTRBOT_IMAGE=original-digest\n", encoding="utf-8")
            database = existing / "data/database.db"
            database.parent.mkdir()
            database.write_bytes(b"existing-database")
            before = {path: path.read_bytes() for path in (compose, old_env, database)}
            with self.assertRaisesRegex(ValueError, "existing AstrBot"):
                prepare.prepare("fresh", existing)
            addon = existing / "combined"
            prepare.prepare("existing", addon, existing)
            self.assertFalse((addon / "state/astrbot").exists())
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_mode_change_and_short_existing_key_fail_without_key_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            prepare.prepare("fresh", root)
            before = (root / ".env").read_bytes()
            with self.assertRaisesRegex(ValueError, "different deployment mode"):
                prepare.prepare("existing", root, Path(folder))
            self.assertEqual((root / ".env").read_bytes(), before)
            (root / ".env").write_text("BRIDGE_TOKEN=short\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to reset"):
                prepare.prepare("fresh", root)
            self.assertEqual((root / ".env").read_text(), "BRIDGE_TOKEN=short\n")

    def test_env_round_trip_does_not_evaluate_shell_content(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            content = "path with spaces and 'quotes' $(do-not-execute)"
            env.write_text("PROJECT_SOURCE=" + prepare.env_value(content) + "\n", encoding="utf-8")
            self.assertEqual(prepare.parse_env(env)["PROJECT_SOURCE"], content)
            env.write_text("BRIDGE_TOKEN=first\nBRIDGE_TOKEN=second\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                prepare.parse_env(env)

    def test_refuses_symlink_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "real"
            target.mkdir()
            link = Path(folder) / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation unavailable on this host")
            with self.assertRaisesRegex(ValueError, "symlink"):
                prepare.prepare("fresh", link)
            self.assertFalse((target / ".env").exists())

    def test_parallel_setup_cannot_replace_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            prepare.prepare("fresh", root)
            before = (root / ".env").read_bytes()
            lock = root / ".combined-setup.lock"
            lock.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "setup lock"):
                prepare.prepare("fresh", root)
            self.assertEqual((root / ".env").read_bytes(), before)

    def test_normalized_filesystem_root_is_rejected(self):
        root = Path(tempfile.gettempdir()).anchor
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            prepare.prepare("fresh", Path(root) / "does-not-exist" / "..")


@unittest.skipIf(yaml is None, "Install PyYAML to validate Compose structures")
class ComposeTests(unittest.TestCase):
    def load(self, name):
        return yaml.safe_load((ROOT / "deploy" / name).read_text(encoding="utf-8"))

    def test_loopback_management_and_private_protocols(self):
        compose = self.load("compose.yaml")
        for service in compose["services"].values():
            self.assertNotIn("privileged", service)
            self.assertNotIn("network_mode", service)
            for port in service.get("ports", []):
                self.assertTrue(port.startswith("127.0.0.1:"), port)
                self.assertNotIn(port.split(":")[-1], ("6199", "9186"))
            for volume in service.get("volumes", []):
                self.assertNotIn("docker.sock", volume)
        self.assertNotIn("ports", compose["services"]["qwenpaw"])
        self.assertEqual(compose["services"]["qwenpaw"]["environment"]["QWENPAW_ENABLED_CHANNELS"], "console,astrbot")
        self.assertEqual(compose["services"]["qwenpaw"]["environment"]["QWENPAW_AGENT_ID"], "${QWENPAW_AGENT_ID:-default}")
        self.assertEqual(compose["services"]["astrbot"]["image"], "soulter/astrbot:v4.25.1")
        self.assertEqual(compose["services"]["napcat"]["image"], "mlikiowa/napcat-docker:v4.18.33")
        self.assertEqual(compose["services"]["qwenpaw"]["build"]["context"].split("#")[-1], "cae5773707b26ab2fd00903f84b712387894b256")

    def test_only_dedicated_exports_are_shared(self):
        compose = self.load("compose.yaml")
        for name in ("astrbot", "qwenpaw", "napcat"):
            volumes = compose["services"][name]["volumes"]
            matches = [volume for volume in volumes if ":/bridge-files" in volume]
            self.assertEqual(len(matches), 1)
            self.assertIn("/state/bridge-files:", matches[0])
            if name == "napcat":
                self.assertTrue(matches[0].endswith(":ro"))
                self.assertFalse(any("/AstrBot/data" in item for item in volumes))

    def test_addon_cannot_create_second_astrbot_or_replace_existing_data(self):
        addon = self.load("compose.addon.yaml")
        self.assertNotIn("astrbot", addon["services"])
        self.assertTrue(addon["networks"]["bridge"]["external"])
        self.assertEqual(addon["services"]["qwenpaw"]["environment"]["QWENPAW_AGENT_ID"], "${QWENPAW_AGENT_ID:-default}")
        override = self.load("compose.astrbot.override.yaml")
        astrbot = override["services"]["astrbot"]
        for forbidden in ("image", "build", "ports", "container_name", "command", "entrypoint"):
            self.assertNotIn(forbidden, astrbot)
        self.assertFalse(any(volume.endswith(":/AstrBot/data") for volume in astrbot["volumes"]))
        self.assertEqual(override["networks"]["combined"]["external"], True)


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is optional; Compose parsing needs no daemon")
class ComposeCLITests(unittest.TestCase):
    def fixture_env(self, folder):
        path = Path(folder) / ".env"
        path.write_text(
            "INSTALL_ROOT=/opt/bot-combined\n"
            f"PROJECT_SOURCE={ROOT.as_posix()}\n"
            "COMPOSE_PROJECT_NAME=bot-combined-addon\n"
            "BRIDGE_TOKEN=test-only-bridge-not-real-credentials\n"
            "QWENPAW_RUNTIME_INTERNAL_TOKEN=test-only-runtime-not-real-credentials\n"
            "ONEBOT_TOKEN=test-only-onebot-not-real-credentials\n",
            encoding="utf-8",
        )
        return path

    def render(self, env, files, project=None):
        command = ["docker", "compose", "--env-file", str(env)]
        if project:
            command += ["-p", project]
        for file in files:
            command += ["-f", str(file)]
        command += ["config", "--format", "json"]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_and_addon_pass_actual_compose_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture_env(folder)
            for name in ("compose.yaml", "compose.addon.yaml"):
                compose = self.render(env, [ROOT / "deploy" / name])
                self.assertEqual(compose["services"]["qwenpaw"]["environment"]["QWENPAW_AGENT_ID"], "default")
                self.assertNotIn("ports", compose["services"]["qwenpaw"])
                for service in compose["services"].values():
                    for port in service.get("ports", []):
                        self.assertEqual(port["host_ip"], "127.0.0.1")

    def test_explicit_original_project_preserves_existing_mounts_and_network(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture_env(folder)
            base = Path(folder) / "existing-compose.yaml"
            base.write_text(
                "name: original-project\nservices:\n  astrbot:\n"
                "    image: soulter/astrbot:v4.25.1\n    container_name: original-astrbot\n"
                "    volumes:\n      - /opt/bot/data:/AstrBot/data\n"
                "    networks:\n      edge:\n        aliases: [original-astrbot]\n"
                "networks:\n  edge:\n    external: true\n    name: original-edge\n",
                encoding="utf-8",
            )
            compose = self.render(env, [base, ROOT / "deploy/compose.astrbot.override.yaml"], "original-project")
            self.assertEqual(compose["name"], "original-project")
            service = compose["services"]["astrbot"]
            self.assertEqual(service["container_name"], "original-astrbot")
            self.assertEqual(service["image"], "soulter/astrbot:v4.25.1")
            data_mounts = [item for item in service["volumes"] if item["target"] == "/AstrBot/data"]
            self.assertEqual(len(data_mounts), 1)
            self.assertTrue(data_mounts[0]["source"].replace("\\", "/").endswith("/opt/bot/data"))
            self.assertEqual(service["networks"]["edge"]["aliases"], ["original-astrbot"])
            self.assertEqual(service["networks"]["combined"]["aliases"], ["astrbot"])
            self.assertNotIn("ports", service)


if __name__ == "__main__":
    unittest.main()
