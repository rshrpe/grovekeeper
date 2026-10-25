"""graphify-worktree doctor: a read-only report of how this installation is wired up.

It reads package versions, the configured locations, the harness
configuration files under HOME and one repository's Git configuration, and
reports problems as findings. It never writes a file, registers anything,
builds a graph or runs a program other than git, and always exits 0.
"""

import json
import os
from pathlib import Path
import sys
import tomllib

from graphify_worktree import export_state, harness, hooks, settings, upstream
from graphify_worktree.errors import KnowledgeError, PolicyError, SettingsError
from graphify_worktree.policy import SETTINGS, get_policy


def _versions(findings):
    pins = {}
    for package, pinned in upstream.PINS.items():
        installed = upstream.installed_version(package)
        pins[package] = {"pinned": pinned, "installed": installed, "ok": installed == pinned}
        if installed != pinned:
            findings.append(f"{package} {installed or 'is not installed'} is installed but {pinned} is pinned; "
                            "graph commands and the MCP server refuse to run; metadata-only retire commands remain available. Reinstall grovekeeper.")
    return pins


def _configuration(findings):
    path = settings.config_path()
    report = {"path": str(path), "exists": False}
    try:
        report["exists"] = path.exists()
        report["settings"] = {name: {"source": value["source"]} for name, value in settings.describe().items()}
    except (SettingsError, OSError) as error:
        report["error"] = str(error)
        findings.append(f"configuration: {error}")
    return report


def _locations(findings, reported=None):
    """reported: the configuration error already in findings; a location failing with it adds no finding."""
    locations = {}

    def failed(name, error):
        locations[name] = {"error": str(error)}
        if str(error) != reported:
            findings.append(f"{name}: {error}")

    for name, read in (("graph_vault_root", settings.graph_vault_root), ("memory_vault", settings.memory_vault)):
        try:
            path = read()
        except KnowledgeError as error:
            failed(name, error)
            continue
        traverses = Path(os.path.realpath(path)) != path
        locations[name] = {"path": str(path), "exists": path.exists(), "traverses_symlink": traverses}
        if traverses:
            findings.append(f"{name} {path} is or passes through a symlink; memory and export refuse symlinked paths")
    try:
        cli = settings.obsidian_cli()
        present = cli.is_file() and os.access(cli, os.X_OK)
        locations["obsidian_cli"] = {"path": str(cli), "present": present}
        if not present:
            findings.append(f"Obsidian CLI not found at {cli}; only `export --open` needs it")
    except KnowledgeError as error:
        failed("obsidian_cli", error)
    return locations


def _mcp_command(name, home):
    """The registered graphify MCP command, or None; ValueError/OSError for an unreadable file."""
    if name == "claude":
        path = home / ".claude.json"
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}) if path.exists() else {}
    elif name == "codex":
        path = home / ".codex/config.toml"
        servers = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {}) if path.exists() else {}
    else:
        path = home / ".omp/agent/mcp.json"
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}) if path.exists() else {}
    server = servers.get(harness.SERVER)
    return server.get("command") if isinstance(server, dict) else None


def _hooks(name, home, findings):
    path = home / harness.HOOK_FILES[name]
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    report = {}
    for spec in harness.HOOKS:
        groups = data.get("hooks", {}).get(spec.event, []) if isinstance(data, dict) else []
        ours = [(group.get("matcher"), handler.get("command")) for group in groups for handler in group.get("hooks", [])
                if str(handler.get("command", "")).endswith(f"{harness.CLI_COMMAND} {spec.subcommand}")]
        report[spec.subcommand] = {"registered": bool(ours), "commands": [command for _, command in ours]}
        for matcher, command in ours:
            executable = command.rsplit(" ", 1)[0].strip("'")
            if not Path(executable).exists():
                findings.append(f"{name}: the {spec.label} hook runs {executable}, which does not exist")
            if matcher != spec.matcher:
                findings.append(f"{name}: the {spec.label} hook matcher is {matcher!r}, not {spec.matcher!r}; "
                                f"re-run the installer with it enabled to update it")
    return report


def _harnesses(findings):
    home = Path.home()
    expected = Path(sys.executable).parent / harness.SERVER_COMMAND
    report = {}
    for name, spec in harness.HARNESSES.items():
        if not (home / spec.marker).is_dir():
            report[name] = {"present": False}
            continue
        entry = report[name] = {"present": True}
        try:
            command = _mcp_command(name, home)
        except (OSError, ValueError) as error:
            entry["mcp"] = {"error": str(error)}
            findings.append(f"{name}: cannot read its MCP configuration: {error}")
        else:
            here = command is not None and os.path.realpath(command) == os.path.realpath(expected)
            entry["mcp"] = {"registered": command is not None, "command": command, "this_installation": here}
            if command is None:
                findings.append(f"{name}: the graphify MCP server is not registered; run ./install.sh")
            elif not here:
                findings.append(f"{name}: the graphify MCP server runs {command}, not this installation's {expected}")
        if name in harness.HOOK_FILES:
            try:
                entry["hooks"] = _hooks(name, home, findings)
            except (OSError, ValueError) as error:
                entry["hooks"] = {"error": str(error)}
                findings.append(f"{name}: cannot read {harness.HOOK_FILES[name]}: {error}")
    return report


def _repository(path, findings):
    try:
        policy = get_policy(path)
    except PolicyError as error:
        if error.not_repository:
            return {"policy": {"state": "not_repository", "reason": str(error)}}
        policy = error.policy or {"state": "invalid", "reason": str(error)}
        findings.append(f"repository policy: {error}")
    for variable in policy.get("deprecated_environment", []):
        name = next(name for name, (_, env) in SETTINGS.items() if env == variable)
        findings.append(f"{variable} is deprecated and decides {name} here; run "
                        f"graphify-worktree configure --{name.replace('_', '-')} on PATH, then unset it")
    for key in policy.get("invalid_settings", []):
        findings.append(f"{key} has an invalid or conflicting value and counts as off")
    result = {"policy": policy}
    try:
        status = hooks.status(path)
    except (KnowledgeError, OSError) as error:
        status = {"state": "error", "reason": str(error)}
    result["hooks"] = status
    if status["state"] in ("conflict", "relocated", "error"):
        findings.append(f"git hooks {status['state']}: {status.get('reason')}")
    if status.get("unforwarded"):
        findings.append(f"git hooks added after hooks install are not forwarded: {', '.join(status['unforwarded'])}; "
                        "run hooks install again")
    return result


def _retirement(findings):
    report = {"path": str(export_state.retirement_path()), "sample_limit": 50,
              "eligibility": "not checked; use retire reconcile --all --dry-run"}
    if os.environ.get("HARNESS_KNOWLEDGE_MODE", "default") != "default":
        return {**report, "state": "skipped", "reason": "session knowledge mode is not default"}
    try:
        with export_state.retirement_directory() as directory:
            if directory is None:
                return {**report, "state": "absent", "registered_exports": 0, "exports": []}
            identifiers = export_state.export_ids(directory)
            exports = []
            for identifier in identifiers[:report["sample_limit"]]:
                entry = {"export_uuid": identifier}
                try:
                    record, _ = export_state.read_record(directory, identifier)
                    entry["publication_state"] = record.get("state") if record is not None else "missing"
                except (KnowledgeError, OSError) as error:
                    entry["error"] = str(error)
                    findings.append(f"retirement export {identifier}: {error}")
                exports.append(entry)
            return {**report, "state": "present", "registered_exports": len(identifiers),
                    "truncated": len(identifiers) > len(exports), "exports": exports}
    except (KnowledgeError, OSError) as error:
        findings.append(f"retirement: {error}")
        return {**report, "state": "error", "error": str(error)}


def report(path):
    """The read-only installation report for the repository containing path."""
    findings = []
    pins = _versions(findings)
    configuration = _configuration(findings)
    result = {
        "package": {"grovekeeper": upstream.installed_version("grovekeeper"), "python": sys.executable},
        "pins": pins,
        "configuration": configuration,
        "locations": _locations(findings, configuration.get("error")),
        "harnesses": _harnesses(findings),
        "session": {"HARNESS_KNOWLEDGE_MODE": os.environ.get("HARNESS_KNOWLEDGE_MODE", "default"),
                    "debug": os.environ.get("WORKTREE_KNOWLEDGE_DEBUG") == "1"},
        "repository": _repository(path, findings),
        "retirement": _retirement(findings),
    }
    result["findings"] = findings
    return result
