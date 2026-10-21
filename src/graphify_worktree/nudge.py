"""Consent-aware pre-tool nudge, modelled on upstream `graphify hook-guard`.

Before a search (Grep, Glob, a shell command that runs grep/rg/find/...) or a
source-file Read, suggest the managed graph tools, but only where the
repository allows graph use. Unlike upstream it is advisory only (never a
permission decision, never "mandatory"), keeps its per-session budget outside
the repository, never creates graphify-out, and stays quiet after the agent
used a managed graph tool or opted out through knowledge_session.

Fail-open: any error, a busy state file or a malformed payload means silence.
"""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from graphify_worktree import artifacts, gitutil, settings, upstream
from graphify_worktree.policy import get_policy, resolve_root


CAP = 5                 # nudges per agent per session, as upstream
QUIET_SECONDS = 1800    # after the agent used a managed graph tool, as upstream's TTL
GC_SECONDS = 86400
LOCK_WAIT = 0.2
SERVER = "mcp__graphify__"
SEARCH_TOOLS = ("Grep", "Glob")

PLAIN = ("worktree-knowledge: graph use is allowed in this repository ({mode} mode). For structural "
         "questions (where something is defined, its callers, dependencies, paths between symbols) the "
         "managed graphify tools such as query_graph, get_neighbors and shortest_path with "
         'project_path "{root}" may answer faster than searching files. A suggestion only: searching '
         "and reading files directly is fine.")
MISSING = ("worktree-knowledge: graph use is allowed in this repository ({mode} mode), but no graph has "
           "been built for this worktree yet. The managed graphify tools (query_graph with project_path "
           '"{root}") build one on first use, which can take a while on a large checkout. Searching and '
           "reading files directly is fine.")
STALE = ("worktree-knowledge: this worktree's graph may be out of date ({reason}). The managed graphify "
         'tools (query_graph with project_path "{root}") rebuild it before answering, which can take a '
         "while. Searching and reading files directly is fine.")


def _candidate(tool, tool_input, cwd):
    """True for a search, the source path for a Read, None otherwise. No Git, no I/O."""
    if tool in SEARCH_TOOLS:
        return True
    if tool == "Bash":
        command = tool_input.get("command")
        return True if isinstance(command, str) and upstream.bash_runs_search(command) else None
    path = tool_input.get("file_path") if tool == "Read" else None
    if isinstance(path, str) and path and Path(path).suffix in upstream.code_suffixes():
        return Path(cwd) / path
    return None


def _staleness(root, read):
    """None when the graph looks current, "missing", or why it may be stale. Cheap: no lock, no scan."""
    output = root / "graphify-out"
    if not (output / "graph.json").is_file():
        return "missing"
    saved = artifacts.load_provenance(output)
    if saved is None:
        return "missing"
    state = gitutil.checkout_state(root)
    if (state["head"], state["branch"]) != (saved.get("head"), saved.get("branch")):
        return "HEAD or branch changed since it was built"
    event = artifacts.read_event(output)
    if event is not None and event.get("generation") == saved.get("extracted_at"):
        return "a Git event changed the checkout since it was built"
    if read is not None and read.stat().st_mtime > datetime.fromisoformat(saved["extracted_at"]).timestamp():
        return "this file changed after it was built"
    return None


def _message(root, mode, read):
    reason = _staleness(root, read)
    template = PLAIN if reason is None else MISSING if reason == "missing" else STALE
    return template.format(mode=mode, root=root, reason=reason)


@contextmanager
def _session_state(session, agent):
    """Exclusive, briefly-waited access to one agent's {count, graph_used_at, session_off}."""
    directory = settings.state_dir() / "nudge"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (hashlib.sha256(f"{session}\0{agent}".encode()).hexdigest()[:32] + ".json")
    created = not path.exists()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            state = json.loads(stream.read() or "{}")
        except ValueError:
            state = {}
        state = state if isinstance(state, dict) else {}
        yield state
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream)
    if created:
        _collect_garbage(directory)


def _collect_garbage(directory):
    cutoff = time.time() - GC_SECONDS
    for entry in os.scandir(directory):
        try:
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass


def _respond(payload, now):
    if not isinstance(payload, dict):
        return ""
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input") or {}
    cwd, session, agent = payload.get("cwd"), payload.get("session_id"), payload.get("agent_id") or ""
    if not all(isinstance(value, str) for value in (tool, cwd, session, agent)) or not session \
            or not isinstance(tool_input, dict):
        return ""
    if tool.startswith(SERVER):
        # Observe managed calls: graph use quiets the nudge, knowledge_session
        # off silences it (the hook cannot see the server's in-memory state).
        name = tool.removeprefix(SERVER)
        with _session_state(session, agent) as state:
            if name == "knowledge_session" and tool_input.get("mode") in ("off", "default"):
                state["session_off"] = tool_input["mode"] == "off"
            elif name != "knowledge_session" and not name.startswith("memory_"):
                state["graph_used_at"] = now
        return ""
    candidate = _candidate(tool, tool_input, cwd)
    if candidate is None:
        return ""
    root = resolve_root(cwd)
    mode = get_policy(root)["effective_mode"]
    if mode not in ("graph", "obsidian"):
        return ""
    read = None
    if candidate is not True:
        read = candidate.resolve()
        if not read.is_relative_to(root) or not read.is_file():
            return ""
    with _session_state(session, agent) as state:
        if state.get("session_off") or now - state.get("graph_used_at", float("-inf")) < QUIET_SECONDS:
            return ""
        if state.get("count", 0) >= CAP:
            return ""
        state["count"] = state.get("count", 0) + 1
    text = _message(root, mode, read)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
                      ensure_ascii=False)


def respond(payload, *, now=None):
    """Hook JSON to print for one PreToolUse payload, or "" for silence. Never raises."""
    try:
        return _respond(payload, time.time() if now is None else now)
    except Exception:
        return ""
