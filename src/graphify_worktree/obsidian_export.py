"""Export a fresh worktree graph into its own generated Obsidian vault."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from graphify_worktree import artifacts, settings, upstream
from graphify_worktree.errors import GraphError
from graphify_worktree.freshness import inspect


def export_canvas(vault, graph, communities, labels):
    if graph.number_of_nodes() <= 5000:
        upstream.to_canvas(graph, communities, str(vault / "graph.canvas"), community_labels=labels)
        mode = "symbols"
        keep = set()
    else:
        # Obsidian timed out on the 96k-node checkout. Page community links
        # instead of rendering every symbol; all native notes and MCP data remain.
        targets = sorted(name for name in artifacts.load_json(vault / ".graphify_obsidian_manifest.json")["files"]
                         if name.startswith("_COMMUNITY_") and name.endswith(".md"))
        keep = set()

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
            try:
                artifacts.write_json(temporary, {"nodes": nodes, "edges": []})
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

        level = 0
        while True:
            links = []
            for index, start in enumerate(range(0, len(targets), 100)):
                relative = f"_graphify-worktree/communities-{level}-{index + 1:04}.canvas"
                destination = vault / relative
                page(destination, targets[start:start + 100])
                keep.add(destination)
                links.append(relative)
            if len(links) <= 100:
                page(vault / "graph.canvas", links, overview=True)
                break
            targets = links
            level += 1
        mode = "community-index"
    for previous in (vault / "_graphify-worktree").glob("communities-*.canvas"):
        if previous not in keep:
            previous.unlink()
    return mode


def export(root, output, scratch, *, wiki=False):
    """Write the fresh graph into its own generated vault; returns the result JSON.

    A graph that is not fresh is not exported: its status is returned instead.
    wiki=True also writes upstream's wiki articles to _graphify-worktree/wiki/,
    a directory upstream owns outright (it deletes pages it did not produce).
    Without it, a wiki left by an earlier export is removed rather than left
    stale beside a fresh export.
    """
    status = inspect(root, output, scratch)
    if status["state"] != "fresh":
        return status
    vault = Path(status["vault_path"])
    if vault.is_symlink() or vault.resolve() != vault:
        raise GraphError("generated vault path must not traverse symlinks")
    if vault.exists() and any(vault.iterdir()):
        try:
            owner = artifacts.load_json(vault / artifacts.PROVENANCE)
        except (OSError, ValueError):
            raise GraphError(f"refusing to overwrite an unrelated vault: {vault}") from None
        if not isinstance(owner, dict) or owner.get("worktree_root") != str(root):
            raise GraphError(f"vault belongs to another worktree: {vault}")
    vault.mkdir(parents=True, exist_ok=True)
    marker = vault / artifacts.PROVENANCE
    if not marker.exists():
        # Claim an empty generated vault before export so a failed native export
        # remains safely retryable, without claiming it completed.
        artifacts.write_json(marker, {"schema": artifacts.SCHEMA, "worktree_root": str(root), "export_state": "pending"})
    report_dir = vault / "_graphify-worktree"
    if report_dir.is_symlink():
        raise GraphError("vault report directory must not be a symlink")
    report_dir.mkdir(exist_ok=True)
    try:
        data = artifacts.load_json(output / "graph.json")
        analysis = artifacts.load_json(output / ".graphify_analysis.json")
        communities = {int(key): members for key, members in analysis["communities"].items()}
        cohesion = {int(key): value for key, value in analysis["cohesion"].items()}
        labels = {int(key): value for key, value in artifacts.load_json(output / ".graphify_labels.json").items()}
    except (KeyError, TypeError, ValueError) as error:
        raise GraphError(f"graph analysis artifacts are incomplete ({error!r}); refresh the graph") from error
    graph = upstream.build_graph(data)
    upstream.to_obsidian(graph, communities, str(vault), community_labels=labels, cohesion=cohesion)
    canvas_mode = export_canvas(vault, graph, communities, labels)
    if (report_dir / "GRAPH_REPORT.md").is_symlink():
        raise GraphError("vault report must not be a symlink")
    shutil.copyfile(output / "GRAPH_REPORT.md", report_dir / "GRAPH_REPORT.md")
    wiki_dir = report_dir / "wiki"
    if wiki_dir.is_symlink():
        raise GraphError("generated wiki directory must not be a symlink")
    articles = None
    if wiki:
        try:
            articles = upstream.to_wiki(graph, communities, str(wiki_dir), community_labels=labels, cohesion=cohesion,
                                        god_nodes_data=analysis.get("gods") or upstream.god_nodes(graph))
        except ValueError as error:
            raise GraphError(f"wiki export failed: {error}") from error
    elif wiki_dir.is_dir():
        for page in wiki_dir.glob("*.md"):
            page.unlink()
        try:
            wiki_dir.rmdir()
        except OSError:
            pass  # something else lives there; it is not ours to remove
    # Native exporter tracks and prunes only its generated files; it preserves
    # authored notes and .obsidian settings in this already-owned vault.
    provenance = artifacts.load_json(output / artifacts.PROVENANCE)
    provenance["exported_at"] = datetime.now(timezone.utc).isoformat()
    descriptor, temporary = tempfile.mkstemp(dir=vault, prefix=".graphify-worktree-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(provenance, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, marker)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result = {"state": "exported", "vault_path": str(vault), "canvas_mode": canvas_mode}
    if wiki:
        result.update(wiki_path=str(wiki_dir), wiki_articles=articles)
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
