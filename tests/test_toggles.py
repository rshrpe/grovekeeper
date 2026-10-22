"""Incremental rebuilds and general memory are repository settings every process agrees on."""
import json
import subprocess
import sys

import pytest

from graphify_worktree import cli, policy
from graphify_worktree.errors import MemoryRequestError
from graphify_worktree.memory import handle

from conftest import git
from integration import isolated_env

pytestmark = pytest.mark.repo(commit=False)


def lesson(root):
    return handle("save", root, {"kind": "lesson", "title": "Retries need jitter", "content": "c", "source": "s",
                                 "status": "accepted", "scope": "general", "recorded_by": "manual",
                                 "confirmed": True, "confirmation": "ok"})


def test_both_are_off_by_default_and_reported_by_policy(repo, monkeypatch):
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_INCREMENTAL", raising=False)
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_GENERAL_MEMORY", raising=False)
    current = policy.get_policy(repo)
    assert (current["incremental"], current["general_memory"]) == (False, False)
    assert "deprecated_environment" not in current and "invalid_settings" not in current


def test_setting_one_never_touches_the_knowledge_mode(repo):
    current = policy.configure(repo, incremental=True)
    assert (current["incremental"], current["general_memory"], current["state"]) == (True, False, "unconfigured")
    assert git(repo, "config", "--get", "harness.knowledgeIncremental") == "true"
    current = policy.configure(repo, incremental=False, general_memory=True)
    assert (current["incremental"], current["general_memory"]) == (False, True)


def test_another_process_sees_the_saved_setting(repo, tmp_path):
    policy.configure(repo, "graph", general_memory=True)
    result = subprocess.run([sys.executable, "-m", "graphify_worktree.cli", "policy", str(repo)],
                            env=isolated_env(tmp_path / "home"), capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["general_memory"] is True


@pytest.mark.parametrize("argv, mode, incremental, general", [
    (["configure", "--incremental", "on", "{repo}"], None, True, False),
    (["configure", "{repo}", "--general-memory", "on"], None, False, True),
    (["configure", "graph", "{repo}", "--incremental", "on", "--general-memory", "off"], "graph", True, False),
    (["configure", "obsidian", "{repo}"], "obsidian", False, False),
])
def test_configure_command_line(repo, capsys, argv, mode, incremental, general):
    assert cli.main([value.format(repo=repo) for value in argv]) == 0
    current = json.loads(capsys.readouterr().out)
    assert (current["saved_mode"], current["incremental"], current["general_memory"]) == (mode, incremental, general)


@pytest.mark.parametrize("argv", [["configure"], ["configure", "{repo}"], ["configure", "maybe", "{repo}"],
                                  ["configure", "--incremental", "yes", "{repo}"]])
def test_configure_without_a_mode_or_setting_is_a_usage_error(repo, argv):
    with pytest.raises(SystemExit) as raised:
        cli.main([value.format(repo=repo) for value in argv])
    assert raised.value.code == 2
    assert policy.get_policy(repo)["state"] == "unconfigured"


def test_a_directory_named_like_a_mode_is_not_taken_for_one_when_only_a_setting_is_given(repo, monkeypatch):
    (repo / "graph").mkdir()
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as raised:
        cli.main(["configure", "--incremental", "on", "graph"])
    assert raised.value.code == 2
    assert policy.get_policy(repo)["saved_mode"] is None


def test_the_environment_is_a_deprecated_fallback_only_while_the_key_is_unset(repo, monkeypatch):
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_INCREMENTAL", "1")
    current = policy.get_policy(repo)
    assert current["incremental"] is True
    assert current["deprecated_environment"] == ["WORKTREE_KNOWLEDGE_INCREMENTAL"]
    policy.configure(repo, incremental=False)
    current = policy.get_policy(repo)
    assert current["incremental"] is False and "deprecated_environment" not in current


@pytest.mark.parametrize("values", [["maybe"], ["true", "false"]])
def test_an_invalid_or_conflicting_value_means_off_and_is_reported(repo, values):
    for value in values:
        git(repo, "config", "--add", "harness.generalMemory", value)
    current = policy.get_policy(repo)
    assert current["general_memory"] is False and current["invalid_settings"] == ["harness.generalMemory"]


def test_included_configuration_never_turns_a_setting_on(repo, tmp_path):
    included = tmp_path / "included.gitconfig"
    included.write_text("[harness]\n\tgeneralMemory = true\n\tknowledgeIncremental = true\n")
    git(repo, "config", "include.path", str(included))
    current = policy.get_policy(repo)
    assert (current["incremental"], current["general_memory"]) == (False, False)


def test_general_memory_follows_the_repository_setting(repo, monkeypatch):
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_GENERAL_MEMORY", raising=False)
    policy.configure(repo, "obsidian")
    with pytest.raises(MemoryRequestError, match="configure --general-memory on"):
        lesson(repo)
    policy.configure(repo, general_memory=True)
    assert lesson(repo)["note"]["scope"] == "general"
