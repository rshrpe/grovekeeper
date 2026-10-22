"""Repository-local knowledge consent; no graph or vault artifacts are inspected."""

import os

from graphify_worktree import gitutil
from graphify_worktree.errors import GitError, PolicyError
from graphify_worktree.gitutil import resolve_root

__all__ = ["KEY", "MODES", "PolicyError", "configure", "get_policy", "require", "resolve_root"]

KEY = "harness.knowledgeMode"
MODES = ("off", "graph", "obsidian")


def _session(session_mode):
    """The session mode; an inherited HARNESS_KNOWLEDGE_MODE=off beats any in-process value."""
    environment = os.environ.get("HARNESS_KNOWLEDGE_MODE", "default")
    if environment == "off" or session_mode is None:
        return environment
    return session_mode


def _config(root):
    try:
        return gitutil.common_dir(root) / "config"
    except GitError as error:
        raise PolicyError(str(error) or "cannot resolve common Git directory") from error


def get_policy(root, session_mode=None):
    root = resolve_root(root)
    policy = {
        "state": "unconfigured", "saved_mode": None, "effective_mode": "off",
        "worktree_root": str(root), "reason": "knowledge is unconfigured; choose off, graph, or obsidian before first use",
    }
    try:
        result = gitutil.run(root, "config", "--file", str(_config(root)), "--no-includes", "--null", "--get-all", KEY)
        if result.returncode not in (0, 1):
            raise PolicyError(result.stderr.strip() or "cannot read knowledge preference")
        values = result.stdout.removesuffix("\0").split("\0") if result.returncode == 0 else []
        if any(value not in MODES for value in values) or len(set(values)) > 1:
            raise PolicyError("invalid or conflicting repository knowledge preferences; configure off, graph, or obsidian")
        if values:
            policy.update(state=values[0], saved_mode=values[0], effective_mode=values[0], reason=f"repository knowledge mode is {values[0]}")
        session = _session(session_mode)
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
    if capability not in ("graph", "obsidian"):
        raise PolicyError(f"unknown knowledge capability: {capability}", policy)
    if _session(session_mode) == "off":
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
        result = gitutil.run(root, "config", "--file", str(_config(root)), "--no-includes", "--replace-all", KEY, mode)
        if result.returncode:
            raise PolicyError(result.stderr.strip() or "cannot save knowledge preference")
    except OSError as error:
        raise PolicyError(str(error)) from error
    return get_policy(root)
