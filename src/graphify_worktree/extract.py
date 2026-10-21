"""Run Graphify's structural extraction into a scratch directory and write its report."""

import subprocess
import sys

from graphify_worktree import artifacts, upstream
from graphify_worktree.errors import GraphError


def run_graphify(arguments, scoped):
    result = subprocess.run(
        [*upstream.graphify_command(), *map(str, arguments)], cwd=scoped,
        stdout=sys.stderr,
    )
    if result.returncode:
        raise GraphError(f"Graphify {arguments[0]} failed (exit {result.returncode}); previous graph retained")


def _report(staged, root, scoped, before):
    # Extraction already clustered. Reuse its analysis rather than clustering
    # a large checkout twice; hub naming and report generation are entirely local.
    graph_path = staged / "graph.json"
    data = artifacts.load_json(graph_path)
    if not data.get("nodes"):
        raise GraphError("Graphify produced an empty graph")
    # Native extraction makes citations scope-relative. MCP project_path is the
    # worktree root, so published citations must use that same coordinate system.
    def rebase(value):
        if isinstance(value, dict):
            source = value.get("source_file")
            if isinstance(source, str) and source:
                absolute = (scoped / source).resolve()
                if absolute.is_relative_to(root):
                    value["source_file"] = absolute.relative_to(root).as_posix()
            for child in value.values():
                rebase(child)
        elif isinstance(value, list):
            for child in value:
                rebase(child)

    rebase(data)
    analysis = artifacts.load_json(staged / ".graphify_analysis.json")
    graph = upstream.build_graph(data)
    communities = {int(key): members for key, members in analysis["communities"].items()}
    labels = upstream.label_communities(graph, communities)
    questions = upstream.suggest_questions(graph, communities, labels)
    report = upstream.generate_report(
        graph, communities,
        {int(key): value for key, value in analysis["cohesion"].items()},
        labels, analysis["gods"], analysis["surprises"],
        {"warning": f"Code-only scope: {before['scope']}; {len(before['manifest'])} supported code files. Documents and semantic extraction excluded."},
        {"input": 0, "output": 0}, str(root),
        suggested_questions=questions, built_at_commit=before["head"],
    )
    report = report.replace("`graphify update .`", "`graphify-worktree refresh .`")
    for node in data["nodes"]:
        if node.get("community") is not None:
            node["community_name"] = labels.get(int(node["community"]), "")
    artifacts.write_json(graph_path, data)
    analysis["questions"] = questions
    artifacts.write_json(staged / ".graphify_analysis.json", analysis)
    artifacts.write_json(staged / ".graphify_labels.json", labels)
    (staged / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")


def extract(root, scoped, scratch, before):
    """Extract scoped sources into scratch/graphify-out; returns that staging directory."""
    arguments = ["extract", scoped, "--code-only", "--force", "--out", scratch, "--max-workers", "4"]
    for excluded in before["excludes"]:
        arguments.extend(["--exclude", excluded])
    run_graphify(arguments, scoped)
    staged = scratch / "graphify-out"
    try:
        _report(staged, root, scoped, before)
    except (KeyError, TypeError, ValueError) as error:
        # Malformed upstream output is a failed build, not a bug in this package.
        raise GraphError(f"Graphify output is incomplete ({error!r}); previous graph retained") from error
    return staged
