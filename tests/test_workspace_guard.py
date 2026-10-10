"""Contract tests for the real AgentScope on_acting / ToolCallBlock API."""
import copy
import importlib.util
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from module_stubs import module_overrides
from qwenpaw_plugin_astrbot_bridge.bridge_client import BridgeSettings


AGENT = "abp_" + "1" * 32
GROUP = "abg_" + "2" * 32
OTHER_AGENT = "abp_" + "3" * 32
SESSION = "ab_" + "a" * 32
OTHER_SESSION = "ab_" + "b" * 32


class ToolCallBlock:
    """Official fields: input is raw JSON text, not an arguments dictionary."""
    def __init__(self, name, arguments, identifier="call-1"):
        self.name = name
        self.input = json.dumps(arguments, ensure_ascii=False)
        self.id = identifier
        self.type = "tool_call"

    def model_copy(self, *, update):
        block = copy.copy(self)
        for field, value in update.items():
            setattr(block, field, value)
        return block


class WorkspaceGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        modules = {name: types.ModuleType(name) for name in (
            "agentscope", "agentscope.message", "agentscope.middleware", "agentscope.tool")}
        modules["agentscope.message"].TextBlock = SimpleNamespace
        modules["agentscope.message"].ToolResultState = SimpleNamespace(ERROR="error")
        modules["agentscope.middleware"].MiddlewareBase = type("MiddlewareBase", (), {})
        modules["agentscope.tool"].ToolResponse = SimpleNamespace
        source = Path(__file__).resolve().parents[1] / "qwenpaw_plugin_astrbot_bridge" / "workspace_guard.py"
        spec = importlib.util.spec_from_file_location("qwenpaw_plugin_astrbot_bridge.workspace_guard_under_test", source)
        self.module = importlib.util.module_from_spec(spec)
        with module_overrides(modules):
            spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspaces" / AGENT
        self.other = self.root / "workspaces" / OTHER_AGENT
        self.files = self.root / "bridge-files"
        self.session = self.files / SESSION
        self.other_session = self.files / OTHER_SESSION
        for directory in (self.workspace, self.other, self.session / "inbound", self.session / "outbound", self.other_session):
            directory.mkdir(parents=True)
        (self.workspace / "notes.txt").write_text("My own notes", encoding="utf8")
        (self.other / "private.txt").write_text("Other user's secret", encoding="utf8")
        self.guard = self.module.WorkspaceGuard(agent_id=AGENT, workspace_dir=self.workspace,
                                                session_id=SESSION, files_root=self.files)
        self.assertFalse(self.guard.invalid_scope)

    def tearDown(self):
        self.temp.cleanup()

    def deny(self, tool, arguments):
        with self.assertRaises(self.module.WorkspaceAccessDenied):
            self.guard.validate(tool, arguments)

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            self.skipTest(f"Host cannot create symlinks: {exc}")

    def test_native_file_fields_are_normalized_and_inputs_not_mutated(self):
        for name, field in (("read_file", "file_path"), ("write_file", "file_path"),
                            ("edit_file", "file_path"), ("append_file", "file_path"),
                            ("send_file_to_user", "file_path"), ("view_image", "image_path"),
                            ("view_video", "video_path")):
            with self.subTest(name=name):
                original = {field: "notes.txt"}
                result = self.guard.validate(name, original)
                self.assertEqual(result[field], str(self.workspace / "notes.txt"))
                self.assertEqual(original[field], "notes.txt")

    def test_nested_new_file_stays_in_workspace(self):
        target = self.guard.validate("write_file", {"file_path": "reports/new/report.txt", "content": "hello"})
        self.assertEqual(target["file_path"], str(self.workspace / "reports" / "new" / "report.txt"))
        self.assertFalse((self.workspace / "reports").exists())

    def test_parent_traversal_absolute_other_user_and_prefix_sibling_are_denied(self):
        for value in ("../private.txt", f"../{OTHER_AGENT}/private.txt", "safe/../../private.txt",
                      str(self.other / "private.txt"), str(self.workspace) + "-sibling/private.txt",
                      str(self.root / "outside.txt")):
            with self.subTest(value=value):
                self.deny("read_file", {"file_path": value})

    def test_all_file_tools_reject_other_user_paths(self):
        for tool, (field, _fields) in self.module._FILE_TOOLS.items():
            self.deny(tool, {field: str(self.other / "private.txt")})

    def test_only_current_bridge_session_is_allowed(self):
        for relative in ("inbound/photo.png", "outbound/report.txt"):
            expected = self.session / relative
            self.assertEqual(self.guard.validate("read_file", {"file_path": str(expected)})["file_path"], str(expected))
        for path in (self.other_session / "secret.txt", self.files, self.files / "unknown" / "secret.txt"):
            self.deny("read_file", {"file_path": str(path)})

    def test_group_agent_never_gets_private_workspace(self):
        group_dir = self.root / "workspaces" / GROUP
        group_dir.mkdir()
        guard = self.module.WorkspaceGuard(agent_id=GROUP, workspace_dir=group_dir, session_id=SESSION, files_root=self.files)
        with self.assertRaises(self.module.WorkspaceAccessDenied):
            guard.validate("read_file", {"file_path": str(self.workspace / "notes.txt")})
        self.assertEqual(guard.validate("read_file", {"file_path": "group.txt"})["file_path"], str(group_dir / "group.txt"))

    def test_encoded_uri_and_home_paths_cannot_be_reinterpreted_by_native_tools(self):
        for value in ("%2e%2e/private.txt", "notes%2etxt", "file:///other/private.txt",
                      "https://example.com/image.png", "~/private.txt", "notes\x00.txt", "notes\u200b.txt",
                      "//server/share/private.txt"):
            self.deny("read_file", {"file_path": value})
            self.deny("view_image", {"image_path": value})
            self.deny("send_file_to_user", {"file_path": value})

    def test_extra_path_or_agent_selector_is_not_silently_accepted(self):
        self.deny("read_file", {"file_path": "notes.txt", "workspace": str(self.other)})
        self.deny("glob_search", {"path": ".", "pattern": "*", "follow_symlinks": True})
        self.deny("memory_search", {"query": "name", "agent_id": OTHER_AGENT})
        self.deny("memory_search", {"query": "name", "workspace": str(self.other)})

    def test_runtime_configuration_credentials_and_executable_plugin_entries_are_denied(self):
        for relative in ("agent.json", "config.json", "credentials.yaml", "plugins/backdoor.py",
                         "active_skills/escape/main.py", "customized_skills/escape/run.py",
                         ".qwenpaw/policy.json", ".env", "policy/allow.json", "credentials/provider.key"):
            self.deny("read_file", {"file_path": relative})
            self.deny("write_file", {"file_path": relative, "content": "overwrite"})
        self.guard.validate("write_file", {"file_path": "MEMORY.md", "content": "likes tea"})

    def test_native_browser_rejected_even_if_code_looks_like_safe_https(self):
        for code in ("page = await browser.open('https://example.com')",
                     "page = await browser.open('file:///private.txt')",
                     "open('/other/private.txt').read()", "__import__('os').system('cat /private')"):
            self.deny("browser", {"code": code})
        self.assertEqual(self.guard.validate("astrbot_browser", {"action": "open", "url": "https://example.com"}),
                         {"action": "open", "url": "https://example.com"})

    def test_shell_batch_cross_agent_and_unknown_tools_fail_closed(self):
        for tool in ("execute_shell_command", "shell", "exec", "run_tool_batch", "spawn_subagent",
                     "delegate_external_agent", "agent_management", "some_new_mcp_tool", "Skill"):
            self.deny(tool, {})

    def test_bridge_tools_and_native_memory_search_are_explicitly_allowlisted(self):
        for tool in self.module._BRIDGE_TOOLS:
            arguments = {"test": "unchanged; downstream checks actual context"}
            self.assertEqual(self.guard.validate(tool, arguments), arguments)
        args = {"query": "喜欢喝什么", "max_results": 3, "min_score": 0}
        self.assertEqual(self.guard.validate("memory_search", args), args)
        self.assertEqual(self.guard.validate("get_current_time", {}), {})
        self.deny("set_user_timezone", {"timezone_name": "UTC"})

    def test_search_provider_only_accepts_search_term_and_raw_fetch_is_denied(self):
        self.assertEqual(self.guard.validate("web_search", {"search_term": "北京天气"}), {"search_term": "北京天气"})
        self.deny("web_search", {"search_term": "weather", "url": "http://gateway"})
        self.deny("web_fetch", {"url": "https://public.example/redirects-to-private"})
        self.deny("web_fetch", {"url": "http://127.0.0.1/admin"})

    def test_recursive_search_cannot_read_runtime_configuration(self):
        (self.workspace / "agent.json").write_text("private runtime settings", encoding="utf8")
        self.deny("grep_search", {"pattern": "private"})

    def test_recursive_search_normalizes_root_and_rejects_traversal_patterns(self):
        for name in ("grep_search", "glob_search"):
            result = self.guard.validate(name, {"pattern": "*"})
            self.assertEqual(result["path"], str(self.workspace))
            self.deny(name, {"pattern": "*", "path": str(self.other)})
        for pattern in ("../*", "**/../*", "/etc/*", "file://*", "..\\*", "%2e%2e/*"):
            self.deny("glob_search", {"pattern": pattern})
        self.deny("grep_search", {"pattern": "tea", "include_pattern": "../*"})

    def test_recursive_search_refuses_overlarge_tree(self):
        with patch.object(self.module, "_MAX_SEARCH_ENTRIES", 0):
            self.deny("grep_search", {"pattern": "notes"})

    def test_symlink_file_and_directory_cannot_access_other_users(self):
        self.symlink(self.workspace / "linked.txt", self.other / "private.txt")
        self.symlink(self.workspace / "linked-dir", self.other)
        self.deny("read_file", {"file_path": "linked.txt"})
        self.deny("write_file", {"file_path": "linked-dir/new.txt", "content": "x"})
        self.deny("read_file", {"file_path": "linked-dir/private.txt"})

    def test_recursive_search_cannot_follow_a_file_symlink(self):
        self.symlink(self.workspace / "linked.txt", self.other / "private.txt")
        for name in ("grep_search", "glob_search"):
            self.deny(name, {"pattern": "*"})

    def test_hardlinks_are_rejected_even_when_link_is_inside_workspace(self):
        linked = self.workspace / "hardlink.txt"
        os.link(self.other / "private.txt", linked)
        self.deny("read_file", {"file_path": "hardlink.txt"})
        self.deny("write_file", {"file_path": "hardlink.txt", "content": "overwrite"})
        self.deny("grep_search", {"pattern": "secret"})

    def test_workspace_symlink_and_name_mismatch_return_denying_guard(self):
        wrong = self.module.WorkspaceGuard(agent_id=AGENT, workspace_dir=self.other,
                                          session_id=SESSION, files_root=self.files)
        self.assertTrue(wrong.invalid_scope)
        with self.assertRaises(self.module.WorkspaceAccessDenied):
            wrong.validate("get_current_time", {})
        linked_parent = self.root / "linked-workspaces"
        self.symlink(linked_parent, self.root / "workspaces")
        linked = self.module.WorkspaceGuard(agent_id=AGENT, workspace_dir=linked_parent / AGENT,
                                           session_id=SESSION, files_root=self.files)
        self.assertTrue(linked.invalid_scope)

    def test_missing_or_invalid_session_never_grants_shared_attachment_root(self):
        for session_id in (None, "", "cron_123", "../other", SESSION + "/extra"):
            guard = self.module.WorkspaceGuard(agent_id=AGENT, workspace_dir=self.workspace,
                                               session_id=session_id, files_root=self.files)
            self.assertFalse(guard.invalid_scope)
            guard.validate("read_file", {"file_path": "notes.txt"})
            with self.assertRaises(self.module.WorkspaceAccessDenied):
                guard.validate("read_file", {"file_path": str(self.session / "outbound" / "report.txt")})

    def test_factory_never_changes_default_console_or_unmanaged_agents(self):
        for agent in ("default", "root", "my-personal-agent", "abp_bad", None):
            ctx = SimpleNamespace(agent_id=agent, workspace_dir=self.workspace,
                                  session_id=SESSION, request=SimpleNamespace(channel="console"))
            self.assertIsNone(self.module.workspace_guard_factory(ctx, None))

    def test_factory_guards_managed_console_and_cron_without_nonce(self):
        for channel in ("console", "cron", "astrbot"):
            ctx = SimpleNamespace(agent_id=AGENT, workspace_dir=self.workspace,
                                  session_id=SESSION, request=SimpleNamespace(channel=channel))
            with patch.object(self.module, "BRIDGE_FILES_ROOT", self.files):
                guard = self.module.workspace_guard_factory(ctx, None)
            self.assertIsInstance(guard, self.module.WorkspaceGuard)
            self.assertFalse(guard.invalid_scope)
            with self.assertRaises(self.module.WorkspaceAccessDenied):
                guard.validate("read_file", {"file_path": str(self.other / "private.txt")})

    def configured_guard(self, config, env=None):
        ctx = SimpleNamespace(agent_id=AGENT, workspace_dir=self.workspace, session_id=SESSION)
        with patch.dict(os.environ, env or {}, clear=True):
            return self.module.workspace_guard_factory(ctx, config)

    def test_factory_uses_current_agent_channel_files_root_and_preserves_session_boundary(self):
        for config in (
            {"channels": {"astrbot": {"files_dir": str(self.files)}}},
            SimpleNamespace(channels=SimpleNamespace(astrbot=SimpleNamespace(files_dir=str(self.files)))),
            {"channels": {"astrbot": {"bridge": {"files_dir": str(self.files)}}}},
        ):
            with self.subTest(config=config):
                guard = self.configured_guard(config)
                self.assertFalse(guard.invalid_scope)
                own = str(self.session / "outbound" / "report.txt")
                self.assertEqual(guard.validate("write_file", {"file_path": own, "content": "report"})["file_path"], own)
                for denied in (self.other_session / "secret.txt", self.root / "old-files" / SESSION / "report.txt"):
                    with self.assertRaises(self.module.WorkspaceAccessDenied):
                        guard.validate("read_file", {"file_path": str(denied)})

    def test_factory_files_root_precedence_matches_bridge_settings(self):
        nested = str(self.root / "nested-files")
        direct = str(self.root / "direct-files")
        legacy = str(self.root / "legacy-env-files")
        current = str(self.root / "current-env-files")
        cases = (
            ({"bridge": {"files_dir": nested}}, {}, nested),
            ({"files_dir": None, "bridge": {"files_dir": nested}}, {}, nested),
            ({"files_dir": direct, "bridge": {"files_dir": nested}}, {}, direct),
            ({"files_dir": direct}, {"BRIDGE_FILES_DIR": legacy}, legacy),
            ({"files_dir": direct}, {"BRIDGE_FILES_DIR": legacy, "BRIDGE_FILES_ROOT": current}, current),
        )
        for channel, env, expected in cases:
            with self.subTest(channel=channel, env=env):
                guard = self.configured_guard({"channels": {"astrbot": channel}}, env)
                with patch.dict(os.environ, {**env, "BRIDGE_TOKEN": "test-token"}, clear=True):
                    settings = BridgeSettings.from_config(channel)
                self.assertEqual(settings.files_dir, expected)
                self.assertFalse(guard.invalid_scope)
                self.assertEqual(guard.session_root, Path(settings.files_dir) / SESSION)
                own = str(Path(expected) / SESSION / "outbound" / "report.txt")
                self.assertEqual(guard.validate("write_file", {"file_path": own, "content": "report"})["file_path"], own)

    def test_factory_plugin_fallback_and_channel_override_match_bridge_settings(self):
        plugin_root = str(self.root / "plugin-files")
        channel_root = str(self.root / "channel-files")
        env_root = str(self.root / "env-files")
        for plugin in ({"files_dir": plugin_root}, {"bridge": {"files_dir": plugin_root}}):
            self.module.configure_workspace_guard(plugin)
            cases = (({}, {}, plugin_root),
                     ({"files_dir": None}, {}, plugin_root),
                     ({"files_dir": channel_root}, {}, channel_root),
                     ({"bridge": {"files_dir": channel_root}}, {}, channel_root),
                     ({"files_dir": channel_root}, {"BRIDGE_FILES_ROOT": env_root}, env_root))
            for channel, env, expected in cases:
                with self.subTest(plugin=plugin, channel=channel, env=env):
                    guard = self.configured_guard({"channels": {"astrbot": channel}}, env)
                    with patch.dict(os.environ, {**env, "BRIDGE_TOKEN": "test-token"}, clear=True):
                        settings = BridgeSettings.from_config(channel, plugin)
                    self.assertEqual(settings.files_dir, expected)
                    self.assertFalse(guard.invalid_scope)
                    self.assertEqual(guard.session_root, Path(settings.files_dir) / SESSION)
        self.module.configure_workspace_guard(None)
        with patch.object(self.module, "BRIDGE_FILES_ROOT", self.files):
            self.assertEqual(self.configured_guard(None).session_root, self.session)

    def test_factory_rejects_invalid_configured_roots_instead_of_granting_default(self):
        for value in ("", "relative-files", "../outside", str(self.root / ".." / "outside"), [], 17):
            with self.subTest(value=value):
                guard = self.configured_guard({"channels": {"astrbot": {"files_dir": value}}})
                self.assertTrue(guard.invalid_scope)
                with self.assertRaises(self.module.WorkspaceAccessDenied):
                    guard.validate("read_file", {"file_path": "notes.txt"})
        for env in ({"BRIDGE_FILES_ROOT": ""}, {"BRIDGE_FILES_DIR": "relative-files"}):
            guard = self.configured_guard({"channels": {"astrbot": {"files_dir": str(self.files)}}}, env)
            self.assertTrue(guard.invalid_scope)

    def test_factory_rejects_linked_shared_root(self):
        linked = self.root / "linked-files"
        self.symlink(linked, self.files)
        guard = self.configured_guard({"channels": {"astrbot": {"files_dir": str(linked)}}})
        self.assertTrue(guard.invalid_scope)

    def test_factory_configuration_error_returns_denying_guard(self):
        class BrokenConfig:
            @property
            def channels(self):
                raise RuntimeError("configuration unavailable")
        guard = self.configured_guard(BrokenConfig())
        self.assertIsNotNone(guard)
        self.assertTrue(guard.invalid_scope)
        with self.assertRaises(self.module.WorkspaceAccessDenied):
            guard.validate("get_current_time", {})

    def test_factory_does_not_take_attachment_root_from_request_metadata(self):
        ctx = SimpleNamespace(agent_id=AGENT, workspace_dir=self.workspace, session_id=SESSION,
                              request=SimpleNamespace(files_dir=str(self.other),
                                                      metadata={"files_dir": str(self.other)}))
        with patch.dict(os.environ, {}, clear=True):
            guard = self.module.workspace_guard_factory(ctx, {"channels": {"astrbot": {"files_dir": str(self.files)}}})
        self.assertEqual(guard.session_root, self.session)
        with self.assertRaises(self.module.WorkspaceAccessDenied):
            guard.validate("read_file", {"file_path": str(self.other / "private.txt")})

    async def test_actual_hook_contract_rewrites_json_input_and_calls_handler_once(self):
        block = ToolCallBlock("read_file", {"file_path": "notes.txt"})
        calls = []
        sentinel = object()
        async def next_handler(**kwargs):
            calls.append(kwargs)
            yield sentinel
        output = [item async for item in self.guard.on_acting(None, {"tool_call": block}, next_handler)]
        self.assertEqual(output, [sentinel])
        self.assertEqual(len(calls), 1)
        forwarded = calls[0]["tool_call"]
        self.assertEqual(forwarded.id, block.id)
        self.assertEqual(json.loads(forwarded.input)["file_path"], str(self.workspace / "notes.txt"))
        self.assertEqual(json.loads(block.input)["file_path"], "notes.txt")
        self.assertIsNot(forwarded, block)

    async def test_denial_yields_official_tool_response_and_never_executes_handler(self):
        block = ToolCallBlock("read_file", {"file_path": str(self.other / "private.txt")})
        async def next_handler(**kwargs):
            self.fail("denied tool was executed")
            yield None
        output = [item async for item in self.guard.on_acting(None, {"tool_call": block}, next_handler)]
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].id, block.id)
        self.assertEqual(output[0].state, "error")
        self.assertNotIn(OTHER_AGENT, output[0].content[0].text)
        self.assertNotIn(str(self.other), output[0].content[0].text)

    async def test_malformed_json_and_nonobject_inputs_never_execute(self):
        async def next_handler(**kwargs):
            self.fail("malformed tool was executed")
            yield None
        for raw in ("{bad", "[]", "null", '"string"'):
            block = ToolCallBlock("read_file", {})
            block.input = raw
            output = [item async for item in self.guard.on_acting(None, {"tool_call": block}, next_handler)]
            self.assertEqual(output[0].state, "error")

    async def test_factory_failure_cannot_be_silently_skipped_by_qwenpaw(self):
        ctx = SimpleNamespace(agent_id=AGENT, workspace_dir=None, session_id=SESSION)
        guard = self.module.workspace_guard_factory(ctx, None)
        self.assertIsNotNone(guard)
        self.assertTrue(guard.invalid_scope)
        async def next_handler(**kwargs):
            self.fail("invalid-scope tool was executed")
            yield None
        output = [item async for item in guard.on_acting(None, {"tool_call": ToolCallBlock("get_current_time", {})}, next_handler)]
        self.assertEqual(output[0].state, "error")


if __name__ == "__main__":
    unittest.main()
