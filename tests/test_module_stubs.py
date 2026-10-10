"""Regression for lazy dependency identity across framework stub imports."""

from pathlib import Path
import subprocess
import sys
import unittest


class ModuleIsolationTests(unittest.TestCase):
    def test_cold_aiohttp_import_still_builds_real_test_server(self):
        # A subprocess guarantees aiohttp.web is initially cold, regardless of
        # unittest discovery order. This recreates the full-suite failure.
        code = """
import asyncio
import sys
import types
from module_stubs import module_overrides

name = '_bridge_test_runtime_stub'
original = types.ModuleType(name)
replacement = types.ModuleType(name)
sys.modules[name] = original
with module_overrides({name: replacement, name + '.absent': replacement}):
    from aiohttp import web
    assert sys.modules[name] is replacement
assert sys.modules[name] is original
assert name + '.absent' not in sys.modules

from aiohttp.test_utils import TestServer
async def check():
    server = TestServer(web.Application())
    await server.start_server()
    await server.close()
asyncio.run(check())
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
