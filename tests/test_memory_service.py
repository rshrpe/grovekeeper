import subprocess
from pathlib import Path

import pytest

from graphify_worktree import policy
from graphify_worktree.errors import MemoryRequestError
from graphify_worktree.memory import handle


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    policy.configure(root, "obsidian")
    return root.resolve()


def save(root, title, content):
    return handle("save", root, {"kind": "fact", "title": title, "content": content, "source": "test",
                                 "recorded_by": "manual", "confirmed": True, "confirmation": "test approval"})["note"]


def test_search_skips_a_malformed_note_with_a_warning(repo):
    good = save(repo, "Retry ceiling", "Use three retries.")
    broken = save(repo, "Retry backoff", "Back off exponentially between retries.")
    Path(broken["path"]).write_text("hand edited without frontmatter\n")
    result = handle("search", repo, {"query": "retry"})
    assert [note["id"] for note in result["notes"]] == [good["id"]]
    assert any(broken["id"] in warning and "skipped" in warning for warning in result["warnings"])


def test_get_of_a_malformed_note_still_fails(repo):
    broken = save(repo, "Retry backoff", "Back off.")
    Path(broken["path"]).write_text("hand edited\n")
    with pytest.raises(MemoryRequestError, match="cannot read memory note"):
        handle("get", repo, {"id": broken["id"]})


def test_memory_namespace_uses_the_repository_identity_name(repo, tmp_path):
    note = save(repo, "Namespace", "Where notes live.")
    assert Path(note["path"]).parent.parent.name.startswith("repo-")
