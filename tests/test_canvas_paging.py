"""Large-graph Canvas paging: one index level of at most 100 pages of 100 community notes."""
import json

import pytest

from graphify_worktree.errors import GraphError
from graphify_worktree.obsidian_export import export_canvas


class LargeGraph:
    """Only what the paged branch reads: more than 5,000 nodes."""

    def number_of_nodes(self):
        return 5001


def vault_with(tmp_path, communities):
    vault = tmp_path / "vault"
    (vault / "_graphify-worktree").mkdir(parents=True)
    files = [f"_COMMUNITY_{index:05}.md" for index in range(communities)] + ["helper.md"]
    (vault / ".graphify_obsidian_manifest.json").write_text(json.dumps({"files": files}))
    return vault


def links(path):
    return [node["text"] for node in json.loads(path.read_text())["nodes"] if node["text"].startswith("[[")]


@pytest.mark.parametrize("communities, pages", [(10_000, 100), (101, 2), (1, 1)])
def test_one_index_level_covers_every_community(tmp_path, communities, pages):
    vault = vault_with(tmp_path, communities)
    assert export_canvas(vault, LargeGraph(), {}, {}) == "community-index"
    written = sorted((vault / "_graphify-worktree").glob("communities-*.canvas"))
    assert [path.name for path in written] == [f"communities-0-{index:04}.canvas" for index in range(1, pages + 1)]
    assert links(vault / "graph.canvas") == [f"[[_graphify-worktree/{path.name}]]" for path in written]
    assert sum(len(links(path)) for path in written) == communities


def test_more_than_ten_thousand_communities_is_an_error_and_writes_no_pages(tmp_path):
    vault = vault_with(tmp_path, 10_001)
    with pytest.raises(GraphError, match="10,001 communities"):
        export_canvas(vault, LargeGraph(), {}, {})
    assert not list((vault / "_graphify-worktree").glob("communities-*.canvas"))
    assert not (vault / "graph.canvas").exists()


def test_page_temporary_symlink_cannot_overwrite_an_outside_file(tmp_path):
    vault = vault_with(tmp_path, 1)
    private = tmp_path / "private.json"
    private.write_text("private")
    temporary = vault / "_graphify-worktree/communities-0-0001.canvas.tmp"
    temporary.symlink_to(private)
    with pytest.raises(GraphError, match="symlink"):
        export_canvas(vault, LargeGraph(), {}, {})
    assert private.read_text() == "private"
    assert temporary.is_symlink()
