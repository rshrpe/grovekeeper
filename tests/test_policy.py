"""Consent semantics: saved mode x session x one-off x capability."""
import subprocess

import pytest

from graphify_worktree import policy
from graphify_worktree.policy import PolicyError


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root.resolve()


def allowed(root, capability, *, once=False, session_mode=None):
    try:
        policy.require(root, capability=capability, once=once, session_mode=session_mode)
        return True
    except PolicyError:
        return False


@pytest.mark.parametrize("mode, graph, obsidian", [
    (None, False, False),
    ("off", False, False),
    ("graph", True, False),
    ("obsidian", True, True),
])
def test_saved_mode_grants_exactly_its_capabilities(repo, mode, graph, obsidian):
    if mode:
        policy.configure(repo, mode)
    assert allowed(repo, "graph") is graph
    assert allowed(repo, "obsidian") is obsidian


@pytest.mark.parametrize("mode", [None, "off", "graph"])
def test_once_permits_one_operation_without_saving(repo, mode):
    if mode:
        policy.configure(repo, mode)
    assert allowed(repo, "obsidian", once=True)
    assert policy.get_policy(repo)["saved_mode"] == mode


@pytest.mark.parametrize("session", [{"session_mode": "off"}, {"env": "off"}])
def test_session_off_outranks_saved_mode_and_once(repo, monkeypatch, session):
    policy.configure(repo, "obsidian")
    if "env" in session:
        monkeypatch.setenv("HARNESS_KNOWLEDGE_MODE", "off")
    mode = session.get("session_mode")
    assert not allowed(repo, "graph", once=True, session_mode=mode)
    current = policy.get_policy(repo, session_mode=mode)
    assert (current["saved_mode"], current["effective_mode"]) == ("obsidian", "off")


def test_invalid_session_environment_is_a_denial(repo, monkeypatch):
    policy.configure(repo, "graph")
    monkeypatch.setenv("HARNESS_KNOWLEDGE_MODE", "maybe")
    with pytest.raises(PolicyError):
        policy.get_policy(repo)


@pytest.mark.parametrize("values", [["yes"], ["graph", "obsidian"]])
def test_invalid_or_conflicting_saved_values_deny_even_once(repo, values):
    for value in values:
        git(repo, "config", "--add", "harness.knowledgeMode", value)
    with pytest.raises(PolicyError) as raised:
        policy.require(repo, capability="graph", once=True)
    assert raised.value.policy["effective_mode"] == "off"


def test_worktree_specific_and_included_settings_never_grant(repo, tmp_path):
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "harness.knowledgeMode", "obsidian")
    included = tmp_path / "included.gitconfig"
    included.write_text("[harness]\n\tknowledgeMode = obsidian\n")
    git(repo, "config", "include.path", str(included))
    assert policy.get_policy(repo)["state"] == "unconfigured"
    assert not allowed(repo, "graph")


def test_mode_is_shared_by_linked_worktrees(repo, tmp_path):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@l",
                    "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "other", str(other))
    policy.configure(other.resolve(), "graph")
    assert policy.get_policy(repo)["saved_mode"] == "graph"


def test_non_repository_is_reported_as_such(tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PolicyError) as raised:
        policy.get_policy(plain)
    assert raised.value.not_repository is True


def test_unknown_capability_is_denied(repo):
    policy.configure(repo, "obsidian")
    with pytest.raises(PolicyError):
        policy.require(repo, capability="filesystem")
