"""Upstream Graphify work memory (save-result, reflect) behind the graph consent gate.

Saved results live in the worktree's graphify-out/memory/, lessons in
graphify-out/reflections/LESSONS.md and the derived overlay that query_graph
shows as learning= in graphify-out/.graphify_learning.json. Publishing a new
graph never touches any of them. Both actions are writes, so each needs actual
user approval; both pass the same consent, lock and freshness gates as graph
tools before writing.
"""

import json
import os
from pathlib import Path
import time

from graphify_worktree import upstream
from graphify_worktree.errors import GraphError, LearningRequestError, MemoryRequestError
from graphify_worktree.guard import guarded_graph
from graphify_worktree.memory.schema import APPROVAL, GRAPH_REQUEST, TEXT, Action, validate
from graphify_worktree.policy import require, resolve_root


OUTCOMES = ("useful", "dead_end", "corrected")  # graphify.ingest.OUTCOMES
TYPES = ("query", "path_query", "explain")
REQUIRES = "Requires graph project consent (graph or obsidian mode). "

ACTIONS = {action.name: action for action in (
    Action(
        "save_result",
        "Record a graph answer and how it worked out in this worktree's Graphify work memory, then refresh its "
        "lessons so query_graph marks the cited nodes with learning=. Only after explicit user approval to record it.",
        {
            "question": TEXT,
            "answer": TEXT,
            "type": {"type": "string", "enum": list(TYPES), "default": "query"},
            "nodes": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 10,
                      "description": "Graph node labels or ids the answer relied on."},
            "outcome": {"type": "string", "enum": list(OUTCOMES)},
            "correction": {**TEXT, "description": "What the right answer was (with outcome corrected)."},
            **APPROVAL,
        },
        ("question", "answer", "confirmed", "confirmation"),
        writes=True, requires=REQUIRES, request=GRAPH_REQUEST,
    ),
    Action(
        "reflect",
        "Regenerate this worktree's lessons and learning overlay from its saved results if they are out of date. "
        "Deterministic and local; only after explicit user approval.",
        {**APPROVAL},
        ("confirmed", "confirmation"),
        writes=True, requires=REQUIRES, request=GRAPH_REQUEST,
    ),
)}


def _paths(output):
    paths = {"memory": output / "memory", "lessons": output / "reflections" / "LESSONS.md",
             "learning": output / upstream.LEARNING_SIDECAR, "graph": output / "graph.json"}
    for path in (paths["memory"], paths["lessons"].parent, paths["lessons"], paths["learning"]):
        if path.is_symlink():
            raise GraphError(f"{path} must be worktree-local, not a symlink")
    return paths


def _reflect(paths):
    """Run upstream reflect --if-stale semantics; the overlay must exist too."""
    graph = paths["graph"]
    if paths["learning"].exists() and upstream.lessons_fresh(
            paths["lessons"], paths["memory"], graph,
            graph.parent / ".graphify_analysis.json", graph.parent / ".graphify_labels.json"):
        state = "fresh"
    else:
        # Upstream servers cache a graph by graph.json's mtime and size and read
        # the overlay only on (re)load. A new mtime makes every server, in any
        # process, reload; the content (and so freshness) is unchanged.
        modified = max(time.time_ns(), graph.stat().st_mtime_ns + 1)
        os.utime(graph, ns=(modified, modified))
        upstream.reflect(paths["memory"], paths["lessons"], graph)
        state = "reflected"
    return {"state": state, "lessons": str(paths["lessons"]),
            "learning": str(paths["learning"]) if paths["learning"].exists() else None}


def handle(action, root, arguments, *, once=False, session_mode=None, cancelled=None):
    """Validate, pass the graph gates, then write; returns JSON-compatible results."""
    if type(once) is not bool:
        raise LearningRequestError("once must be boolean")
    root = resolve_root(root)

    def session():
        return session_mode() if callable(session_mode) else session_mode

    require(root, capability="graph", once=once, session_mode=session())
    try:
        args = validate(action, arguments, ACTIONS)
    except MemoryRequestError as error:
        raise LearningRequestError(str(error)) from error
    with guarded_graph(root, once=once, session_mode=session, cancelled=cancelled, verify=False) as status:
        paths = _paths(Path(status["graph_path"]).parent)
        result = {"worktree_root": str(root),
                  "graph": {key: status[key] for key in ("state", "generation", "rebuilt", "scope")}}
        if action == "save_result":
            saved = upstream.save_query_result(
                question=args["question"], answer=args["answer"], memory_dir=paths["memory"],
                query_type=args["type"], source_nodes=args.get("nodes"),
                outcome=args.get("outcome"), correction=args.get("correction"))
            result["saved"] = str(saved)
        result["reflection"] = _reflect(paths)
        return result


def add_parser(commands):
    parser = commands.add_parser("learning", help="record graph answers and refresh lessons in this worktree's Graphify work memory (graph consent, explicit approval)")
    actions = parser.add_subparsers(dest="learning_action", required=True)
    for action in ACTIONS.values():
        command = actions.add_parser(action.name.replace("_", "-"), help=action.description)
        command.add_argument("path", nargs="?", default=".", help="directory in the worktree (default: cwd)")
        command.add_argument("--once", action="store_true", help="permit only this operation; session off still wins")
        command.add_argument("--input-file", required=True, metavar="FILE", help="JSON object with the approved fields; '-' reads stdin")


def run(arguments, stdin):
    try:
        if arguments.input_file == "-":
            payload = json.load(stdin)
        else:
            with Path(arguments.input_file).open(encoding="utf-8") as stream:
                payload = json.load(stream)
    except ValueError as error:
        raise LearningRequestError(f"input is not valid JSON: {error}") from error
    return handle(arguments.learning_action.replace("-", "_"), arguments.path, payload, once=arguments.once)
