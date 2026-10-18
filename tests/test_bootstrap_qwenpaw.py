"""First-start channel bootstrap preserves native configuration and secrets."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/bootstrap_qwenpaw.py"
SPEC = importlib.util.spec_from_file_location("_qwenpaw_bootstrap_under_test", SCRIPT)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class NativeApi:
    """Fixed 2.2.1 response contract including server-added default fields."""

    def __init__(self, config=None):
        self.config = copy.deepcopy(config)
        self.requests = []
        self.health_states = []
        self.channel_states = []
        self.types = ["console", "astrbot"]
        self.version = "2.2.1"
        self.config_error = None
        self.concurrent_config = None
        self.write_status = 200
        self.corrupt_saved = False
        self.corrupt_final = False
        self.reads = 0

    def __call__(self, path, token, method="GET", payload=None):
        assert token == "r" * 32
        self.requests.append((method, path, copy.deepcopy(payload)))
        if path == "/api/healthz":
            status = self.health_states.pop(0) if self.health_states else 200
            return status, {"status": "ok", "agents_loaded": ["default"]}
        if path == "/api/version":
            return 200, {"version": self.version}
        if path.endswith("/channels/types"):
            return 200, self.types
        if path.endswith("/health"):
            status = self.channel_states.pop(0) if self.channel_states else 200
            return status, {"channel": "astrbot", "status": "healthy"}
        assert path == bootstrap.CHANNEL_PATH
        if method == "PUT":
            if self.write_status != 200:
                return self.write_status, None
            self.config = dict(payload, bot_prefix="", filter_bot_messages=True)
            if self.corrupt_saved:
                self.config["enabled"] = False
            return 200, copy.deepcopy(self.config)
        assert method == "GET" and payload is None
        self.reads += 1
        if self.config_error is not None:
            return self.config_error, None
        if self.reads == 2 and self.concurrent_config is not None:
            self.config = copy.deepcopy(self.concurrent_config)
        if self.corrupt_final and self.reads >= 2 and self.config is not None:
            self.config["callback_url"] = "http://changed:9186"
        return (200, copy.deepcopy(self.config)) if self.config is not None else (404, None)

    @property
    def writes(self):
        return [request for request in self.requests if request[0] != "GET"]


class BootstrapTests(unittest.TestCase):
    def run_probe(self, api, **env):
        settings = {"QWENPAW_RUNTIME_INTERNAL_TOKEN": "r" * 32, "QWENPAW_AGENT_ID": "default"}
        settings.update(env)
        with patch.dict(os.environ, settings), patch.object(bootstrap, "request_json", api), patch.object(bootstrap.time, "sleep"):
            return bootstrap.bootstrap()

    def test_missing_channel_enables_once_and_accepts_server_defaults(self):
        api = NativeApi()
        result = self.run_probe(api)
        self.assertTrue(result["passed"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["existing_config_preserved"])
        self.assertEqual(api.writes, [("PUT", bootstrap.CHANNEL_PATH, {"enabled": True})])
        self.assertIn("bot_prefix", api.config)

    def test_empty_channel_config_is_fresh(self):
        api = NativeApi({})
        self.assertTrue(self.run_probe(api)["passed"])
        self.assertEqual(len(api.writes), 1)

    def test_existing_enabled_config_retains_nested_values_and_never_puts(self):
        original = {"enabled": True, "callback_url": "http://custom:9186", "token": "private-channel-secret", "nested": {"keep": [1, 2]}}
        api = NativeApi(original)
        result = self.run_probe(api)
        self.assertTrue(result["passed"])
        self.assertFalse(result["changed"])
        self.assertEqual(api.config, original)
        self.assertFalse(api.writes)

    def test_disabled_channel_is_preserved_and_skip_succeeds(self):
        original = {"enabled": False, "token": "private-channel-secret", "nested": {"keep": 1}}
        api = NativeApi(original)
        result = self.run_probe(api)
        self.assertTrue(result["passed"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["existing_disabled_preserved"])
        self.assertEqual(api.config, original)
        self.assertFalse(api.writes)
        self.assertFalse(any(path.endswith("/health") for _, path, _ in api.requests))

    def test_custom_channel_config_without_enabled_is_preserved(self):
        api = NativeApi({"callback_url": "http://custom:9186"})
        result = self.run_probe(api)
        self.assertTrue(result["passed"])
        self.assertTrue(result["skipped"])
        self.assertFalse(api.writes)

    def test_concurrent_disabled_config_is_preserved(self):
        api = NativeApi()
        api.concurrent_config = {"enabled": False, "token": "concurrent-private-secret"}
        result = self.run_probe(api)
        self.assertTrue(result["passed"])
        self.assertTrue(result["skipped"])
        self.assertFalse(api.writes)
        self.assertEqual(api.config, api.concurrent_config)

    def test_concurrent_enabled_config_is_checked_without_overwrite(self):
        api = NativeApi()
        api.concurrent_config = {"enabled": True, "callback_url": "http://custom:9186"}
        self.assertTrue(self.run_probe(api)["passed"])
        self.assertFalse(api.writes)

    def test_missing_or_wrong_token_never_calls_api(self):
        api = NativeApi()
        self.assertFalse(self.run_probe(api, QWENPAW_RUNTIME_INTERNAL_TOKEN="")["passed"])
        self.assertFalse(api.requests)

    def test_different_agent_is_not_modified(self):
        api = NativeApi()
        self.assertFalse(self.run_probe(api, QWENPAW_AGENT_ID="other")["passed"])
        self.assertFalse(api.requests)

    def test_unauthorized_startup_fails_without_mutation(self):
        api = NativeApi()
        api.health_states = [401]
        self.assertFalse(self.run_probe(api)["passed"])
        self.assertEqual(len(api.requests), 1)
        self.assertFalse(api.writes)

    def test_startup_and_reload_are_observed_with_get_only(self):
        api = NativeApi()
        api.health_states = [503, 200]
        api.channel_states = [404, 200]
        self.assertTrue(self.run_probe(api)["passed"])
        self.assertEqual(len(api.writes), 1)

    def test_startup_timeout_does_not_write(self):
        api = NativeApi()
        api.health_states = [503]
        with patch.object(bootstrap, "READY_TIMEOUT_SECONDS", 0):
            self.assertFalse(self.run_probe(api)["passed"])
        self.assertFalse(api.writes)

    def test_reload_timeout_does_not_retry_put(self):
        api = NativeApi()
        api.channel_states = [404]
        with patch.object(bootstrap, "RELOAD_TIMEOUT_SECONDS", 0):
            result = self.run_probe(api)
        self.assertFalse(result["passed"])
        self.assertTrue(result["changed"])
        self.assertEqual(len(api.writes), 1)

    def test_unregistered_channel_does_not_write(self):
        api = NativeApi()
        api.types = ["console"]
        self.assertFalse(self.run_probe(api)["passed"])
        self.assertFalse(api.writes)

    def test_wrong_upstream_version_does_not_write(self):
        api = NativeApi()
        api.version = "9.9.9"
        result = self.run_probe(api)
        self.assertFalse(result["passed"])
        self.assertTrue(result["unsupported_version"])
        self.assertFalse(api.writes)

    def test_config_read_error_is_not_treated_as_missing(self):
        api = NativeApi()
        api.config_error = 500
        self.assertFalse(self.run_probe(api)["passed"])
        self.assertFalse(api.writes)

    def test_uncertain_put_outcome_is_not_retried(self):
        api = NativeApi()
        api.write_status = 503
        self.assertFalse(self.run_probe(api)["passed"])
        self.assertEqual(len(api.writes), 1)

    def test_server_did_not_save_enabled_fails(self):
        api = NativeApi()
        api.corrupt_saved = True
        self.assertFalse(self.run_probe(api)["passed"])
        self.assertEqual(len(api.writes), 1)

    def test_changed_existing_field_is_reported(self):
        api = NativeApi({"enabled": True, "callback_url": "http://original:9186"})
        api.corrupt_final = True
        result = self.run_probe(api)
        self.assertFalse(result["passed"])
        self.assertFalse(result["existing_config_preserved"])
        self.assertFalse(api.writes)

    def test_main_output_never_contains_tokens_or_config(self):
        api = NativeApi({"enabled": False, "token": "private-channel-secret", "callback_url": "http://private-host:9186"})
        with patch.dict(os.environ, {"QWENPAW_RUNTIME_INTERNAL_TOKEN": "r" * 32, "QWENPAW_AGENT_ID": "default"}), patch.object(bootstrap, "request_json", api), contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = bootstrap.main()
        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["skipped"])
        for secret in ("r" * 32, "private-channel-secret", "http://private-host:9186"):
            self.assertNotIn(secret, output.getvalue())

    def test_main_exception_does_not_leak_upstream_error(self):
        with patch.object(bootstrap, "bootstrap", side_effect=RuntimeError("private-error-secret")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bootstrap.main(), 1)
        self.assertNotIn("private-error-secret", output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["script_error"])

    def test_no_redirect_is_allowed(self):
        self.assertIsNone(bootstrap.NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere"))

    def test_saved_fields_allow_additional_defaults_but_require_original_values(self):
        self.assertTrue(bootstrap.matches_existing_keys({"enabled": True}, {"enabled": True, "bot_prefix": ""}))
        self.assertFalse(bootstrap.matches_existing_keys({"token": "original"}, {"token": "changed"}))
        self.assertFalse(bootstrap.matches_existing_keys({"token": "original"}, {}))


if __name__ == "__main__":
    unittest.main()
