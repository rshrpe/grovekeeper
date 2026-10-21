"""export --wiki: upstream wiki articles inside the worktree's generated vault only."""
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from integration import isolated_env

pytestmark = pytest.mark.integration


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *args],
                   check=True, capture_output=True)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return isolated_env(home, WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT=str(tmp_path / "vaults/Graphify"),
                        WORKTREE_KNOWLEDGE_MEMORY_VAULT=str(tmp_path / "vaults/Engineering"))


@pytest.fixture
def repo(tmp_path, env):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/core.py").write_text("def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n")
    (root / "pkg/other.py").write_text("from pkg.core import helper\n\n\ndef user():\n    return helper()\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    cli(env, "configure", "obsidian", root)
    return root.resolve()


def cli(env, *args, expected=0):
    result = subprocess.run([sys.executable, "-m", "graphify_worktree.cli", *map(str, args)],
                            env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    return result


def export(env, repo, *flags, expected=0):
    return json.loads(cli(env, "export", repo, *flags, expected=expected).stdout)


def test_wiki_articles_are_written_inside_the_generated_vault(repo, env, tmp_path):
    cli(env, "ensure", repo)
    result = export(env, repo, "--wiki")
    vault = Path(result["vault_path"])
    wiki = Path(result["wiki_path"])
    assert result["state"] == "exported" and wiki == vault / "_graphify-worktree/wiki"
    assert (wiki / "index.md").is_file() and result["wiki_articles"] >= 1
    assert result["wiki_articles"] == len(list(wiki.glob("*.md"))) - 1
    for target in re.findall(r"\]\(([^)]+\.md)\)", (wiki / "index.md").read_text()):
        assert (wiki / target).is_file(), f"index links to a missing article: {target}"
    assert not (tmp_path / "vaults/Engineering").exists(), "the authored vault is never written"


def test_export_without_wiki_is_unchanged_and_removes_an_earlier_wiki(repo, env):
    cli(env, "ensure", repo)
    plain = export(env, repo)
    assert "wiki_path" not in plain
    vault = Path(plain["vault_path"])
    assert not (vault / "_graphify-worktree/wiki").exists()
    export(env, repo, "--wiki")
    (vault / "_graphify-worktree/wiki/keep.txt").write_text("not ours\n")
    export(env, repo)
    wiki = vault / "_graphify-worktree/wiki"
    assert list(wiki.glob("*.md")) == [] and (wiki / "keep.txt").read_text() == "not ours\n"


def test_a_stale_graph_is_not_exported_to_a_wiki(repo, env):
    cli(env, "ensure", repo)
    (repo / "pkg/core.py").write_text("def changed():\n    return 2\n")
    result = export(env, repo, "--wiki", expected=3)
    assert result["state"] == "stale" and "wiki_path" not in result


def test_wiki_follows_the_current_graph(repo, env):
    cli(env, "ensure", repo)
    first = export(env, repo, "--wiki")
    wiki = Path(first["wiki_path"])
    before = {path.name: path.read_text() for path in wiki.glob("*.md")}
    (repo / "pkg/extra.py").write_text("def brand_new_symbol():\n    return 3\n")
    cli(env, "ensure", repo)
    export(env, repo, "--wiki")
    after = {path.name: path.read_text() for path in wiki.glob("*.md")}
    assert after != before and "brand_new_symbol" in "".join(after.values())


def test_a_symlinked_wiki_directory_is_refused(repo, env, tmp_path):
    cli(env, "ensure", repo)
    vault = Path(export(env, repo)["vault_path"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (vault / "_graphify-worktree/wiki").symlink_to(elsewhere)
    result = cli(env, "export", repo, "--wiki", expected=1)
    assert "symlink" in result.stderr and list(elsewhere.iterdir()) == []


def test_wiki_needs_obsidian_consent(repo, env):
    cli(env, "configure", "graph", repo)
    cli(env, "export", repo, "--wiki", expected=5)
