"""The MCP tool surface agents see must not drift during refactors.

tests/fixtures/mcp_tools.json was captured from the pre-package live server
(and matched the packaged server byte for byte) on 2026-10-21.
"""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from integration import isolated_env

FIXTURE = Path(__file__).parent / "fixtures/mcp_tools.json"


async def list_tools(env):
    command = Path(sys.executable).parent / "graphify-worktree-mcp"
    async with stdio_client(StdioServerParameters(command=str(command), env=env)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            resources = (await session.list_resources()).resources
            return [t.model_dump(mode="json", exclude_none=True) for t in tools], resources


@pytest.mark.integration
def test_tool_listing_matches_captured_surface(tmp_path):
    tools, resources = asyncio.run(list_tools(isolated_env(tmp_path)))
    assert json.loads(json.dumps(tools, sort_keys=True)) == json.loads(FIXTURE.read_text())
    assert resources == []


async def call(env, name, arguments):
    command = Path(sys.executable).parent / "graphify-worktree-mcp"
    async with stdio_client(StdioServerParameters(command=str(command), env=env)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            return result.is_error, json.loads(result.content[0].text)


@pytest.mark.integration
@pytest.mark.parametrize("name", ["memory_bogus", "memory_", "learning_bogus", "bogus"])
def test_a_name_the_server_does_not_offer_is_unknown_tool(tmp_path, name):
    is_error, payload = asyncio.run(call(isolated_env(tmp_path), name, {"project_path": str(tmp_path)}))
    assert is_error and payload == {"error": "unknown_tool", "reason": f"Unknown tool: {name}"}


@pytest.mark.integration
def test_an_offered_memory_tool_still_reaches_its_own_validation(tmp_path):
    is_error, payload = asyncio.run(call(isolated_env(tmp_path), "memory_search", {"project_path": "relative"}))
    assert is_error and payload["error"] == "invalid_memory_request"
