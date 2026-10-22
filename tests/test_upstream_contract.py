"""The private upstream touchpoints and data formats this package relies on (graphifyy 0.9.93).

Run this first after changing PINS: a failure names the exact touchpoint that
moved, before any smoke fails somewhere downstream of it.
"""
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify_worktree import upstream

# (module, attribute, how the call sites pass arguments: positional names in order, then
# "*" and the names passed as keywords)
SIGNATURES = [
    ("graphify.detect", "_is_ignored", ["path", "root", "patterns", "*", "_cache"]),
    ("graphify.detect", "_is_noise_dir", ["part", "parent"]),
    ("graphify.detect", "_load_dir_own_ignore", ["d"]),
    ("graphify.detect", "_load_graphifyignore", ["root"]),
    ("graphify.detect", "detect", ["root", "*", "google_workspace", "extra_excludes", "cache_root"]),
    ("graphify.cache", "_flush_stat_index", []),
    ("graphify.serve", "_build_server", ["graph_path"]),
    ("graphify.serve", "_filter_graph_by_context", ["G", "context_filters"]),
    ("graphify.serve", "_filter_blank_stdin", []),
    ("graphify.cli", "_bash_invokes_search", ["cmd_str"]),
    ("graphify.build", "build_from_json", ["extraction", "*", "directed"]),
    ("graphify.cluster", "label_communities_by_hub", ["G", "communities"]),
    ("graphify.analyze", "suggest_questions", ["G", "communities", "community_labels"]),
    ("graphify.analyze", "god_nodes", ["G"]),
    ("graphify.report", "generate", ["G", "communities", "cohesion_scores", "community_labels", "god_node_list",
                                     "surprise_list", "detection_result", "token_cost", "root",
                                     "*", "suggested_questions", "built_at_commit"]),
    ("graphify.export", "to_canvas", ["G", "communities", "output_path", "*", "community_labels"]),
    ("graphify.export", "to_obsidian", ["G", "communities", "output_dir", "*", "community_labels", "cohesion"]),
    ("graphify.wiki", "to_wiki", ["G", "communities", "output_dir", "*", "community_labels", "cohesion",
                                  "god_nodes_data"]),
    ("graphify.ingest", "save_query_result", ["*", "question", "answer", "memory_dir", "query_type",
                                              "source_nodes", "outcome", "correction"]),
    ("graphify.reflect", "lessons_fresh", ["out_path", "memory_dir", "graph_path", "analysis_path", "labels_path"]),
    ("graphify.reflect", "reflect", ["memory_dir", "out_path", "graph_path"]),
]
STAT_INDEX = ("_stat_index", "_stat_index_root", "_stat_index_anchor", "_stat_index_path",
              "_stat_index_dirty", "_stat_index_atexit_registered")


@pytest.mark.parametrize("module, name, parameters", SIGNATURES, ids=[f"{m}.{n}" for m, n, _ in SIGNATURES])
def test_called_function_exists_with_the_parameters_passed(module, name, parameters):
    signature = inspect.signature(getattr(importlib.import_module(module), name))
    passed = parameters.index("*") if "*" in parameters else len(parameters)
    positional = [p.name for p in signature.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert positional[:passed] == parameters[:passed], signature
    for keyword in parameters[passed + 1:]:
        assert keyword in signature.parameters and signature.parameters[keyword].kind != inspect.Parameter.POSITIONAL_ONLY, signature
    required = [p.name for p in signature.parameters.values()
                if p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    assert set(required) <= set(parameters) - {"*"}, f"{signature} needs more than the call passes"


def test_module_state_that_is_read_or_reset_exists():
    from graphify import cache, detect
    for name in STAT_INDEX:
        assert hasattr(cache, name), name
    assert isinstance(detect.GRAPHIFY_OUT, str) and isinstance(detect.CODE_EXTENSIONS, (set, frozenset))
    assert ".py" in detect.CODE_EXTENSIONS


def test_mcp_server_keeps_constructor_callbacks_in_its_request_handler_table(tmp_path):
    proxied = upstream.upstream_server(str(tmp_path / "graph.json"))
    assert callable(proxied.list_tools) and callable(proxied.call_tool) and proxied.version


def test_bash_search_parser_tells_running_from_mentioning():
    assert upstream.bash_runs_search("rg helper src") is True
    assert upstream.bash_runs_search("echo rg") is False


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    """One tiny real upstream extraction, as extract.py runs it."""
    base = tmp_path_factory.mktemp("contract")
    source = base / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg/core.py").write_text("def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n")
    (source / "pkg/other.py").write_text("from pkg.core import helper\n\n\ndef user():\n    return helper()\n")
    env = {**os.environ, "GRAPHIFY_BACKEND": "none", "GRAPHIFY_GOOGLE_WORKSPACE": "0",
           "GRAPHIFY_OUT": str(base / "graphify-out")}
    subprocess.run([sys.executable, "-m", "graphify", "extract", str(source), "--code-only", "--force",
                    "--out", str(base), "--max-workers", "1"], cwd=source, env=env, check=True,
                   capture_output=True, timeout=300)
    return base / "graphify-out"


@pytest.mark.integration
def test_extract_writes_what_extract_export_and_incremental_read(extracted):
    analysis = json.loads((extracted / ".graphify_analysis.json").read_text())
    assert {"communities", "cohesion", "gods", "surprises"} <= analysis.keys()  # extract.py, obsidian_export.py
    graph = json.loads((extracted / "graph.json").read_text())
    assert graph["nodes"] and "links" in graph  # extract.py, diff.py
    assert all("source_file" in node for node in graph["nodes"])  # citation rebase
    assert isinstance(json.loads((extracted / "manifest.json").read_text()), dict)  # incremental seed


@pytest.mark.integration
def test_report_still_suggests_the_command_extract_rewrites(extracted):
    graph = upstream.build_graph(json.loads((extracted / "graph.json").read_text()))
    analysis = json.loads((extracted / ".graphify_analysis.json").read_text())
    communities = {int(key): members for key, members in analysis["communities"].items()}
    labels = upstream.label_communities(graph, communities)
    report = upstream.generate_report(graph, communities, {int(k): v for k, v in analysis["cohesion"].items()},
                                      labels, analysis["gods"], analysis["surprises"], {"warning": "contract"},
                                      {"input": 0, "output": 0}, str(extracted), built_at_commit="0" * 40)
    assert "`graphify update .`" in report


@pytest.mark.integration
def test_obsidian_export_writes_the_manifest_canvas_paging_reads(extracted, tmp_path):
    graph = upstream.build_graph(json.loads((extracted / "graph.json").read_text()))
    analysis = json.loads((extracted / ".graphify_analysis.json").read_text())
    communities = {int(key): members for key, members in analysis["communities"].items()}
    upstream.to_obsidian(graph, communities, str(tmp_path), community_labels=upstream.label_communities(graph, communities))
    files = json.loads((tmp_path / ".graphify_obsidian_manifest.json").read_text())["files"]
    assert any(name.startswith("_COMMUNITY_") and name.endswith(".md") for name in files)


@pytest.mark.integration
def test_learning_overlay_is_a_graph_attribute_the_patched_filter_keeps(extracted):
    from graphify import serve
    graph = serve._read_graph(str(extracted / "graph.json"))
    assert isinstance(graph.graph.get("_learning_overlay"), dict)
    graph.graph["_learning_overlay"] = {"marker": {}}
    upstream.upstream_server(str(extracted / "graph.json"))  # installs the patch (idempotent)
    filtered = serve._filter_graph_by_context(graph, ["call"])
    assert filtered is not graph and filtered.graph["_learning_overlay"] == {"marker": {}}
