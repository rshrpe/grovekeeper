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


def test_scan_yields_every_note_in_name_order_with_unreadable_ones_as_errors(repo):
    from graphify_worktree.memory import service, store
    good = save(repo, "Retry ceiling", "Use three retries.")
    broken = save(repo, "Retry backoff", "Back off.")
    Path(broken["path"]).write_text("hand edited\n")
    (Path(good["path"]).parent / "notes.txt").write_text("not a note")
    folder = service.repository(repo)
    with store.directory(Path(good["path"]).parent, create=False, writing=False) as fd:
        scanned = list(store.scan(fd, folder))
    assert [name for name, *_ in scanned] == sorted([good["id"] + ".md", broken["id"] + ".md"])
    by_name = {name: (metadata, content, error) for name, metadata, content, error in scanned}
    metadata, content, error = by_name[good["id"] + ".md"]
    assert (metadata["title"], content, error) == ("Retry ceiling", "Use three retries.", None)
    metadata, content, error = by_name[broken["id"] + ".md"]
    assert metadata is None and content is None and "cannot read memory note" in str(error)
    assert list(store.scan(None, folder)) == []
