"""Opt-in incremental rebuilds: seeded upstream extract, same gates, full extract on any doubt."""
import json
import os
from pathlib import Path
import shutil

import pytest

from graphify_worktree import artifacts, cli, incremental, settings, upstream
from graphify_worktree.guard import ensure

pytestmark = [pytest.mark.integration, pytest.mark.repo(mode="graph", files={
    "pkg/a.py": "from pkg.b import beta\n\n\ndef alpha():\n    return beta()\n",
    "pkg/b.py": "def beta():\n    return 1\n",
    "lib/c.py": "def gamma():\n    return 3\n",
})]


@pytest.fixture(autouse=True)
def incremental_on(monkeypatch):
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_INCREMENTAL", "1")

SEEDS = ".graphify-worktree-seed"


def build(repo):
    return artifacts.load_provenance(repo / "graphify-out").get("build")


def graph(repo):
    data = json.loads((repo / "graphify-out/graph.json").read_text())
    nodes = {node["id"]: {key: value for key, value in node.items() if not key.startswith("community")}
             for node in data["nodes"]}
    edges = {(edge["source"], edge["target"], edge.get("relation")) for edge in data.get("links", data.get("edges"))}
    return nodes, edges


def labels(repo):
    return {node["label"] for node in graph(repo)[0].values()}


def seeds(repo):
    directory = repo / "graphify-out" / SEEDS
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def full_rebuild_of(repo, monkeypatch, capfd):
    """The graph a full extract produces for the current sources (incremental disabled)."""
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_INCREMENTAL")
    assert cli.main(["refresh", str(repo)]) == 0
    capfd.readouterr()
    monkeypatch.setenv("WORKTREE_KNOWLEDGE_INCREMENTAL", "1")
    return graph(repo)


def test_the_setting_is_on_only_for_1(monkeypatch):
    for value, enabled in (("1", True), ("0", False), ("", False), ("true", False)):
        monkeypatch.setenv("WORKTREE_KNOWLEDGE_INCREMENTAL", value)
        assert settings.incremental() is enabled
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_INCREMENTAL")
    assert settings.incremental() is False


def test_disabled_builds_are_unchanged_full_builds_without_seeds(repo, monkeypatch):
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_INCREMENTAL")
    ensure(repo)
    (repo / "pkg/b.py").write_text("def beta():\n    return 2\n")
    ensure(repo)
    assert build(repo) is None and seeds(repo) == []


def test_first_build_is_full_then_an_edit_is_rebuilt_incrementally(repo, monkeypatch, capfd):
    ensure(repo)
    assert build(repo)["mode"] == "full" and len(seeds(repo)) == 1
    (repo / "pkg/b.py").write_text("def beta():\n    return 2\n\n\ndef delta():\n    return 4\n")
    status = ensure(repo)
    assert status["rebuilt"] is True
    assert build(repo) == {"mode": "incremental", "since_full": 1, "seed": seeds(repo)[0], "reason": None}
    assert len(seeds(repo)) == 1, "older seeds are removed"
    assert "delta()" in labels(repo)
    assert ensure(repo)["rebuilt"] is False, "the published generation passes the fingerprint gate"
    incremental_graph = graph(repo)
    assert incremental_graph == full_rebuild_of(repo, monkeypatch, capfd)


def test_a_content_change_that_keeps_mtime_and_size_is_still_reextracted(repo):
    ensure(repo)
    source = repo / "lib/c.py"
    stat = source.stat()
    source.write_text("def kappa():\n    return 3\n")  # same length as before
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source.stat().st_size == stat.st_size and source.stat().st_mtime_ns == stat.st_mtime_ns
    ensure(repo)
    assert build(repo)["mode"] == "incremental"
    assert "kappa()" in labels(repo) and "gamma()" not in labels(repo)


def test_deleted_and_added_files_are_reflected(repo):
    ensure(repo)
    (repo / "lib/c.py").unlink()
    (repo / "lib/d.py").write_text("def epsilon():\n    return 5\n")
    ensure(repo)
    assert build(repo)["mode"] == "incremental"
    assert "epsilon()" in labels(repo) and "gamma()" not in labels(repo)


@pytest.mark.parametrize("doubt, reason", [
    ("missing seed", "seed"),
    ("graphify version", "Graphify version"),
    ("scope", "scope"),
    ("worktree", "worktree"),
])
def test_any_doubt_about_the_seed_means_a_full_extract(repo, monkeypatch, capfd, doubt, reason):
    ensure(repo)
    output = repo / "graphify-out"
    if doubt == "missing seed":
        shutil.rmtree(output / SEEDS)
    elif doubt == "graphify version":
        current = upstream.graphify_version()
        monkeypatch.setattr(upstream, "graphify_version", lambda: current + ".post1")
    elif doubt == "worktree":
        saved = artifacts.load_provenance(output)
        artifacts.write_json(output / artifacts.PROVENANCE, {**saved, "worktree_root": "/elsewhere"})
    if doubt == "scope":
        assert cli.main(["refresh", str(repo), "--scope", "pkg"]) == 0
        capfd.readouterr()
    else:
        (repo / "pkg/b.py").write_text("def beta():\n    return 2\n")
        ensure(repo)
    monkeypatch.undo()
    assert build(repo)["mode"] == "full" and reason in build(repo)["reason"]


def test_a_full_extract_is_forced_after_a_run_of_incremental_builds(repo, monkeypatch):
    monkeypatch.setattr(incremental, "FULL_EVERY", 3)
    modes = []
    for number in range(4):
        (repo / "pkg/b.py").write_text(f"def beta():\n    return {number}\n")
        ensure(repo)
        modes.append((build(repo)["mode"], build(repo)["since_full"]))
    assert modes == [("full", 0), ("incremental", 1), ("incremental", 2), ("full", 0)]


def test_a_failing_incremental_extract_falls_back_to_a_full_one(repo):
    ensure(repo)
    seed = repo / "graphify-out" / SEEDS / seeds(repo)[0]
    (seed / incremental.RAW_GRAPH).write_text("{ not json")
    (repo / "pkg/b.py").write_text("def beta():\n    return 2\n\n\ndef delta():\n    return 4\n")
    status = ensure(repo)
    assert status["state"] == "fresh" and "delta()" in labels(repo)
    assert build(repo)["mode"] == "full" and "incremental extract failed" in build(repo)["reason"]


def test_disabling_removes_seeds_on_the_next_build(repo, monkeypatch):
    ensure(repo)
    assert seeds(repo)
    monkeypatch.delenv("WORKTREE_KNOWLEDGE_INCREMENTAL")
    (repo / "pkg/b.py").write_text("def beta():\n    return 2\n")
    ensure(repo)
    assert seeds(repo) == [] and build(repo) is None


def test_seeds_never_touch_work_memory(repo):
    ensure(repo)
    memory = repo / "graphify-out/memory"
    memory.mkdir()
    (memory / "note.md").write_text("kept\n")
    (repo / "pkg/b.py").write_text("def beta():\n    return 2\n")
    ensure(repo)
    assert (memory / "note.md").read_text() == "kept\n"
