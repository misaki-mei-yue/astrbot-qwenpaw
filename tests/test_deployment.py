"""Guard secret preservation, isolation, port exposure, and existing-data safety."""
import ast
import hashlib
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

    def test_fresh_qq_ends_share_onebot_credentials_and_private_listener(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            env = prepare.prepare("fresh", root)
            path = root / "state/astrbot/cmd_config.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(config), {"platform"})
            self.assertEqual(len(config["platform"]), 1)
            platform = config["platform"][0]
            # Keys/types follow the fixed 4.25.1 OneBot v11 template. The
            # adapter consumes ws_reverse_*; generic host/token keys fail.
            self.assertEqual(set(platform), {
                "id", "type", "enable", "ws_reverse_host",
                "ws_reverse_port", "ws_reverse_token",
            })
            self.assertEqual(platform["id"], "qq")
            self.assertEqual(platform["type"], "aiocqhttp")
            self.assertIs(platform["enable"], True)
            self.assertEqual(platform["ws_reverse_host"], "0.0.0.0")
            self.assertIs(type(platform["ws_reverse_port"]), int)
            self.assertEqual(platform["ws_reverse_port"], 6199)
            napcat = json.loads((root / "state/napcat/config/onebot11.json").read_text())
            client = napcat["network"]["websocketClients"][0]
            self.assertEqual(client["url"], f"ws://astrbot:{platform['ws_reverse_port']}/ws")
            self.assertEqual(platform["ws_reverse_token"], client["token"])
            self.assertEqual(platform["ws_reverse_token"], env["ONEBOT_TOKEN"])
            self.assertGreaterEqual(len(platform["ws_reverse_token"]), 32)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_fresh_qq_preparation_never_changes_any_existing_astrbot_config(self):
        for original in (
            b"",
            b"not-valid-json\n",
            b'\xef\xbb\xbf{"platform": [], "dashboard": {"password": "keep-hash"}}\r\n',
            b'{"platform": [{"id": "other", "type": "aiocqhttp", "ws_reverse_token": "keep-token"}], "provider": [{"id": "keep-provider"}]}\n',
        ):
            with self.subTest(existing=original[:20]), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / "install"
                path = root / "state/astrbot/cmd_config.json"
                path.parent.mkdir(parents=True)
                path.write_bytes(original)
                before = hashlib.sha256(path.read_bytes()).digest()
                prepare.prepare("fresh", root)
                prepare.prepare("fresh", root)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_generated_astrbot_config_survives_repeated_prepare(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            first = prepare.prepare("fresh", root)
            path = root / "state/astrbot/cmd_config.json"
            before = path.read_bytes()
            second = prepare.prepare("fresh", root)
            self.assertEqual(first["ONEBOT_TOKEN"], second["ONEBOT_TOKEN"])
            self.assertEqual(path.read_bytes(), before)

    def test_credentials_and_napcat_config_survive_second_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            first = prepare.prepare("fresh", root)
            self.assertEqual(first["OWNER_USER_IDS"], "")
            self.assertEqual(first["TOOL_ALLOWLIST"], "")
            self.assertEqual(first["QWENPAW_AGENT_ID"], "default")
            self.assertEqual(first["BRIDGE_MAX_FILE_BYTES"], "20971520")
            self.assertEqual(first["BRIDGE_MAX_FILES"], "4")
            self.assertEqual(first["BRIDGE_SOURCE_ROOTS"], "/AstrBot/data/temp")
            self.assertEqual(first["BRIDGE_NAPCAT_HOSTS"], "napcat")
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

    def test_smaller_media_limits_survive_and_excess_is_rejected_without_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "install"
            original = prepare.prepare("fresh", root)
            env_path = root / ".env"
            env = prepare.parse_env(env_path)
            env.update(BRIDGE_MAX_FILE_BYTES="1024", BRIDGE_MAX_FILES="1")
            env_path.write_text("\n".join(f"{k}={prepare.env_value(v)}" for k, v in env.items()) + "\n", encoding="utf-8")
            reduced = prepare.prepare("fresh", root)
            self.assertEqual(reduced["BRIDGE_MAX_FILE_BYTES"], "1024")
            self.assertEqual(reduced["BRIDGE_MAX_FILES"], "1")
            for key in prepare.SECRET_KEYS:
                self.assertEqual(reduced[key], original[key])
            for key, value in (("BRIDGE_MAX_FILE_BYTES", "20971521"), ("BRIDGE_MAX_FILES", "5"), ("BRIDGE_MAX_FILES", "0"), ("BRIDGE_MAX_FILES", "-1")):
                with self.subTest(key=key, value=value):
                    invalid = dict(reduced, **{key: value})
                    content = "\n".join(f"{k}={prepare.env_value(v)}" for k, v in invalid.items()) + "\n"
                    env_path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, key):
                        prepare.prepare("fresh", root)
                    self.assertEqual(env_path.read_text(encoding="utf-8"), content)


@unittest.skipIf(yaml is None, "Install PyYAML to validate Compose structures")
class ComposeTests(unittest.TestCase):
    def load(self, name):
        return yaml.safe_load((ROOT / "deploy" / name).read_text(encoding="utf-8"))

    def test_bundle_uses_verified_official_qwen_image_without_source_build(self):
        services = self.load("compose.bundle.yaml")["services"]
        self.assertEqual(set(services), {"astrbot", "qwenpaw", "napcat", "gateway"})
        qwen = services["qwenpaw"]
        self.assertEqual(qwen["image"], "agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/qwenpaw@sha256:4127130c41f415434aca5a9ea8eada99d3185d99e2bf181bd95c6e7fb959a3a7")
        self.assertEqual(qwen["pull_policy"], "missing")
        self.assertNotIn("build", qwen)
        self.assertEqual(services["astrbot"]["image"], "soulter/astrbot:v4.25.1")
        self.assertEqual(services["napcat"]["image"], "mlikiowa/napcat-docker:v4.18.33")
        self.assertEqual(services["gateway"]["build"]["dockerfile"], "bundle_gateway/Dockerfile")

    def test_source_override_only_changes_qwen_image_and_build_policy(self):
        overlay = self.load("compose.qwenpaw.source.yaml")
        self.assertEqual(set(overlay), {"services"})
        self.assertEqual(set(overlay["services"]), {"qwenpaw"})
        qwen = overlay["services"]["qwenpaw"]
        self.assertEqual(set(qwen), {"image", "pull_policy", "build"})
        self.assertEqual(qwen["image"], "bot-combined/qwenpaw:2.2.1-local")
        self.assertEqual(qwen["pull_policy"], "never")
        self.assertEqual(qwen["build"]["context"].split("#")[-1], "cae5773707b26ab2fd00903f84b712387894b256")
        self.assertEqual(qwen["build"]["dockerfile"], "deploy/Dockerfile")

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

    def test_media_contract_is_identical_in_fresh_and_existing_services(self):
        fresh = self.load("compose.yaml")["services"]
        addon = self.load("compose.addon.yaml")["services"]
        override = self.load("compose.astrbot.override.yaml")["services"]
        for service in (fresh["astrbot"], fresh["qwenpaw"], addon["qwenpaw"], override["astrbot"]):
            env = service["environment"]
            self.assertEqual(env["BRIDGE_FILES_ROOT"], "/bridge-files")
            self.assertEqual(env["BRIDGE_MAX_FILE_BYTES"], "${BRIDGE_MAX_FILE_BYTES:-20971520}")
            self.assertEqual(env["BRIDGE_MAX_FILES"], "${BRIDGE_MAX_FILES:-4}")
        for service in (fresh["astrbot"], override["astrbot"]):
            env = service["environment"]
            self.assertEqual(env["BRIDGE_SOURCE_ROOTS"], "${BRIDGE_SOURCE_ROOTS:-/AstrBot/data/temp}")
            self.assertEqual(env["BRIDGE_NAPCAT_HOSTS"], "${BRIDGE_NAPCAT_HOSTS:-napcat}")
        for services in (fresh, addon):
            mount = [v for v in services["napcat"]["volumes"] if ":/bridge-files" in v]
            self.assertEqual(mount, ["${INSTALL_ROOT:?}/state/bridge-files:/bridge-files:ro"])


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

    def test_bundle_and_optional_source_pass_cli_with_identical_data_mounts(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture_env(folder)
            bundle = ROOT / "deploy/compose.bundle.yaml"
            base = self.render(env, [bundle], "bundle-patch-test")
            source = self.render(env, [bundle, ROOT / "deploy/compose.qwenpaw.source.yaml"], "bundle-patch-test")
            self.assertNotIn("build", base["services"]["qwenpaw"])
            self.assertIn("@sha256:4127130c", base["services"]["qwenpaw"]["image"])
            self.assertEqual(source["services"]["qwenpaw"]["image"], "bot-combined/qwenpaw:2.2.1-local")
            self.assertEqual(source["services"]["qwenpaw"]["pull_policy"], "never")
            for key in ("volumes", "environment", "networks"):
                self.assertEqual(base["services"]["qwenpaw"][key], source["services"]["qwenpaw"][key])
            for name in ("astrbot", "napcat", "gateway"):
                self.assertEqual(base["services"][name], source["services"][name])

    def test_fresh_and_addon_pass_actual_compose_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture_env(folder)
            for name in ("compose.yaml", "compose.addon.yaml"):
                compose = self.render(env, [ROOT / "deploy" / name])
                self.assertEqual(compose["services"]["qwenpaw"]["environment"]["QWENPAW_AGENT_ID"], "default")
                self.assertEqual(compose["services"]["qwenpaw"]["environment"]["BRIDGE_MAX_FILE_BYTES"], "20971520")
                self.assertEqual(compose["services"]["qwenpaw"]["environment"]["BRIDGE_MAX_FILES"], "4")
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
