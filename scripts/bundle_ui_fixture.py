"""Local native-page substitutes for browser checks, never production accounts."""
import asyncio
import json
from pathlib import Path
import sys

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle_gateway.server import STATE, create_app


async def main():
    runners = []
    upstreams = {}
    def handler(name):
        async def serve(request):
            if request.path == "/api/healthz":
                return web.json_response({"status": "ok", "agents_loaded": ["default"]})
            if request.path == "/api/check":
                return web.json_response({"app": name, "posted": await request.text(), "runtime_header": bool(request.headers.get("X-QwenPaw-Runtime-Token"))})
            if request.path == "/download":
                return web.Response(body=b"test-only file", headers={"Content-Disposition": 'attachment; filename="test.txt"'})
            html = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Test native ' + name + '</title></head><body><h1>原生页面测试替身：' + name + '</h1><p>这是自动化测试内容，并非实际产品后台。</p><label>保留输入 <input id="draft"></label><button id="call">请求自身 API</button><output id="result"></output><a href="/download" download>下载测试文件</a><script>localStorage.setItem("native-session", "' + name + '"); document.querySelector("#call").onclick=async()=>{const r=await fetch("/api/check",{method:"POST",headers:{"Content-Type":"text/plain"},body:"native-post"});document.querySelector("#result").textContent=(await r.json()).app;};</script></body></html>'
            return web.Response(text=html, content_type="text/html", headers={"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "script-src 'self' 'unsafe-inline'; frame-ancestors 'self'; object-src 'none'"})
        return serve
    try:
        for name in ("astrbot", "qwenpaw", "napcat"):
            app = web.Application()
            app.router.add_route("*", "/{path:.*}", handler(name))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            runners.append(runner)
            upstreams[name] = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        app = create_app(token="browser-fixture-" + "a" * 32, upstreams=upstreams)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        app[STATE].update(port=port, portal=f"http://localhost:{port}")
        print(json.dumps({"port": port}), flush=True)
        await asyncio.Event().wait()
    finally:
        for runner in reversed(runners):
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
