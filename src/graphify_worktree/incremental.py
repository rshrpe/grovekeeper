"""Opt-in incremental rebuilds (`graphify-worktree configure --incremental on`).

A build keeps upstream's raw extraction output (manifest.json, the AST cache
and its graph.json before citations are rebased) as a seed in
graphify-out/.graphify-worktree-seed/<token>/, named in the provenance. The
next rebuild moves that seed into the staged workspace and runs upstream
extract without --force, so only changed files are re-extracted.

Upstream trusts an unchanged mtime, so every file whose content hash differs
between the seed's generation and now is first dropped from the seed manifest:
our content fingerprint, not mtimes, decides what is re-extracted. Any doubt
about the seed means a full extract, and so does every FULL_EVERY-th build,
because an incremental merge can name unresolved external references
differently from a full build (graphifyy 0.9.93). Either way the published
generation passes the same snapshot comparison, consent check and staged
publish as before.
"""

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid

from graphify_worktree.errors import GraphError
from graphify_worktree.extract import RAW_GRAPH


SEEDS = ".graphify-worktree-seed"
FULL_EVERY = 10
TOKEN = re.compile(r"[0-9a-f]{32}")


def plan(root, output, saved, before):
    """(seed directory, None) for an incremental build, or (None, reason) for a full one."""
    build = saved.get("build") if isinstance(saved, dict) else None
    if not isinstance(build, dict) or not isinstance(build.get("seed"), str) or not TOKEN.fullmatch(build["seed"]):
        return None, "no seed from the previous build"
    if saved.get("worktree_root") != str(root):
        return None, "seed belongs to another worktree"
    if saved.get("graphify_version") != before["graphify_version"]:
        return None, "Graphify version changed"
    if saved.get("scope") != before["scope"]:
        return None, "scope changed"
    if saved.get("excludes") != before["excludes"]:
        return None, "nested checkout exclusions changed"
    if not isinstance(saved.get("manifest"), dict):
        return None, "previous content manifest is missing"
    since = build.get("since_full")
    if type(since) is not int or since + 1 >= FULL_EVERY:
        return None, f"periodic full extract (at most {FULL_EVERY - 1} incremental builds in a row)"
    seeds = output / SEEDS
    seed = seeds / build["seed"]
    if seeds.is_symlink() or seed.is_symlink() or not (seed / RAW_GRAPH).is_file() \
            or not (seed / "manifest.json").is_file():
        return None, "seed is missing or incomplete"
    return seed, None


def seed(source, staged, saved, before):
    """Move the seed into staged and make upstream re-extract every changed file."""
    # Seeds live in the checkout: a committed or replaced cache must not redirect
    # upstream reads/writes (including manifest rewrites) outside the workspace.
    def walk_error(error):
        raise error

    for directory, names, files in os.walk(source, onerror=walk_error):
        for name in names + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise GraphError(f"incremental seed contains a non-regular path: {path}")
    if staged.exists():
        shutil.rmtree(staged)  # only detection caches of this workspace
    os.rename(source, staged)
    os.replace(staged / RAW_GRAPH, staged / "graph.json")
    path = staged / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise GraphError(f"seed manifest is unreadable: {error}") from error
    if not isinstance(manifest, dict):
        raise GraphError("seed manifest is not an object")
    old, new = saved["manifest"], before["manifest"]
    for changed in {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}:
        relative = PurePosixPath(changed)
        if before["scope"] != ".":
            relative = relative.relative_to(before["scope"])
        manifest.pop(relative.as_posix(), None)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def new_token():
    return uuid.uuid4().hex


def retain(staged, output, token):
    """After publish: keep staged as the seed named token (None keeps none); drop older seeds."""
    seeds = output / SEEDS
    if seeds.is_symlink():
        raise GraphError(f"{seeds} must not be a symlink")
    if token is not None:
        seeds.mkdir(exist_ok=True)
        os.rename(staged, seeds / token)
    if seeds.is_dir():
        for entry in seeds.iterdir():
            if entry.name != token:
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        if token is None:
            seeds.rmdir()
