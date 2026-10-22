"""CLI paths without smoke coverage: --scope, --once, export --open, memory --limit."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from integration import isolated_env
from conftest import git

pytestmark = [pytest.mark.integration, pytest.mark.repo(files={
    "src/core.py": "def core_symbol():\n    return 1\n",
    "tools/tool.py": "def tool_symbol():\n    return 2\n",
})]


@pytest.fixture
def env(tmp_path, home):
    return isolated_env(home, WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT=str(tmp_path / "vaults/Graphify"),
                        WORKTREE_KNOWLEDGE_MEMORY_VAULT=str(tmp_path / "vaults/Engineering"))


def cli(env, *args, expected=0, input=None):
    result = subprocess.run([sys.executable, "-m", "graphify_worktree.cli", *map(str, args)],
                            env=env, capture_output=True, text=True, input=input, timeout=300)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    return result


def labels(root):
    data = json.loads((root / "graphify-out/graph.json").read_text())
    return {node.get("label", "").removesuffix("()") for node in data["nodes"]}


def test_scope_narrows_is_reused_and_resets(repo, env):
    cli(env, "configure", "graph", repo)
    assert json.loads(cli(env, "refresh", repo, "--scope", "src").stdout)["scope"] == "src"
    assert "core_symbol" in labels(repo) and "tool_symbol" not in labels(repo)
    assert json.loads(cli(env, "status", repo).stdout)["scope"] == "src"
    (repo / "src/core.py").write_text("def core_changed():\n    return 3\n")
    cli(env, "refresh", repo)
    assert json.loads(cli(env, "status", repo).stdout)["scope"] == "src"
    assert "core_changed" in labels(repo) and "tool_symbol" not in labels(repo)
    cli(env, "refresh", repo, "--scope", ".")
    assert {"core_changed", "tool_symbol"} <= labels(repo)


@pytest.mark.parametrize("scope", ["/tmp", "../outside", "missing", "graphify-out", "nested"])
def test_invalid_scopes_are_operation_errors(repo, env, scope):
    cli(env, "configure", "graph", repo)
    (repo / "graphify-out").mkdir()
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    cli(env, "refresh", repo, "--scope", scope, expected=1)


def test_ensure_once_builds_without_enrolling_and_session_off_still_wins(repo, env):
    status = json.loads(cli(env, "ensure", repo, "--once").stdout)
    assert (status["state"], status["rebuilt"]) == ("fresh", True)
    assert json.loads(cli(env, "policy", repo).stdout)["state"] == "unconfigured"
    cli(env | {"HARNESS_KNOWLEDGE_MODE": "off"}, "ensure", repo, "--once", expected=5)


def test_corrupt_provenance_reads_as_stale_not_a_crash(repo, env):
    cli(env, "configure", "graph", repo)
    cli(env, "refresh", repo)
    (repo / "graphify-out/.graphify-worktree.json").write_text("[1, 2]")
    assert json.loads(cli(env, "status", repo, expected=3).stdout)["state"] == "stale"


def fake_obsidian(tmp_path, reply):
    log = tmp_path / "obsidian.log"
    script = tmp_path / "obsidian"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {log}\necho '{reply}'\n")
    script.chmod(0o755)
    return script, log


def test_export_open_registers_the_vault_through_the_obsidian_cli(repo, env, tmp_path):
    cli(env, "configure", "obsidian", repo)
    cli(env, "refresh", repo)
    script, log = fake_obsidian(tmp_path, "=> true")
    result = json.loads(cli(env | {"WORKTREE_KNOWLEDGE_OBSIDIAN_CLI": str(script)}, "export", repo, "--open").stdout)
    assert result["state"] == "exported"
    calls = log.read_text()
    assert calls.startswith("eval\ncode=") and json.dumps(result["vault_path"]) in calls


def test_export_open_failure_is_reported_after_the_export(repo, env, tmp_path):
    cli(env, "configure", "obsidian", repo)
    cli(env, "refresh", repo)
    script, _ = fake_obsidian(tmp_path, "Error: vault-open refused")
    result = cli(env | {"WORKTREE_KNOWLEDGE_OBSIDIAN_CLI": str(script)}, "export", repo, "--open", expected=1)
    assert json.loads(result.stdout)["state"] == "exported"
    assert "Obsidian registration failed" in result.stderr


def test_export_requires_obsidian_consent_and_a_fresh_graph(repo, env):
    cli(env, "configure", "graph", repo)
    cli(env, "export", repo, expected=5)
    cli(env, "configure", "obsidian", repo)
    assert json.loads(cli(env, "export", repo, expected=4).stdout)["state"] == "missing"


def test_memory_search_limit_bounds_results(repo, env):
    cli(env, "configure", "obsidian", repo)
    for title in ("Retry one", "Retry two", "Retry three"):
        cli(env, "memory", "save", repo, "--input-file", "-", input=json.dumps({
            "kind": "fact", "title": title, "content": "retry note", "source": "test",
            "recorded_by": "manual", "confirmed": True, "confirmation": "test approval"}))
    found = json.loads(cli(env, "memory", "search", repo, "--query", "retry", "--limit", "2").stdout)
    assert len(found["notes"]) == 2
    cli(env, "memory", "search", repo, "--query", "retry", "--limit", "0", expected=1)


def test_memory_input_must_be_json(repo, env):
    cli(env, "configure", "obsidian", repo)
    result = cli(env, "memory", "save", repo, "--input-file", "-", input="not json", expected=1)
    assert "input is not valid JSON" in result.stderr
