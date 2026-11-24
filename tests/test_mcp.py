"""Exercise the actual MCP transport without a desktop or Codex process."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent


class MCPTests(unittest.TestCase):
    def test_registered_tools_and_disconnected_state(self):
        checks = r"""
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import json
import sys

async def main():
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            status = await client.call_tool("desktop_status", {})
            denied = await client.call_tool("desktop_screenshot", {"target": "screen"})
            status_tool = next(tool for tool in tools.tools if tool.name == "desktop_status")
            print(json.dumps({"tools": [tool.name for tool in tools.tools],
                              "status_is_read_only": status_tool.annotations.readOnlyHint,
                              "status": status.structuredContent,
                              "screenshot_blocked": denied.isError}))

anyio.run(main)
"""
        result = subprocess.run([sys.executable, "-c", checks], cwd=ROOT,
                                env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                                capture_output=True, text=True, check=True, timeout=10)
        data = json.loads(result.stdout)
        self.assertEqual(set(data["tools"]),
                         {"desktop_status", "desktop_inspect", "desktop_screenshot",
                          "desktop_action", "codex_usage_status"})
        self.assertEqual(data["status"], {"state": "disconnected", "available": False})
        self.assertTrue(data["status_is_read_only"])
        self.assertTrue(data["screenshot_blocked"])
