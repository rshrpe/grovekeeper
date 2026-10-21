"""The only module that imports Graphify or the MCP SDK.

Several hooks used here are private upstream APIs (underscore names, the MCP
server's request-handler table, Graphify's process-global stat index). They are
pinned to exact versions and checked at startup, so an upgrade fails loudly
instead of silently changing freshness, extraction or consent behaviour.

Graphify modules are imported lazily: Graphify reads GRAPHIFY_OUT once at
import time, and cheap commands such as `policy` must not pay for the import.
"""

import atexit
from importlib.metadata import PackageNotFoundError, version
import sys
from types import SimpleNamespace

from graphify_worktree.errors import UpstreamVersionError


PINS = {"graphifyy": "0.9.93", "mcp": "2.3.0"}


def assert_versions():
    for package, expected in PINS.items():
        try:
            found = version(package)
        except PackageNotFoundError:
            raise UpstreamVersionError(f"{package}=={expected} is required but not installed") from None
        if found != expected:
            raise UpstreamVersionError(
                f"{package}=={expected} is required (found {found}); private upstream APIs "
                "are only verified against the pinned versions. Reinstall grovekeeper."
            )


def graphify_version():
    return version("graphifyy")


def graphify_command():
    """Run the Graphify CLI from this interpreter's environment."""
    return [sys.executable, "-m", "graphify"]


# Detection -----------------------------------------------------------------

def ignore_rules():
    from graphify.detect import _is_ignored, _is_noise_dir, _load_dir_own_ignore, _load_graphifyignore

    return SimpleNamespace(
        is_ignored=_is_ignored, is_noise_dir=_is_noise_dir,
        load_dir_own_ignore=_load_dir_own_ignore, load_graphifyignore=_load_graphifyignore,
    )


def detect(scoped, *, excludes, scratch):
    from graphify import detect as detector

    # MCP has already imported Graphify's default paths. Override only detection
    # so its special memory scan and document conversions stay in scratch.
    previous_output = detector.GRAPHIFY_OUT
    detector.GRAPHIFY_OUT = str(scratch / "graphify-out")
    try:
        return detector.detect(scoped, google_workspace=False, extra_excludes=excludes, cache_root=scratch)
    finally:
        detector.GRAPHIFY_OUT = previous_output


def release_stat_index(scratch):
    # Graphify's stat index is process-global, but this cache belongs to
    # the temporary workspace. Release it before removing its directory
    # so later worktrees cannot inherit an expired cache root.
    cache = sys.modules.get("graphify.cache")
    if cache is not None and cache._stat_index_root == scratch:
        atexit.unregister(cache._flush_stat_index)
        cache._stat_index.clear()
        cache._stat_index_root = None
        cache._stat_index_anchor = None
        cache._stat_index_path = None
        cache._stat_index_dirty = False
        cache._stat_index_atexit_registered = False


# Analysis, report and export ----------------------------------------------

def build_graph(data):
    from graphify.build import build_from_json

    return build_from_json(data, directed=bool(data.get("directed", False)))


def label_communities(graph, communities):
    from graphify.cluster import label_communities_by_hub

    return label_communities_by_hub(graph, communities)


def suggest_questions(graph, communities, labels):
    from graphify.analyze import suggest_questions

    return suggest_questions(graph, communities, labels)


def generate_report(*args, **kwargs):
    from graphify.report import generate

    return generate(*args, **kwargs)


def to_canvas(*args, **kwargs):
    from graphify.export import to_canvas

    return to_canvas(*args, **kwargs)


def to_wiki(*args, **kwargs):
    from graphify.wiki import to_wiki

    return to_wiki(*args, **kwargs)


def god_nodes(graph):
    from graphify.analyze import god_nodes

    return god_nodes(graph)


def to_obsidian(*args, **kwargs):
    from graphify.export import to_obsidian

    return to_obsidian(*args, **kwargs)


# Work memory (save-result / reflect) ----------------------------------------

LEARNING_SIDECAR = ".graphify_learning.json"  # graphify.reflect.LEARNING_SIDECAR_NAME


def save_query_result(**kwargs):
    from graphify.ingest import save_query_result

    return save_query_result(**kwargs)


def lessons_fresh(*args, **kwargs):
    from graphify.reflect import lessons_fresh

    return lessons_fresh(*args, **kwargs)


def reflect(*args, **kwargs):
    from graphify.reflect import reflect

    return reflect(*args, **kwargs)


# Hook-guard helpers (pre-tool nudge) -----------------------------------------

def bash_runs_search(command):
    """Whether a shell command executes grep/rg/find/... (not merely mentions one)."""
    from graphify.cli import _bash_invokes_search

    return _bash_invokes_search(command)


def code_suffixes():
    """File suffixes code-only extraction indexes (upstream's guard list also has docs)."""
    from graphify.detect import CODE_EXTENSIONS

    return CODE_EXTENSIONS


# MCP -----------------------------------------------------------------------

def mcp_api():
    """Public MCP SDK names used by the managed server."""
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    return SimpleNamespace(types=types, Server=Server, stdio_server=stdio_server)


def upstream_server(graph_path):
    """Build Graphify's MCP server and return its version and tool callbacks.

    MCP 2 stores constructor callbacks in Server._request_handlers; reuse them
    instead of vendoring Graphify's schemas, dispatch table, graph cache, or
    query implementations.
    """
    from graphify import serve

    _keep_learning_overlay_when_filtering(serve)
    server = serve._build_server(graph_path)
    return SimpleNamespace(
        version=server.version,
        list_tools=server._request_handlers["tools/list"].handler,
        call_tool=server._request_handlers["tools/call"].handler,
    )


def _keep_learning_overlay_when_filtering(serve):
    """query_graph drops the work-memory overlay when a context filter applies.

    _filter_graph_by_context builds a fresh graph and copies nodes and edges but
    not the graph attributes the overlay is stored in, so any question that
    implies a filter ("who calls ...") loses its learning= annotations. Carry
    that one attribute over; everything else upstream does is unchanged.
    """
    original = serve._filter_graph_by_context
    if getattr(original, "keeps_learning_overlay", False):
        return

    def filtered(graph, context_filters):
        result = original(graph, context_filters)
        if result is not graph and "_learning_overlay" in graph.graph:
            result.graph["_learning_overlay"] = graph.graph["_learning_overlay"]
        return result

    filtered.keeps_learning_overlay = True
    serve._filter_graph_by_context = filtered


def filter_blank_stdin():
    from graphify.serve import _filter_blank_stdin

    _filter_blank_stdin()
