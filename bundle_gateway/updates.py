"""Same-origin browser API for the isolated, signed host update mailbox."""
from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
import re
import secrets
import time
import uuid

from aiohttp import web
from bundle_updater.common import atomic_json, metadata, read_json, safe_path, sign, verify

PHASES = {"idle", "checking", "available", "updating", "restarting", "completed", "failed", "rolled_back"}
MESSAGES = {
    "idle": "可以手动检查本项目的兼容版本更新。",
    "checking": "正在检查并校验发布版本，请稍候。",
    "available": "发现兼容版本。确认后会先备份，再更新。",
    "updating": "正在备份并更新，请保持 Docker 和电脑运行。",
    "restarting": "正在重启服务，页面会自动重新连接。",
    "completed": "更新完成，服务检查通过。可以刷新界面。",
    "failed": "操作未完成。请查看本机更新服务的诊断记录。",
    "rolled_back": "更新未通过检查，已恢复之前的程序和数据。",
}
ERROR_MESSAGES = {
    "release_manifest_required": "此源码目录没有安装清单。首次请使用完整 Release 包安装，再使用网页更新。",
    "unmanaged_source": "此源码目录尚未登记安装清单，请使用完整 Release 包安装。",
    "local_source_modified": "检测到本地源码修改，为保留改动已停止更新。请先备份并处理修改。",
    "insufficient_backup_space": "安装盘剩余空间不足，无法保存完整备份，本次没有更新。",
    "source_build_unsupported": "当前使用 QwenPaw 源码构建模式，请手动升级并验证。",
    "scheduled_jobs_enabled": "请先在 QwenPaw 暂停定时任务，等待正在进行的任务结束后再更新。",
    "task_check_unavailable": "暂时无法确认 QwenPaw 的任务状态。请检查其后台后再更新。",
    "expired_candidate": "此版本候选已过期，请重新检查更新。",
    "network_unavailable": "暂时无法连接 GitHub，请检查本机网络后再试。",
    "manual_recovery_required": "恢复尚未完成，已暂停更新。请保留备份并检查本机更新记录。",
}
STEPS = {
    "preparing": "校验安装状态与更新包", "staged": "更新文件已经准备好",
    "downloading_images": "准备组件镜像", "building_gateway": "构建新版入口",
    "stopping": "正在停止聊天与任务服务", "stopped": "服务已经停止",
    "backing_up": "正在备份配置、聊天与记忆", "backed_up": "备份完成",
    "installing": "安装新版代码", "starting": "正在启动新版服务",
    "checking": "检查原生服务与桥接", "rolling_back": "正在恢复旧程序与数据",
    "rollback_failed": "需要人工恢复，请保留备份",
}


class UpdateAPI:
    def __init__(self, ipc: Path | None, token: str, version_root: Path):
        self.ipc = safe_path(ipc) if ipc else None
        self.token = token
        self.version_root = version_root
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()
        self.pending_until = 0.0

    def snapshot(self):
        try:
            version = metadata(self.version_root)["version"]
        except (ValueError, OSError):
            version = "unknown"
        result = {"enabled": False, "current_version": version, "phase": "idle", "active": False,
                  "message": "请通过新版启动器启动本机更新服务。", "csrf_token": self.csrf_token}
        if not self.ipc:
            return result
        try:
            state = verify(read_json(self.ipc / "status.json"), self.token)
            if not isinstance(state, dict):
                return result
            heartbeat = state.get("heartbeat")
            if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)) or not 0 <= time.time() - heartbeat <= 20:
                return result
            phase = state.get("phase")
            if phase not in PHASES:
                return result
            result.update(enabled=True, phase=phase, active=state.get("active") is True, message=MESSAGES[phase])
            if state.get("update_capable") is False:
                result["update_capable"] = False
            error = state.get("error")
            if error in ERROR_MESSAGES:
                result["message"] = ERROR_MESSAGES[error]
            if state.get("step") in STEPS:
                result["step"] = STEPS[state["step"]]
            if state.get("recovery_required") is True:
                result.update(recovery_required=True, message="恢复尚未完成，已暂停更新。请保留备份并检查本机更新记录。")
            # Bound and validate every field; never forward raw host diagnostics.
            current = state.get("current_version")
            if isinstance(current, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", current):
                result["current_version"] = current
            if isinstance(state.get("request_id"), str) and re.fullmatch(r"[0-9a-f-]{36}", state["request_id"]):
                result["request_id"] = state["request_id"]
            if isinstance(state.get("progress"), int) and not isinstance(state["progress"], bool):
                result["progress"] = max(0, min(100, state["progress"]))
            if phase == "idle" and state.get("checked") is True:
                result["message"] = "检查完成，目前已经是可用的最新兼容版本。"
            candidate = state.get("candidate")
            if isinstance(candidate, dict):
                cid, ver = candidate.get("id"), candidate.get("version")
                components = candidate.get("components")
                if (isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{32}", cid)
                        and isinstance(ver, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", ver)
                        and isinstance(components, dict) and set(components) == {"astrbot", "qwenpaw", "napcat"}
                        and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", v) for v in components.values())):
                    result["candidate"] = {"id": cid, "version": ver, "components": components,
                                           "notes": str(candidate.get("notes", ""))[:16000],
                                           "html_url": "https://github.com/misaki-mei-yue/astrbot-qwenpaw/releases/tag/v" + ver}
            return result
        except (ValueError, TypeError, OSError):
            return result

    async def handle(self, request, portal: str):
        if request.method == "GET" and request.path == "/bundle-update":
            return web.json_response(self.snapshot(), headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        if request.path not in {"/bundle-update/check", "/bundle-update/apply"}:
            raise web.HTTPNotFound()
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        supplied = request.headers.get("X-Bundle-Update-Token", "")
        if (request.headers.get("Origin") != portal or request.content_type != "application/json"
                or not hmac.compare_digest(self.csrf_token.encode(), supplied.encode("utf-8"))):
            return web.json_response({"error": "forbidden", "message": "请从本机主页重新打开更新窗口。"}, status=403)
        raw = await request.content.read(4097)
        if len(raw) > 4096:
            return web.json_response({"error": "invalid_request", "message": "更新请求无效。"}, status=400)
        import json
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        action = request.path.rsplit("/", 1)[-1]
        if (not isinstance(body, dict) or (action == "check" and body)
                or (action == "apply" and (set(body) != {"candidate_id"} or not isinstance(body["candidate_id"], str)
                                           or not re.fullmatch(r"[0-9a-f]{32}", body["candidate_id"])) )):
            return web.json_response({"error": "invalid_request", "message": "更新请求无效。"}, status=400)
        async with self.lock:
            state = self.snapshot()
            if not state["enabled"]:
                return web.json_response({"error": "updater_unavailable", "message": "请通过新版启动器启动本机更新服务。"}, status=503)
            if state["active"] or time.monotonic() < self.pending_until:
                return web.json_response({"error": "busy", "message": "已有任务正在处理，请等待结果。"}, status=409)
            if state.get("recovery_required"):
                return web.json_response({"error": "recovery_required", "message": "请先完成上次更新的恢复。"}, status=409)
            if action == "apply" and state.get("update_capable") is False:
                return web.json_response({"error": "unmanaged_source", "message": state["message"]}, status=409)
            if action == "apply" and body["candidate_id"] != state.get("candidate", {}).get("id"):
                return web.json_response({"error": "invalid_candidate", "message": "版本已过期，请重新检查更新。"}, status=400)
            rid = str(uuid.uuid4())
            payload = {"id": rid, "action": action, "created": time.time()}
            if action == "apply":
                payload["candidate_id"] = body["candidate_id"]
            try:
                atomic_json(self.ipc / ("request-" + rid + ".json"), sign(payload, self.token), mode=0o644)
            except (OSError, ValueError):
                return web.json_response({"error": "updater_unavailable", "message": "更新服务暂时无法接收请求。"}, status=503)
            self.pending_until = time.monotonic() + 5
            return web.json_response({"request_id": rid, "phase": "checking" if action == "check" else "updating", "active": True}, status=202)
