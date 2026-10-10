"""Loopback-only bundle gateway. Never expose this local entry on a public IP."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TraceConfig, WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

APPS = {"astrbot": "http://astrbot:6185", "qwenpaw": "http://qwenpaw:8088", "napcat": "http://napcat:6099"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
MAX_UPLOAD = 128 * 1024 * 1024
STATIC = Path(__file__).parent / "static"
STATE = web.AppKey("gateway", dict)


def clean_headers(headers, *, response=False):
    excluded = HOP | {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    excluded |= {"host", "x-qwenpaw-runtime-token", "forwarded"}
    result = CIMultiDict()
    for name, value in headers.items():
        lower = name.lower()
        if lower in excluded or lower.startswith(("x-forwarded-", "x-qwenpaw-hub-")):
            continue
        if response and (lower.startswith("access-control-") or lower == "x-frame-options"):
            continue
        result.add(name, value)
    return result


def framed_headers(headers, portal):
    result = clean_headers(headers, response=True)
    policies = result.getall("Content-Security-Policy", [])
    if policies:
        result.popall("Content-Security-Policy")
        for policy in policies:
            # Keep script/object/base restrictions; only permit the local parent.
            ancestor = "frame-ancestors 'self' " + portal
            if re.search(r"(?:^|;)\s*frame-ancestors\b", policy, re.I):
                policy = re.sub(r"(^|;)\s*frame-ancestors\b[^;]*", lambda match: match[1] + " " + ancestor, policy, flags=re.I)
            else:
                policy = policy.rstrip("; ") + "; " + ancestor
            result.add("Content-Security-Policy", policy)
    else:
        result["Content-Security-Policy"] = "frame-ancestors 'self' " + portal
    return result


def resolve_app(request, port):
    authority = request.headers.get("Host", "").lower()
    allowed = {f"localhost:{port}": None, f"127.0.0.1:{port}": None}
    allowed.update({f"{name}.localhost:{port}": name for name in APPS})
    if authority not in allowed:
        raise web.HTTPForbidden(text="Unrecognized local bundle host.")
    origin = request.headers.get("Origin", "")
    if origin and origin != "http://" + authority:
        raise web.HTTPForbidden(text="Cross-origin requests are not accepted.")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        if request.method not in {"GET", "HEAD"} or request.headers.get("Sec-Fetch-Dest") not in {"iframe", "document"}:
            raise web.HTTPForbidden(text="Cross-site requests are not accepted.")
    if not request.raw_path.startswith("/") or request.raw_path.startswith("//"):
        raise web.HTTPBadRequest(text="Invalid request path.")
    return allowed[authority]


async def health(state):
    async def check(name, upstream):
        paths = ["/webui/"] if name == "napcat" else ["/"]
        if name == "qwenpaw":
            paths.append("/api/healthz")
        headers = {"Accept-Encoding": "identity"}
        if name == "qwenpaw":
            headers["X-QwenPaw-Runtime-Token"] = state["token"]
        try:
            for path in paths:
                async with state["session"].get(upstream + path, headers=headers, timeout=ClientTimeout(total=3), allow_redirects=False) as reply:
                    if reply.status != 200:
                        return {"ready": False}
                    if path == "/api/healthz":
                        # Bound this diagnostic response; never return raw data.
                        raw = await reply.content.read(65537)
                        if len(raw) > 65536:
                            return {"ready": False}
                        import json
                        body = json.loads(raw)
                        if body.get("status") != "ok" or state["agent"] not in body.get("agents_loaded", []):
                            return {"ready": False}
                    else:
                        sample = await reply.content.read(4096)
                        if b"<html" not in sample.lower() and b"<!doctype html" not in sample.lower():
                            return {"ready": False}
            return {"ready": True}
        except Exception:
            return {"ready": False}
    values = await asyncio.gather(*(check(name, upstream) for name, upstream in state["upstreams"].items()))
    services = dict(zip(state["upstreams"], values))
    return {"status": "ready" if all(item["ready"] for item in values) else "starting", "services": services}


async def relay_websocket(request, state, target, headers):
    # aiohttp constructs its own handshake; don't forward a browser's key.
    for name in list(headers):
        if name.lower().startswith("sec-websocket-"):
            headers.popall(name, None)
    protocols = [item.strip() for item in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if item.strip()]
    try:
        upstream = await state["session"].ws_connect(target, headers=headers, protocols=protocols, max_msg_size=32 * 1024 * 1024, autoclose=False, autoping=True)
    except Exception:
        return web.json_response({"error": "native_websocket_unavailable"}, status=502)
    async with upstream:
        downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else [], max_msg_size=32 * 1024 * 1024, autoclose=False, autoping=True)
        await downstream.prepare(request)

        async def copy(source, destination):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await destination.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await destination.send_bytes(message.data)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        tasks = [asyncio.create_task(copy(downstream, upstream)), asyncio.create_task(copy(upstream, downstream))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await downstream.close()
            await upstream.close()
        return downstream


async def proxy(request, state, app_name):
    if request.content_length is not None and request.content_length > MAX_UPLOAD:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=request.content_length)
    upstream = state["upstreams"][app_name]
    target = URL(upstream + request.raw_path, encoded=True)
    headers = clean_headers(request.headers)
    # Preserve the public native origin for generated URLs and Origin checks.
    headers["Host"] = request.headers["Host"]
    headers["X-Forwarded-Host"] = request.headers["Host"]
    headers["X-Forwarded-Proto"] = "http"
    if app_name == "qwenpaw":
        headers["X-QwenPaw-Runtime-Token"] = state["token"]
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await relay_websocket(request, state, target, headers)

    async def upload():
        size = 0
        async for chunk in request.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=size)
            yield chunk

    response = None
    try:
        async with state["session"].request(request.method, target, headers=headers, data=upload() if request.can_read_body else None, allow_redirects=False) as reply:
            output_headers = framed_headers(reply.headers, state["portal"])
            location = output_headers.get("Location")
            if location and (location == upstream or location.startswith(upstream + "/")):
                output_headers["Location"] = "http://" + request.headers["Host"] + location[len(upstream):]
            response = web.StreamResponse(status=reply.status, headers=output_headers)
            await response.prepare(request)
            async for chunk in reply.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
            return response
    except (asyncio.CancelledError, ConnectionResetError):
        raise
    except Exception:
        if response is not None and response.prepared:
            # Never append a JSON error to an already-started SSE/download.
            if request.transport:
                request.transport.close()
            return response
        return web.json_response({"error": "native_application_unavailable"}, status=502)


async def dispatch(request):
    state = request.app[STATE]
    app_name = resolve_app(request, state["port"])
    if app_name:
        return await proxy(request, state, app_name)
    if request.method not in {"GET", "HEAD"}:
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
    if request.path == "/bundle-health":
        return web.json_response(await health(state), headers={"Cache-Control": "no-store"})
    if request.host.startswith("127.0.0.1:"):
        raise web.HTTPFound(state["portal"] + request.rel_url.path_qs)
    assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
    asset = assets.get(request.path)
    if not asset:
        raise web.HTTPNotFound()
    response = web.FileResponse(STATIC / asset)
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-src " + " ".join(f"http://{name}.localhost:{state['port']}" for name in APPS) + "; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = f'microphone=(self "http://astrbot.localhost:{state["port"]}" "http://qwenpaw.localhost:{state["port"]}"), publickey-credentials-get=(self "http://napcat.localhost:{state["port"]}"), publickey-credentials-create=(self "http://napcat.localhost:{state["port"]}")'
    return response


def create_app(*, token, port=18080, upstreams=None, agent="default"):
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token or ""):
        raise ValueError("A private runtime token is required.")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Choose a valid local bundle port.")
    targets = dict(upstreams or APPS)
    if set(targets) != set(APPS):
        raise ValueError("Three native applications are required.")
    for target in targets.values():
        parsed = urlsplit(target)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Invalid native application endpoint.")
    state = {"token": token, "port": port, "portal": f"http://localhost:{port}", "upstreams": targets, "agent": agent}
    app = web.Application(client_max_size=MAX_UPLOAD)
    app[STATE] = state

    async def session_context(app):
        trace = TraceConfig()
        async def reject_redirect(*args):
            # ws_connect follows redirects internally; never leak private headers.
            raise ValueError("Native websocket redirects are not accepted.")
        trace.on_request_redirect.append(reject_redirect)
        async with ClientSession(cookie_jar=DummyCookieJar(), trust_env=False, auto_decompress=False, trace_configs=[trace], timeout=ClientTimeout(total=None, sock_connect=5, sock_read=None)) as session:
            state["session"] = session
            yield
    app.cleanup_ctx.append(session_context)
    app.router.add_route("*", "/{tail:.*}", dispatch)
    return app


def main():
    port = int(os.environ.get("BUNDLE_PORT", "18080"))
    app = create_app(token=os.environ.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", ""), port=port, agent=os.environ.get("QWENPAW_AGENT_ID", "default"))
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
