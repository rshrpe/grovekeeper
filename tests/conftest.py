import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME with every worktree-knowledge override cleared."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    for name in ("WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", "WORKTREE_KNOWLEDGE_MEMORY_VAULT",
                 "WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", "WORKTREE_KNOWLEDGE_SKILL_DIR"):
        monkeypatch.delenv(name, raising=False)
    return path
