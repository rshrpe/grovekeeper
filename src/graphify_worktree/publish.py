"""Rebuild a worktree's graph and publish it only if nothing moved meanwhile."""

from datetime import datetime, timedelta, timezone
import os
import shutil
import sys
import time

from graphify_worktree import artifacts
from graphify_worktree.errors import GraphError
from graphify_worktree.extract import extract
from graphify_worktree.freshness import FRESH_REASON, describe, resolve_scope, snapshot


def refresh(root, output, scratch, requested_scope, *, authorize=None):
    """Extract, verify and publish; returns the fresh status of the new generation.

    Caller holds the worktree lock. authorize() runs immediately before
    anything is published so a consent change during extraction wins.
    """
    artifacts.check_artifacts(output)
    # Any previous provenance is replaced, so only its scope and generation
    # time matter; an unsupported schema must not block the rebuild.
    adopting = not (output / artifacts.PROVENANCE).exists()
    saved = artifacts.load_provenance(output) or {}
    scope = requested_scope if requested_scope is not None else saved.get("scope", ".")
    if not isinstance(scope, str):
        raise GraphError("saved provenance scope is invalid; pass --scope to choose one")
    scoped = resolve_scope(root, scope)
    before = snapshot(root, scoped, scratch)
    if not before["manifest"]:
        raise GraphError("scope has no supported code files; previous graph retained")
    staged = extract(root, scoped, scratch, before)
    if before != snapshot(root, scoped, scratch):
        raise GraphError("sources, HEAD, branch or scope changed during extraction; previous graph retained")
    if authorize is not None:
        authorize()
    extracted_at = datetime.now(timezone.utc)
    if saved.get("extracted_at"):
        try:
            previous = datetime.fromisoformat(saved["extracted_at"])
        except (TypeError, ValueError) as error:
            raise GraphError(f"saved provenance generation is invalid: {error}") from error
        extracted_at = max(extracted_at, previous + timedelta(microseconds=1))
    provenance = {
        "schema": artifacts.SCHEMA,
        **before,
        "extracted_at": extracted_at.isoformat(),
        "graph_sha256": artifacts.digest(staged / "graph.json"),
        "report_sha256": artifacts.digest(staged / "GRAPH_REPORT.md"),
        "vault_path": str(artifacts.vault_path(root)),
    }
    artifacts.write_json(staged / artifacts.PROVENANCE, provenance)
    # Upstream's graph cache keys on mtime/size. Published generations must have
    # distinct keys even for equal-sized graphs or a clock moving backwards.
    graph = output / "graph.json"
    modified = max(time.time_ns(), graph.stat().st_mtime_ns + 1 if graph.exists() else 0)
    os.utime(staged / "graph.json", ns=(modified, modified))
    if adopting and graph.exists():
        archive = output / ("pre-worktree-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        archive.mkdir()
        for name in artifacts.LEGACY:
            previous = output / name
            if previous.is_file():
                shutil.copy2(previous, archive / name)
        print(f"Archived pre-wrapper graph: {archive}", file=sys.stderr)
    # Only owned artifacts are replaced; memory, reflections and unrelated files
    # stay untouched. See artifacts.OWNED for why the order is safe.
    for name in artifacts.OWNED:
        os.replace(staged / name, output / name)
    return describe(root, output, provenance, state="fresh", reason=FRESH_REASON)
