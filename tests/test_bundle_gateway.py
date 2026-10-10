"""Real local HTTP/WS tests for native UI isolation and streaming transport."""
import asyncio
import gzip
from pathlib import Path
import unittest

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer
from yarl import URL

from bundle_gateway.server import MAX_UPLOAD, STATE, create_app, framed_headers


class BundleGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.loaded = True
        self.release = asyncio.Event()
        self.sent = asyncio.Event()
        self.upstreams = {}
        self.servers = []
        self.token = "test-token-" + "a" * 40

        def handler(name):
            async def serve(request):
                self.calls.append((name, request.path, dict(request.headers)))
                if request.path in {"/", "/webui/"}:
                    return web.Response(text="<!doctype html><html>Native " + name + "</html>", content_type="text/html")
                if request.path == "/api/healthz":
                    return web.json_response({"status": "ok", "agents_loaded": ["default"] if self.loaded else []})
                if request.path == "/compressed":
                    return web.Response(body=gzip.compress(b"native-compressed"), headers={"Content-Encoding": "gzip"})
                if request.path == "/redirect":
                    raise web.HTTPFound(self.upstreams[name] + "/target")
                if request.path == "/external":
                    raise web.HTTPFound(self.upstreams["napcat"] + "/leak")
                if request.path == "/cookie":
                    reply = web.Response(text="ok")
                    reply.set_cookie("session", name, httponly=True)
                    reply.set_cookie("other", "second")
                    return reply
                if request.path == "/csp":
                    return web.Response(text="page", headers={"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "script-src 'self'; frame-ancestors 'self'; object-src 'none'", "Access-Control-Allow-Origin": "*"})
                if request.path == "/stream":
                    reply = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                    await reply.prepare(request)
                    await reply.write(b"data: first\n\n")
                    self.sent.set()
                    await self.release.wait()
                    await reply.write(b"data: last\n\n")
                    return reply
                if request.path == "/ws":
                    ws = web.WebSocketResponse(protocols=["native-protocol"])
                    await ws.prepare(request)
                    async for message in ws:
                        if message.type == WSMsgType.TEXT:
                            await ws.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await ws.send_bytes(message.data)
                    return ws
                return web.json_response({"app": name, "path": request.raw_path, "body": (await request.read()).decode(), "cookie": request.headers.get("Cookie", ""), "runtime": request.headers.get("X-QwenPaw-Runtime-Token", ""), "hub_callback": request.headers.get("X-QwenPaw-Hub-OAuth-Callback-Url", ""), "host": request.host, "origin": request.headers.get("Origin", ""), "authorization": request.headers.get("Authorization", ""), "forwarded": request.headers.get("X-Forwarded-Host", "")})
            return serve

        for name in ("astrbot", "qwenpaw", "napcat"):
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", handler(name))
            server = TestServer(app)
            await server.start_server()
            self.servers.append(server)
            self.upstreams[name] = str(server.make_url("/")).rstrip("/")
        app = create_app(token=self.token, upstreams=self.upstreams)
        self.gateway = TestServer(app)
        await self.gateway.start_server()
        self.port = self.gateway.port
        app[STATE].update(port=self.port, portal=f"http://localhost:{self.port}")
        self.client = ClientSession()

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        await self.gateway.close()
        for server in self.servers:
            await server.close()

    def headers(self, name=None, **values):
        return {"Host": f"{name + '.' if name else ''}localhost:{self.port}", **values}

    async def test_portal_contains_only_native_navigation_and_no_token(self):
        async with self.client.get(self.gateway.make_url("/"), headers=self.headers()) as reply:
            text = await reply.text()
            self.assertEqual(reply.status, 200)
            self.assertIn("聊天与插件", text)
            self.assertNotIn(self.token, text)
            self.assertIn("frame-ancestors 'none'", reply.headers["Content-Security-Policy"])

    async def test_exact_hosts_routes_and_private_header_isolation(self):
        for name in self.upstreams:
            target = URL(str(self.gateway.make_url("/")) + "echo?x=a%2Fb", encoded=True)
            async with self.client.post(target, headers=self.headers(name, **{"X-QwenPaw-Runtime-Token": "browser-injected", "X-QwenPaw-Hub-OAuth-Callback-Url": "https://evil.example/callback", "X-Forwarded-Host": "evil.example", "Authorization": "Bearer native-test"}), data=b"upload-bytes") as reply:
                body = await reply.json()
                self.assertEqual(body["app"], name)
                self.assertEqual(body["runtime"], self.token if name == "qwenpaw" else "")
                self.assertEqual(body["body"], "upload-bytes")
                self.assertEqual(body["path"], "/echo?x=a%2Fb")
                self.assertEqual(body["authorization"], "Bearer native-test")
                self.assertEqual(body["hub_callback"], "")
                self.assertEqual(body["forwarded"], self.headers(name)["Host"])

    async def test_unknown_host_and_cross_site_calls_are_rejected(self):
        cases = [{"Host": "attacker.example"}, self.headers("qwenpaw", Origin="https://attacker.example"), self.headers("qwenpaw", **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "empty"})]
        for headers in cases:
            async with self.client.get(self.gateway.make_url("/echo"), headers=headers) as reply:
                self.assertEqual(reply.status, 403)
        self.assertEqual(self.calls, [])

    async def test_native_origin_and_iframe_navigation_are_accepted(self):
        native = f"http://qwenpaw.localhost:{self.port}"
        async with self.client.post(self.gateway.make_url("/echo"), headers=self.headers("qwenpaw", Origin=native), json={}) as reply:
            self.assertEqual((await reply.json())["origin"], native)
        async with self.client.get(self.gateway.make_url("/"), headers=self.headers("qwenpaw", **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "iframe"})) as reply:
            self.assertEqual(reply.status, 200)

    async def test_framing_preserves_other_csp_restrictions(self):
        async with self.client.get(self.gateway.make_url("/csp"), headers=self.headers("astrbot")) as reply:
            self.assertNotIn("X-Frame-Options", reply.headers)
            self.assertNotIn("Access-Control-Allow-Origin", reply.headers)
            csp = reply.headers["Content-Security-Policy"]
            self.assertIn("script-src 'self'", csp)
            self.assertIn("object-src 'none'", csp)
            self.assertIn(f"frame-ancestors 'self' http://localhost:{self.port}", csp)

    async def test_cookies_forward_but_are_never_shared_by_gateway(self):
        async with self.client.get(self.gateway.make_url("/cookie"), headers=self.headers("astrbot")) as reply:
            self.assertEqual(len(reply.headers.getall("Set-Cookie")), 2)
        async with self.client.get(self.gateway.make_url("/echo"), headers=self.headers("qwenpaw")) as reply:
            self.assertEqual((await reply.json())["cookie"], "")

    async def test_sse_is_forwarded_before_upstream_finishes(self):
        async with self.client.get(self.gateway.make_url("/stream"), headers=self.headers("qwenpaw")) as reply:
            chunk = await asyncio.wait_for(reply.content.readuntil(b"\n\n"), timeout=2)
            self.assertEqual(chunk, b"data: first\n\n")
            self.assertFalse(self.release.is_set())
            self.release.set()
            self.assertEqual(await reply.read(), b"data: last\n\n")

    async def test_websocket_protocol_text_binary_and_auth(self):
        async with self.client.ws_connect(self.gateway.make_url("/ws"), headers=self.headers("qwenpaw", Authorization="Bearer chrome-bridge-test"), protocols=["native-protocol"]) as ws:
            self.assertEqual(ws.protocol, "native-protocol")
            await ws.send_str("hello")
            self.assertEqual((await ws.receive(timeout=2)).data, "hello")
            await ws.send_bytes(b"\x00binary")
            self.assertEqual((await ws.receive(timeout=2)).data, b"\x00binary")
        call = next(call for call in self.calls if call[1] == "/ws")
        self.assertEqual(call[2]["X-QwenPaw-Runtime-Token"], self.token)
        self.assertEqual(call[2]["Authorization"], "Bearer chrome-bridge-test")

    async def test_redirects_do_not_leak_runtime_credentials(self):
        async with self.client.get(self.gateway.make_url("/redirect"), headers=self.headers("qwenpaw"), allow_redirects=False) as reply:
            self.assertEqual(reply.headers["Location"], f"http://qwenpaw.localhost:{self.port}/target")
        async with self.client.get(self.gateway.make_url("/external"), headers=self.headers("qwenpaw"), allow_redirects=False) as reply:
            self.assertEqual(reply.status, 302)
        async with self.client.get(self.gateway.make_url("/external"), headers=self.headers("qwenpaw", Upgrade="websocket")) as reply:
            self.assertEqual(reply.status, 502)
        self.assertFalse(any(path == "/leak" for _, path, _ in self.calls))

    async def test_compressed_native_resources_survive(self):
        async with self.client.get(self.gateway.make_url("/compressed"), headers=self.headers("napcat")) as reply:
            self.assertEqual(await reply.read(), b"native-compressed")

    async def test_health_requires_loaded_agent_and_returns_no_private_data(self):
        async with self.client.get(self.gateway.make_url("/bundle-health"), headers=self.headers()) as reply:
            self.assertEqual((await reply.json())["status"], "ready")
        self.loaded = False
        async with self.client.get(self.gateway.make_url("/bundle-health"), headers=self.headers()) as reply:
            body = await reply.json()
            self.assertEqual(body["status"], "starting")
            self.assertFalse(body["services"]["qwenpaw"]["ready"])
            self.assertNotIn(self.token, str(body))

    async def test_bundle_publishes_only_one_loopback_port_and_no_fixed_names(self):
        import yaml
        compose = yaml.safe_load((Path(__file__).parents[1] / "deploy/compose.bundle.yaml").read_text(encoding="utf-8"))
        ports = [port for service in compose["services"].values() for port in service.get("ports", [])]
        self.assertEqual(ports, ["127.0.0.1:18080:18080"])
        self.assertFalse(any("container_name" in service for service in compose["services"].values()))
        self.assertFalse(any("name" in network for network in compose["networks"].values()))


if __name__ == "__main__":
    unittest.main()
