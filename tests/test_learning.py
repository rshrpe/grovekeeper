"""Upstream work memory (save-result, reflect) behind the graph consent gate."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify_worktree import learning, policy
from graphify_worktree.errors import LearningRequestError, PolicyError
from graphify_worktree.guard import ensure

from integration import isolated_env

pytestmark = pytest.mark.repo(files={"pkg/core.py": "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"})

APPROVED = {"confirmed": True, "confirmation": "user approved recording this outcome"}
RESULT = {"question": "where is helper defined", "answer": "pkg/core.py", "nodes": ["helper()"],
          "outcome": "useful", **APPROVED}


def files(directory):
    return {path.relative_to(directory).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*")) if path.is_file()}


# Validation and consent, before any graph work ---------------------------------

@pytest.mark.parametrize("change, message", [
    ({"confirmed": False}, "explicit user approval"),
    ({"confirmation": " "}, "confirmation"),
    ({"outcome": "great"}, "outcome must be one of: useful, dead_end, corrected"),
    ({"type": "guess"}, "type must be one of: query, path_query, explain"),
    ({"nodes": []}, "nodes must be a list of 1 to 10 strings"),
    ({"nodes": ["n"] * 11}, "nodes must be a list of 1 to 10 strings"),
    ({"nodes": ["ok", ""]}, "nodes must be a nonempty string"),
    ({"extra": 1}, "unknown fields: extra"),
])
def test_invalid_save_requests_are_refused_before_any_graph_work(repo, change, message):
    policy.configure(repo, "graph")
    with pytest.raises(LearningRequestError, match=message):
        learning.handle("save_result", repo, {**RESULT, **change})
    assert not (repo / "graphify-out").exists()


def test_reflect_requires_approval(repo):
    policy.configure(repo, "graph")
    with pytest.raises(LearningRequestError, match="missing fields: confirmation, confirmed"):
        learning.handle("reflect", repo, {})


@pytest.mark.parametrize("mode", [None, "off"])
def test_denied_without_graph_consent_and_nothing_is_created(repo, mode):
    if mode:
        policy.configure(repo, mode)
    with pytest.raises(PolicyError):
        learning.handle("save_result", repo, RESULT)
    assert not (repo / "graphify-out").exists()


def test_session_off_wins_over_allow_once(repo, monkeypatch):
    policy.configure(repo, "graph")
    monkeypatch.setenv("HARNESS_KNOWLEDGE_MODE", "off")
    with pytest.raises(PolicyError):
        learning.handle("save_result", repo, RESULT, once=True)
    with pytest.raises(PolicyError):
        learning.handle("reflect", repo, APPROVED, once=True, session_mode="default")


# Writes (real extraction) --------------------------------------------------------

@pytest.mark.integration
def test_save_result_records_the_result_and_reflects_it_into_the_overlay(repo):
    policy.configure(repo, "graph")
    result = learning.handle("save_result", repo, RESULT)
    output = repo / "graphify-out"
    saved = Path(result["saved"])
    assert saved.parent == output / "memory" and "where is helper defined" in saved.read_text()
    assert result["reflection"]["state"] == "reflected"
    assert Path(result["reflection"]["lessons"]) == output / "reflections/LESSONS.md"
    overlay = json.loads((output / ".graphify_learning.json").read_text())
    assert overlay["nodes"]["pkg_core_helper"]["status"] == "tentative"
    assert result["graph"]["rebuilt"] is True and result["graph"]["state"] == "fresh"


@pytest.mark.integration
def test_one_off_permission_allows_a_single_write_without_enrolling(repo):
    learning.handle("save_result", repo, RESULT, once=True)
    assert policy.get_policy(repo)["saved_mode"] is None


@pytest.mark.integration
def test_reflect_skips_when_lessons_are_current_and_reruns_after_a_new_result(repo):
    policy.configure(repo, "graph")
    learning.handle("save_result", repo, RESULT)
    assert learning.handle("reflect", repo, APPROVED)["reflection"]["state"] == "fresh"
    learning.handle("save_result", repo, {**RESULT, "question": "who calls helper"})
    (repo / "graphify-out/.graphify_learning.json").unlink()
    assert learning.handle("reflect", repo, APPROVED)["reflection"]["state"] == "reflected"
    assert (repo / "graphify-out/.graphify_learning.json").exists()


@pytest.mark.integration
def test_reflect_bumps_graph_mtime_without_making_the_graph_stale(repo):
    policy.configure(repo, "graph")
    before = ensure(repo)
    graph = repo / "graphify-out/graph.json"
    old = graph.stat().st_mtime_ns
    learning.handle("save_result", repo, RESULT)
    assert graph.stat().st_mtime_ns > old
    after = ensure(repo)
    assert (after["rebuilt"], after["generation"]) == (False, before["generation"])


@pytest.mark.integration
def test_writes_refuse_symlinked_work_memory(repo, tmp_path):
    policy.configure(repo, "graph")
    ensure(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "graphify-out/memory").symlink_to(elsewhere)
    with pytest.raises(Exception, match="symlink"):
        learning.handle("save_result", repo, RESULT)
    assert list(elsewhere.iterdir()) == []


def test_reflection_refuses_symlinked_individual_notes(tmp_path):
    from graphify_worktree.errors import GraphError

    output = tmp_path / "graphify-out"
    memory = output / "memory"
    memory.mkdir(parents=True)
    private = tmp_path / "private.md"
    private.write_text('---\ntype: "query"\noutcome: "dead_end"\nquestion: "private"\n---\n')
    (memory / "linked.md").symlink_to(private)
    with pytest.raises(GraphError, match="symlink"):
        learning._paths(output)
    assert not (output / "reflections").exists()


@pytest.mark.integration
def test_publish_leaves_work_memory_reflections_and_overlay_untouched(repo):
    policy.configure(repo, "graph")
    learning.handle("save_result", repo, RESULT)
    output = repo / "graphify-out"
    kept = {name: files(output / name) for name in ("memory", "reflections")}
    overlay = output / ".graphify_learning.json"
    overlay_state = (overlay.read_bytes(), overlay.stat().st_mtime_ns)
    generation = ensure(repo)["generation"]
    (repo / "pkg/core.py").write_text("def helper():\n    return 2\n")
    rebuilt = ensure(repo)
    assert rebuilt["rebuilt"] is True and rebuilt["generation"] != generation
    assert {name: files(output / name) for name in ("memory", "reflections")} == kept
    assert (overlay.read_bytes(), overlay.stat().st_mtime_ns) == overlay_state


# CLI ---------------------------------------------------------------------------

def cli(env, *args, input=None):
    return subprocess.run([sys.executable, "-m", "graphify_worktree.cli", *map(str, args)],
                          env=env, capture_output=True, text=True, input=input, timeout=300)


@pytest.mark.integration
def test_cli_routes_save_result_and_reflect_with_documented_exit_codes(repo, tmp_path):
    env = isolated_env(tmp_path / "home", WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT=str(tmp_path / "vaults"))
    denied = cli(env, "learning", "save-result", repo, "--input-file", "-", input=json.dumps(RESULT))
    assert denied.returncode == 5 and json.loads(denied.stdout)["effective_mode"] == "off"
    once = cli(env, "learning", "save-result", repo, "--once", "--input-file", "-", input=json.dumps(RESULT))
    assert once.returncode == 0, once.stderr
    assert json.loads(once.stdout)["reflection"]["state"] == "reflected"
    invalid = cli(env, "learning", "reflect", repo, "--once", "--input-file", "-", input="{}")
    assert invalid.returncode == 1 and "missing fields" in invalid.stderr
    current = cli(env, "learning", "reflect", repo, "--once", "--input-file", "-", input=json.dumps(APPROVED))
    assert current.returncode == 0 and json.loads(current.stdout)["reflection"]["state"] == "fresh"


# Managed MCP -------------------------------------------------------------------

async def mcp_session(env, calls):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = Path(sys.executable).parent / "graphify-worktree-mcp"
    async with stdio_client(StdioServerParameters(command=str(command), env=env)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            results = []
            for name, arguments in calls:
                reply = await session.call_tool(name, arguments)
                results.append((reply.is_error, reply.content[0].text))
            return results


@pytest.mark.integration
def test_managed_query_graph_surfaces_the_overlay_after_an_mcp_save(repo, tmp_path):
    policy.configure(repo, "graph")
    env = isolated_env(tmp_path / "home", WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT=str(tmp_path / "vaults"))
    root = str(repo)
    query = ("query_graph", {"project_path": root, "question": "helper"})
    filtered = ("query_graph", {"project_path": root, "question": "who calls helper"})
    results = asyncio.run(mcp_session(env, [
        query,
        ("learning_save_result", {"project_path": root, **RESULT}),
        query,
        filtered,
        ("learning_reflect", {"project_path": root}),
    ]))
    (before_error, before), (saved_error, saved), (after_error, after), (_, after_filtered), (bad_error, bad) = results
    assert not before_error and "learning=" not in before
    assert not saved_error and json.loads(saved)["reflection"]["state"] == "reflected"
    assert not after_error and "learning=tentative" in after
    assert "learning=tentative" in after_filtered
    assert bad_error and json.loads(bad)["error"] == "invalid_learning_request"


@pytest.mark.integration
def test_managed_learning_tools_are_denied_without_consent(repo, tmp_path):
    env = isolated_env(tmp_path / "home", WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT=str(tmp_path / "vaults"))
    (error, text), = asyncio.run(mcp_session(env, [
        ("learning_save_result", {"project_path": str(repo), **RESULT})]))
    assert error and json.loads(text)["error"] == "knowledge_policy_denied"
    assert not (repo / "graphify-out").exists()
