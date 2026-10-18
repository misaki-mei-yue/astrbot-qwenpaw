"""QwenPaw 2.2.1 REST contract tests with isolated loopback fixtures only."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from aiohttp import web

from astrbot_plugin_qwenpaw_bridge.bridge_core import BridgeError, QwenPawClient
from astrbot_plugin_qwenpaw_bridge.personal_agent import PersonalAgentManager


PRIVATE = "abp_" + "a" * 32
OTHER = "abp_" + "b" * 32
GROUP = "abg_" + "c" * 32
TOKEN = "fixture-runtime-token-" + "x" * 40


def fresh_agent(agent_id, description=""):
    return {
        "id": agent_id, "name": "Fixture", "description": description,
        "workspace_dir": f"/fixture/workspaces/{agent_id}", "backend": "qwenpaw",
        "running": {"memory_manager_backend": "remelight", "reme_light_memory_config": {
            "embedding_model_config": {"api_key": "", "enabled": False},
        }},
        "tools": {"builtin_tools": {"execute_shell_command": {"enabled": True},
                                    "browser": {"enabled": True}}},
    }


class PersonalAgentContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agents = {"default": fresh_agent("default")}
        self.agents["default"].update({
            "active_model": {"provider_id": "fixture", "model": "fixture-model"},
            "fallback_models": [{"provider_id": "fixture", "model": "fallback"}],
            "approval_level": "SMART", "language": "zh",
            "project_dir": "/private-owner-files",
            "channels": {"qq": {"enabled": True, "token": "DO-NOT-COPY"},
                         "wechat": {"enabled": True, "account": "DO-NOT-COPY"}},
            "mcp": {"clients": {"private": {"env": {"API_KEY": "DO-NOT-COPY"}}}},
            "mail": {"password": "DO-NOT-COPY"},
            "system_prompt_files": ["PRIVATE-OWNER.md"],
            "tools": {"builtin_tools": {
                "read_file": {"name": "read_file", "enabled": True, "config": {"key": "DO-NOT-COPY"}},
                "browser": {"name": "browser", "enabled": True, "async_execution": False},
                "write_file": {"name": "write_file", "enabled": False},
                "chat_with_agent": {"name": "chat_with_agent", "enabled": True},
                "set_user_timezone": {"name": "set_user_timezone", "enabled": True},
                "get_token_usage": {"name": "get_token_usage", "enabled": True},
                "unknown_plugin": {"name": "unknown_plugin", "enabled": True, "config": {"key": "DO-NOT-COPY"}},
            }},
            "running": {"memory_manager_backend": "remelight", "reme_light_memory_config": {
                "auto_memory_interval": 7, "daily_dir": "../private-owner-memory",
                "digest_dir": "/somebody-else", "dream_cron_enabled": False,
                "embedding_model_config": {"api_key": "DO-NOT-COPY"},
            }},
        })
        self.skills = [
            {"name": "browser-guide", "enabled": True, "channels": ["all"]},
            {"name": "disabled", "enabled": False, "channels": ["all"]},
            {"name": "qq-only", "enabled": True, "channels": ["qq"]},
        ]
        self.calls = []
        self.notes = {}
        self.files = {}
        self.create_count = 0
        self.create_error_after_commit = False
        self.reindex_status = 200
        self.active_model = {"provider_id": "fixture", "model": "fixture-model"}
        self.get_status = {}
        self.app = web.Application()
        self.app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = QwenPawClient(f"http://127.0.0.1:{port}", "default", TOKEN)
        self.manager = PersonalAgentManager(self.client)

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def handle(self, request):
        body = await request.json() if request.can_read_body else None
        path, method = request.path, request.method
        agent_id = request.headers.get("X-Agent-Id")
        self.calls.append({"method": method, "path": path, "agent": agent_id,
                           "query": dict(request.query), "body": body})
        self.assertEqual(request.headers.get("X-QwenPaw-Runtime-Token"), TOKEN)
        if path == "/api/models/active":
            self.assertEqual(request.query.get("scope"), "effective")
            self.assertEqual(request.query.get("agent_id"), agent_id)
            return web.json_response({"active_llm": self.active_model})
        if path == "/api/skills":
            self.assertEqual(agent_id, "default")
            return web.json_response(self.skills)
        if path == "/api/agents" and method == "POST":
            self.create_count += 1
            new_id = body["id"]
            if new_id in self.agents:
                return web.json_response({"detail": "already exists"}, status=400)
            self.agents[new_id] = {**fresh_agent(new_id), **body}
            if self.create_error_after_commit:
                return web.json_response({"detail": "proxy error DO-NOT-ECHO"}, status=502)
            return web.json_response({"id": new_id, "workspace_dir": self.agents[new_id]["workspace_dir"],
                                      "enabled": True}, status=201)
        if path.startswith("/api/agents/") and "/memory/reindex" in path:
            self.assertIn(request.query.get("scope"), {"all", "bm25"})
            return web.json_response({"status": "completed", "scope": request.query["scope"]}, status=self.reindex_status)
        if path.startswith("/api/agents/"):
            target = path.rsplit("/", 1)[1]
            if method == "GET":
                if target in self.get_status:
                    return web.json_response({"detail": "DO-NOT-ECHO"}, status=self.get_status[target])
                if target not in self.agents:
                    return web.json_response({"detail": "not found"}, status=404)
                return web.json_response(self.agents[target])
            if method == "PUT":
                self.agents[target].update(copy.deepcopy(body))
                return web.json_response(self.agents[target])
        if path.startswith("/api/workspace/files/"):
            self.assertEqual(method, "PUT")
            self.assertEqual(path, "/api/workspace/files/BRIDGE_IDENTITY.md")
            self.files[agent_id] = body["content"]
            return web.json_response({"written": True})
        if path == "/api/workspace/memory":
            self.assertEqual(request.query.get("section"), "digest")
            listing = [{"filename": name} for owner, name in self.notes if owner == agent_id]
            listing.append({"filename": "automatic-user-preferences.md"})
            listing.append({"filename": "nested/bridge-note-" + "d" * 32 + ".md"})
            return web.json_response(listing)
        if path.startswith("/api/workspace/memory/"):
            self.assertEqual(request.query.get("section"), "digest")
            name = path.rsplit("/", 1)[1]
            key = (agent_id, name)
            if method == "GET":
                if key not in self.notes:
                    return web.json_response({"detail": "not found"}, status=404)
                return web.json_response({"content": self.notes[key].strip()})
            if method == "PUT":
                self.notes[key] = body["content"]
                return web.json_response({"written": True})
        return web.json_response({"detail": "unexpected fixture request"}, status=500)

    async def test_stable_creation_preserves_settings_without_private_documents_or_secrets(self):
        config = await self.manager.ensure_agent(PRIVATE)
        self.assertEqual(self.create_count, 1)
        self.assertEqual(config["id"], PRIVATE)
        self.assertEqual(config["workspace_dir"], f"/fixture/workspaces/{PRIVATE}")
        create = next(call["body"] for call in self.calls if call["path"] == "/api/agents")
        self.assertEqual(create["skill_names"], ["browser-guide"])
        self.assertNotIn("workspace_dir", create)
        self.assertEqual(config["active_model"], self.agents["default"]["active_model"])
        self.assertEqual(config["approval_level"], "SMART")
        self.assertEqual(config["channels"], {"console": {"enabled": True}, "astrbot": {"enabled": True}})
        memory = config["running"]["reme_light_memory_config"]
        self.assertEqual(memory["auto_memory_interval"], 7)
        self.assertEqual(memory["auto_memory_search_config"], {"enabled": True, "max_results": 3})
        self.assertEqual(memory["daily_dir"], "memory")
        self.assertEqual(memory["digest_dir"], "digest")
        tools = config["tools"]["builtin_tools"]
        self.assertTrue(tools["read_file"]["enabled"])
        self.assertFalse(tools["write_file"]["enabled"])
        for name in ("unknown_plugin", "chat_with_agent", "execute_shell_command", "execute_python_code"):
            self.assertFalse(tools[name]["enabled"])
        self.assertFalse(tools["browser"]["enabled"])
        self.assertFalse(tools["web_fetch"]["enabled"])
        self.assertFalse(tools["set_user_timezone"]["enabled"])
        self.assertFalse(tools["get_token_usage"]["enabled"])
        for name in ("astrbot_browser", "astrbot_list_tools", "astrbot_call_tool", "astrbot_media_workspace",
                     "astrbot_send_file", "memory_search"):
            self.assertTrue(tools[name]["enabled"])
        mutations = [call for call in self.calls if call["method"] != "GET"]
        wire = json.dumps(mutations)
        for secret in (TOKEN, "DO-NOT-COPY", "PRIVATE-OWNER.md", "/private-owner-files"):
            self.assertNotIn(secret, wire)
        self.assertFalse(any("/copy" in call["path"] or "PROFILE.md" in call["path"] for call in self.calls))

    async def test_same_agent_is_stable_after_manager_restart_but_revalidated(self):
        config = await self.manager.ensure_agent(PRIVATE)
        again = await PersonalAgentManager(self.client).ensure_agent(PRIVATE)
        self.assertEqual(config, again)
        self.assertEqual(self.create_count, 1)
        self.agents[PRIVATE]["description"] = "an unrelated owner's agent"
        with self.assertRaisesRegex(BridgeError, "拒绝接管"):
            await self.manager.ensure_agent(PRIVATE)

    async def test_first_requests_are_serialized_and_do_not_create_duplicates(self):
        results = await asyncio.gather(*(self.manager.ensure_agent(PRIVATE) for _ in range(5)))
        self.assertEqual(self.create_count, 1)
        self.assertTrue(all(item["id"] == PRIVATE for item in results))

    async def test_uncertain_create_is_reconciled_only_at_the_same_id(self):
        self.create_error_after_commit = True
        config = await self.manager.ensure_agent(PRIVATE)
        self.assertEqual(config["id"], PRIVATE)
        self.assertEqual(self.create_count, 1)
        self.assertEqual(set(self.agents), {"default", PRIVATE})

    async def test_timeout_after_create_recovers_without_another_post(self):
        original = self.manager._request
        async def timeout_after_post(method, path, *args, **kwargs):
            result = await original(method, path, *args, **kwargs)
            if method == "POST" and path == "/api/agents":
                raise BridgeError("模拟连接结果未知")
            return result
        with patch.object(self.manager, "_request", side_effect=timeout_after_post):
            config = await self.manager.ensure_agent(PRIVATE)
        self.assertEqual(config["id"], PRIVATE)
        self.assertEqual(self.create_count, 1)

    async def test_person_and_group_memory_are_distinct_and_restart_persistent(self):
        own = await self.manager.remember(PRIVATE, "我偏好无糖茶")
        other = await self.manager.remember(OTHER, "我偏好咖啡")
        group = await self.manager.remember(GROUP, "群里周五开会")
        restarted = PersonalAgentManager(self.client)
        for agent, note in ((PRIVATE, own), (OTHER, other), (GROUP, group)):
            self.assertEqual(note["index_status"], "pending")
            self.assertEqual(await restarted.memory_list(agent), [{"id": note["id"], "text": note["text"]}])
        self.assertIn("不能导入或披露任何私聊记忆", self.files[GROUP])
        with self.assertRaisesRegex(BridgeError, "没有该记忆编号"):
            await restarted.correct(OTHER, own["id"], "恶意覆盖")
        with self.assertRaisesRegex(BridgeError, "没有该记忆编号"):
            await restarted.forget(GROUP, own["id"])

    async def test_explicit_note_can_be_corrected_and_forgotten_without_touching_auto_memory(self):
        note = await self.manager.remember(PRIVATE, "叫我甲")
        fixed = await self.manager.correct(PRIVATE, note["id"], "叫我乙")
        self.assertEqual(fixed, {"id": note["id"], "text": "叫我乙", "index_status": "pending"})
        self.assertEqual(await self.manager.memory_list(PRIVATE), [{"id": fixed["id"], "text": fixed["text"]}])
        result = await self.manager.forget(PRIVATE, note["id"])
        self.assertEqual(result["scope"], "explicit_note_only")
        self.assertEqual(result["index_status"], "pending")
        self.assertEqual(await self.manager.memory_list(PRIVATE), [])
        self.assertTrue(all("automatic-user-preferences" not in call["path"] for call in self.calls))
        reindexes = [call for call in self.calls if "/memory/reindex" in call["path"]]
        self.assertEqual(len(reindexes), 3)
        self.assertTrue(all(PRIVATE in call["path"] for call in reindexes))
        self.assertTrue(all(call["query"]["scope"] == "bm25" for call in reindexes))

    async def test_embedding_configured_note_reindexes_all_but_does_not_send_credentials(self):
        await self.manager.ensure_agent(PRIVATE)
        embedding = {"backend": "openai", "model_name": "fixture-embedding", "api_key": "fixture-key"}
        self.agents[PRIVATE]["running"]["reme_light_memory_config"]["embedding_model_config"] = embedding
        await self.manager.remember(PRIVATE, "带向量模型的备注")
        reindex = next(call for call in reversed(self.calls) if "/memory/reindex" in call["path"])
        self.assertEqual(reindex["query"]["scope"], "all")
        self.assertNotIn("fixture-key", json.dumps(reindex))

    async def test_active_note_limit_rejects_new_write_but_list_and_delete_still_work(self):
        with patch("astrbot_plugin_qwenpaw_bridge.personal_agent._MAX_NOTES", 2):
            first = await self.manager.remember(PRIVATE, "第一条")
            second = await self.manager.remember(PRIVATE, "第二条")
            count = len([call for call in self.calls if call["method"] == "PUT" and "/workspace/memory/" in call["path"]])
            with self.assertRaisesRegex(BridgeError, "达到 2 条"):
                await self.manager.remember(PRIVATE, "不应写入的第三条")
            self.assertEqual(len(await self.manager.memory_list(PRIVATE)), 2)
            self.assertEqual(count, len([call for call in self.calls if call["method"] == "PUT" and "/workspace/memory/" in call["path"]]))
            await self.manager.forget(PRIVATE, first["id"])
            third = await self.manager.remember(PRIVATE, "删除后可新增")
            self.assertEqual(await self.manager.memory_list(PRIVATE), [{"id": note["id"], "text": note["text"]} for note in (second, third)])

    async def test_missing_model_does_not_disable_manual_memory(self):
        self.agents["default"]["active_model"] = None
        note = await self.manager.remember(PRIVATE, "不需要模型也能存下这条备注")
        self.assertEqual(await self.manager.memory_list(PRIVATE), [{"id": note["id"], "text": note["text"]}])
        self.assertIsNone(self.agents[PRIVATE]["active_model"])

    async def test_off_approval_is_never_inherited(self):
        self.agents["default"]["approval_level"] = "OFF"
        config = await self.manager.ensure_agent(PRIVATE)
        self.assertEqual(config["approval_level"], "AUTO")

    async def test_disabled_auto_extraction_cadence_is_preserved(self):
        self.agents["default"]["running"]["reme_light_memory_config"]["auto_memory_interval"] = 0
        config = await self.manager.ensure_agent(PRIVATE)
        self.assertEqual(config["running"]["reme_light_memory_config"]["auto_memory_interval"], 0)

    async def test_scope_id_and_note_traversal_rejected_before_http(self):
        for bad in ("default", "abp_../default", "abp_" + "a" * 31, PRIVATE + "/..", "ABP_" + "a" * 32):
            with self.subTest(bad=bad), self.assertRaises(BridgeError):
                await self.manager.ensure_agent(bad)
        with self.assertRaises(BridgeError):
            await self.manager.ensure_agent(GROUP, "private")
        with self.assertRaises(BridgeError):
            await self.manager.correct(PRIVATE, "../automatic-user-preferences", "x")
        self.assertEqual(self.calls, [])

    async def test_modified_workspace_or_unsafe_permissions_fail_closed(self):
        config = await self.manager.ensure_agent(PRIVATE)
        for field, value in (("workspace_dir", "/fixture/workspaces/default"),
                             ("project_dir", "/private-owner-files"), ("approval_level", "OFF")):
            self.agents[PRIVATE] = {**copy.deepcopy(config), field: value}
            with self.subTest(field=field), self.assertRaises(BridgeError):
                await self.manager.memory_list(PRIVATE)
        self.agents[PRIVATE] = copy.deepcopy(config)
        self.agents[PRIVATE]["tools"]["builtin_tools"]["execute_shell_command"]["enabled"] = True
        with self.assertRaises(BridgeError):
            await self.manager.ensure_agent(PRIVATE)
        self.assertFalse(any(call["path"].startswith("/api/workspace/memory") for call in self.calls))

    async def test_notes_copied_from_someone_else_cannot_be_read_or_changed(self):
        note = await self.manager.remember(PRIVATE, "私密备注")
        await self.manager.ensure_agent(OTHER)
        filename = f"bridge-note-{note['id']}.md"
        self.notes[OTHER, filename] = self.notes[PRIVATE, filename]
        with self.assertRaisesRegex(BridgeError, "不是当前用户"):
            await self.manager.memory_list(OTHER)
        with self.assertRaisesRegex(BridgeError, "不是当前用户"):
            await self.manager.forget(OTHER, note["id"])

    async def test_failed_index_rebuild_never_claims_memory_operation_complete(self):
        self.reindex_status = 503
        note = await self.manager.remember(PRIVATE, "已经存文件但索引不确定")
        self.assertEqual(note["index_status"], "pending")
        self.assertRegex(note["index_warning"], "记忆文件已更新.*索引")
        self.assertEqual(await self.manager.memory_list(PRIVATE), [{"id": note["id"], "text": note["text"]}])
        self.assertEqual(len(self.notes), 1)

    async def test_index_failure_after_correction_or_forget_reports_persisted_change(self):
        note = await self.manager.remember(PRIVATE, "旧内容")
        self.reindex_status = 503
        fixed = await self.manager.correct(PRIVATE, note["id"], "新内容")
        self.assertEqual(fixed["index_status"], "pending")
        self.assertIn("已更新", fixed["index_warning"])
        self.assertEqual(await self.manager.memory_list(PRIVATE), [{"id": note["id"], "text": "新内容"}])
        result = await self.manager.forget(PRIVATE, note["id"])
        self.assertTrue(result["forgotten"])
        self.assertEqual(result["index_status"], "pending")
        self.assertIn("已更新", result["index_warning"])
        self.assertEqual(await self.manager.memory_list(PRIVATE), [])

    async def test_effective_model_readiness_checks_ownership_and_does_not_call_model(self):
        self.assertTrue(await self.manager.model_ready(PRIVATE))
        self.active_model = None
        self.assertFalse(await self.manager.model_ready(PRIVATE))
        self.active_model = {"provider_id": "fixture", "model": ""}
        self.assertFalse(await self.manager.model_ready(PRIVATE))
        self.agents[PRIVATE]["description"] = "not managed"
        count = len([call for call in self.calls if call["path"] == "/api/models/active"])
        with self.assertRaises(BridgeError):
            await self.manager.model_ready(PRIVATE)
        self.assertEqual(count, len([call for call in self.calls if call["path"] == "/api/models/active"]))
        self.assertFalse(any("/console/chat" in call["path"] for call in self.calls))

    async def test_authentication_failure_never_creates_or_uses_default_chat(self):
        self.get_status[PRIVATE] = 403
        with self.assertRaises(BridgeError) as error:
            await self.manager.ensure_agent(PRIVATE)
        self.assertNotIn("DO-NOT-ECHO", str(error.exception))
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertEqual(self.create_count, 0)
        self.assertFalse(any("/console/chat" in call["path"] for call in self.calls))


if __name__ == "__main__":
    unittest.main()
