"""Install or remove the harness registrations for Claude Code, Codex and OMP.

Run by install.sh/uninstall.sh after `uv tool install`. Every edit is
idempotent, limited to entries this tool owns, and preceded by a copy of the
original file in the backup directory. Harnesses whose configuration
directory does not exist are skipped.
"""

import argparse
import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

from graphify_worktree import settings


TITLE = "Worktree knowledge"
START = "<!-- worktree-knowledge:start -->"
END = "<!-- worktree-knowledge:end -->"
SERVER = "graphify"
SERVER_COMMAND = "graphify-worktree-mcp"
TOML_HEADER = f"[mcp_servers.{SERVER}]"
IGNORE_LINE = "graphify-out/"
CODEX_TIMEOUT_SEC = 600
OMP_TIMEOUT_MS = 600_000


@dataclass(frozen=True)
class Harness:
    rules: str      # relative to HOME
    level: int      # Markdown heading level of the rules section
    marker: str     # directory whose presence means the harness is installed


HARNESSES = {
    "claude": Harness(".claude/CLAUDE.md", 1, ".claude"),
    "codex": Harness(".codex/AGENTS.md", 1, ".codex"),
    "omp": Harness(".omp/agent/RULES.md", 2, ".omp/agent"),
}


# Rendering -----------------------------------------------------------------

def render(template, *, level, skill_ref):
    text = template.replace("{{HEADING}}", "#" * level + " " + TITLE).replace("{{SKILL}}", skill_ref)
    leftover = re.findall(r"\{\{(\w+)\}\}", text)
    if leftover:
        raise ValueError("unknown template placeholders: " + ", ".join(leftover))
    return text


def skill_reference(name, skill_dir):
    if name == "omp":
        return f"`skill://{skill_dir.name}`"
    path = skill_dir / "SKILL.md"
    if path.is_relative_to(Path.home()):
        return f"`~/{path.relative_to(Path.home()).as_posix()}`"
    return f"`{path}`"


# Markdown sections ---------------------------------------------------------

def _ends_section(line, level):
    return re.match(r"#{1,%d} \S" % level, line) is not None


def upsert_section(text, block, level):
    """Replace our marked block, adopt an unmarked section, or append."""
    marked = f"{START}\n{block.rstrip(chr(10))}\n{END}\n"
    if START in text and END in text:
        before, rest = text.split(START, 1)
        after = rest.split(END, 1)[1].removeprefix("\n")
        return before + marked + after
    lines = text.splitlines(keepends=True)
    heading = "#" * level + " " + TITLE
    for index, line in enumerate(lines):
        if line.rstrip("\n") == heading:
            end = index + 1
            while end < len(lines) and not _ends_section(lines[end], level):
                end += 1
            after = "".join(lines[end:])
            return "".join(lines[:index]) + marked + ("\n" + after if after else "")
    if not text.strip():
        return marked
    return text.rstrip("\n") + "\n\n" + marked


def remove_section(text):
    if START not in text or END not in text:
        return text
    before, rest = text.split(START, 1)
    before, after = before.rstrip("\n"), rest.split(END, 1)[1].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    return before + "\n" if before else after


# Codex TOML ----------------------------------------------------------------

def _toml_tables(lines):
    """Yield (start, end, header) for every table header line."""
    headers = [index for index, line in enumerate(lines) if line.lstrip().startswith("[")]
    for position, start in enumerate(headers):
        end = headers[position + 1] if position + 1 < len(headers) else len(lines)
        yield start, end, lines[start].strip()


def upsert_toml_server(text, command):
    owned = {"command": json.dumps(command), "tool_timeout_sec": str(CODEX_TIMEOUT_SEC)}
    lines = text.splitlines(keepends=True)
    table = next(((start, end) for start, end, header in _toml_tables(lines) if header == TOML_HEADER), None)
    if table is None:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        body = "".join(f"{key} = {value}\n" for key, value in owned.items())
        result = prefix + ("\n" if prefix.strip() else "") + TOML_HEADER + "\n" + body
    else:
        start, end = table
        for key, value in owned.items():
            pattern = re.compile(r"\s*%s\s*=" % re.escape(key))
            existing = next((index for index in range(start + 1, end) if pattern.match(lines[index])), None)
            if existing is None:
                lines.insert(start + 1, f"{key} = {value}\n")
                end += 1
            else:
                lines[existing] = f"{key} = {value}\n"
        result = "".join(lines)
    server = tomllib.loads(result)["mcp_servers"][SERVER]
    if server.get("command") != command or server.get("tool_timeout_sec") != CODEX_TIMEOUT_SEC:
        raise ValueError(f"could not update {TOML_HEADER} safely; edit it by hand")
    return result


def remove_toml_server(text):
    lines = text.splitlines(keepends=True)
    drop = set()
    for start, end, header in _toml_tables(lines):
        if header == TOML_HEADER or header.startswith(f"[mcp_servers.{SERVER}."):
            drop.update(range(start, end))
    return "".join(line for index, line in enumerate(lines) if index not in drop)


# OMP JSON ------------------------------------------------------------------

def upsert_json_server(data, command):
    result = copy.deepcopy(data)
    servers = result.setdefault("mcpServers", {})
    servers[SERVER] = {**servers.get(SERVER, {}), "type": "stdio", "command": command, "timeout": OMP_TIMEOUT_MS}
    return result


def remove_json_server(data):
    result = copy.deepcopy(data)
    result.get("mcpServers", {}).pop(SERVER, None)
    return result


# Claude --------------------------------------------------------------------

def register_claude(command):
    """Claude rewrites ~/.claude.json itself, so only its CLI writes it."""
    desired = {"type": "stdio", "command": command, "args": [], "env": {}}
    if _claude_server() == desired:
        return "unchanged"
    executable = shutil.which("claude")
    if executable is None:
        return "manual"
    subprocess.run([executable, "mcp", "remove", "--scope", "user", SERVER], capture_output=True)
    subprocess.run([executable, "mcp", "add-json", "--scope", "user", SERVER, json.dumps(desired)],
                   check=True, capture_output=True)
    return "registered"


def unregister_claude(command):
    current = _claude_server()
    executable = shutil.which("claude")
    if current is None or current.get("command") != command:
        return "unchanged"
    if executable is None:
        return "manual"
    subprocess.run([executable, "mcp", "remove", "--scope", "user", SERVER], check=True, capture_output=True)
    return "removed"


def _claude_server():
    try:
        config = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    server = config.get("mcpServers", {}).get(SERVER) if isinstance(config, dict) else None
    return server if isinstance(server, dict) else None


# Global ignore, skill links, OMP discovery ----------------------------------

def global_excludes_file():
    result = subprocess.run(["git", "config", "--global", "--path", "--get", "core.excludesFile"],
                            capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "git" / "ignore"


def ensure_ignore_line(path):
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.strip() in (IGNORE_LINE, IGNORE_LINE.rstrip("/")) for line in text.splitlines()):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = text if not text or text.endswith("\n") else text + "\n"
    path.write_text(prefix + IGNORE_LINE + "\n", encoding="utf-8")
    return True


def link(path, target, *, backup_dir):
    if path.is_symlink() and path.resolve() == target.resolve():
        return "unchanged"
    if path.exists() or path.is_symlink():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(_free(backup_dir / path.name)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return "linked"


def unlink_if_points_to(path, target):
    if path.is_symlink() and path.resolve() == target.resolve():
        path.unlink()
        return True
    return False


def omp_discovers(skill_dir, config_text):
    lines = config_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "customDirectories:":
            continue
        for item in lines[index + 1:]:
            match = re.match(r"\s*-\s*(.+?)\s*$", item)
            if not match:
                break
            if Path(match.group(1).strip("'\"")).expanduser() == skill_dir.parent:
                return True
    return False


# Files ---------------------------------------------------------------------

def _free(path):
    candidate, number = path, 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{path.name}.{number}")
        number += 1
    return candidate


def _write_if_changed(path, text, backup_dir):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    if old is not None:
        copy_path = _free(backup_dir / "files" / path.relative_to(Path.home()))
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if old is not None else 0o644
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def _edit(path, change, backup_dir):
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    return _write_if_changed(path, change(old), backup_dir)


def _edit_json(path, change, backup_dir):
    if not path.exists():
        return _write_if_changed(path, json.dumps(change({}), indent=2) + "\n", backup_dir)
    old = json.loads(path.read_text(encoding="utf-8"))
    new = change(old)
    return new != old and _write_if_changed(path, json.dumps(new, indent=2, ensure_ascii=False) + "\n", backup_dir)


# Install / uninstall -------------------------------------------------------

def install(repo, bin_dir, backup_dir, *, report=print):
    repo, bin_dir, backup_dir = Path(repo).resolve(), Path(bin_dir), Path(backup_dir)
    home = Path.home()
    template = (repo / "harness/worktree-knowledge.md").read_text(encoding="utf-8")
    source = repo / "skills/worktree-knowledge"
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"not a grovekeeper checkout: {repo}")
    command = str(bin_dir / SERVER_COMMAND)
    if not Path(command).exists():
        report(f"warning: {command} does not exist yet; MCP registrations point at it")

    skill_dir = settings.skill_dir()
    report(f"skill: {skill_dir} -> {source}: {link(skill_dir, source, backup_dir=backup_dir / 'skills')}")

    for name, spec in HARNESSES.items():
        if not (home / spec.marker).is_dir():
            report(f"{name}: not installed, skipped")
            continue
        block = render(template, level=spec.level, skill_ref=skill_reference(name, skill_dir))
        rules = home / spec.rules
        changed = _edit(rules, lambda text: upsert_section(text, block, spec.level), backup_dir)
        report(f"{name}: rules {rules} {'updated' if changed else 'unchanged'}")
        if name == "claude":
            claude_skill = home / ".claude/skills" / skill_dir.name
            report(f"claude: skill link {link(claude_skill, skill_dir, backup_dir=backup_dir / 'claude-skills')}")
            status = register_claude(command)
            if status == "manual":
                status = f"claude CLI not found; run: claude mcp add-json --scope user {SERVER} " \
                         + repr(json.dumps({"type": "stdio", "command": command, "args": [], "env": {}}))
            report(f"claude: MCP {status}")
        elif name == "codex":
            changed = _edit(home / ".codex/config.toml", lambda text: upsert_toml_server(text, command), backup_dir)
            report(f"codex: MCP {'updated' if changed else 'unchanged'}")
        elif name == "omp":
            changed = _edit_json(home / ".omp/agent/mcp.json", lambda data: upsert_json_server(data, command), backup_dir)
            report(f"omp: MCP {'updated' if changed else 'unchanged'}")
            config = home / ".omp/agent/config.yml"
            if not omp_discovers(skill_dir, config.read_text(encoding="utf-8") if config.exists() else ""):
                report(f"omp: add {skill_dir.parent} to skills.customDirectories in {config} so OMP discovers the skill")

    ignore = global_excludes_file()
    report(f"git: {ignore} {'now ignores' if ensure_ignore_line(ignore) else 'already ignores'} {IGNORE_LINE}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        report(f"warning: {bin_dir} is not on PATH; harness instructions call graphify-worktree by name")


def uninstall(repo, bin_dir, backup_dir, *, report=print):
    home = Path.home()
    command = str(Path(bin_dir) / SERVER_COMMAND)
    source = Path(repo).resolve() / "skills/worktree-knowledge"
    skill_dir = settings.skill_dir()
    backup_dir = Path(backup_dir)
    for name, spec in HARNESSES.items():
        if not (home / spec.marker).is_dir():
            continue
        changed = _edit(home / spec.rules, remove_section, backup_dir) if (home / spec.rules).exists() else False
        report(f"{name}: rules {'section removed' if changed else 'unchanged'}")
        if name == "claude":
            unlink_if_points_to(home / ".claude/skills" / skill_dir.name, skill_dir)
            report(f"claude: MCP {unregister_claude(command)}")
        elif name == "codex" and (home / ".codex/config.toml").exists():
            _edit(home / ".codex/config.toml", remove_toml_server, backup_dir)
        elif name == "omp" and (home / ".omp/agent/mcp.json").exists():
            _edit_json(home / ".omp/agent/mcp.json", remove_json_server, backup_dir)
    if unlink_if_points_to(skill_dir, source):
        report(f"skill: removed link {skill_dir}")
    report(f"pre-uninstall copies of edited files: {backup_dir}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m graphify_worktree.harness")
    actions = parser.add_subparsers(dest="action", required=True)
    install_parser = actions.add_parser("install")
    install_parser.add_argument("--repo", required=True, help="grovekeeper checkout providing the skill and rules")
    install_parser.add_argument("--bin-dir", required=True, help="directory holding graphify-worktree-mcp")
    install_parser.add_argument("--backup-dir", required=True)
    uninstall_parser = actions.add_parser("uninstall")
    uninstall_parser.add_argument("--repo", required=True)
    uninstall_parser.add_argument("--bin-dir", required=True)
    uninstall_parser.add_argument("--backup-dir", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.action == "install":
            install(arguments.repo, arguments.bin_dir, arguments.backup_dir)
        else:
            uninstall(arguments.repo, arguments.bin_dir, arguments.backup_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"grovekeeper harness: {error}")


if __name__ == "__main__":
    main()
