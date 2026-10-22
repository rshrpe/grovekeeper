"""WORKTREE_KNOWLEDGE_DEBUG, --version, the MCP startup line and the upstream timeout."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from graphify_worktree import cli, nudge, recall, upstream
from graphify_worktree.errors import GraphError
from graphify_worktree.mcp_server import UpstreamLoop

from integration import isolated_env

BIN = Path(sys.executable).parent


def broken(*args, **kwargs):
    raise RuntimeError("deliberate failure for the test")


@pytest.mark.parametrize("debug", [False, True])
def test_recall_and_nudge_print_a_swallowed_failure_only_in_debug(repo, monkeypatch, capsys, debug):
    if debug:
        monkeypatch.setenv("WORKTREE_KNOWLEDGE_DEBUG", "1")
    else:
        monkeypatch.delenv("WORKTREE_KNOWLEDGE_DEBUG", raising=False)
    monkeypatch.setattr(recall, "resolve_root", broken)
    monkeypatch.setattr(nudge, "_respond", broken)
    assert cli.run_recall(str(repo)) == 0
    assert nudge.respond({"tool_name": "Grep"}) == ""
    monkeypatch.setattr(recall, "session_context", broken)
    assert cli.run_recall(str(repo)) == 0
    out, err = capsys.readouterr()
    assert out == ""
    if debug:
        assert err.count("graphify-worktree debug:") == 3 and err.count("Traceback") == 3
        assert "RuntimeError: deliberate failure for the test" in err
    else:
        assert err == ""


def test_upstream_handler_timeout_withholds_the_result():
    loop = UpstreamLoop()
    with pytest.raises(GraphError, match="did not finish within"):
        loop.run(asyncio.sleep(5), timeout=0.05)
    assert loop.run(asyncio.sleep(0, result="next call runs"), timeout=5) == "next call runs"


def test_version_line_names_the_package_and_both_pins():
    line = upstream.version_line("graphify-worktree")
    assert line.startswith("graphify-worktree (grovekeeper ")
    assert "graphifyy 0.9.93 (pinned 0.9.93)" in line and "mcp 2.3.0 (pinned 2.3.0)" in line


@pytest.mark.integration
@pytest.mark.parametrize("executable", ["graphify-worktree", "graphify-worktree-mcp"])
def test_both_executables_print_their_version(tmp_path, executable):
    result = subprocess.run([str(BIN / executable), "--version"], env=isolated_env(tmp_path),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.startswith(executable + " (grovekeeper ")


async def failing_query(env, errlog, root):
    params = StdioServerParameters(command=str(BIN / "graphify-worktree-mcp"), env=env)
    async with stdio_client(params, errlog=errlog) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool("graph_stats", {"project_path": str(root)})
            return json.loads(result.content[0].text)


@pytest.mark.integration
@pytest.mark.repo(mode="graph")
@pytest.mark.parametrize("debug", [False, True])
def test_mcp_startup_line_and_debug_cause_of_graph_unavailable(repo, tmp_path, debug):
    env = isolated_env(tmp_path / "home", **({"WORKTREE_KNOWLEDGE_DEBUG": "1"} if debug else {}))
    log = tmp_path / "server.log"
    with log.open("w") as errlog:
        payload = asyncio.run(failing_query(env, errlog, repo))
    assert payload["error"] == "graph_unavailable" and "no supported code files" in payload["reason"]
    lines = log.read_text().splitlines()
    assert lines[0].startswith("graphify-worktree-mcp (grovekeeper ") and "memory vault" in lines[0]
    assert ("Traceback" in log.read_text()) is debug
