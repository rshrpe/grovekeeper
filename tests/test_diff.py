"""graphify-worktree diff A B: two fresh worktree graphs of one repository, compared by node id."""
import json
import subprocess
import sys

import pytest

from integration import isolated_env

pytestmark = pytest.mark.integration

BASE = "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"
FEATURE = "def new_func():\n    return 0\n\n\ndef helper():\n    return 1\n"


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                   check=True, capture_output=True)


def cli(env, *args, expected=0):
    result = subprocess.run([sys.executable, "-m", "graphify_worktree.cli", *map(str, args)],
                            env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    return result


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return isolated_env(home, WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT=str(tmp_path / "vaults"))


@pytest.fixture
def worktrees(tmp_path, env):
    main = tmp_path / "main"
    (main / "pkg").mkdir(parents=True)
    (main / "pkg/core.py").write_text(BASE)
    git(main, "init", "-q", "-b", "main")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "base")
    feature = tmp_path / "feature"
    git(main, "worktree", "add", "-q", "-b", "feature", str(feature))
    (feature / "pkg/core.py").write_text(FEATURE)
    cli(env, "configure", "graph", main)
    return main.resolve(), feature.resolve()


def diff(env, a, b, *flags, expected=0):
    return json.loads(cli(env, "diff", a, b, *flags, expected=expected).stdout)


def test_added_removed_and_moved_nodes_and_edges(worktrees, env):
    main, feature = worktrees
    result = diff(env, main, feature)
    assert (result["a"]["worktree_root"], result["b"]["worktree_root"]) == (str(main), str(feature))
    assert (result["a"]["branch"], result["b"]["branch"]) == ("main", "feature")
    assert result["nodes"]["added"] == 1 and result["nodes"]["removed"] == 1 and result["nodes"]["moved"] == 1
    assert [node["label"] for node in result["examples"]["added_nodes"]] == ["new_func()"]
    assert [node["label"] for node in result["examples"]["removed_nodes"]] == ["caller()"]
    moved = result["examples"]["moved_nodes"]
    assert [(node["label"], node["from"]["source_location"], node["to"]["source_location"]) for node in moved] == [
        ("helper()", "L1", "L5")]
    removed_edges = {(edge["source"], edge["target"], edge["relation"]) for edge in result["examples"]["removed_edges"]}
    assert ("pkg_core_caller", "pkg_core_helper", "calls") in removed_edges
    assert result["edges"]["added"] == len(result["examples"]["added_edges"]) >= 1
    assert result["truncated"] is False and result["warnings"] == []


def test_identical_worktrees_have_no_differences(worktrees, env):
    main, feature = worktrees
    (feature / "pkg/core.py").write_text(BASE)
    result = diff(env, main, feature)
    assert result["nodes"] == {"added": 0, "removed": 0, "moved": 0, "unchanged": result["nodes"]["unchanged"]}
    assert result["edges"] == {"added": 0, "removed": 0}


def test_both_graphs_are_built_on_demand_and_left_fresh(worktrees, env):
    main, feature = worktrees
    diff(env, main, feature)
    assert json.loads(cli(env, "status", main).stdout)["state"] == "fresh"
    assert json.loads(cli(env, "status", feature).stdout)["state"] == "fresh"


def test_examples_are_capped(worktrees, env):
    main, feature = worktrees
    (feature / "pkg/many.py").write_text("".join(f"def f{index}():\n    return {index}\n\n\n" for index in range(8)))
    result = diff(env, main, feature, "--limit", "3")
    assert result["nodes"]["added"] >= 9 and len(result["examples"]["added_nodes"]) == 3
    assert result["truncated"] is True


def test_different_scopes_are_flagged(worktrees, env):
    main, feature = worktrees
    (main / "lib").mkdir()
    (main / "lib/x.py").write_text("def x():\n    return 1\n")
    cli(env, "refresh", main, "--scope", "pkg")
    assert any("scope" in warning for warning in diff(env, main, feature)["warnings"])


def test_the_same_worktree_or_another_repository_is_refused(worktrees, env, tmp_path):
    main, _ = worktrees
    assert "same worktree" in cli(env, "diff", main, main / "pkg", expected=1).stderr
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    assert "same repository" in cli(env, "diff", main, other, expected=1).stderr


def test_needs_graph_consent_and_once_works(worktrees, env):
    main, feature = worktrees
    cli(env, "configure", "off", main)
    cli(env, "diff", main, feature, expected=5)
    assert not (feature / "graphify-out").exists()
    diff(env, main, feature, "--once")


def test_a_negative_limit_is_a_usage_error(worktrees, env):
    main, feature = worktrees
    assert "must not be negative" in cli(env, "diff", main, feature, "--limit", "-1", expected=2).stderr
