"""Freshness decision, independent of Graphify and the filesystem."""
import pytest

from graphify_worktree.errors import GraphError
from graphify_worktree.freshness import Generation, classify

ROOT = "/work/repo"
SAVED = {
    "schema": 1, "worktree_root": ROOT, "scope": ".", "branch": "main", "head": "abc",
    "graphify_version": "0.9.93", "manifest": {"a.py": "1"}, "fingerprint": "f1", "excludes": [],
    "extracted_at": "2026-10-21T10:00:00+00:00", "graph_sha256": "g1", "report_sha256": "r1",
}
CURRENT = {key: SAVED[key] for key in ("branch", "head", "worktree_root", "scope", "graphify_version",
                                       "manifest", "fingerprint", "excludes")}


def decide(saved=SAVED, *, root=ROOT, graph="g1", report="r1", current=None, event=None):
    return classify(saved, root=root, graph_sha256=graph, report_sha256=report,
                    current=current or (lambda: dict(CURRENT)), event=event)


def test_matching_sources_and_artifacts_are_fresh():
    assert decide() is None


@pytest.mark.parametrize("name", [".graphify_analysis.json", ".graphify_labels.json", ".graphify_learning.json"])
def test_sidecar_symlinks_are_refused_before_upstream_reads(tmp_path, name):
    from graphify_worktree import artifacts

    output = tmp_path / "graphify-out"
    output.mkdir()
    private = tmp_path / "private.json"
    private.write_text("{}")
    (output / name).symlink_to(private)
    with pytest.raises(GraphError, match="symlink"):
        artifacts.check_artifacts(output)


def test_graph_from_another_worktree_is_stale():
    assert decide(root="/work/other") == "graph belongs to a different worktree"


@pytest.mark.parametrize("graph, report, reason", [
    ("edited", "r1", "graph changed outside graphify-worktree"),
    (None, "r1", "graph changed outside graphify-worktree"),
    ("g1", None, "graph report is missing or changed"),
])
def test_tampered_or_missing_artifacts_are_stale(graph, report, reason):
    assert decide(graph=graph, report=report) == reason


def test_artifact_checks_run_before_the_expensive_source_scan():
    def scan():
        raise AssertionError("sources must not be scanned for a tampered graph")
    assert decide(graph="edited", current=scan) == "graph changed outside graphify-worktree"


@pytest.mark.parametrize("changes, reason", [
    ({"head": "def"}, "changed: head"),
    ({"branch": None}, "changed: branch"),
    ({"manifest": {"a.py": "2"}, "fingerprint": "f2"}, "changed: fingerprint"),
    ({"graphify_version": "0.9.94"}, "changed: graphify_version"),
    ({"scope": "src", "head": "def"}, "changed: head, scope"),
])
def test_source_or_checkout_changes_are_stale(changes, reason):
    assert decide(current=lambda: {**CURRENT, **changes}) == reason


def test_scan_failure_is_stale_with_its_reason():
    def scan():
        raise GraphError("source changed while hashing: a.py")
    assert decide(current=scan) == "source changed while hashing: a.py"


def test_event_for_this_generation_invalidates_it():
    event = {"event": "post-merge", "generation": SAVED["extracted_at"]}
    assert decide(event=event) == "Git event invalidated this generation: post-merge"


def test_event_for_an_older_generation_is_ignored():
    assert decide(event={"event": "post-merge", "generation": "2026-10-20T00:00:00+00:00"}) is None


def test_generation_identity_covers_what_a_reader_depends_on():
    status = {"generation": "t1", "fingerprint": "f1", "graph_sha256": "g1", "scope": ".", "reason": "x"}
    assert Generation.of(status) == Generation.of({**status, "reason": "y"})
    for key in ("generation", "fingerprint", "graph_sha256", "scope"):
        assert Generation.of(status) != Generation.of({**status, key: "changed"})
