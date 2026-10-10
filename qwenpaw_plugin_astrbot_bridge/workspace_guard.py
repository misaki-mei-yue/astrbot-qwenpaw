"""Audited tool boundary for bridge-owned QwenPaw agent workspaces.

Official QwenPaw 2.2.1 PluginApi.register_middleware calls this factory for each
request. AgentScope MiddlewareBase.on_acting receives a ToolCallBlock whose
input is a JSON string, and yields ToolChunk / ToolResponse. This guard uses
that public interception point, not lifecycle hooks or private Toolkit patches.

This is an application-level file/tool boundary, NOT an operating-system
sandbox. It cannot defend against a hostile process concurrently replacing
files between validation and a native tool opening them, or arbitrary installed
Python plugins. Provisioned agents must keep shell, cross-agent execution and
arbitrary plugins disabled. Native browser(code) executes Python and is denied;
only the separately constrained astrbot_browser entry point is admitted.
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
import unicodedata
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from .bridge_client import config_value


MANAGED_AGENT = re.compile(r"ab[pg]_[0-9a-f]{32}\Z")
BRIDGE_SESSION = re.compile(r"ab_[0-9a-f]{32}\Z")
BRIDGE_FILES_ROOT = Path("/bridge-files")
_plugin_config = {}
_MAX_SEARCH_ENTRIES = 20000
_PRIVATE_RUNTIME_ENTRIES = frozenset({"agent.json", "config.json", "credentials.yaml", "credentials", ".qwenpaw", ".env", "policy", "policies", "plugins", "active_skills", "customized_skills"})

# These are the actual QwenPaw 2.2.1 Python argument names. Restrict extra
# fields too: adding a new path/agent selector upstream must trigger review.
_FILE_TOOLS = {
    "read_file": ("file_path", frozenset({"file_path", "start_line", "end_line"})),
    "write_file": ("file_path", frozenset({"file_path", "content"})),
    "edit_file": ("file_path", frozenset({"file_path", "old_text", "new_text"})),
    "append_file": ("file_path", frozenset({"file_path", "content"})),
    "view_image": ("image_path", frozenset({"image_path"})),
    "view_video": ("video_path", frozenset({"video_path"})),
    "send_file_to_user": ("file_path", frozenset({"file_path"})),
}
_SEARCH_TOOLS = {
    "glob_search": frozenset({"pattern", "path"}),
    "grep_search": frozenset({"pattern", "path", "is_regex", "case_sensitive", "context_lines", "include_pattern", "show_file"}),
}
_BRIDGE_TOOLS = frozenset({
    "astrbot_list_tools", "astrbot_call_tool", "astrbot_media_workspace",
    "astrbot_send_file", "astrbot_browser",
})
_MEMORY_FIELDS = frozenset({"query", "max_results", "min_score", "limit"})


class WorkspaceAccessDenied(ValueError):
    """Public denial without exposing host paths or other users' identities."""


def _deny(message="此工具只能访问当前用户的工作区和当前会话附件。"):
    raise WorkspaceAccessDenied(message)


def _field(block, key, default=None):
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _has_reparse_point(info):
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check_no_links(path: Path):
    """Reject each existing link component, special file and file hardlink."""
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or _has_reparse_point(info):
            _deny("工作区不能通过符号链接或目录连接访问文件。")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            _deny("仅允许访问普通工作文件和目录。")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            _deny("工作区不能访问存在其他硬链接的文件。")


class WorkspaceGuard(MiddlewareBase):
    def __init__(self, *, agent_id, workspace_dir, session_id,
                 files_root=BRIDGE_FILES_ROOT):
        self.agent_id = agent_id
        self.workspace = None
        self.session_root = None
        self.invalid_scope = False
        try:
            if not isinstance(agent_id, str) or not MANAGED_AGENT.fullmatch(agent_id):
                _deny("无法确认当前工作区身份。")
            workspace = Path(workspace_dir)
            if (not workspace.is_absolute() or workspace.name != agent_id
                    or ".." in workspace.parts):
                _deny("无法确认当前工作区身份。")
            _check_no_links(workspace)
            if not workspace.is_dir():
                _deny("当前用户的工作区尚未准备完成。")
            self.workspace = workspace.resolve(strict=True)
            if isinstance(session_id, str) and BRIDGE_SESSION.fullmatch(session_id):
                files = Path(files_root)
                if not files.is_absolute() or ".." in files.parts:
                    _deny("无法确认当前会话的附件目录。")
                _check_no_links(files)
                self.session_root = files / session_id
                _check_no_links(self.session_root)
        except (TypeError, ValueError, OSError, RuntimeError):
            # QwenPaw catches factory exceptions and silently skips failed
            # middleware. Return a denying instance instead of raising here.
            self.invalid_scope = True

    def _path(self, value) -> str:
        if self.invalid_scope or self.workspace is None:
            _deny("当前用户工作区校验失败，已暂停工具执行。")
        if (not isinstance(value, str) or not value or len(value) > 4096
                or "%" in value or value.startswith("~")
                or any(unicodedata.category(char).startswith("C") for char in value)
                or (os.name != "nt" and "\\" in value)):
            _deny("文件路径格式不正确，不支持链接、编码路径或控制字符。")
        value = unicodedata.normalize("NFC", value)
        path = Path(value)
        # Reject file://, http://, UNC, drive-relative and parent traversal
        # instead of allowing a native tool to reinterpret the spelling.
        if (".." in path.parts or "://" in value or value.startswith(("//", "\\\\"))
                or (path.drive and not path.is_absolute())
                or (":" in value and not (os.name == "nt" and path.drive))):
            _deny()
        if not path.is_absolute():
            path = self.workspace / path
        roots = [self.workspace]
        if self.session_root is not None:
            roots.append(self.session_root)
        if not any(path.is_relative_to(root) for root in roots):
            _deny()
        if path.is_relative_to(self.workspace):
            relative = path.relative_to(self.workspace)
            if relative.parts and relative.parts[0].casefold() in _PRIVATE_RUNTIME_ENTRIES:
                _deny("工具不能读取凭据或修改运行配置及插件加载目录。")
        for root in roots:
            _check_no_links(root)
        _check_no_links(path)
        resolved = path.resolve(strict=False)
        if not any(resolved.is_relative_to(root) for root in roots):
            _deny()
        return str(resolved)

    def _search_tree(self, path: Path):
        # Native grep follows symlink files even with os.walk(followlinks=False),
        # and glob can traverse explicit symlink paths. Inspect the whole tree
        # before either recursive tool, including links unrelated to a match.
        count = 0
        if not path.is_dir():
            return
        def walk_error(_error):
            _deny("搜索目录无法完整校验，请缩小搜索范围。")
        for directory, directories, files in os.walk(path, followlinks=False, onerror=walk_error):
            for name in [*directories, *files]:
                count += 1
                if count > _MAX_SEARCH_ENTRIES:
                    _deny("搜索目录过大，请指定更小的工作区子目录。")
                self._path(str(Path(directory) / name))

    @staticmethod
    def _pattern(value):
        if (not isinstance(value, str) or not value or len(value) > 1024
                or value.startswith(("/", "\\", "~")) or "\\" in value
                or ".." in value.split("/") or ":" in value or "%" in value
                or any(unicodedata.category(char).startswith("C") for char in value)):
            _deny("搜索模式不能访问工作区之外的路径。")

    def validate(self, name: str, arguments: dict) -> dict:
        """Return copied inputs with every native path made absolute."""
        if self.invalid_scope or self.workspace is None:
            _deny("当前用户工作区校验失败，已暂停工具执行。")
        _check_no_links(self.workspace)
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            _deny("工具参数格式不正确。")
        values = dict(arguments)
        if name in _FILE_TOOLS:
            field, fields = _FILE_TOOLS[name]
            if set(values) - fields:
                _deny("此工具新增的参数尚未经过工作区隔离校验。")
            values[field] = self._path(values.get(field))
            return values
        if name in _SEARCH_TOOLS:
            if set(values) - _SEARCH_TOOLS[name]:
                _deny("此工具新增的参数尚未经过工作区隔离校验。")
            values["path"] = self._path(values.get("path") or ".")
            if name == "glob_search":
                self._pattern(values.get("pattern"))
            if values.get("include_pattern") is not None:
                self._pattern(values["include_pattern"])
            self._search_tree(Path(values["path"]))
            return values
        if name == "memory_search":
            if set(values) - _MEMORY_FIELDS:
                _deny("记忆搜索仅限当前用户，不接受其他工作区或用户选择参数。")
            # The actual tool is bound to the request's native per-workspace
            # MemoryManager; no caller-supplied workspace/agent id is allowed.
            return values
        if name == "web_search":
            if (set(values) != {"search_term"} or not isinstance(values["search_term"], str)
                    or not values["search_term"].strip() or len(values["search_term"]) > 8192):
                _deny("网页搜索仅接受 search_term，不接受自选 URL 或网络地址。")
            return values
        if name == "get_current_time" and not values:
            return values
        if name in _BRIDGE_TOOLS:
            # Bridge tools independently derive authority from actual request
            # context and validate owner/session on the AstrBot side. A tool
            # name alone never grants a nonce or cross-account authority.
            return values
        if name in {"browser", "execute_shell_command", "shell", "exec", "run_tool_batch"}:
            _deny("当前隔离模式不开放原生脚本执行；浏览器请使用 astrbot_browser。")
        _deny("此工具尚未通过用户工作区隔离审核，已拒绝执行。")

    async def on_acting(self, agent, input_kwargs, next_handler):
        tool_call = input_kwargs.get("tool_call") if isinstance(input_kwargs, dict) else None
        call_id = _field(tool_call, "id", "workspace-guard-denied")
        try:
            name = _field(tool_call, "name")
            raw = _field(tool_call, "input")
            if not isinstance(name, str) or not isinstance(raw, str):
                _deny("工具调用格式不正确。")
            values = self.validate(name, json.loads(raw))
            updated = json.dumps(values, ensure_ascii=False)
            # Official blocks are Pydantic ToolCallBlock objects; the dict
            # branch is useful for wire adapters, without mutating originals.
            if isinstance(tool_call, dict):
                forwarded_call = {**tool_call, "input": updated}
            elif hasattr(tool_call, "model_copy"):
                forwarded_call = tool_call.model_copy(update={"input": updated})
            else:
                forwarded_call = copy.copy(tool_call)
                forwarded_call.input = updated
        except (WorkspaceAccessDenied, OSError, RuntimeError, TypeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, WorkspaceAccessDenied) else "工作区安全检查失败，工具未执行。"
            yield ToolResponse(id=call_id, state=ToolResultState.ERROR,
                               content=[TextBlock(type="text", text=message)])
            return
        async for item in next_handler(**{**input_kwargs, "tool_call": forwarded_call}):
            yield item


def configure_workspace_guard(config):
    """Receive trusted PluginApi configuration alongside the bridge tools."""
    global _plugin_config
    _plugin_config = dict(config or {})


def workspace_guard_factory(ctx, agent_config):
    """Register with api.register_middleware(..., priority=0).

    Guard every request of a bridge-owned agent, including scheduled work and
    console requests. A turn nonce/channel name is intentionally not required.
    Independent root/default/user agents do not receive this middleware.
    """
    agent_id = getattr(ctx, "agent_id", None)
    if not isinstance(agent_id, str) or not MANAGED_AGENT.fullmatch(agent_id):
        return None
    try:
        # These values come from native per-Agent configuration, never a tool
        # call or message metadata. Keep BridgeSettings.from_config precedence:
        # environment > channel config > plugin config > shipped default.
        # At each config level, a direct field takes priority over bridge.*.
        channels = config_value(agent_config, "channels", {})
        channel = config_value(channels, "astrbot", {})
        files_root = BRIDGE_FILES_ROOT
        for config in (_plugin_config, channel):
            value = config_value(config, "files_dir")
            if value is None:
                value = config_value(config_value(config, "bridge", {}), "files_dir")
            if value is not None:
                files_root = value
        files_root = os.environ.get("BRIDGE_FILES_ROOT", os.environ.get("BRIDGE_FILES_DIR", files_root))
    except Exception:
        # QwenPaw skips a middleware whose factory raises. A malformed trusted
        # config must leave a denying guard in place, not remove the boundary.
        return WorkspaceGuard(agent_id=agent_id, workspace_dir=None, session_id=None)
    return WorkspaceGuard(agent_id=agent_id, workspace_dir=getattr(ctx, "workspace_dir", None),
                          session_id=getattr(ctx, "session_id", None), files_root=files_root)
