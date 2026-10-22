"""Opt-in general-scope lessons in General/Memory/, shared across repositories."""
from pathlib import Path
import shutil

import pytest

from graphify_worktree import policy, recall
from graphify_worktree.errors import MemoryRequestError, PolicyError
from graphify_worktree.memory import handle


@pytest.fixture
def vault(tmp_path, make_repo, monkeypatch):
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_GENERAL_MEMORY", raising=False)
    return tmp_path / "vault"


@pytest.fixture
def repos(make_repo, vault):
    alpha, beta = make_repo("alpha", mode="obsidian"), make_repo("beta", mode="obsidian")
    for root in (alpha, beta):
        policy.configure(root, general_memory=True)
    return alpha, beta


APPROVAL = {"recorded_by": "manual", "confirmed": True, "confirmation": "test approval"}


def save(root, title, scope=None, status="accepted", content="Retries need jitter.", **extra):
    payload = {"kind": "lesson", "title": title, "content": content, "source": "test", "status": status,
               **APPROVAL, **extra}
    if scope:
        payload["scope"] = scope
    return handle("save", root, payload)


def search(root, query="retries jitter", **extra):
    return handle("search", root, {"query": query, **extra})["notes"]


# Saving --------------------------------------------------------------------------

def test_a_general_lesson_lives_outside_every_repository_namespace(repos, vault):
    note = save(repos[0], "Retries need jitter", "general")["note"]
    assert Path(note["path"]).parent == vault / "General/Memory"
    assert note["scope"] == "general" and note["applicability"] == "accepted for all repositories (general)"
    assert note["provenance"]["worktree"] == str(repos[0]), "the originating checkout is still recorded"


def test_general_lessons_must_be_accepted(repos):
    with pytest.raises(MemoryRequestError, match="general notes must be saved as accepted"):
        save(repos[0], "Retries need jitter", "general", status="proposed")


def test_repository_notes_are_unchanged(repos):
    note = save(repos[0], "Retries need jitter")["note"]
    assert "scope" not in note and "scope" not in Path(note["path"]).read_text()


def test_off_unless_enabled_for_the_repository(repos):
    save(repos[0], "Retries need jitter", "general")
    policy.configure(repos[1], general_memory=False)
    with pytest.raises(MemoryRequestError, match="general memory is off"):
        save(repos[1], "Timeouts need budgets", "general")
    assert search(repos[1]) == []
    assert [note["scope"] for note in search(repos[0])] == ["general"]


# Reading -------------------------------------------------------------------------

def test_other_repositories_find_general_lessons_after_their_own_notes(repos):
    general = save(repos[0], "Retries need jitter and backoff", "general",
                   content="Retries need jitter. Retries need backoff.")["note"]
    local = save(repos[1], "Queue consumer retries", content="Consumer retries use jitter.")["note"]
    found = search(repos[1])
    assert [note["id"] for note in found] == [local["id"], general["id"]]
    assert found[1]["scope"] == "general" and "scope" not in found[0]


def test_the_limit_covers_both_scopes(repos):
    save(repos[0], "Retries need jitter", "general")
    save(repos[1], "Queue consumer retries")
    assert len(search(repos[1], limit=1)) == 1


def test_get_and_verify_find_a_general_lesson_from_any_repository(repos):
    note = save(repos[0], "Retries need jitter", "general")["note"]
    assert handle("get", repos[1], {"id": note["id"]})["note"]["scope"] == "general"
    verified = handle("verify", repos[1], {"id": note["id"], "source": "re-checked", **APPROVAL})["note"]
    assert verified["metadata"]["audit_action"] == ["save", "verify"]


def test_supersede_stays_within_one_scope(repos):
    old = save(repos[0], "Retries need jitter", "general")["note"]
    new = save(repos[0], "Retry with full jitter", "general")["note"]
    local = save(repos[0], "Local retry rule")["note"]
    with pytest.raises(MemoryRequestError, match="same scope"):
        handle("supersede", repos[0], {"id": old["id"], "replacement_id": local["id"], "source": "s", **APPROVAL})
    with pytest.raises(MemoryRequestError, match="same scope"):
        handle("supersede", repos[0], {"id": local["id"], "replacement_id": new["id"], "source": "s", **APPROVAL})
    result = handle("supersede", repos[1], {"id": old["id"], "replacement_id": new["id"], "source": "s", **APPROVAL})
    assert result["note"]["status"] == "superseded"
    assert [note["id"] for note in search(repos[1], "jitter")] == [new["id"]]


def test_duplicates_are_checked_within_the_same_scope(repos):
    save(repos[0], "Retries need jitter", "general")
    with pytest.raises(MemoryRequestError, match="possible duplicate"):
        save(repos[1], "Retries need jitter!", "general")
    assert save(repos[1], "Retries need jitter")["note"], "a repository note is not blocked by a general one"


# Isolation -----------------------------------------------------------------------

def test_a_general_note_planted_in_a_repository_folder_is_rejected(repos):
    general = save(repos[0], "Retries need jitter", "general")["note"]
    local = save(repos[1], "Queue consumer retries")["note"]
    shutil.copy(general["path"], Path(local["path"]).parent / Path(general["path"]).name)
    result = handle("search", repos[1], {"query": "retries jitter"})
    assert [note["id"] for note in result["notes"]] == [local["id"], general["id"]]
    assert result["notes"][1]["path"] == general["path"], "only the copy in General/Memory is served"
    assert any(general["id"] in warning for warning in result["warnings"])


def test_a_repository_note_copied_into_general_is_rejected(repos, vault):
    local = save(repos[0], "Queue consumer retries")["note"]
    (vault / "General/Memory").mkdir(parents=True)
    shutil.copy(local["path"], vault / "General/Memory" / Path(local["path"]).name)
    result = handle("search", repos[1], {"query": "retries jitter"})
    assert result["notes"] == [] and any(local["id"] in warning for warning in result["warnings"])


def test_general_lessons_need_obsidian_consent_in_the_current_repository(repos):
    note = save(repos[0], "Retries need jitter", "general")["note"]
    policy.configure(repos[1], "graph")
    with pytest.raises(PolicyError):
        handle("get", repos[1], {"id": note["id"]})


def test_recall_lint_and_scope_default_stay_repository_only(repos):
    save(repos[0], "Retries need jitter", "general")
    assert recall.session_context(repos[1]) == ""
    assert handle("lint", repos[1], {})["checked"] == 0
