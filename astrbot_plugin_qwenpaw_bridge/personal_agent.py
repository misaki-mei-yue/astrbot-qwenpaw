"""Per-person native QwenPaw agents and explicit, user-managed memory notes.

Only the official 2.2.1 HTTP API is used. A workspace is a memory namespace,
not an operating-system sandbox. Never treat these checks as file/shell ACLs.
The management signature deliberately fails closed after a runtime-token
rotation; an administrator must migrate ownership explicitly in that case.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import re
import secrets
from pathlib import PurePosixPath
from urllib.parse import quote

import aiohttp

from .bridge_core import BridgeError


_AGENT = re.compile(r"ab(?P<scope>[pg])_[0-9a-f]{32}\Z")
_NOTE = re.compile(r"[0-9a-f]{32}\Z")
_NOTE_FILE = re.compile(r"bridge-note-([0-9a-f]{32})\.md\Z")
_CROSS_AGENT_TOOLS = {
    "list_agents", "chat_with_agent", "submit_to_agent", "check_agent_task",
    "delegate_external_agent", "spawn_agent", "spawn_subagent",
}
_SAFE_TOOLS = {
    "read_file", "write_file", "edit_file", "append_file", "grep_search", "glob_search",
    "send_file_to_user", "view_image", "view_video", "web_search",
    "get_current_time", "memory_search",
    "astrbot_list_tools", "astrbot_call_tool", "astrbot_media_workspace", "astrbot_send_file", "astrbot_browser",
}
_UNSAFE_TOOLS = _CROSS_AGENT_TOOLS | {
    "execute_shell_command", "execute_python_code", "run_tool_batch", "ast_search",
    "desktop_screenshot", "activate_f1_exploration_mode", "browser", "web_fetch",
    "set_user_timezone", "get_token_usage",
}
_BRIDGE_TOOLS = {
    "astrbot_list_tools", "astrbot_call_tool", "astrbot_media_workspace", "astrbot_send_file",
    "astrbot_browser", "memory_search",
}
_MEMORY_DIRS = {
    "metadata_dir": "mem_metadata", "session_dir": "mem_session",
    "mem_session_dir": "mem_agent", "resource_dir": "resource",
    "daily_dir": "memory", "digest_dir": "digest",
}
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_TEXT = 8000
_MAX_NOTES = 200


class _ApiError(BridgeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"QwenPaw 接口返回 HTTP {status}，请在其后台检查；没有切换到其他用户的助手。")


class PersonalAgentManager:
    """Provision stable agents without copying a template user's documents.

    ``client.agent_id`` is the administrator-selected configuration template.
    Every operation rechecks ownership against the server. There is no trusted
    ready cache, and no fallback to the shared/default agent.
    """

    def __init__(self, client):
        self.client = client
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _scope(agent_id: str, scope: str | None = None) -> str:
        match = _AGENT.fullmatch(agent_id) if isinstance(agent_id, str) else None
        if not match:
            raise BridgeError("个人助手标识无效，请让管理员检查桥接身份记录。")
        actual = "private" if match["scope"] == "p" else "group"
        if scope is not None and scope != actual:
            raise BridgeError("助手的私聊／群聊范围不匹配，已停止访问记忆。")
        return actual

    def _lock(self, agent_id: str) -> asyncio.Lock:
        self._scope(agent_id)
        return self._locks.setdefault(agent_id, asyncio.Lock())

    def _signature(self, value: str) -> str:
        return hmac.new(self.client._runtime_token.encode(), value.encode(), hashlib.sha256).hexdigest()

    def _marker(self, agent_id: str, scope: str, state: str) -> str:
        signature = self._signature(f"astrbot-personal-agent:v1:{agent_id}:{scope}")
        return f"astrbot-qwenpaw-bridge:v1:{scope}:{signature}:{state}"

    async def _request(self, method, path, payload=None, *, agent_id=None,
                       statuses=(200,), missing=False, timeout=25):
        http = await self.client._session()
        headers = {"X-Agent-Id": agent_id} if agent_id else {}
        try:
            async with http.request(
                method, self.client.base_url + path, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                allow_redirects=False,
            ) as response:
                if response.status == 404 and missing:
                    return None
                if response.status not in statuses:
                    raise _ApiError(response.status)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE:
                        raise BridgeError("QwenPaw 返回的数据过大，已停止访问。")
                body = json.loads(raw)
                if not isinstance(body, (dict, list)):
                    raise ValueError("Expected JSON object or array")
                return body
        except _ApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            raise BridgeError("无法确认 QwenPaw 接口结果，请检查后台和连接后重试；没有访问其他助手。") from None

    @staticmethod
    def _workspace_root(template: dict) -> PurePosixPath:
        raw = template.get("workspace_dir")
        path = PurePosixPath(raw) if isinstance(raw, str) else None
        if not path or not path.is_absolute() or ".." in path.parts or path.name != template.get("id"):
            raise BridgeError("模板工作区路径不符合独立助手布局，请在 QwenPaw 后台检查。")
        return path.parent

    def _verify(self, config, agent_id, scope, root, *, ready=False):
        if not isinstance(config, dict):
            raise BridgeError("QwenPaw 的助手配置格式异常，已停止访问。")
        expected = {self._marker(agent_id, scope, "ready")}
        if not ready:
            expected.add(self._marker(agent_id, scope, "pending"))
        if (config.get("id") != agent_id or config.get("description") not in expected
                or config.get("workspace_dir") != str(root / agent_id)
                or config.get("backend", "qwenpaw") != "qwenpaw"):
            raise BridgeError("此助手不属于当前桥接或其工作区已改变，已拒绝接管；令牌更换后请由管理员迁移管理标记。")
        if config.get("description") == self._marker(agent_id, scope, "ready"):
            running = config.get("running") or {}
            memory = running.get("reme_light_memory_config") or {}
            channels = config.get("channels") or {}
            builtin = (config.get("tools") or {}).get("builtin_tools") or {}
            if (config.get("project_dir") not in (None, "")
                    or config.get("approval_level") not in {"AUTO", "SMART", "STRICT"}
                    or running.get("memory_manager_backend") != "remelight"
                    or any(memory.get(key) != value for key, value in _MEMORY_DIRS.items())
                    or any(isinstance(value, dict) and value.get("enabled")
                           for key, value in channels.items() if key not in {"console", "astrbot"})
                    or any(isinstance(value, dict) and value.get("enabled")
                           for key, value in builtin.items() if key not in _SAFE_TOOLS)):
                raise BridgeError("此助手的工作区、记忆目录或频道隔离设置已改变，请先由管理员检查。")
        return config

    async def ensure_agent(self, agent_id: str, scope: str = "private") -> dict:
        self._scope(agent_id, scope)
        async with self._lock(agent_id):
            return await self._ensure_locked(agent_id, scope)

    async def model_ready(self, agent_id: str) -> bool:
        """Check effective model selection without calling or billing a model."""
        scope = self._scope(agent_id)
        async with self._lock(agent_id):
            await self._ensure_locked(agent_id, scope)
            result = await self._request(
                "GET", f"/api/models/active?scope=effective&agent_id={agent_id}", agent_id=agent_id,
            )
            if not isinstance(result, dict):
                raise BridgeError("无法确认助手的模型设置，请在 QwenPaw 后台检查。")
            model = result.get("active_llm")
            return (isinstance(model, dict) and bool(model.get("provider_id"))
                    and bool(model.get("model")))

    async def _ensure_locked(self, agent_id: str, scope: str) -> dict:
        template_id = self.client.agent_id
        if not isinstance(template_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id):
            raise BridgeError("QwenPaw 模板助手标识无效，请检查桥接配置。")
        template = await self._request("GET", f"/api/agents/{quote(template_id, safe='')}")
        if not isinstance(template, dict) or template.get("id") != template_id:
            raise BridgeError("QwenPaw 模板助手配置不一致，已停止创建。")
        root = self._workspace_root(template)
        path = f"/api/agents/{agent_id}"
        current = await self._request("GET", path, missing=True)
        if current is not None:
            self._verify(current, agent_id, scope, root)
            if current.get("description") == self._marker(agent_id, scope, "ready"):
                return current
        else:
            skills = await self._request("GET", "/api/skills", agent_id=template_id)
            if not isinstance(skills, list):
                raise BridgeError("模板技能列表格式异常，已停止创建。")
            names = []
            for skill in skills:
                if (isinstance(skill, dict) and skill.get("enabled") is True
                        and isinstance(skill.get("name"), str)
                        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", skill["name"])
                        and set(skill.get("channels") or ["all"]) & {"all", "astrbot"}):
                    names.append(skill["name"])
            payload = {
                "id": agent_id, "name": f"AstrBot {scope} {agent_id[-8:]}",
                "description": self._marker(agent_id, scope, "pending"),
                "language": template.get("language") or "zh", "backend": "qwenpaw",
                "skill_names": sorted(set(names)),
            }
            for field in ("active_model", "fallback_models", "fallback_policy", "subagent_model"):
                if field in template:
                    payload[field] = copy.deepcopy(template[field])
            try:
                await self._request("POST", "/api/agents", payload, statuses=(201,))
            except BridgeError as original:
                # POST can have reached the server before a timeout/duplicate-ID
                # response. Only look up the SAME ID; never POST a random copy.
                try:
                    current = await self._request("GET", path, missing=True)
                except BridgeError:
                    raise original from None
                if current is None:
                    raise original from None
            if current is None:
                current = await self._request("GET", path)
            self._verify(current, agent_id, scope, root)
            if current.get("description") == self._marker(agent_id, scope, "ready"):
                return current

        updated = self._configuration(current, template, agent_id, scope)
        await self._request("PUT", path, updated)
        instructions = (
            "# AstrBot 会话与记忆范围\n\n"
            f"本助手的独立范围是 {scope}，标识 {agent_id}。\n"
            "只根据本范围的对话记录事实、偏好、决定和待办；不把猜测写成用户事实。\n"
            "不要访问、复制或委派读取其他助手的工作区、记忆和会话。\n"
            "原生记忆工具负责自动提炼和召回；用户显式记住的备注位于 digest/bridge-note-*.md。\n"
            "遇到记忆冲突先确认并更正，不把旧偏好当成永久事实。\n"
            "桥接备注的删除仅作用于对应备注，不代表历史对话或自动记忆已经全部删除。\n"
        )
        if scope == "group":
            instructions += "本范围是群内上下文，不能导入或披露任何私聊记忆。\n"
        result = await self._request(
            "PUT", "/api/workspace/files/BRIDGE_IDENTITY.md", {"content": instructions}, agent_id=agent_id,
        )
        if not isinstance(result, dict) or result.get("written") is not True:
            raise BridgeError("独立记忆说明尚未保存，助手未完成初始化，请重试。")
        updated["description"] = self._marker(agent_id, scope, "ready")
        await self._request("PUT", path, updated)
        persisted = await self._request("GET", path)
        return self._verify(persisted, agent_id, scope, root, ready=True)

    def _configuration(self, current, template, agent_id, scope):
        result = {
            "id": agent_id, "name": f"AstrBot {scope} {agent_id[-8:]}",
            "description": self._marker(agent_id, scope, "pending"),
            "workspace_dir": current["workspace_dir"], "project_dir": None,
            "backend": "qwenpaw", "backend_settings": {},
            "channels": {"console": {"enabled": True}, "astrbot": {"enabled": True}},
            "mcp": {}, "mail": None, "last_dispatch": None,
            "language": template.get("language") or "zh",
            "approval_level": (template.get("approval_level")
                               if template.get("approval_level") in {"STRICT", "SMART"} else "AUTO"),
            "system_prompt_files": ["AGENTS.md", "SOUL.md", "PROFILE.md", "BRIDGE_IDENTITY.md"],
        }
        for field in ("active_model", "fallback_models", "fallback_policy", "subagent_model", "thinking_level"):
            if field in template:
                result[field] = copy.deepcopy(template[field])
        builtin = {**((current.get("tools") or {}).get("builtin_tools") or {}),
                   **((template.get("tools") or {}).get("builtin_tools") or {})}
        tools = {}
        for name, tool in builtin.items():
            if not isinstance(tool, dict) or not isinstance(name, str):
                continue
            tools[name] = {"name": name}
            for flag in ("enabled", "display_to_user", "async_execution"):
                if isinstance(tool.get(flag), bool):
                    tools[name][flag] = tool[flag]
            if name not in _SAFE_TOOLS:
                tools[name]["enabled"] = False
        for name in _UNSAFE_TOOLS:
            tools.setdefault(name, {"name": name})["enabled"] = False
        for name in _BRIDGE_TOOLS:
            tools.setdefault(name, {"name": name})["enabled"] = True
        result["tools"] = {"builtin_tools": tools}
        # Start from this NEW agent's defaults, not the template's directories,
        # embedding secrets, plugin payloads, sessions or memory files.
        running = copy.deepcopy(current.get("running") or {})
        running["memory_manager_backend"] = "remelight"
        running["memory_backend_configs"] = {}
        running["daily_memory_dir"] = "memory"
        memory = running.setdefault("reme_light_memory_config", {})
        source = (template.get("running") or {}).get("reme_light_memory_config") or {}
        memory.update(_MEMORY_DIRS)
        for field in ("auto_memory_interval", "dream_cron_enabled", "dream_cron",
                      "auto_memory_inbox_push_enabled", "auto_dream_inbox_push_enabled"):
            if field in source:
                memory[field] = copy.deepcopy(source[field])
        memory.setdefault("auto_memory_interval", 5)
        memory["memory_search_enabled"] = True
        memory["auto_memory_search_config"] = {"enabled": True, "max_results": 3}
        memory["daily_paper_cron_enabled"] = False
        memory["auto_fin_cron_enabled"] = False
        result["running"] = running
        return result

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
            raise BridgeError(f"请提供 1 至 {_MAX_TEXT} 个字符的记忆内容。")
        return text.strip()

    @staticmethod
    def _note_id(note_id):
        if not isinstance(note_id, str) or not _NOTE.fullmatch(note_id):
            raise BridgeError("记忆编号无效，请先查看记忆列表并使用其中的编号。")
        return note_id

    def _note_prefix(self, agent_id, note_id):
        marker = self._signature(f"astrbot-memory-note:v1:{agent_id}:{note_id}")
        return f"<!-- astrbot-bridge-note:v1:{marker} -->\n\n"

    @staticmethod
    def _note_path(note_id):
        return f"/api/workspace/memory/bridge-note-{note_id}.md?section=digest"

    async def _read_note(self, agent_id, note_id):
        result = await self._request("GET", self._note_path(note_id), agent_id=agent_id, missing=True)
        if result is None:
            raise BridgeError("这个范围中没有该记忆编号，请重新查看记忆列表。")
        content = result.get("content") if isinstance(result, dict) else None
        if content == "":
            return None
        prefix = self._note_prefix(agent_id, note_id)
        if not isinstance(content, str) or not content.startswith(prefix):
            raise BridgeError("此文件不是当前用户的桥接备注，已拒绝读取或修改。")
        return {"id": note_id, "text": content[len(prefix):].strip()}

    async def _reindex(self, agent_id, config):
        # Native reindex only rebuilds currently ingested chunks. File watching
        # is asynchronous, so even HTTP 200 does NOT prove this edit searchable.
        state = {"index_status": "pending"}
        # The native all-scope endpoint returns 409 without an embedding
        # provider. Match QwenPaw's actual enablement contract; BM25 needs no
        # model and remains usable when the assistant is not configured yet.
        embedding = (((config.get("running") or {}).get("reme_light_memory_config") or {})
                     .get("embedding_model_config") or {})
        backend = embedding.get("backend", "openai")
        enabled = bool(str(embedding.get("model_name") or "").strip()) and (
            backend == "ollama" or (backend in {"openai", "dashscope", "dashscope_multimodal", "gemini"}
                                     and bool(str(embedding.get("api_key") or "").strip()))
        )
        scope = "all" if enabled else "bm25"
        try:
            result = await self._request("POST", f"/api/agents/{agent_id}/memory/reindex?scope={scope}", timeout=60)
        except BridgeError:
            state["index_warning"] = "记忆文件已更新，但索引刷新请求未完成；请勿重复新增，可先查看记忆列表，并在 QwenPaw 后台检查索引。"
            return state
        if not isinstance(result, dict) or result.get("status") != "completed":
            state["index_warning"] = "记忆文件已更新，但索引刷新结果未确认，请在 QwenPaw 后台检查索引。"
        return state

    async def memory_list(self, agent_id: str) -> list[dict]:
        scope = self._scope(agent_id)
        async with self._lock(agent_id):
            await self._ensure_locked(agent_id, scope)
            return await self._memory_list_locked(agent_id)

    async def _memory_list_locked(self, agent_id):
        files = await self._request("GET", "/api/workspace/memory?section=digest", agent_id=agent_id)
        if not isinstance(files, list):
            raise BridgeError("记忆列表格式异常，请检查 QwenPaw 后台。")
        names = []
        for item in files:
            filename = item.get("filename") if isinstance(item, dict) else None
            match = _NOTE_FILE.fullmatch(filename) if isinstance(filename, str) else None
            if match:
                names.append(match[1])
        result = []
        for note_id in names:
            note = await self._read_note(agent_id, note_id)
            if note is not None:
                result.append(note)
        return result

    async def remember(self, agent_id: str, text: str) -> dict:
        scope, text = self._scope(agent_id), self._text(text)
        async with self._lock(agent_id):
            config = await self._ensure_locked(agent_id, scope)
            if len(await self._memory_list_locked(agent_id)) >= _MAX_NOTES:
                raise BridgeError(f"显式记忆已达到 {_MAX_NOTES} 条，请先查看并删除不再需要的备注；已有记忆仍可查看和更正。")
            note_id = secrets.token_hex(16)
            content = self._note_prefix(agent_id, note_id) + text + "\n"
            result = await self._request("PUT", self._note_path(note_id), {"content": content}, agent_id=agent_id)
            if not isinstance(result, dict) or result.get("written") is not True:
                raise BridgeError("未确认记忆写入，请先查看记忆列表后再重试。")
            return {"id": note_id, "text": text, **await self._reindex(agent_id, config)}

    async def correct(self, agent_id: str, note_id: str, text: str) -> dict:
        scope, note_id, text = self._scope(agent_id), self._note_id(note_id), self._text(text)
        async with self._lock(agent_id):
            config = await self._ensure_locked(agent_id, scope)
            if await self._read_note(agent_id, note_id) is None:
                raise BridgeError("这条备注已经删除，请创建一条新记忆。")
            content = self._note_prefix(agent_id, note_id) + text + "\n"
            result = await self._request("PUT", self._note_path(note_id), {"content": content}, agent_id=agent_id)
            if not isinstance(result, dict) or result.get("written") is not True:
                raise BridgeError("未确认记忆更正，请先查看记忆列表。")
            return {"id": note_id, "text": text, **await self._reindex(agent_id, config)}

    async def forget(self, agent_id: str, note_id: str) -> dict:
        scope, note_id = self._scope(agent_id), self._note_id(note_id)
        async with self._lock(agent_id):
            config = await self._ensure_locked(agent_id, scope)
            await self._read_note(agent_id, note_id)
            # QwenPaw 2.2.1 exposes no DELETE for memory files. Empty only the
            # owned note, then rebuild indexes; never erase unrelated memories.
            result = await self._request("PUT", self._note_path(note_id), {"content": ""}, agent_id=agent_id)
            if not isinstance(result, dict) or result.get("written") is not True:
                raise BridgeError("未确认备注删除，请先查看记忆列表。")
            return {"id": note_id, "forgotten": True, "scope": "explicit_note_only",
                    **await self._reindex(agent_id, config)}
