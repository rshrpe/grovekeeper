"""Consent-aware pre-tool nudge: advisory only, capped, quiet after graph use, never blocking."""
import fcntl
import json
import os
from pathlib import Path
import time

import pytest

from graphify_worktree import nudge, policy

from conftest import git

pytestmark = pytest.mark.repo(mode="graph", files={"pkg/core.py": "def helper():\n    return 1\n",
                                                   "README.md": "# readme\n"})


def call(root, tool="Grep", tool_input=None, *, session="s1", agent=None, now=None):
    payload = {"session_id": session, "cwd": str(root), "hook_event_name": "PreToolUse",
               "tool_name": tool, "tool_input": tool_input if tool_input is not None else {"pattern": "helper"}}
    if agent:
        payload["agent_id"] = agent
    return nudge.respond(payload, now=now)


def context(output):
    data = json.loads(output)
    assert set(data) == {"hookSpecificOutput"}
    specific = data["hookSpecificOutput"]
    assert set(specific) == {"hookEventName", "additionalContext"}, "advisory only: never a permission decision"
    assert specific["hookEventName"] == "PreToolUse"
    return specific["additionalContext"]


# What it says ------------------------------------------------------------------

def test_search_in_an_enabled_repository_suggests_the_managed_tools(repo):
    text = context(call(repo))
    assert "query_graph" in text and f'project_path "{repo}"' in text
    assert "fine" in text
    assert "MUST" not in text and "MANDATORY" not in text


def test_obsidian_mode_nudges_too(repo):
    policy.configure(repo, "obsidian")
    assert call(repo)


def test_without_a_graph_it_is_softer_and_creates_nothing(repo):
    text = context(call(repo))
    assert "no graph has been built" in text
    assert not (repo / "graphify-out").exists()


@pytest.mark.parametrize("tool, tool_input, nudged", [
    ("Grep", {"pattern": "x"}, True),
    ("Glob", {"pattern": "**/*.py"}, True),
    ("Bash", {"command": "rg helper pkg"}, True),
    ("Bash", {"command": "git commit -m 'add flag support'"}, False),
    ("Bash", {"command": "ls -la"}, False),
    ("Read", {"file_path": "pkg/core.py"}, True),
    ("Read", {"file_path": "README.md"}, False),
    ("Read", {"file_path": "/etc/hosts"}, False),
    ("Edit", {"file_path": "pkg/core.py"}, False),
])
def test_only_searches_and_source_reads_inside_the_worktree_are_nudged(repo, tool, tool_input, nudged):
    if tool == "Read" and not tool_input["file_path"].startswith("/"):
        tool_input = {"file_path": str(repo / tool_input["file_path"])}
    assert bool(call(repo, tool, tool_input)) is nudged


# Consent -------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [None, "off"])
def test_silent_without_graph_consent(repo, mode):
    assert call(repo, session="before")
    if mode:
        policy.configure(repo, mode)
    else:
        git(repo, "config", "--unset", policy.KEY)
    assert call(repo) == ""


def test_silent_with_an_invalid_preference(repo):
    assert call(repo, session="before")
    git(repo, "config", "--add", policy.KEY, "obsidian")
    assert call(repo) == ""


def test_silent_when_the_session_is_off(repo, monkeypatch):
    assert call(repo, session="before")
    monkeypatch.setenv("HARNESS_KNOWLEDGE_MODE", "off")
    assert call(repo) == ""


def test_silent_outside_a_repository(tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert call(plain) == ""


def test_an_mcp_session_opt_out_silences_until_resumed(repo):
    assert call(repo, "mcp__graphify__knowledge_session", {"mode": "off"}) == ""
    assert call(repo) == ""
    assert call(repo, "mcp__graphify__knowledge_session", {"mode": "default"}) == ""
    assert call(repo)


# Budget ----------------------------------------------------------------------------

def test_at_most_five_per_agent(repo):
    outputs = [call(repo) for _ in range(7)]
    assert [bool(output) for output in outputs] == [True] * 5 + [False] * 2
    assert call(repo, agent="subagent-1"), "a subagent starts with its own budget"
    assert call(repo, session="s2"), "so does another session"


def test_calls_that_are_not_nudged_do_not_spend_the_budget(repo):
    for _ in range(10):
        call(repo, "Bash", {"command": "ls"})
    assert sum(bool(call(repo)) for _ in range(6)) == 5


def test_silent_without_a_session_id(repo):
    assert call(repo)
    assert call(repo, session="") == ""


def test_quiet_for_thirty_minutes_after_the_agent_used_a_managed_graph_tool(repo):
    start = time.time()
    assert call(repo, "mcp__graphify__query_graph", {"project_path": str(repo)}, now=start) == ""
    assert call(repo, now=start + 1799) == ""
    assert call(repo, agent="subagent-1", now=start + 10), "another agent has not used the graph"
    assert call(repo, now=start + 1801)


def test_old_session_state_is_garbage_collected(repo, home):
    call(repo, session="old")
    state = home / ".local/state/grovekeeper/nudge"
    old = next(state.iterdir())
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    call(repo, session="new")
    assert not old.exists()
    assert len(list(state.iterdir())) == 1


# Fail open -----------------------------------------------------------------------

@pytest.mark.parametrize("payload", [None, [], {"tool_name": 3}, {"session_id": "s", "cwd": 5, "tool_name": "Grep"}])
def test_malformed_payloads_are_silent(payload):
    assert nudge.respond(payload) == ""


def test_unwritable_state_is_silent(repo, monkeypatch, tmp_path):
    assert call(repo)
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker / "state"))
    assert call(repo) == ""


def test_a_busy_session_state_is_silent_and_fast(repo, home):
    assert call(repo)
    state = next((home / ".local/state/grovekeeper/nudge").iterdir())
    with state.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        started = time.monotonic()
        assert call(repo) == ""
        assert time.monotonic() - started < 1


# Staleness (real graph builds) ---------------------------------------------------------

@pytest.mark.integration
def test_current_looking_graph_gets_the_plain_suggestion(repo):
    from graphify_worktree.guard import ensure
    ensure(repo)
    text = context(call(repo))
    assert "may be out of date" not in text and "no graph has been built" not in text


@pytest.mark.integration
def test_moved_head_softens_the_suggestion(repo):
    from graphify_worktree.guard import ensure
    ensure(repo)
    git(repo, "commit", "-q", "--allow-empty", "-m", "next")
    assert "may be out of date" in context(call(repo))


@pytest.mark.integration
def test_reading_a_file_changed_after_the_build_softens_the_suggestion(repo):
    from graphify_worktree.guard import ensure
    ensure(repo)
    source = repo / "pkg/core.py"
    future = time.time() + 5
    os.utime(source, (future, future))
    assert "may be out of date" in context(call(repo, "Read", {"file_path": str(source)}))
    assert "may be out of date" not in context(call(repo, session="s2"))


# CLI ---------------------------------------------------------------------------------

def run_cli(monkeypatch, capsys, stdin):
    import io
    from graphify_worktree import cli
    monkeypatch.setattr("sys.argv", ["graphify-worktree", "nudge"])
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    captured = capsys.readouterr()
    return stopped.value.code, captured.out, captured.err


def test_cli_reads_the_hook_payload_from_stdin(repo, monkeypatch, capsys):
    payload = {"session_id": "s1", "cwd": str(repo), "tool_name": "Grep", "tool_input": {"pattern": "x"}}
    code, out, err = run_cli(monkeypatch, capsys, json.dumps(payload))
    assert (code, err) == (0, "")
    assert "query_graph" in context(out)


def test_cli_is_silent_and_succeeds_on_garbage(repo, monkeypatch, capsys):
    assert run_cli(monkeypatch, capsys, "not json") == (0, "", "")


def test_cli_nudge_does_not_depend_on_upstream_versions(repo, monkeypatch, capsys):
    from graphify_worktree import upstream
    from graphify_worktree.errors import UpstreamVersionError

    def mismatch():
        raise UpstreamVersionError("mismatch")
    monkeypatch.setattr(upstream, "assert_versions", mismatch)
    payload = {"session_id": "s1", "cwd": str(repo), "tool_name": "Grep", "tool_input": {}}
    assert run_cli(monkeypatch, capsys, json.dumps(payload))[0] == 0
