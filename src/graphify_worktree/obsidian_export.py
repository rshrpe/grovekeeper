"""Export a fresh worktree graph into its own generated Obsidian vault."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from graphify_worktree import artifacts, export_state, settings, upstream
from graphify_worktree.errors import GraphError
from graphify_worktree.freshness import inspect


PAGE = 100  # links per Canvas page; graph.canvas links at most PAGE pages of PAGE communities

def export_canvas(vault, graph, communities, labels):
    if graph.number_of_nodes() <= 5000:
        upstream.to_canvas(graph, communities, str(vault / "graph.canvas"), community_labels=labels)
        mode = "symbols"
    else:
        # Obsidian timed out on the 96k-node checkout. Page community links
        # instead of rendering every symbol; all native notes and MCP data remain.
        targets = sorted(name for name in artifacts.load_json(vault / ".graphify_obsidian_manifest.json")["files"]
                         if name.startswith("_COMMUNITY_") and name.endswith(".md"))
        if len(targets) > PAGE * PAGE:
            raise GraphError(f"{len(targets):,} communities exceed the {PAGE * PAGE:,} a one-level Canvas index "
                             "can link; the community notes and MCP still cover the whole graph")

        def page(path, links, overview=False):
            nodes = [
                {"id": hashlib.sha256(link.encode()).hexdigest()[:16],
                 "type": "text", "text": "[[" + link + "]]",
                 "x": (index % 10) * 340, "y": (index // 10) * 180,
                 "width": 300, "height": 140}
                for index, link in enumerate(links)
            ]
            if overview:
                nodes.insert(0, {"id": "0000000000000000", "type": "text",
                                "text": "# Community index\n\nOpen a page, then a community note to follow its symbol links. "
                                        "The full graph remains available through Graphify MCP.",
                                "x": 0, "y": -240, "width": 660, "height": 180})
            temporary = path.with_suffix(".canvas.tmp")
            artifacts.check_artifacts(path.parent, (temporary.name,))
            try:
                artifacts.write_json(temporary, {"nodes": nodes, "edges": []})
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

        links = []
        for index, start in enumerate(range(0, len(targets), PAGE)):
            relative = f"_graphify-worktree/communities-0-{index + 1:04}.canvas"
            destination = vault / relative
            page(destination, targets[start:start + PAGE])
            links.append(relative)
        page(vault / "graph.canvas", links, overview=True)
        mode = "community-index"
    return mode


def export(root, output, scratch, *, wiki=False):
    """Stage the fresh graph, then publish only verified generated files.

    A graph that is not fresh is not exported: its status is returned instead.
    wiki=True includes upstream's wiki; otherwise unchanged owned wiki pages
    from the previous export are pruned, leaving authored content alone.
    """
    status = inspect(root, output, scratch)
    if status["state"] != "fresh":
        return status
    vault = Path(status["vault_path"])
    export_state.claim(root, vault)
    try:
        data = artifacts.load_json(output / "graph.json")
        analysis = artifacts.load_json(output / ".graphify_analysis.json")
        communities = {int(key): members for key, members in analysis["communities"].items()}
        cohesion = {int(key): value for key, value in analysis["cohesion"].items()}
        labels = {int(key): value for key, value in artifacts.load_json(output / ".graphify_labels.json").items()}
    except (KeyError, TypeError, ValueError) as error:
        raise GraphError(f"graph analysis artifacts are incomplete ({error!r}); refresh the graph") from error
    graph = upstream.build_graph(data)
    provenance = artifacts.load_json(output / artifacts.PROVENANCE)
    articles = None
    with export_state.staging(vault) as staged:
        report_dir = staged / "_graphify-worktree"
        report_dir.mkdir(mode=0o700)
        upstream.to_obsidian(graph, communities, str(staged), community_labels=labels, cohesion=cohesion)
        canvas_mode = export_canvas(staged, graph, communities, labels)
        shutil.copyfile(output / "GRAPH_REPORT.md", report_dir / "GRAPH_REPORT.md")
        if wiki:
            try:
                articles = upstream.to_wiki(graph, communities, str(report_dir / "wiki"),
                                            community_labels=labels, cohesion=cohesion,
                                            god_nodes_data=analysis.get("gods") or upstream.god_nodes(graph))
            except ValueError as error:
                raise GraphError(f"wiki export failed: {error}") from error
        published = export_state.publish(root, vault, staged, provenance)
    result = {"state": "exported", "vault_path": str(vault), "canvas_mode": canvas_mode, **published}
    if wiki:
        result.update(wiki_path=str(vault / "_graphify-worktree/wiki"), wiki_articles=articles)
    return result


def open_in_obsidian(vault):
    """Register the vault with the running Obsidian and open its report."""
    # vault-message uses Obsidian's native startup queue. A second CLI call
    # can arrive before a new vault's CLI handlers have registered.
    code = ('(()=>{const ipc=require("electron").ipcRenderer;const path=' + json.dumps(str(vault)) +
            ';const opened=ipc.sendSync("vault-open",path,false);'
            'if(opened!==true)throw Error(String(opened));'
            'ipc.sendSync("vault-message",path,{action:"open",file:"_graphify-worktree/GRAPH_REPORT.md"});return true})()')
    registration = subprocess.run(
        [str(settings.obsidian_cli()), "eval", "code=" + code],
        capture_output=True, text=True,
    )
    if registration.returncode or registration.stdout.strip() != "=> true":
        raise GraphError("vault exported, but Obsidian registration failed: " + (registration.stderr or registration.stdout).strip())
