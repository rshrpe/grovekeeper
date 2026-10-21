"""Machine-specific locations, overridable through the environment.

Every value is read at call time so a process (or test) can redirect vaults
without re-importing. Empty overrides mean "use the default".
"""

import os
from pathlib import Path

from graphify_worktree.errors import SettingsError


def _path(variable, default):
    raw = os.environ.get(variable)
    if not raw:
        return Path.home() / default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SettingsError(f"{variable} must be an absolute path (or start with ~): {raw}")
    return path


def graph_vault_root():
    """Parent of the per-worktree generated vaults written by `export`."""
    return _path("WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", "Obsidian/Graphify")


def memory_vault():
    """Authored vault holding Projects/<repository>/Memory notes."""
    return _path("WORKTREE_KNOWLEDGE_MEMORY_VAULT", "Obsidian/Engineering")


def obsidian_cli():
    """Native Obsidian CLI used only by `export --open`."""
    return _path("WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", ".local/bin/obsidian")


def skill_dir():
    """Where the installer links the worktree-knowledge skill for all harnesses."""
    return _path("WORKTREE_KNOWLEDGE_SKILL_DIR", ".agents/skills/worktree-knowledge")
