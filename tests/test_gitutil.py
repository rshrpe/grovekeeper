import hashlib
import os
import subprocess

import pytest

from graphify_worktree import gitutil
from graphify_worktree.errors import GitError, PolicyError


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, home):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    return root.resolve()


def test_ambient_git_environment_cannot_redirect_explicit_paths(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert gitutil.output(repo, "rev-parse", "--show-toplevel") == str(repo)


def test_failed_command_raises_git_error_with_stderr(repo):
    with pytest.raises(GitError, match="Needed a single revision"):
        gitutil.output(repo, "rev-parse", "--verify", "no-such-branch^{commit}", "--")


def test_listed_exit_codes_mean_absent(repo):
    git(repo, "switch", "-q", "--detach")
    assert gitutil.output(repo, "symbolic-ref", "--quiet", "--short", "HEAD", missing=(1,)) is None


def test_resolve_root_finds_top_level_from_subdirectory(repo):
    (repo / "pkg/sub").mkdir(parents=True)
    assert gitutil.resolve_root(repo / "pkg/sub") == repo


def test_resolve_root_reports_non_repository(tmp_path, home):
    with pytest.raises(PolicyError) as raised:
        gitutil.resolve_root(tmp_path)
    assert raised.value.not_repository is True


def test_resolve_root_rejects_missing_path(tmp_path, home):
    with pytest.raises(PolicyError) as raised:
        gitutil.resolve_root(tmp_path / "missing")
    assert raised.value.not_repository is False


def test_worktrees_share_identity(repo, tmp_path):
    other = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "linked", str(other))
    main, linked = gitutil.RepoIdentity.of(repo), gitutil.RepoIdentity.of(other.resolve())
    expected_id = hashlib.sha256(os.fsencode(repo / ".git")).hexdigest()
    assert (main.id, main.name, main.common_git_dir) == (expected_id, "repo", repo / ".git")
    assert (linked.id, linked.name) == (main.id, main.name)


def test_bare_repository_name_drops_git_suffix(tmp_path, home):
    bare = tmp_path / "service.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    tree = tmp_path / "tree"
    git(bare, "worktree", "add", "-q", "--orphan", "-b", "main", str(tree))
    assert gitutil.RepoIdentity.of(tree.resolve()).name == "service"


def test_separate_git_dir_name_comes_from_the_git_directory(tmp_path, home):
    tree, store = tmp_path / "checkout", tmp_path / "store" / "project-data"
    tree.mkdir()
    store.parent.mkdir()
    git(tree, "init", "-q", "--separate-git-dir", str(store))
    assert gitutil.RepoIdentity.of(tree.resolve()).name == "project-data"


def test_name_is_sanitised_for_paths(tmp_path, home):
    root = tmp_path / "my project (v2)"
    root.mkdir()
    git(root, "init", "-q")
    assert gitutil.RepoIdentity.of(root.resolve()).name == "my-project-v2"


def test_checkout_state_on_branch_detached_and_unborn(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    assert gitutil.checkout_state(repo) == {"branch": "main", "head": head}
    git(repo, "switch", "-q", "--detach")
    assert gitutil.checkout_state(repo) == {"branch": None, "head": head}
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q", "-b", "trunk")
    assert gitutil.checkout_state(unborn.resolve(), allow_unborn=True) == {"branch": "trunk", "head": None}
    with pytest.raises(GitError):
        gitutil.checkout_state(unborn.resolve())
