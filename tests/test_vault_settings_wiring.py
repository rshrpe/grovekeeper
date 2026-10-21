"""Configured vault locations are honoured by the real memory and graph paths."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from graphify_worktree import memory, policy


@pytest.fixture
def repo(tmp_path, home):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return root.resolve()


def test_memory_notes_are_written_under_configured_memory_vault(repo, tmp_path, monkeypatch):
    vault = tmp_path / "engineering"
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(vault))
    policy.configure(repo, "obsidian")
    note = memory.handle("save", repo, {
        "kind": "fact", "title": "Configured vault", "content": "Stored in the override.",
        "source": "unit test", "recorded_by": "manual", "confirmed": True, "confirmation": "test",
    })["note"]
    assert Path(note["path"]).is_relative_to(vault / "Projects")
    assert Path(note["path"]).is_file()


def test_status_reports_vault_under_configured_graph_root(repo, tmp_path, monkeypatch):
    root = tmp_path / "generated"
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", str(root))
    policy.configure(repo, "graph")
    result = subprocess.run([sys.executable, "-m", "graphify_worktree.cli", "status", str(repo)],
                            capture_output=True, text=True)
    assert result.returncode == 4, result.stderr
    status = json.loads(result.stdout)
    assert status["state"] == "missing"
    assert Path(status["vault_path"]).parent == root / "repo"


def test_generated_vault_and_memory_namespace_share_one_repository_name(tmp_path, home, monkeypatch):
    from graphify_worktree.artifacts import vault_path

    root = tmp_path / "my repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    root = root.resolve()
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(tmp_path / "engineering"))
    policy.configure(root, "obsidian")
    note = memory.handle("save", root, {
        "kind": "fact", "title": "Shared name", "content": "Same label everywhere.",
        "source": "unit test", "recorded_by": "manual", "confirmed": True, "confirmation": "test",
    })["note"]
    namespace = Path(note["path"]).parent.parent.name
    assert vault_path(root).parent.name == "my-repo"
    assert namespace.startswith("my-repo-")
