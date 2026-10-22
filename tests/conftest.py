import subprocess

import pytest

from graphify_worktree import policy


def git(root, *args):
    """Run git in root with a throwaway identity; returns stripped stdout."""
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME with every worktree-knowledge override cleared."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    for name in ("WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", "WORKTREE_KNOWLEDGE_MEMORY_VAULT",
                 "WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", "WORKTREE_KNOWLEDGE_SKILL_DIR",
                 "WORKTREE_KNOWLEDGE_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    # Keep `git config --global` and XDG lookups inside the temporary HOME.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    return path


@pytest.fixture
def make_repo(tmp_path, home, monkeypatch):
    """Factory for disposable repositories under tmp_path, with vaults in tmp_path/vault(s).

    make_repo(name="repo", mode=None, files=None, commit=True): files maps
    relative paths to text; commit makes one base commit of them (empty if
    none); mode saves a knowledge mode.
    """
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", str(tmp_path / "vaults"))

    def make(name="repo", *, mode=None, files=None, commit=True):
        root = tmp_path / name
        root.mkdir()
        for relative, text in (files or {}).items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text)
        git(root, "init", "-q", "-b", "main")
        if commit:
            git(root, "add", ".")
            git(root, "commit", "-q", "--allow-empty", "-m", "base")
        if mode:
            policy.configure(root, mode)
        return root.resolve()

    return make


@pytest.fixture
def repo(request, make_repo):
    """One repository; @pytest.mark.repo(...) passes make_repo's arguments."""
    marker = request.node.get_closest_marker("repo")
    return make_repo(**(marker.kwargs if marker else {}))
