"""Machine-specific locations from TOML, overridable through the environment.

Every value is read at call time so a process (or test) can redirect vaults
without re-importing. Empty overrides mean "use config or the default".
File settings only choose locations: they grant no participation, session
access, general memory or cleanup permission.
"""

import os
from pathlib import Path
import stat
import tomllib

from graphify_worktree.errors import SettingsError


_LOCATIONS = {
    "memory_vault": ("WORKTREE_KNOWLEDGE_MEMORY_VAULT", "Obsidian/Engineering"),
    "graph_vault_root": ("WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", "Obsidian/Graphify"),
    "obsidian_cli": ("WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", ".local/bin/obsidian"),
}


def config_path():
    """The optional location settings file under the current config home."""
    base = os.environ.get("XDG_CONFIG_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".config") / "grovekeeper/config.toml"


def _configured_paths():
    path = config_path()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        if path.is_symlink():
            raise SettingsError(f"{path}: paths: config is not a regular file") from error
        return {}
    except OSError as error:
        raise SettingsError(f"{path}: paths: cannot read config: {error}") from error
    if not stat.S_ISREG(mode):
        raise SettingsError(f"{path}: paths: config is not a regular file")
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise SettingsError(f"{path}: paths: cannot read config: {error}") from error
    for key in data:
        if key != "paths":
            raise SettingsError(f"{path}: {key}: unknown top-level key")
    values = data.get("paths", {})
    if not isinstance(values, dict):
        raise SettingsError(f"{path}: paths: must be a table")
    paths = {}
    for key, raw in values.items():
        if key not in _LOCATIONS:
            raise SettingsError(f"{path}: paths.{key}: unknown key")
        if not isinstance(raw, str) or not raw:
            raise SettingsError(f"{path}: paths.{key}: must be a non-empty string")
        if not os.path.isabs(raw) and not raw.startswith("~"):
            raise SettingsError(f"{path}: paths.{key}: must be an absolute path (or start with ~): {raw}")
        try:
            paths[key] = Path(raw).expanduser()
        except (RuntimeError, ValueError) as error:
            raise SettingsError(f"{path}: paths.{key}: cannot expand path: {raw}") from error
        if not paths[key].is_absolute():
            raise SettingsError(f"{path}: paths.{key}: must expand to an absolute path: {raw}")
    return paths


def _location(name, configured):
    variable, default = _LOCATIONS[name]
    if os.environ.get(variable):
        return {"path": _path(variable, default), "source": "environment"}
    if name in configured:
        return {"path": configured[name], "source": "config"}
    return {"path": Path.home() / default, "source": "default"}


def describe():
    """Effective location paths and their environment, config or default source."""
    configured = _configured_paths()
    return {name: _location(name, configured) for name in _LOCATIONS}


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
    return _location("graph_vault_root", _configured_paths())["path"]


def memory_vault():
    """Authored vault holding Projects/<repository>/Memory notes."""
    return _location("memory_vault", _configured_paths())["path"]


def obsidian_cli():
    """Native Obsidian CLI used only by `export --open`."""
    return _location("obsidian_cli", _configured_paths())["path"]


def skill_dir():
    """Where the installer links the worktree-knowledge skill for all harnesses."""
    return _path("WORKTREE_KNOWLEDGE_SKILL_DIR", ".agents/skills/worktree-knowledge")


def state_dir():
    """Per-user nudge counters and export retirement records: $XDG_STATE_HOME/grovekeeper.

    Never inside a repository. An empty or relative XDG_STATE_HOME is ignored,
    as the XDG Base Directory specification requires.
    """
    base = os.environ.get("XDG_STATE_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".local/state") / "grovekeeper"
