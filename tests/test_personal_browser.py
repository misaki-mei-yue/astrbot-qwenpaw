"""Structured browser checks. Real Chromium uses opt-in offline fixtures only."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import os
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from module_stubs import module_overrides
from qwenpaw_plugin_astrbot_bridge import browser
from qwenpaw_plugin_astrbot_bridge.bridge_client import BridgeError

PERSON = "abp_" + "1" * 32
OTHER = "abp_" + "2" * 32
GROUP = "abg_" + "3" * 32


class FixtureURLPolicy:
    def __init__(self, origin="https://example.test"):
        self.origin = origin

    async def permits(self, url):
        if not browser._http_url(url):
            return False
        parsed = urlsplit(url)
        return parsed.scheme + "://" + parsed.netloc == self.origin


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def inner_text(self, timeout=None):
        return self.page.text

    async def get_attribute(self, name):
        return "file" if self.selector == "css=#upload" and name == "type" else None

    async def click(self):
        self.page.text = "clicked " + self.selector

    async def fill(self, text):
        self.page.text = text


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.text = "fixture"
        self.closed = False
        self.handlers = {}
        self.main_frame = SimpleNamespace(url=self.url)

    def is_closed(self): return self.closed
    def set_default_timeout(self, value): self.timeout = value
    def on(self, name, fn): self.handlers[name] = fn
    async def close(self): self.closed = True
    async def title(self): return "Fixture title"
    def locator(self, selector): return FakeLocator(self, selector)

    async def goto(self, url, **kwargs):
        self.url = url
        self.main_frame.url = url
        self.handlers["framenavigated"](self.main_frame)


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.handlers = {}
        self.cookies = {}
        self.closed = False

    async def route(self, pattern, callback): self.http_route = callback
    async def route_web_socket(self, pattern, callback): self.ws_route = callback
    async def new_page(self): return self.page
    def on(self, name, callback): self.handlers[name] = callback
    async def close(self): self.closed = True


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.contexts = []
        async def factory():
            context = FakeContext()
            self.contexts.append(context)
            return context
        self.factory = factory
        self.controller = browser.BrowserController(factory, FixtureURLPolicy())

    async def asyncTearDown(self):
        await self.controller.close()

    async def test_contexts_pages_and_cookies_are_per_managed_agent(self):
        await self.controller.execute(PERSON, "open", "https://example.test/one")
        await self.controller.execute(OTHER, "open", "https://example.test/two")
        self.contexts[0].cookies["private"] = "one"
        self.assertEqual(self.contexts[1].cookies, {})
        one = await self.controller.execute(PERSON, "status")
        two = await self.controller.execute(OTHER, "status")
        self.assertEqual(one["url"], "https://example.test/one")
        self.assertEqual(two["url"], "https://example.test/two")
        await self.controller.execute(PERSON, "close")
        self.assertTrue(self.contexts[0].closed)
        self.assertFalse(self.contexts[1].closed)

    async def test_literal_fill_css_click_read_and_bounded_output(self):
        await self.controller.execute(PERSON, "open", "https://example.test")
        literal = "__import__('os').system('not executed'); <script>literal</script>"
        result = await self.controller.execute(PERSON, "fill", selector="#input", text=literal)
        self.assertEqual(result["text"], literal)
        result = await self.controller.execute(PERSON, "click", selector="#button")
        self.assertEqual(result["text"], "clicked css=#button")
        self.contexts[0].page.text = "x" * (browser.MAX_TEXT + 100)
        result = await self.controller.execute(PERSON, "read")
        self.assertEqual(len(result["text"]), browser.MAX_TEXT)
        self.assertTrue(result["truncated"])

    async def test_upload_and_model_code_parameters_are_not_available(self):
        await self.controller.execute(PERSON, "open", "https://example.test")
        with self.assertRaises(BridgeError):
            await self.controller.execute(PERSON, "click", selector="#upload")
        for action in ("evaluate", "python", "upload", "screenshot", "download", ["open"]):
            with self.subTest(action=action), self.assertRaises(BridgeError):
                await self.controller.execute(PERSON, action)
        self.assertEqual(list(inspect.signature(browser.astrbot_browser).parameters),
                         ["action", "url", "selector", "text"])
        with self.assertRaises(TypeError):
            await browser.astrbot_browser("read", agent_id=OTHER)

    async def test_non_http_and_private_destinations_fail_before_context_creation(self):
        for url in ("file:///etc/passwd", "data:text/html,secret", "javascript:alert(1)",
                    "ftp://example.test/a", "https://user:pass@example.test", "http://127.0.0.1:8088",
                    "https://other.test", "https://example.test/\\local", "https://example.test/ bad"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                await self.controller.execute(PERSON, "open", url)
        self.assertEqual(self.contexts, [])

    async def test_request_redirect_and_websocket_guards(self):
        await self.controller.execute(PERSON, "open", "https://example.test")
        route = SimpleNamespace(request=SimpleNamespace(url="http://127.0.0.1/private"),
                                continue_=AsyncMock(), abort=AsyncMock())
        await self.contexts[0].http_route(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
        route.request.url = "https://example.test/allowed"
        await self.contexts[0].http_route(route)
        route.continue_.assert_awaited_once()
        socket = SimpleNamespace(close=AsyncMock())
        await self.contexts[0].ws_route(socket)
        socket.close.assert_awaited_once()

    async def test_local_document_navigation_closes_page_and_never_reads_dom(self):
        await self.controller.execute(PERSON, "open", "https://example.test")
        page = self.contexts[0].page
        page.url = page.main_frame.url = "file:///private/memory.md"
        page.handlers["framenavigated"](page.main_frame)
        await asyncio.sleep(0)
        self.assertTrue(page.closed)
        with self.assertRaises(BridgeError):
            await self.controller.execute(PERSON, "read")

    async def test_navigation_during_dom_read_does_not_release_text(self):
        await self.controller.execute(PERSON, "open", "https://example.test")
        page = self.contexts[0].page
        async def changed(locator, timeout=None):
            page.url = "file:///private/memory.md"
            return "private data must not be returned"
        with patch.object(FakeLocator, "inner_text", new=changed):
            with self.assertRaises(BridgeError):
                await self.controller.execute(PERSON, "read")

    async def test_popups_downloads_and_file_choosers_are_closed_or_cancelled(self):
        await self.controller.execute(PERSON, "open", "https://example.test")
        context = self.contexts[0]
        popup = SimpleNamespace(close=AsyncMock())
        download = SimpleNamespace(cancel=AsyncMock())
        chooser = SimpleNamespace(set_files=AsyncMock())
        context.handlers["page"](popup)
        context.page.handlers["download"](download)
        context.page.handlers["filechooser"](chooser)
        await asyncio.sleep(0)
        popup.close.assert_awaited_once()
        download.cancel.assert_awaited_once()
        chooser.set_files.assert_awaited_once_with([])

    async def test_capacity_does_not_close_another_person_browser(self):
        self.controller._max_contexts = 1
        await self.controller.execute(PERSON, "open", "https://example.test")
        with self.assertRaisesRegex(BridgeError, "capacity"):
            await self.controller.execute(OTHER, "open", "https://example.test")
        self.assertFalse(self.contexts[0].closed)

    async def test_invalid_agent_and_missing_page_fail_closed(self):
        for agent in ("default", "", None, "abp_" + "A" * 32):
            with self.assertRaises(BridgeError):
                await self.controller.execute(agent, "status")
        with self.assertRaises(BridgeError):
            await self.controller.execute(PERSON, "read")
        self.assertFalse((await self.controller.execute(PERSON, "status"))["open"])

    async def test_public_policy_rejects_private_mixed_dns_and_resolution_failure(self):
        for addresses in (["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["::1"],
                          ["224.0.0.1"], ["8.8.8.8", "192.168.1.1"], []):
            policy = browser.PublicURLPolicy(AsyncMock(return_value=addresses))
            self.assertFalse(await policy.permits("https://example.test"))
        policy = browser.PublicURLPolicy(AsyncMock(return_value=["8.8.8.8"]))
        self.assertTrue(await policy.permits("https://example.test"))
        self.assertFalse(await policy.permits("http://127.0.0.1"))
        self.assertFalse(await policy.permits("http://@example.test"))
        policy = browser.PublicURLPolicy(AsyncMock(side_effect=OSError("private detail")))
        self.assertFalse(await policy.permits("https://example.test"))

    async def test_tool_uses_native_managed_context_including_cron_without_nonce(self):
        context = SimpleNamespace(agent=PERSON, channel="astrbot")
        module = types.ModuleType("qwenpaw.app.agent_context")
        module.peek_current_agent_id = lambda: context.agent
        module.get_current_agent_id = lambda: context.agent or "default"
        module.get_current_channel = lambda: context.channel
        with module_overrides({"qwenpaw.app.agent_context": module}), patch.object(browser, "_controller", self.controller):
            await browser.astrbot_browser("open", "https://example.test")
            context.agent = GROUP
            self.assertFalse((await browser.astrbot_browser("status"))["open"])
            context.agent = None
            with self.assertRaises(BridgeError):
                await browser.astrbot_browser("status")
            context.agent, context.channel = PERSON, "console"
            with self.assertRaises(BridgeError):
                await browser.astrbot_browser("read")


class FixtureServer(BaseHTTPRequestHandler):
    private_requests = 0

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2:" + str(self.server.server_port) + "/private")
            self.end_headers()
            return
        if self.path.startswith("/private"):
            type(self).private_requests += 1
        body = b"""<html><head><title>Managed fixture</title></head><body>
        <h1>Offline isolated browser</h1><input id='name'><button id='save'
        onclick="document.cookie='fixture='+document.getElementById('name').value; document.getElementById('value').textContent=document.cookie">Save</button>
        <button id='show' onclick="document.getElementById('value').textContent=document.cookie || 'empty'">Show</button>
        <p id='value'>empty</p><input id='upload' type='file'>
        </body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("ASTRBOT_BROWSER_REAL_TESTS") == "1",
                     "Opt-in real Chromium offline fixture only")
class RealBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FixtureServer.private_requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureServer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.controller = browser.BrowserController(url_policy=FixtureURLPolicy(self.origin))

    async def asyncTearDown(self):
        await self.controller.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=3)

    async def test_real_read_click_fill_and_cookie_isolation(self):
        opened = await self.controller.execute(PERSON, "open", self.origin)
        self.assertIn("Offline isolated browser", opened["text"])
        await self.controller.execute(PERSON, "fill", selector="#name", text="person-one")
        changed = await self.controller.execute(PERSON, "click", selector="#save")
        self.assertIn("fixture=person-one", changed["text"])
        await self.controller.execute(OTHER, "open", self.origin)
        other = await self.controller.execute(OTHER, "click", selector="#show")
        self.assertNotIn("person-one", other["text"])
        one = await self.controller.execute(PERSON, "click", selector="#show")
        self.assertIn("person-one", one["text"])
        with self.assertRaises(BridgeError):
            await self.controller.execute(PERSON, "click", selector="#upload")
        await self.controller.execute(PERSON, "close")
        self.assertTrue((await self.controller.execute(OTHER, "status"))["open"])

    async def test_real_local_url_and_disallowed_redirect_never_reach_destination(self):
        for url in ("file:///etc/passwd", "data:text/html,private", "http://127.0.0.2/private"):
            with self.assertRaises(BridgeError):
                await self.controller.execute(PERSON, "open", url)
        with self.assertRaises(BridgeError):
            await self.controller.execute(PERSON, "open", self.origin + "/redirect")
        self.assertEqual(FixtureServer.private_requests, 0)


if __name__ == "__main__":
    unittest.main()
