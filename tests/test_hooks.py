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
