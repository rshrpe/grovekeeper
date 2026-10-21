import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from graphify_worktree import hooks, policy
from graphify_worktree.errors import HookError

EVENTS = {"post-checkout", "post-merge", "post-rewrite", "post-commit"}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def hook(directory, name, body="exit 0\n"):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    policy.configure(root, "graph")
    custom = root / ".custom-hooks"
    custom.mkdir()
    hook(custom, "pre-commit")
    hook(custom, "pre-push")
    git(root, "config", "core.hooksPath", ".custom-hooks")
    return root.resolve()


def shims(root):
    return {path.name for path in (root / ".git/graphify-worktree-hooks/hooks").iterdir()}


def test_install_shims_event_hooks_and_existing_originals_only(repo):
    assert hooks.install(repo)["changed"] is True
    assert shims(repo) == EVENTS | {"pre-commit", "pre-push"}


def test_hooks_added_later_are_reported_then_forwarded_on_reinstall(repo):
    hooks.install(repo)
    hook(repo / ".custom-hooks", "commit-msg")
    status = hooks.status(repo)
    assert (status["state"], status["unforwarded"]) == ("installed", ["commit-msg"])
    assert hooks.install(repo)["changed"] is True
    assert "commit-msg" in shims(repo)
    assert hooks.status(repo)["unforwarded"] == []
    assert hooks.install(repo)["changed"] is False


def test_status_reports_conflict_when_hooks_path_changes(repo):
    hooks.install(repo)
    git(repo, "config", "core.hooksPath", ".elsewhere")
    assert hooks.status(repo)["state"] == "conflict"


def test_status_without_installation(repo):
    status = hooks.status(repo)
    assert (status["state"], status["hooks_path"]) == ("not-installed", ".custom-hooks")


def test_uninstall_restores_hooks_path_and_removes_state(repo):
    hooks.install(repo)
    hooks.uninstall(repo)
    assert git(repo, "config", "--get", "core.hooksPath") == ".custom-hooks"
    assert not (repo / ".git/graphify-worktree-hooks").exists()


def run_shim(repo, name, *args, python="/missing/python", module="/missing/hooks.py"):
    saved = {"original_directory": ".custom-hooks", "python": python, "module": module, "schema": 2}
    script = repo / f"shim-{name}"
    script.write_text(hooks.shim_script(repo / ".git/state", name, saved))
    script.chmod(0o755)
    return subprocess.run([str(script), *args], cwd=repo, capture_output=True, text=True)


def test_shim_runs_only_the_original_hook_when_the_tool_is_gone(repo):
    hook(repo / ".custom-hooks", "commit-msg", 'echo "original $*"; exit 7\n')
    result = run_shim(repo, "commit-msg", "MSG_FILE")
    assert (result.returncode, result.stdout) == (7, "original MSG_FILE\n")


def test_shim_without_tool_or_original_hook_succeeds(repo):
    assert run_shim(repo, "post-merge", "0").returncode == 0


def test_shim_dispatches_through_the_tool_when_present(repo):
    hooks.install(repo)
    hook(repo / ".custom-hooks", "pre-commit", 'echo "dispatched"; exit 3\n')
    shim = repo / ".git/graphify-worktree-hooks/hooks/pre-commit"
    result = subprocess.run([str(shim)], cwd=repo, capture_output=True, text=True)
    assert (result.returncode, result.stdout) == (3, "dispatched\n")


def test_legacy_installation_is_still_recognised_and_removable(repo):
    common = repo / ".git"
    state = common / "graphify-worktree-hooks"
    (state / "hooks").mkdir(parents=True)
    python, module = sys.executable, "/old/skill/scripts/knowledge_hooks.py"
    for name in hooks.HOOKS:
        path = state / "hooks" / name
        command = " ".join(shlex.quote(str(v)) for v in (python, module, "--dispatch", state, name))
        path.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
        path.chmod(0o700)
    (state / "manifest.json").write_text(json.dumps({
        "schema": 1, "common_dir": str(common), "hooks_path": str(state / "hooks"),
        "original_local": [".custom-hooks"], "original_directory": ".custom-hooks",
        "python": python, "module": module}))
    git(repo, "config", "core.hooksPath", str(state / "hooks"))
    assert hooks.status(repo)["state"] == "installed"
    assert hooks.install(repo)["changed"] is False
    hooks.uninstall(repo)
    assert git(repo, "config", "--get", "core.hooksPath") == ".custom-hooks"



def blocking_repo(root, custom):
    """A repository whose own pre-commit hook exits 7, in core.hooksPath or the default directory."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    policy.configure(root, "graph")
    directory = root / (".hooks" if custom else ".git/hooks")
    directory.mkdir(exist_ok=True)
    hook(directory, "pre-commit", 'echo "original pre-commit" >&2; exit 7\n')
    if custom:
        git(root, "config", "core.hooksPath", ".hooks")
    return root.resolve()


def commit(root):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l",
                           "commit", "-q", "--allow-empty", "-m", "change"], capture_output=True, text=True)


def local_hooks_path(root):
    result = subprocess.run(["git", "-C", str(root), "config", "--local", "--get", "core.hooksPath"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


@pytest.mark.parametrize("custom", [True, False], ids=["core-hooks-path", "default-hooks-directory"])
def test_hooks_install_repairs_a_moved_repository(tmp_path, home, monkeypatch, custom):
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    old = blocking_repo(tmp_path / "repo", custom)
    hooks.install(old)
    assert commit(old).returncode == 1
    (tmp_path / "repo").rename(tmp_path / "moved")
    moved = (tmp_path / "moved").resolve()
    status = hooks.status(moved)
    assert (status["state"], status["moved_from"]) == ("relocated", str(old / ".git"))
    assert hooks.run(argparse.Namespace(hooks_action="status", path=moved)) == 1
    result = hooks.install(moved)
    assert (result["changed"], result["moved_from"]) == (True, str(old / ".git"))
    blocked = commit(moved)
    assert blocked.returncode == 1 and "original pre-commit" in blocked.stderr
    assert local_hooks_path(moved) == str(moved / ".git/graphify-worktree-hooks/hooks")
    assert hooks.status(moved)["state"] == "installed"
    assert hooks.install(moved)["changed"] is False


@pytest.mark.parametrize("custom", [True, False], ids=["core-hooks-path", "default-hooks-directory"])
def test_hooks_uninstall_works_on_a_moved_repository(tmp_path, home, monkeypatch, custom):
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    blocking_repo(tmp_path / "repo", custom)
    hooks.install(tmp_path / "repo")
    (tmp_path / "repo").rename(tmp_path / "moved")
    moved = (tmp_path / "moved").resolve()
    assert hooks.uninstall(moved)["changed"] is True
    assert local_hooks_path(moved) == (".hooks" if custom else None)
    assert not (moved / ".git/graphify-worktree-hooks").exists()
    assert commit(moved).returncode == 1, "Git runs the original pre-commit again"


def test_a_shim_of_a_moved_repository_never_bypasses_the_original_hook(tmp_path, home, monkeypatch):
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    blocking_repo(tmp_path / "repo", True)
    hooks.install(tmp_path / "repo")
    (tmp_path / "repo").rename(tmp_path / "moved")
    moved = (tmp_path / "moved").resolve()
    assert hooks.dispatch(str(moved / ".git/graphify-worktree-hooks"), "pre-commit", []) == 1

@pytest.fixture
def graph_output(repo):
    output = repo / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text("{}")
    (output / ".graphify-worktree.json").write_text(json.dumps(
        {"worktree_root": str(repo), "extracted_at": "2026-10-21T10:00:00+00:00"}))
    return output


def test_mark_event_tags_the_published_generation(repo, graph_output):
    assert hooks.mark_event(repo, "post-merge") is True
    marker = json.loads((graph_output / ".graphify-worktree-event.json").read_text())
    assert (marker["event"], marker["generation"]) == ("post-merge", "2026-10-21T10:00:00+00:00")


def test_mark_event_waits_for_the_worktree_lock_then_gives_up(repo, graph_output):
    with open(graph_output / ".graphify-worktree.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert hooks.mark_event(repo, "post-merge", wait=0.2) is False
    assert not (graph_output / ".graphify-worktree-event.json").exists()
    assert hooks.mark_event(repo, "post-merge", wait=0.2) is True


def test_mark_event_ignores_non_events_and_disabled_repositories(repo, graph_output):
    assert hooks.mark_event(repo, "pre-commit") is False
    policy.configure(repo, "off")
    assert hooks.mark_event(repo, "post-merge") is False
    assert not (graph_output / ".graphify-worktree-event.json").exists()
