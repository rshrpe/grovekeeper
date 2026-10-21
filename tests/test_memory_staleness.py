"""Optional review dates on memory notes, memory_verify, and the read-only memory lint."""
from datetime import date, timedelta
from pathlib import Path
import re
import subprocess

import pytest

from graphify_worktree import policy
from graphify_worktree.errors import MemoryRequestError, PolicyError
from graphify_worktree.memory import handle, service

APPROVAL = {"recorded_by": "manual", "confirmed": True, "confirmation": "test approval"}
TODAY = date(2026, 10, 21)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_MEMORY_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("HARNESS_KNOWLEDGE_MODE", raising=False)
    monkeypatch.setattr(service, "today", lambda: TODAY)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    policy.configure(root, "obsidian")
    return root.resolve()


def day(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def save(root, title="Retry ceiling", status="accepted", **dates):
    return handle("save", root, {"kind": "fact", "title": title, "content": "Use three retries.",
                                 "source": "test", "status": status, **APPROVAL, **dates})["note"]


def later(monkeypatch, offset):
    monkeypatch.setattr(service, "today", lambda: TODAY + timedelta(days=offset))


# Review dates ------------------------------------------------------------------

def test_notes_without_dates_have_no_freshness_and_an_unchanged_file(repo):
    note = save(repo)
    assert "freshness" not in note
    text = Path(note["path"]).read_text()
    assert "verify_by" not in text and "stale_after" not in text


def test_review_dates_are_stored_and_reported(repo):
    note = save(repo, verify_by=day(30), stale_after=day(90))
    assert note["freshness"] == {"verify_by": day(30), "stale_after": day(90), "state": "current"}
    assert note["metadata"]["verify_by"] == day(30)


@pytest.mark.parametrize("offset, state", [(30, "current"), (31, "verify_due"), (90, "verify_due"), (91, "stale")])
def test_freshness_state_follows_the_dates(repo, monkeypatch, offset, state):
    note = save(repo, verify_by=day(30), stale_after=day(90))
    later(monkeypatch, offset)
    assert handle("get", repo, {"id": note["id"]})["note"]["freshness"]["state"] == state
    found = handle("search", repo, {"query": "retry"})["notes"]
    assert [result["freshness"]["state"] for result in found] == [state], "search still returns it, flagged"


@pytest.mark.parametrize("dates, message", [
    ({"verify_by": "2026-13-01"}, "verify_by must be a date (YYYY-MM-DD)"),
    ({"verify_by": "21 Oct 2026"}, "verify_by must be a date (YYYY-MM-DD)"),
    ({"stale_after": day(-1)}, "stale_after must not be in the past"),
    ({"verify_by": day(60), "stale_after": day(30)}, "stale_after must not be before verify_by"),
])
def test_invalid_review_dates_are_refused(repo, dates, message):
    with pytest.raises(MemoryRequestError, match=re.escape(message)):
        save(repo, **dates)


# memory_verify ------------------------------------------------------------------

def verify(root, note, **dates):
    return handle("verify", root, {"id": note["id"], "source": "re-checked against src/retry.py", **APPROVAL, **dates})


def test_verify_sets_exactly_the_given_dates_and_records_an_audit_event(repo, monkeypatch):
    note = save(repo, verify_by=day(30), stale_after=day(90))
    later(monkeypatch, 40)
    result = verify(repo, note, verify_by=day(100))["note"]
    assert result["freshness"] == {"verify_by": day(100), "stale_after": None, "state": "current"}
    assert result["metadata"]["audit_action"] == ["save", "verify"]
    assert result["content"] == "Use three retries." and result["provenance"] == note["provenance"]


def test_verify_without_dates_clears_them(repo):
    note = save(repo, verify_by=day(30))
    result = verify(repo, note)["note"]
    assert "freshness" not in result
    assert "verify_by" not in Path(result["path"]).read_text()


def test_verify_requires_approval_and_refuses_superseded_notes(repo):
    old, new = save(repo, "Old"), save(repo, "New")
    with pytest.raises(MemoryRequestError, match="explicit user approval"):
        handle("verify", repo, {"id": old["id"], "source": "s", "recorded_by": "manual",
                                "confirmed": False, "confirmation": "c"})
    handle("supersede", repo, {"id": old["id"], "replacement_id": new["id"], "source": "s", **APPROVAL})
    with pytest.raises(MemoryRequestError, match="superseded notes cannot be verified"):
        verify(repo, old)


def test_verify_needs_obsidian_consent(repo):
    note = save(repo)
    policy.configure(repo, "graph")
    with pytest.raises(PolicyError):
        verify(repo, note)


# memory lint ---------------------------------------------------------------------

def issues(result):
    return sorted((finding["issue"], finding.get("id") or finding.get("file")) for finding in result["findings"])


def test_lint_of_healthy_memory_has_no_findings(repo):
    save(repo, verify_by=day(30))
    result = handle("lint", repo, {})
    assert result["findings"] == [] and result["checked"] == 1


def test_lint_reports_due_and_stale_notes(repo, monkeypatch):
    due = save(repo, "Due", verify_by=day(5))
    stale = save(repo, "Stale", verify_by=day(5), stale_after=day(10))
    later(monkeypatch, 20)
    assert issues(handle("lint", repo, {})) == sorted([("verify_due", due["id"]), ("stale", stale["id"])])


def test_lint_reports_unreadable_notes_and_proposals_on_deleted_branches(repo):
    broken = save(repo, "Broken")
    Path(broken["path"]).write_text("hand edited\n")
    git(repo, "switch", "-q", "-c", "feature")
    proposal = save(repo, "Feature idea", status="proposed")
    git(repo, "switch", "-q", "main")
    git(repo, "branch", "-q", "-D", "feature")
    assert issues(handle("lint", repo, {})) == sorted([
        ("unreadable", f"{broken['id']}.md"), ("branch_gone", proposal["id"])])


def test_lint_reports_a_supersede_link_to_a_missing_replacement(repo):
    old, new = save(repo, "Old"), save(repo, "New")
    handle("supersede", repo, {"id": old["id"], "replacement_id": new["id"], "source": "s", **APPROVAL})
    Path(new["path"]).unlink()
    assert ("dangling_replacement", old["id"]) in issues(handle("lint", repo, {}))


def test_lint_is_read_only_and_creates_nothing(repo, tmp_path):
    result = handle("lint", repo, {})
    assert result["findings"] == [] and result["checked"] == 0
    assert not (tmp_path / "vault").exists()


def test_lint_needs_obsidian_consent(repo):
    policy.configure(repo, "graph")
    with pytest.raises(PolicyError):
        handle("lint", repo, {})


def test_cli_lint_and_verify(repo, capsys, tmp_path):
    import json
    from graphify_worktree import cli
    note = save(repo, verify_by=day(5))
    assert cli.main(["memory", "lint", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []
    payload = tmp_path / "verify.json"
    payload.write_text(json.dumps({"id": note["id"], "source": "re-checked", "verify_by": day(40), **APPROVAL}))
    assert cli.main(["memory", "verify", str(repo), "--input-file", str(payload)]) == 0
    assert json.loads(capsys.readouterr().out)["note"]["freshness"]["verify_by"] == day(40)
