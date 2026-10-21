"""Repository-local knowledge consent; no graph or vault artifacts are inspected."""

import os
from pathlib import Path
import subprocess


KEY = "harness.knowledgeMode"
MODES = ("off", "graph", "obsidian")


class PolicyError(Exception):
    def __init__(self, reason, policy=None, *, not_repository=False):
        super().__init__(reason)
        self.policy = policy
        self.not_repository = not_repository


def git_env():
    # Explicit paths must win over ambient repository/config redirection.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], env=git_env(),
        capture_output=True, text=True,
    )


def resolve_root(path):
    try:
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise PolicyError(f"not a directory: {path}")
        result = _git(path, "rev-parse", "--show-toplevel")
        if result.returncode:
            reason = result.stderr.strip() or "cannot resolve Git worktree"
            raise PolicyError(reason, not_repository="not a git repository" in reason.lower())
        return Path(result.stdout.rstrip("\n")).resolve(strict=True)
    except OSError as error:
        raise PolicyError(str(error)) from error


def _config(root):
    result = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode:
        raise PolicyError(result.stderr.strip() or "cannot resolve common Git directory")
    return Path(result.stdout.rstrip("\n")).resolve(strict=True) / "config"


def get_policy(root, session_mode=None):
    root = resolve_root(root)
    policy = {
        "state": "unconfigured", "saved_mode": None, "effective_mode": "off",
        "worktree_root": str(root), "reason": "knowledge is unconfigured; choose off, graph, or obsidian before first use",
    }
    try:
        result = _git(root, "config", "--file", str(_config(root)), "--no-includes", "--null", "--get-all", KEY)
        if result.returncode not in (0, 1):
            raise PolicyError(result.stderr.strip() or "cannot read knowledge preference")
        values = result.stdout.removesuffix("\0").split("\0") if result.returncode == 0 else []
        if any(value not in MODES for value in values) or len(set(values)) > 1:
            raise PolicyError("invalid or conflicting repository knowledge preferences; configure off, graph, or obsidian")
        if values:
            policy.update(state=values[0], saved_mode=values[0], effective_mode=values[0], reason=f"repository knowledge mode is {values[0]}")
        session = os.environ.get("HARNESS_KNOWLEDGE_MODE", "default") if session_mode is None else session_mode
        if session not in ("default", "off"):
            raise PolicyError("invalid HARNESS_KNOWLEDGE_MODE; use off or default")
        if session == "off":
            policy.update(effective_mode="off", reason="knowledge is disabled for this session")
        return policy
    except (OSError, PolicyError) as error:
        policy.update(effective_mode="off", reason=str(error))
        raise PolicyError(str(error), policy) from error


def require(root, capability="graph", once=False, session_mode=None):
    policy = get_policy(root, session_mode=session_mode)
    session = os.environ.get("HARNESS_KNOWLEDGE_MODE", "default") if session_mode is None else session_mode
    if capability not in ("graph", "obsidian"):
        raise PolicyError(f"unknown knowledge capability: {capability}", policy)
    if session == "off":
        raise PolicyError(policy["reason"], policy)
    if once:
        return {**policy, "reason": f"one-operation permission for {capability}; saved preference unchanged"}
    allowed = ("graph", "obsidian") if capability == "graph" else ("obsidian",)
    if policy["effective_mode"] not in allowed:
        policy["reason"] += f"; {capability} requires an enabled repository mode or explicit --once/allow_once"
        raise PolicyError(policy["reason"], policy)
    return policy


def configure(root, mode):
    if mode not in MODES:
        raise PolicyError("knowledge mode must be off, graph, or obsidian")
    root = resolve_root(root)
    try:
        # Git's config.lock + rename updates only the common local config, even
        # with extensions.worktreeConfig enabled. Includes never grant consent.
        result = _git(root, "config", "--file", str(_config(root)), "--no-includes", "--replace-all", KEY, mode)
        if result.returncode:
            raise PolicyError(result.stderr.strip() or "cannot save knowledge preference")
    except OSError as error:
        raise PolicyError(str(error)) from error
    return get_policy(root)
