"""Verify a real MCP subprocess: handshake, discovery and all six read-only tools."""

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(root: Path, registry: Path | None = None) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    args = ["-m", "devflow_core.cli", "--root", str(root)]
    if registry:
        args += ["--registry", str(registry)]
    args += ["serve"]
    server = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "DevFlow ServerMaster"
            listed = await session.list_tools()
            expected = {"servermaster_ping", "registry_projects", "registry_servers",
                        "registry_validate", "project_status", "continuity_status"}
            assert {tool.name for tool in listed.tools} == expected
            for tool in listed.tools:
                assert tool.annotations.readOnlyHint is True
                response = await session.call_tool(tool.name, {})
                assert not response.isError, tool.name
                body = json.loads(response.content[0].text)
                if tool.name == "servermaster_ping":
                    assert body["read_only"] and body["transport"] == "stdio"
                if tool.name == "registry_validate":
                    assert body["ok"] and body["projects"] >= 1
            denied = await session.call_tool("shell_exec", {"command": "id"})
            assert denied.isError
    print("PASS: MCP initialize, 6 read-only tools, calls, unknown-tool rejection and shutdown")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    options = parser.parse_args()
    asyncio.run(asyncio.wait_for(check(options.root.resolve(), options.registry), timeout=45))
