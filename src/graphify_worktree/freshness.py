"""Is a worktree's published graph a faithful view of its current sources?"""

from dataclasses import dataclass
import glob
import hashlib
import json
import os
from pathlib import Path

from graphify_worktree import artifacts, gitutil, upstream
from graphify_worktree.errors import GraphError, KnowledgeError, PolicyError, ProvenanceError


FRESH_REASON = "source content, scope, HEAD and branch match provenance"


@dataclass(frozen=True)
class Generation:
    """What a reader depends on: if any of this moves during a query, its answer is withheld."""

    extracted_at: str
    fingerprint: str
    graph_sha256: str
    scope: str

    @classmethod
    def of(cls, status):
        return cls(status.get("generation"), status.get("fingerprint"), status.get("graph_sha256"), status.get("scope"))


def resolve_scope(root, scope):
    relative = Path(scope)
    if relative.is_absolute():
        raise GraphError("--scope must be relative to the worktree root")
    scoped = (root / relative).resolve(strict=True)
    if not scoped.is_relative_to(root):
        raise GraphError("scope escapes the worktree")
    try:
        nested = scoped.is_dir() and gitutil.resolve_root(scoped) != root
    except PolicyError as error:
        raise GraphError(str(error)) from error
    if not scoped.is_dir() or nested:
        raise GraphError("scope must be a directory in this worktree, not a nested checkout")
    if "graphify-out" in scoped.relative_to(root).parts:
        raise GraphError("generated graphify-out directories cannot be a source scope")
    return scoped


def exclusions(scoped):
    # Reuse Graphify's traversal rules, adding hard checkout boundaries that its
    # native walker otherwise crosses when a nested checkout is not gitignored.
    rules = upstream.ignore_rules()
    patterns = rules.load_graphifyignore(scoped)
    cache = {}
    excluded = []

    def walk_error(error):
        raise error

    for directory, names, _ in os.walk(scoped, onerror=walk_error):
        parent = Path(directory)
        if parent != scoped:
            patterns.extend(rules.load_dir_own_ignore(parent))
        keep = []
        for name in names:
            child = parent / name
            if child.is_symlink() or rules.is_noise_dir(name, parent):
                continue
            if (child / ".git").exists():
                literal = glob.escape(child.relative_to(scoped).as_posix()).replace("#", "[#]")
                excluded.append(f"/{literal}/")
            elif not rules.is_ignored(child, scoped, patterns, _cache=cache):
                keep.append(name)
        names[:] = keep
    return sorted(excluded)


def snapshot(root, scoped, scratch):
    """Content fingerprint of every supported source file plus checkout state."""
    state = gitutil.checkout_state(root)
    excluded = exclusions(scoped)
    detected = upstream.detect(scoped, excludes=excluded, scratch=scratch)
    if detected.get("walk_errors"):
        raise GraphError("source enumeration was incomplete; refusing a partial graph")
    manifest = {}
    for name in detected["files"]["code"]:
        source = Path(name)
        if not source.resolve().is_relative_to(scoped):
            raise GraphError(f"source escaped scope: {source}")
        before = source.stat()
        content_hash = artifacts.digest(source)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise GraphError(f"source changed while hashing: {source}")
        manifest[source.relative_to(root).as_posix()] = content_hash
    if gitutil.checkout_state(root) != state:
        raise GraphError("HEAD or branch changed while inspecting sources")
    fingerprint = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        **state,
        "worktree_root": str(root),
        "scope": scoped.relative_to(root).as_posix(),
        "graphify_version": upstream.graphify_version(),
        "manifest": manifest,
        "fingerprint": fingerprint,
        "excludes": excluded,
    }


def classify(saved, *, root, graph_sha256, report_sha256, current, event):
    """Reason the saved generation is stale, or None when it is fresh.

    current is called only after the cheap artifact checks pass, because it
    walks and hashes every source file.
    """
    if saved["worktree_root"] != str(root):
        return "graph belongs to a different worktree"
    if graph_sha256 != saved["graph_sha256"]:
        return "graph changed outside graphify-worktree"
    if report_sha256 != saved["report_sha256"]:
        return "graph report is missing or changed"
    try:
        now = current()
    except (KnowledgeError, OSError) as error:
        return str(error)
    # The fingerprint is the digest of the manifest, so comparing both is redundant.
    differences = [key for key, value in now.items() if key != "manifest" and saved.get(key) != value]
    if differences:
        return "changed: " + ", ".join(differences)
    if event is not None and event.get("generation") == saved["extracted_at"]:
        return f"Git event invalidated this generation: {event.get('event', 'unknown')}"
    return None


def describe(root, output, saved, *, state, reason):
    """Status JSON shared by status, ensure and the MCP guard."""
    status = {
        "state": state,
        "worktree_root": str(root),
        "graph_path": str(output / "graph.json"),
        "vault_path": str(artifacts.vault_path(root)),
        "scope": saved["scope"] if saved else None,
        "reason": reason,
    }
    if saved:
        status.update(extracted_at=saved["extracted_at"], generation=saved["extracted_at"],
                      fingerprint=saved["fingerprint"], graph_sha256=saved["graph_sha256"])
    if state == "fresh":
        status.update(branch=saved["branch"], head=saved["head"], code_files=len(saved["manifest"]))
    return status


def _digest_or_none(path):
    try:
        return None if path.is_symlink() else artifacts.digest(path)
    except OSError:
        return None


def inspect(root, output, scratch):
    """missing, stale or fresh; never rebuilds."""
    artifacts.check_artifacts(output)
    graph = output / "graph.json"
    if not graph.is_file():
        return describe(root, output, None, state="missing", reason="graph.json does not exist")
    try:
        saved = artifacts.read_provenance(output)
        if saved is None:
            raise ProvenanceError("missing or unsupported provenance schema")
    except (ProvenanceError, OSError) as error:
        return describe(root, output, None, state="stale", reason=str(error))
    reason = classify(
        saved, root=root,
        graph_sha256=_digest_or_none(graph),
        report_sha256=_digest_or_none(output / "GRAPH_REPORT.md"),
        current=lambda: snapshot(root, resolve_scope(root, saved["scope"]), scratch),
        event=artifacts.read_event(output),
    )
    if reason:
        return describe(root, output, saved, state="stale", reason=reason)
    return describe(root, output, saved, state="fresh", reason=FRESH_REASON)
