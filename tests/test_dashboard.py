"""The generated Obsidian Bases dashboard over one repository's memory notes."""
from pathlib import Path
import subprocess

import pytest

from graphify_worktree import dashboard, policy
from graphify_worktree.errors import KnowledgeError, PolicyError
from graphify_worktree.memory import handle


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    policy.configure(root, "obsidian")
    return root.resolve()


def memory_folder(repo):
    note = handle("save", repo, {"kind": "fact", "title": "Retry ceiling", "content": "c", "source": "s",
                                 "recorded_by": "manual", "confirmed": True, "confirmation": "ok"})["note"]
    return Path(note["path"]).parent


def test_the_dashboard_sits_beside_the_memory_folder_and_queries_only_it(repo, tmp_path):
    folder = memory_folder(repo)
    result = dashboard.write(repo)
    path = Path(result["path"])
    assert result["state"] == "created" and path == folder.parent / "Memory.base"
    text = path.read_text()
    relative = folder.relative_to(tmp_path / "vault").as_posix()
    assert f'file.inFolder("{relative}")' in text
    for view in ("Current", "By kind", "Proposals by branch", "Needs review", "History"):
        assert f'name: "{view}"' in text


def test_rerunning_is_unchanged(repo):
    first = dashboard.write(repo)
    assert dashboard.write(repo) == {**first, "state": "unchanged"}


def test_an_edited_dashboard_is_never_overwritten(repo):
    path = Path(dashboard.write(repo)["path"])
    path.write_text(path.read_text() + "  - type: list\n    name: \"Mine\"\n")
    edited = path.read_text()
    with pytest.raises(KnowledgeError, match="changed outside grovekeeper"):
        dashboard.write(repo)
    assert path.read_text() == edited


def test_it_does_not_create_the_memory_folder(repo):
    path = Path(dashboard.write(repo)["path"])
    assert not (path.parent / "Memory").exists()


def test_a_symlinked_dashboard_is_refused(repo, tmp_path):
    path = Path(dashboard.write(repo)["path"])
    path.unlink()
    target = tmp_path / "elsewhere.base"
    target.write_text("theirs\n")
    path.symlink_to(target)
    with pytest.raises(KnowledgeError, match="symlink"):
        dashboard.write(repo)
    assert target.read_text() == "theirs\n"


@pytest.mark.parametrize("mode", [None, "off", "graph"])
def test_needs_obsidian_consent_and_writes_nothing_without_it(repo, tmp_path, mode):
    if mode:
        policy.configure(repo, mode)
    else:
        git(repo, "config", "--unset", policy.KEY)
    with pytest.raises(PolicyError):
        dashboard.write(repo)
    assert not (tmp_path / "vault").exists()


def test_once_allows_it_without_enrolling(repo):
    policy.configure(repo, "off")
    assert dashboard.write(repo, once=True)["state"] == "created"
    assert policy.get_policy(repo)["saved_mode"] == "off"


def test_cli(repo, capsys):
    import json
    from graphify_worktree import cli
    assert cli.main(["dashboard", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "created"
