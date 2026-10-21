"""Files this package owns under graphify-out/ and how they are locked and read."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from graphify_worktree import gitutil, settings, upstream
from graphify_worktree.errors import GraphError, ProvenanceError


PROVENANCE = ".graphify-worktree.json"
EVENT = ".graphify-worktree-event.json"
LOCK = ".graphify-worktree.lock"
SCHEMA = 1
# Replaced on publish, in this order. graph.json goes last, but readers never
# trust it alone: inspect verifies its digest against the provenance, so an
# interrupted publish reads as stale and is rebuilt, never as fresh.
OWNED = ("GRAPH_REPORT.md", ".graphify_analysis.json", ".graphify_labels.json", PROVENANCE, "graph.json")
# Archived when a pre-wrapper graph is adopted.
LEGACY = ("graph.json", "GRAPH_REPORT.md", ".graphify_analysis.json", ".graphify_labels.json", "manifest.json")
PROVENANCE_TEXT = ("scope", "extracted_at", "fingerprint", "graph_sha256", "report_sha256", "worktree_root")


def load_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def vault_path(root):
    """The generated Obsidian vault for one worktree; the name is shared with memory."""
    identity = gitutil.RepoIdentity.of(root)
    suffix = hashlib.sha256(os.fsencode(root)).hexdigest()[:10]
    return settings.graph_vault_root() / identity.name / f"{root.name}-{suffix}"


def load_provenance(output):
    """The saved provenance object as written, or None when never published."""
    path = output / PROVENANCE
    if not path.exists():
        return None
    try:
        saved = load_json(path)
    except ValueError as error:
        raise ProvenanceError(f"unreadable provenance: {error}") from error
    if not isinstance(saved, dict):
        raise ProvenanceError("provenance must be a JSON object")
    return saved


def read_provenance(output):
    """Provenance validated for a freshness decision, or None when never published."""
    saved = load_provenance(output)
    if saved is None:
        return None
    if saved.get("schema") != SCHEMA:
        raise ProvenanceError("missing or unsupported provenance schema")
    for key in PROVENANCE_TEXT:
        if not isinstance(saved.get(key), str):
            raise ProvenanceError(f"provenance field {key} is missing or invalid")
    if not isinstance(saved.get("manifest"), dict):
        raise ProvenanceError("provenance field manifest is missing or invalid")
    return saved


def read_event(output):
    try:
        event = load_json(output / EVENT)
    except (OSError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def check_artifacts(output):
    for name in ("graph.json", "GRAPH_REPORT.md", PROVENANCE, EVENT):
        path = output / name
        if path.is_symlink() or path.resolve() != path:
            raise GraphError(f"{path} must be worktree-local, not a symlink")


@contextmanager
def locked(root, create=False, *, timeout=None):
    """Hold the worktree's exclusive lock; yields graphify-out/ (which may not exist).

    timeout=None waits indefinitely; otherwise GraphError after that many seconds.
    """
    output = root / "graphify-out"
    if output.is_symlink() or output.resolve() != output:
        raise GraphError("graphify-out must not traverse symlinks; each worktree owns its output")
    if create:
        output.mkdir(exist_ok=True)
    if not output.exists():
        yield output
        return
    descriptor = os.open(output / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GraphError("graph is busy (another refresh or query holds the worktree lock)") from None
                time.sleep(0.05)
        yield output


@contextmanager
def workspace(output):
    # Detection and extraction stay structural-only and same-filesystem staged.
    # Restore the process environment before the MCP upstream query runs.
    parent = output if output.is_dir() else None
    with tempfile.TemporaryDirectory(prefix=".graphify-worktree-", dir=parent) as temporary:
        scratch = Path(temporary).resolve()
        environment = {
            "GRAPHIFY_OUT": str(scratch / "graphify-out"),
            "GRAPHIFY_GOOGLE_WORKSPACE": "0",
            "GRAPHIFY_BACKEND": "none",
        }
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            yield scratch
        finally:
            upstream.release_stat_index(scratch)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
