"""Portal-only API. Private plaintext and Docker control stay on the host."""
from __future__ import annotations

import asyncio
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
import uuid

from aiohttp import BodyPartReader, web
from bundle_updater.common import atomic_json, read_json, safe_path, sign, verify

MAX_ARCHIVE = 8 * 1024**3
ID = re.compile(r"[0-9a-f]{32}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z")
MESSAGES = {
    "idle": "可以导出加密备份，或上传已有备份检查恢复。",
    "exporting": "正在暂停服务、保存一致快照并加密，完成后会恢复服务。",
    "inspecting": "正在解密并检查备份、组件版本和目标路径，尚未覆盖当前数据。",
    "ready_to_restore": "备份检查通过。请确认旧服务器已停止，再开始恢复。",
    "restoring": "正在保存当前数据并恢复备份，入口重启后会自动重新连接。",
    "completed": "操作完成，服务检查通过。",
    "failed": "操作未完成。请检查备份密码、可用空间和本机备份诊断记录。",
    "rolled_back": "恢复后的服务未通过检查，已恢复本次操作前的数据。",
}
ERRORS = {
    "component_version_mismatch": "三套组件版本与备份不一致，请安装匹配版本后再导入。",
    "architecture_mismatch": "备份与新服务器的 Docker CPU 架构不同，暂不支持自动迁移。",
    "restore_plan_expired": "恢复检查已过期，请重新上传检查。",
    "addon_backup_unsupported": "当前为外部 AstrBot 附加安装，暂不支持完整迁移备份。",
    "overwrite_confirmation_required": "目标已有数据，请先确认替换。",
    "password": "备份密码至少需要 12 个字符，最多 1024 字节。",
    "authentication": "密码不正确，或备份文件已损坏。当前数据没有被替换。",
    "archive_invalid": "备份文件损坏或格式不受支持，当前数据没有被替换。",
    "payload_invalid": "备份内容未通过完整性或安全检查，当前数据没有被替换。",
    "archive_limit": "备份超过安全大小限制，或服务器剩余空间不足。",
    "invalid_password": "密码不符合要求，至少需要 12 个字符。",
    "decryption_failed": "密码不正确，或备份文件已损坏。当前数据没有被替换。",
    "authentication_failed": "密码不正确，或备份文件已损坏。当前数据没有被替换。",
    "version_mismatch": "三套组件版本与备份不一致，请安装匹配版本后再导入。",
    "incompatible_components": "三套组件版本与备份不一致，请安装匹配版本后再导入。",
    "insufficient_space": "磁盘空间不足，请腾出空间后再试。",
    "insufficient_backup_space": "磁盘空间不足，请腾出空间后再试。",
    "unsupported_mode": "当前为外部 AstrBot 附加安装，暂不支持完整迁移备份。",
    "expired_plan": "恢复检查已过期，请重新上传检查。",
    "updater_restart_failed": "数据恢复已结束，但更新服务需要通过启动脚本重新启动。",
}


def response(value, status=200):
    return web.json_response(value, status=status, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field.")
        value[key] = item
    return value


class BackupAPI:
    def __init__(self, ipc: Path | None, token: str):
        self.ipc = safe_path(ipc) if ipc else None
        self.token = token
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()
        self.pending_until = 0.0
        self.pending_id = None
        self.pending_phase = None

    def snapshot(self):
        result = {"enabled": False, "phase": "idle", "active": False, "backups": [],
                  "message": "请使用新版启动器启动备份服务。外部 AstrBot 附加安装暂不支持完整备份。", "csrf_token": self.csrf_token}
        if not self.ipc:
            return result
        try:
            value = verify(read_json(self.ipc / "status.json", 262144), self.token)
            stamp = value.get("heartbeat")
            phase = value.get("phase")
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 20 or phase not in MESSAGES:
                return result
            result.update(enabled=value.get("ready") is True, phase=phase, active=value.get("active") is True, message=MESSAGES[phase])
            request_id = value.get("request_id")
            if isinstance(request_id, str) and re.fullmatch(r"[0-9a-f-]{36}", request_id):
                result["request_id"] = request_id
            if value.get("recovery_required") is True:
                result.update(recovery_required=True, message="恢复尚未完成，请保留私有备份目录并检查诊断记录。")
            error = value.get("error")
            if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error):
                result["error"] = error
                if error in ERRORS:
                    result["message"] = ERRORS[error]
            catalog = value.get("backups", [])
            for item in (catalog[:100] if isinstance(catalog, list) else []):
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]):
                    continue
                size, version, created = item.get("size"), item.get("version"), item.get("created_at")
                if type(size) is not int or not 0 < size <= MAX_ARCHIVE or not isinstance(version, str) or len(version) > 100 or not VERSION.fullmatch(version):
                    continue
                # Do not trust a host filename as a path or response header.
                result["backups"].append({"id": item["id"], "filename": "astrbot-qwenpaw-" + item["id"] + ".aqbackup", "size": size, "version": version,
                                          "created": str(created)[:40] if isinstance(created, (str, int, float)) else ""})
            plan = value.get("plan")
            if isinstance(plan, dict) and isinstance(plan.get("plan_id"), str) and ID.fullmatch(plan["plan_id"]):
                components = plan.get("components")
                if (isinstance(plan.get("version"), str) and VERSION.fullmatch(plan["version"])
                        and isinstance(components, dict) and set(components) == {"astrbot", "qwenpaw", "napcat"}
                        and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", v) for v in components.values())
                        and type(plan.get("files")) is int and 0 <= plan["files"] <= 100000
                        and type(plan.get("bytes")) is int and 0 <= plan["bytes"] <= 32 * 1024**3):
                    # Controller warnings are fixed codes; translate without exposing raw content.
                    warnings = {"platform_login_required": "QQ / 微信可能要求重新登录。", "source_must_be_stopped": "请保持旧服务器停止，避免两个实例同时处理消息和任务。",
                                "same_component_versions": "恢复保留原组件版本，不执行数据库降级。",
                                "platform_login_may_expire": "QQ / 微信可能要求重新登录。",
                                "scheduled_jobs_resume": "原定时任务随数据恢复，请检查时间与执行设置。",
                                "custom_absolute_paths_may_need_review": "用户自行填写的绝对路径和外部服务配置需要恢复后检查。"}
                    result["plan"] = {k: plan[k] for k in ("plan_id", "version", "components", "files", "bytes")}
                    result["plan"].update(target_has_data=plan.get("target_has_data") is True, path_check="passed",
                                          warnings=[warnings.get(w, "请在恢复后检查平台登录、定时任务和外部文件路径。") for w in (plan.get("warnings", [])[:10] if isinstance(plan.get("warnings", []), list) else []) if isinstance(w, str)])
            export = value.get("result")
            if isinstance(export, dict) and export.get("kind") in {"export", "restore"}:
                result["result"] = {"kind": export["kind"]}
                identifier = export.get("backup_id")
                if isinstance(identifier, str) and ID.fullmatch(identifier):
                    result["result"].update(backup_id=identifier, filename="astrbot-qwenpaw-" + identifier + ".aqbackup",
                                            download_url="/bundle-backup/download/" + identifier + "?token=" + self.csrf_token)
            if self.pending_id and time.monotonic() < self.pending_until and request_id != self.pending_id:
                result.update(active=True, phase=self.pending_phase, message=MESSAGES[self.pending_phase], request_id=self.pending_id)
                result.pop("result", None)
                result.pop("plan", None)
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return result

    def authorized(self, request, portal):
        return request.headers.get("Origin") == portal and hmac.compare_digest(self.csrf_token.encode(), request.headers.get("X-Bundle-Backup-Token", "").encode())

    async def upload(self, request):
        if request.content_type != "multipart/form-data":
            return response({"message": "请选择加密备份文件。"}, 400)
        if request.content_length is not None and request.content_length > MAX_ARCHIVE + 65536:
            return response({"message": "备份文件超过 8 GiB 上限。"}, 413)
        identifier = secrets.token_hex(16)
        path = safe_path(self.ipc / "uploads" / (identifier + ".aqbackup"))
        try:
            available = shutil.disk_usage(path.parent).free
            if available < (request.content_length or 64 * 1024**2) + 64 * 1024**2:
                return response({"message": "服务器空间不足，无法接收备份。"}, 507)
            reader = await request.multipart()
            part = await reader.next()
            if not isinstance(part, BodyPartReader) or part.name != "file" or not part.filename or not part.filename.lower().endswith(".aqbackup"):
                return response({"message": "请选择 .aqbackup 加密备份文件。"}, 400)
            # Explicit file selection replaces abandoned ciphertext after a
            # reload. Lock + active-job gate exclude a concurrent inspection.
            for previous in path.parent.glob("*.aqbackup"):
                if ID.fullmatch(previous.stem):
                    previous = safe_path(previous)
                    info = previous.stat()
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        previous.unlink()
            size = 0
            with path.open("xb") as output:
                path.chmod(0o644)
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_ARCHIVE, actual_size=size)
                    if size % (16 * 1024**2) < len(chunk) and shutil.disk_usage(path.parent).free < 64 * 1024**2:
                        raise OSError("Insufficient upload storage.")
                    output.write(chunk)
            if size < 64 or await reader.next() is not None:
                raise ValueError("Invalid upload.")
            return response({"upload_id": identifier}, 201)
        except (OSError, ValueError, AssertionError):
            path.unlink(missing_ok=True)
            return response({"message": "上传未完成，请检查文件与可用空间。"}, 400)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def handle(self, request, portal):
        if request.method == "GET" and request.path == "/bundle-backup":
            return response(self.snapshot())
        if request.path.startswith("/bundle-backup/download/"):
            identifier = request.path.rsplit("/", 1)[-1]
            if request.method != "GET" or not ID.fullmatch(identifier) or not hmac.compare_digest(request.query.get("token", "").encode(), self.csrf_token.encode()):
                raise web.HTTPForbidden()
            if not self.ipc or identifier not in {v["id"] for v in self.snapshot()["backups"]}:
                raise web.HTTPNotFound()
            target = safe_path(self.ipc / "transfers" / (identifier + ".aqbackup"))
            try:
                info = target.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ARCHIVE:
                    raise ValueError("Invalid transfer.")
            except (ValueError, OSError):
                raise web.HTTPNotFound()
            return web.FileResponse(target, headers={"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="astrbot-qwenpaw-' + identifier + '.aqbackup"',
                                                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        action = request.path.rsplit("/", 1)[-1]
        if action not in {"export", "upload", "inspect", "restore"}:
            raise web.HTTPNotFound()
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        if not self.authorized(request, portal):
            return response({"message": "请重新打开备份窗口。"}, 403)
        async with self.lock:
            state = self.snapshot()
            if not state["enabled"]:
                return response({"message": state["message"]}, 503)
            if state["active"] or time.monotonic() < self.pending_until or state.get("recovery_required"):
                return response({"message": "已有操作或恢复正在进行，请等待完成。"}, 409)
            if action == "upload":
                return await self.upload(request)
            try:
                if request.content_type != "application/json" or (request.content_length is not None and request.content_length > 8192):
                    return response({"message": "备份请求无效。"}, 400)
                raw = bytearray()
                while len(raw) <= 8192:
                    block = await request.content.read(min(4096, 8193 - len(raw)))
                    if not block:
                        break
                    raw.extend(block)
                body = json.loads(raw, object_pairs_hook=unique_json_object) if len(raw) <= 8192 else None
            except (ValueError, UnicodeError):
                body = None
            keys = {"export": {"password", "destination"}, "inspect": {"upload_id", "password"}, "restore": {"plan_id", "confirm_source_stopped", "overwrite"}}[action]
            if not isinstance(body, dict) or set(body) != keys:
                return response({"message": "备份请求无效。"}, 400)
            identifier = str(uuid.uuid4())
            payload = {"id": identifier, "created": time.time(), "action": action}
            if action in {"export", "inspect"}:
                password = body.get("password")
                try:
                    valid_password = isinstance(password, str) and len(password) >= 12 and len(password.encode("utf-8")) <= 1024
                except UnicodeError:
                    valid_password = False
                if not valid_password:
                    return response({"message": "备份密码至少需要 12 个字符，最多 1024 字节。"}, 400)
                from bundle_updater.backup_archive import seal_secret
                payload["secret"] = seal_secret(password, self.token, identifier + ":" + action)
                body.pop("password", None)
                password = None
            if action == "export":
                if not isinstance(body["destination"], str) or body["destination"] not in {"server", "download"}:
                    return response({"message": "请选择备份位置。"}, 400)
                payload["destination"] = body["destination"]
            elif action == "inspect":
                if not isinstance(body["upload_id"], str) or not ID.fullmatch(body["upload_id"]):
                    return response({"message": "请先上传备份文件。"}, 400)
                payload["upload_id"] = body["upload_id"]
            else:
                if (not isinstance(body["plan_id"], str) or not ID.fullmatch(body["plan_id"]) or body["confirm_source_stopped"] is not True
                        or type(body["overwrite"]) is not bool or body["plan_id"] != state.get("plan", {}).get("plan_id")
                        or state.get("plan", {}).get("target_has_data") and body["overwrite"] is not True):
                    return response({"message": "请先完成备份检查，并确认旧服务器停止和当前数据替换。"}, 400)
                payload.update(body)
            try:
                atomic_json(self.ipc / ("request-" + identifier + ".json"), sign(payload, self.token), 0o644)
            except (ValueError, OSError):
                return response({"message": "备份服务暂时无法接收请求。"}, 503)
            self.pending_until = time.monotonic() + 5
            self.pending_id = identifier
            self.pending_phase = {"export": "exporting", "inspect": "inspecting", "restore": "restoring"}[action]
            return response({"request_id": identifier, "active": True}, 202)
