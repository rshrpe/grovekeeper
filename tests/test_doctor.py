"""graphify-worktree doctor: a read-only report of how the installation is wired up."""
import json
import os
from pathlib import Path
import sys

import pytest

from graphify_worktree import cli, doctor, harness, hooks, policy, upstream

pytestmark = pytest.mark.repo(mode="graph")

HERE = Path(sys.executable).parent  # where this installation's executables are


@pytest.fixture
def harnesses(home, tmp_path, monkeypatch):
    """Claude Code, Codex and OMP configured by the real installer, pointing at this installation."""
    checkout = Path(__file__).parents[1]
    for marker in (".claude", ".codex", ".omp/agent"):
        (home / marker).mkdir(parents=True)
    monkeypatch.setattr(harness, "register_claude", lambda command: "manual")  # never the real claude CLI
    harness.install(checkout, HERE, tmp_path / "backup", session_recall=True, report=lambda line: None)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"graphify": {
        "type": "stdio", "command": str(HERE / "graphify-worktree-mcp"), "args": [], "env": {}}}}))
    return home


def run(capsys, *argv):
    assert cli.main(["doctor", *map(str, argv)]) == 0
    return json.loads(capsys.readouterr().out)


def files(*roots):
    return {str(path): (path.read_bytes(), path.lstat().st_mtime_ns)
            for root in roots for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


def test_a_healthy_installation_reports_everything_and_no_findings(repo, harnesses, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", sys.executable)
    report = run(capsys, repo)
    assert report["package"]["grovekeeper"] and report["pins"]["graphifyy"] == {
        "pinned": "0.9.93", "installed": "0.9.93", "ok": True}
    for name in ("claude", "codex", "omp"):
        entry = report["harnesses"][name]
        assert entry["present"] and entry["mcp"]["registered"] and entry["mcp"]["this_installation"], name
    for name in ("claude", "codex"):
        assert report["harnesses"][name]["hooks"]["recall"]["registered"] is True
        assert report["harnesses"][name]["hooks"]["nudge"]["registered"] is False
    assert report["locations"]["memory_vault"] == {"path": str(tmp_path / "vault"), "exists": False,
                                                    "traverses_symlink": False}
    assert report["repository"]["policy"]["effective_mode"] == "graph"
    assert report["repository"]["hooks"]["state"] == "not-installed"
    assert report["findings"] == []


def test_doctor_changes_nothing(repo, harnesses, capsys):
    hooks.install(repo)
    before = files(harnesses, repo / ".git")
    run(capsys, repo)
    assert files(harnesses, repo / ".git") == before


def test_problems_become_findings(repo, harnesses, capsys, monkeypatch, tmp_path):
    real = upstream.version
    monkeypatch.setattr(upstream, "version", lambda name: "0.9.94" if name == "graphifyy" else real(name))
    target = tmp_path / "real-vault"
    target.mkdir()
    (tmp_path / "linked-vault").symlink_to(target)
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(tmp_path / "linked-vault"))
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", str(tmp_path / "missing-obsidian"))
    (harnesses / ".omp/agent/mcp.json").write_text(json.dumps({"mcpServers": {"graphify": {"command": "/elsewhere/mcp"}}}))
    (harnesses / ".codex/config.toml").write_text('model = "x"\n')
    policy.configure(repo, incremental=True)
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_GENERAL_MEMORY", "1")
    hooks.install(repo)
    (tmp_path / "repo").rename(tmp_path / "moved")
    report = run(capsys, tmp_path / "moved")
    text = "\n".join(report["findings"])
    for expected in ("graphifyy 0.9.94 is installed but 0.9.93 is pinned", "memory_vault", "symlink",
                     "Obsidian CLI", "omp: the graphify MCP server runs /elsewhere/mcp",
                     "codex: the graphify MCP server is not registered", "WORKTREE_KNOWLEDGE_GENERAL_MEMORY",
                     "relocated"):
        assert expected in text, (expected, text)


def test_outside_a_repository_and_without_harnesses(home, capsys, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    report = run(capsys, plain)
    assert report["repository"]["policy"]["state"] == "not_repository" and "hooks" not in report["repository"]
    assert all(entry == {"present": False} for entry in report["harnesses"].values())


def test_doctor_runs_even_when_the_pins_mismatch(repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["graphify-worktree", "doctor", str(repo)])
    monkeypatch.setattr(upstream, "version", lambda name: "0.0.1")
    with pytest.raises(SystemExit) as raised:
        cli.entrypoint()
    assert raised.value.code == 0
