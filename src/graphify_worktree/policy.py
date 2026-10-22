"""Repository-local knowledge consent and settings; no graph or vault artifacts are inspected.

Everything is read from the common Git directory's own config file without
includes, so every worktree, harness and process of one clone agrees.
"""

import os

from graphify_worktree import gitutil
from graphify_worktree.errors import GitError, PolicyError
from graphify_worktree.gitutil import resolve_root

__all__ = ["KEY", "MODES", "SETTINGS", "PolicyError", "configure", "get_policy", "require", "resolve_root", "settings"]

KEY = "harness.knowledgeMode"
MODES = ("off", "graph", "obsidian")
# Opt-in features: name -> (repository key, deprecated environment fallback used while the key is unset).
SETTINGS = {
    "incremental": ("harness.knowledgeIncremental", "WORKTREE_KNOWLEDGE_INCREMENTAL"),
    "general_memory": ("harness.generalMemory", "WORKTREE_KNOWLEDGE_GENERAL_MEMORY"),
}
TRUE = ("true", "yes", "on", "1")
FALSE = ("false", "no", "off", "0")


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


def _saved(root):
    """Every harness.* value in the common config, includes ignored: {lowercased key: [values]}.

    A key written without a value (Git's shorthand for true) maps to None.
    """
    result = gitutil.run(root, "config", "--file", str(_config(root)), "--no-includes", "--null",
                         "--get-regexp", r"^harness\.")
    if result.returncode not in (0, 1):
        raise PolicyError(result.stderr.strip() or "cannot read knowledge preference")
    saved = {}
    for entry in result.stdout.split("\0")[:-1] if result.returncode == 0 else ():
        key, newline, value = entry.partition("\n")
        saved.setdefault(key, []).append(value if newline else None)
    return saved


def _settings(saved):
    """Effective settings: the repository key, else the deprecated environment variable, else off."""
    result, deprecated, invalid = {}, [], []
    for name, (key, variable) in SETTINGS.items():
        values = saved.get(key.lower())
        if values is None:
            result[name] = os.environ.get(variable) == "1"
            if result[name]:
                deprecated.append(variable)
        elif len(values) == 1 and (values[0] or "true").lower() in TRUE + FALSE:
            result[name] = (values[0] or "true").lower() in TRUE
        else:
            result[name] = False
            invalid.append(key)
    if deprecated:
        result["deprecated_environment"] = deprecated
    if invalid:
        result["invalid_settings"] = invalid
    return result


def settings(root):
    """Effective incremental and general_memory settings of the repository containing root."""
    return _settings(_saved(resolve_root(root)))


def get_policy(root, session_mode=None):
    root = resolve_root(root)
    policy = {
        "state": "unconfigured", "saved_mode": None, "effective_mode": "off",
        "worktree_root": str(root), "reason": "knowledge is unconfigured; choose off, graph, or obsidian before first use",
        "incremental": False, "general_memory": False,
    }
    try:
        saved = _saved(root)
        policy.update(_settings(saved))
        values = saved.get(KEY.lower(), [])
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


def configure(root, mode=None, *, incremental=None, general_memory=None):
    """Save the mode and/or settings given (None leaves one unchanged); returns the policy."""
    if mode is not None and mode not in MODES:
        raise PolicyError("knowledge mode must be off, graph, or obsidian")
    updates = [(KEY, mode)] if mode is not None else []
    for name, value in (("incremental", incremental), ("general_memory", general_memory)):
        if value is not None:
            updates.append((SETTINGS[name][0], "true" if value else "false"))
    if not updates:
        raise PolicyError("nothing to configure: give a mode, --incremental or --general-memory")
    root = resolve_root(root)
    try:
        # Git's config.lock + rename updates only the common local config, even
        # with extensions.worktreeConfig enabled. Includes never grant consent.
        for key, value in updates:
            result = gitutil.run(root, "config", "--file", str(_config(root)), "--no-includes", "--replace-all", key, value)
            if result.returncode:
                raise PolicyError(result.stderr.strip() or "cannot save knowledge preference")
    except OSError as error:
        raise PolicyError(str(error)) from error
    return get_policy(root)
