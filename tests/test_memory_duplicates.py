"""A save whose title nearly matches a current note is refused until the user confirms it."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from graphify_worktree import policy
from graphify_worktree.errors import MemoryRequestError
from graphify_worktree.memory import handle, service

from integration import isolated_env

APPROVAL = {"recorded_by": "manual", "confirmed": True, "confirmation": "test approval"}


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
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    policy.configure(root, "obsidian")
    return root.resolve()


def save(root, title, status="accepted", **extra):
    return handle("save", root, {"kind": "fact", "title": title, "content": "Body.", "source": "test",
                                 "status": status, **APPROVAL, **extra})


def stored(tmp_path):
    return sorted(path.name for path in (tmp_path / "vault").rglob("*.md"))


@pytest.mark.parametrize("first, second, duplicate", [
    ("Retry ceiling is three", "retry ceiling is three!", True),
    ("Retry ceiling is three", "Retry ceiling is 3", True),
    ("Use three retries", "Use three workers", False),
    ("Cache keys include the region", "Cache keys include region and tenant", True),
    ("Retries", "Retries", True),
    ("Database migrations run on deploy", "Retry ceiling is three", False),
    ("Retry policy", "Retry policy for webhooks differs", False),
])
def test_title_similarity(first, second, duplicate):
    assert service.similar_titles(first, second) is duplicate


def test_a_near_duplicate_save_is_refused_and_writes_nothing(repo, tmp_path):
    existing = save(repo, "Retry ceiling is three")["note"]
    before = stored(tmp_path)
    with pytest.raises(MemoryRequestError, match="possible duplicate") as refused:
        save(repo, "Retry ceiling is 3")
    assert stored(tmp_path) == before
    assert [(duplicate["id"], duplicate["title"], duplicate["status"]) for duplicate in refused.value.duplicates] == [
        (existing["id"], "Retry ceiling is three", "accepted")]
    assert "duplicate_ok" in str(refused.value)


def test_duplicate_ok_saves_anyway_with_a_warning(repo):
    existing = save(repo, "Retry ceiling is three")["note"]
    result = save(repo, "Retry ceiling is 3", duplicate_ok=True)
    assert result["note"]["title"] == "Retry ceiling is 3"
    assert any(existing["id"] in warning and "duplicate" in warning for warning in result["warnings"])


def test_superseded_notes_do_not_count_but_other_branch_proposals_do(repo):
    old = save(repo, "Retry ceiling is two")["note"]
    new = save(repo, "Use three retries")["note"]
    handle("supersede", repo, {"id": old["id"], "replacement_id": new["id"], "source": "s", **APPROVAL})
    save(repo, "Retry ceiling is 2")  # only the superseded note is similar
    git(repo, "switch", "-q", "-c", "feature")
    proposal = save(repo, "Workers drain the queue", status="proposed")["note"]
    git(repo, "switch", "-q", "main")
    with pytest.raises(MemoryRequestError) as refused:
        save(repo, "Workers drain the queue first")
    assert [(duplicate["id"], duplicate["applicable"]) for duplicate in refused.value.duplicates] == [
        (proposal["id"], False)]


def test_unreadable_notes_do_not_block_a_save(repo):
    broken = save(repo, "Retry ceiling is three")["note"]
    Path(broken["path"]).write_text("hand edited\n")
    assert save(repo, "Retry ceiling is 3")["note"]


def test_duplicate_ok_must_be_boolean(repo):
    with pytest.raises(MemoryRequestError, match="duplicate_ok must be a boolean"):
        save(repo, "Anything", duplicate_ok="yes")


def test_lint_reports_each_near_duplicate_pair_once(repo):
    first = save(repo, "Retry ceiling is three")["note"]
    second = save(repo, "Retry ceiling is 3", duplicate_ok=True)["note"]
    save(repo, "Unrelated decision")
    findings = [finding for finding in handle("lint", repo, {})["findings"] if finding["issue"] == "near_duplicate"]
    assert len(findings) == 1
    assert {findings[0]["id"], findings[0]["duplicate_of"]} == {first["id"], second["id"]}


def test_cli_refusal_exits_1_and_names_the_duplicate(repo, tmp_path):
    existing = save(repo, "Retry ceiling is three")["note"]
    env = isolated_env(tmp_path / "home", WORKTREE_KNOWLEDGE_MEMORY_VAULT=str(tmp_path / "vault"))
    payload = {"kind": "fact", "title": "Retry ceiling is 3", "content": "c", "source": "s", **APPROVAL}
    result = subprocess.run([sys.executable, "-m", "graphify_worktree.cli", "memory", "save", str(repo),
                             "--input-file", "-"], input=json.dumps(payload), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 1 and existing["id"] in result.stderr and result.stdout == ""


@pytest.mark.integration
def test_mcp_refusal_returns_the_duplicates(repo, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    existing = save(repo, "Retry ceiling is three")["note"]
    env = isolated_env(tmp_path / "home", WORKTREE_KNOWLEDGE_MEMORY_VAULT=str(tmp_path / "vault"))

    async def call():
        command = Path(sys.executable).parent / "graphify-worktree-mcp"
        async with stdio_client(StdioServerParameters(command=str(command), env=env)) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                return await session.call_tool("memory_save", {
                    "project_path": str(repo), "kind": "fact", "title": "Retry ceiling is 3",
                    "content": "c", "source": "s", **APPROVAL})
    reply = asyncio.run(call())
    payload = json.loads(reply.content[0].text)
    assert reply.is_error and payload["error"] == "invalid_memory_request"
    assert [duplicate["id"] for duplicate in payload["duplicates"]] == [existing["id"]]
