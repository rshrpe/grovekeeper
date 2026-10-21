"""Compare two worktrees' fresh graphs of one repository (graphify-worktree diff A B).

Node ids do not depend on where a checkout lives, so nodes are matched by id
and edges by (source, target, relation). Each side is read inside its own
guarded_graph, so a side whose sources change during its read is withheld
(graph_unavailable) rather than compared.
"""

from pathlib import Path

from graphify_worktree import artifacts, gitutil
from graphify_worktree.errors import GraphError
from graphify_worktree.guard import guarded_graph
from graphify_worktree.policy import resolve_root


SIDE = ("worktree_root", "branch", "head", "generation", "scope")


def _read(root, once):
    with guarded_graph(root, once=once) as status:
        data = artifacts.load_json(Path(status["graph_path"]))
        side = {key: status.get(key) for key in SIDE}
    nodes = {node["id"]: node for node in data.get("nodes", [])}
    edges = {(edge["source"], edge["target"], edge.get("relation")) for edge in data.get("links", data.get("edges", []))}
    return side, nodes, edges


def _node(node):
    return {"id": node["id"], "label": node.get("label"),
            "source_file": node.get("source_file"), "source_location": node.get("source_location")}


def _edge(edge):
    return {"source": edge[0], "target": edge[1], "relation": edge[2]}


def compare(a, b, *, once=False, limit=20):
    """JSON-compatible differences from worktree a to worktree b."""
    root_a, root_b = resolve_root(a), resolve_root(b)
    if root_a == root_b:
        raise GraphError("A and B are the same worktree; pass two worktrees of one repository")
    if gitutil.RepoIdentity.of(root_a).common_git_dir != gitutil.RepoIdentity.of(root_b).common_git_dir:
        raise GraphError("A and B must be worktrees of the same repository")
    side_a, nodes_a, edges_a = _read(root_a, once)
    side_b, nodes_b, edges_b = _read(root_b, once)

    added = sorted(nodes_b.keys() - nodes_a.keys())
    removed = sorted(nodes_a.keys() - nodes_b.keys())
    where = lambda node: (node.get("source_file"), node.get("source_location"))
    moved = sorted(key for key in nodes_a.keys() & nodes_b.keys() if where(nodes_a[key]) != where(nodes_b[key]))
    added_edges, removed_edges = sorted(edges_b - edges_a, key=str), sorted(edges_a - edges_b, key=str)
    lists = (added, removed, moved, added_edges, removed_edges)
    warnings = []
    if side_a["scope"] != side_b["scope"]:
        warnings.append(f"the graphs cover different scopes ({side_a['scope']!r} and {side_b['scope']!r}); "
                        "differences include what one scope leaves out")
    return {
        "a": side_a, "b": side_b,
        "nodes": {"added": len(added), "removed": len(removed), "moved": len(moved),
                  "unchanged": len(nodes_a.keys() & nodes_b.keys()) - len(moved)},
        "edges": {"added": len(added_edges), "removed": len(removed_edges)},
        "examples": {
            "added_nodes": [_node(nodes_b[key]) for key in added[:limit]],
            "removed_nodes": [_node(nodes_a[key]) for key in removed[:limit]],
            "moved_nodes": [{"id": key, "label": nodes_b[key].get("label"),
                             "from": _node(nodes_a[key]), "to": _node(nodes_b[key])} for key in moved[:limit]],
            "added_edges": [_edge(edge) for edge in added_edges[:limit]],
            "removed_edges": [_edge(edge) for edge in removed_edges[:limit]],
        },
        "truncated": any(len(items) > limit for items in lists),
        "warnings": warnings,
    }
