"""Every CLI result goes through one JSON emitter that keeps non-ASCII text readable."""
import json
import subprocess

import pytest

from graphify_worktree import cli, policy


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    root = tmp_path / "Ünïcode répo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return root.resolve()


def run(capsys, *argv):
    code = cli.main([*map(str, argv)])
    return code, capsys.readouterr().out


def test_results_print_non_ascii_text_as_itself(repo, capsys):
    code, out = run(capsys, "policy", repo)
    assert code == 0 and "Ünïcode répo" in out and "\\u" not in out
    assert json.loads(out)["worktree_root"] == str(repo)


def test_hooks_results_use_the_same_emitter(repo, capsys):
    code, out = run(capsys, "hooks", "status", repo)
    assert code == 0 and "Ünïcode répo" in out
    code, out = run(capsys, "hooks", "install", repo)
    assert code == 5 and out.startswith("{\n  ") and json.loads(out)["state"] == "denied"
    policy.configure(repo, "graph")
    code, out = run(capsys, "hooks", "install", repo)
    assert code == 0 and "Ünïcode répo" in out and json.loads(out)["state"] == "installed"
