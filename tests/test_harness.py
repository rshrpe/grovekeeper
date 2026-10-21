import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib

import pytest

from graphify_worktree import harness


START = "<!-- worktree-knowledge:start -->"
END = "<!-- worktree-knowledge:end -->"
BLOCK = "# Worktree knowledge\n\nNew rules.\n"
MARKED = f"{START}\n# Worktree knowledge\n\nNew rules.\n{END}\n"


# Rendering -----------------------------------------------------------------

def test_render_substitutes_heading_level_and_skill_reference():
    template = "{{HEADING}}\n\nLoad {{SKILL}} first.\n"
    assert harness.render(template, level=2, skill_ref="`skill://worktree-knowledge`") == (
        "## Worktree knowledge\n\nLoad `skill://worktree-knowledge` first.\n"
    )


def test_render_refuses_unknown_placeholder():
    with pytest.raises(ValueError, match="UNKNOWN"):
        harness.render("{{HEADING}} {{UNKNOWN}}", level=1, skill_ref="x")


def test_skill_reference_uses_tilde_for_paths_under_home(home):
    assert harness.skill_reference("claude", home / ".agents/skills/worktree-knowledge") == (
        "`~/.agents/skills/worktree-knowledge/SKILL.md`"
    )
    assert harness.skill_reference("omp", home / ".agents/skills/worktree-knowledge") == "`skill://worktree-knowledge`"


def test_skill_reference_keeps_absolute_path_outside_home(home, tmp_path):
    assert harness.skill_reference("codex", tmp_path / "skills/wk") == f"`{tmp_path}/skills/wk/SKILL.md`"


def test_repository_template_renders_every_harness_without_placeholders(home):
    template = (Path(__file__).parents[1] / "harness/worktree-knowledge.md").read_text(encoding="utf-8")
    for name in ("claude", "codex", "omp"):
        rendered = harness.render(template, level=harness.HARNESSES[name].level,
                                  skill_ref=harness.skill_reference(name, home / ".agents/skills/worktree-knowledge"))
        assert "{{" not in rendered and rendered.startswith("#" * harness.HARNESSES[name].level + " Worktree knowledge\n")


# Markdown sections ---------------------------------------------------------

def test_upsert_into_empty_file_writes_marked_block():
    assert harness.upsert_section("", BLOCK, level=1) == MARKED


def test_upsert_replaces_unmarked_section_that_fills_the_file():
    old = "# Worktree knowledge\n\nOld rules.\n\nMore old rules.\n"
    assert harness.upsert_section(old, BLOCK, level=1) == MARKED


def test_upsert_replaces_only_the_matching_subsection():
    old = ("# Skill activation gate\n\nGate text.\n\n## Worktree knowledge\n\nOld rules.\n"
           "### Detail\n\nOld detail.\n\n## Other\n\nKeep me.\n")
    block = "## Worktree knowledge\n\nNew rules.\n"
    assert harness.upsert_section(old, block, level=2) == (
        "# Skill activation gate\n\nGate text.\n\n"
        f"{START}\n## Worktree knowledge\n\nNew rules.\n{END}\n\n"
        "## Other\n\nKeep me.\n"
    )


def test_upsert_appends_after_unrelated_content():
    assert harness.upsert_section("# Mine\n\nKeep.", BLOCK, level=1) == f"# Mine\n\nKeep.\n\n{MARKED}"


def test_upsert_replaces_previous_marked_block_and_is_idempotent():
    first = harness.upsert_section("# Mine\n\nKeep.\n", "# Worktree knowledge\n\nOld.\n", level=1)
    second = harness.upsert_section(first, BLOCK, level=1)
    assert second == f"# Mine\n\nKeep.\n\n{MARKED}"
    assert harness.upsert_section(second, BLOCK, level=1) == second


def test_remove_section_drops_only_the_marked_block():
    text = f"# Mine\n\nKeep.\n\n{MARKED}\n# After\n\nAlso keep.\n"
    assert harness.remove_section(text) == "# Mine\n\nKeep.\n\n# After\n\nAlso keep.\n"


def test_remove_section_leaves_unmarked_text_untouched():
    text = "# Worktree knowledge\n\nHand-written, not ours to delete.\n"
    assert harness.remove_section(text) == text


# Codex TOML ----------------------------------------------------------------

def test_toml_server_appended_when_absent():
    text = 'model = "x"\n\n[profiles.fast]\nmodel = "y"\n'
    result = harness.upsert_toml_server(text, "/bin/wk-mcp")
    parsed = tomllib.loads(result)
    assert parsed["mcp_servers"]["graphify"] == {"command": "/bin/wk-mcp", "tool_timeout_sec": 600}
    assert parsed["profiles"]["fast"]["model"] == "y" and result.startswith(text)


def test_toml_server_updates_owned_keys_and_keeps_user_keys_and_subtables():
    text = ('[mcp_servers.graphify]\ncommand = "/old/path"\nenv = { A = "1" }\ntool_timeout_sec = 60\n\n'
            '[mcp_servers.graphify.tools.query_graph]\napproval_mode = "approve"\n\n[other]\nx = 1\n')
    parsed = tomllib.loads(harness.upsert_toml_server(text, "/bin/wk-mcp"))
    assert parsed["mcp_servers"]["graphify"]["command"] == "/bin/wk-mcp"
    assert parsed["mcp_servers"]["graphify"]["tool_timeout_sec"] == 600
    assert parsed["mcp_servers"]["graphify"]["env"] == {"A": "1"}
    assert parsed["mcp_servers"]["graphify"]["tools"]["query_graph"]["approval_mode"] == "approve"
    assert parsed["other"] == {"x": 1}


def test_toml_upsert_is_idempotent():
    once = harness.upsert_toml_server("", "/bin/wk-mcp")
    assert harness.upsert_toml_server(once, "/bin/wk-mcp") == once


def test_toml_remove_drops_server_and_its_subtables_only():
    text = ('a = 1\n\n[mcp_servers.graphify]\ncommand = "/bin/wk-mcp"\n\n'
            '[mcp_servers.graphify.tools.x]\napproval_mode = "approve"\n\n[mcp_servers.other]\ncommand = "o"\n')
    parsed = tomllib.loads(harness.remove_toml_server(text))
    assert parsed == {"a": 1, "mcp_servers": {"other": {"command": "o"}}}


# OMP JSON ------------------------------------------------------------------

def test_json_server_set_without_touching_other_servers():
    data = {"$schema": "s", "mcpServers": {"linear": {"type": "http"}}, "disabledServers": ["x"]}
    result = harness.upsert_json_server(data, "/bin/wk-mcp")
    assert result == {"$schema": "s", "disabledServers": ["x"], "mcpServers": {
        "linear": {"type": "http"},
        "graphify": {"type": "stdio", "command": "/bin/wk-mcp", "timeout": 600000},
    }}
    assert "graphify" not in data["mcpServers"], "input must not be mutated"


def test_json_server_removal_keeps_other_servers():
    data = {"mcpServers": {"graphify": {"command": "c"}, "linear": {"type": "http"}}}
    assert harness.remove_json_server(data) == {"mcpServers": {"linear": {"type": "http"}}}


# Global ignore -------------------------------------------------------------

def test_ignore_line_added_to_default_xdg_file(home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = harness.global_excludes_file()
    assert path == home / ".config/git/ignore"
    assert harness.ensure_ignore_line(path) is True
    assert path.read_text() == "graphify-out/\n"
    assert harness.ensure_ignore_line(path) is False


def test_ignore_line_appended_on_its_own_line(home, tmp_path):
    path = tmp_path / "ignore"
    path.write_text("*.log")
    harness.ensure_ignore_line(path)
    assert path.read_text() == "*.log\ngraphify-out/\n"


def test_configured_core_excludes_file_is_respected(home, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    target = tmp_path / "custom-ignore"
    subprocess.run(["git", "config", "--global", "core.excludesFile", str(target)], check=True)
    assert harness.global_excludes_file() == target


# Skill links ---------------------------------------------------------------

def test_link_creates_symlink_and_parents(tmp_path):
    source = tmp_path / "repo/skills/worktree-knowledge"
    source.mkdir(parents=True)
    link = tmp_path / "home/.agents/skills/worktree-knowledge"
    assert harness.link(link, source, backup_dir=tmp_path / "backup") == "linked"
    assert link.is_symlink() and link.resolve() == source.resolve()
    assert harness.link(link, source, backup_dir=tmp_path / "backup") == "unchanged"


def test_link_moves_existing_directory_into_backup(tmp_path):
    source = tmp_path / "repo/skill"
    source.mkdir(parents=True)
    link = tmp_path / "skills/worktree-knowledge"
    (link / "scripts").mkdir(parents=True)
    (link / "scripts/old.py").write_text("old")
    backup = tmp_path / "backup"
    assert harness.link(link, source, backup_dir=backup) == "linked"
    assert link.resolve() == source.resolve()
    assert (backup / "worktree-knowledge/scripts/old.py").read_text() == "old"


def test_unlink_removes_only_our_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    ours = tmp_path / "ours"
    ours.symlink_to(target)
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    assert harness.unlink_if_points_to(ours, target) is True and not ours.exists()
    assert harness.unlink_if_points_to(theirs, target) is False and theirs.is_dir()


# Claude registration -------------------------------------------------------

FAKE_CLAUDE = """#!{python}
import json, os, pathlib, sys
with open({log!r}, "a") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
config = pathlib.Path(os.environ["HOME"]) / ".claude.json"
data = json.loads(config.read_text()) if config.exists() else {{}}
servers = data.setdefault("mcpServers", {{}})
if sys.argv[1:3] == ["mcp", "add-json"]:
    servers[sys.argv[-2]] = json.loads(sys.argv[-1])
elif sys.argv[1:3] == ["mcp", "remove"]:
    if servers.pop(sys.argv[-1], None) is None:
        sys.exit(1)
config.write_text(json.dumps(data))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that logs its arguments and edits ~/.claude.json like the real CLI."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "claude-calls.log"
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_claude_registration_skipped_when_already_current(home, fake_claude):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"graphify": {
        "type": "stdio", "command": "/bin/wk-mcp", "args": [], "env": {}}}}))
    assert harness.register_claude("/bin/wk-mcp") == "unchanged"
    assert not fake_claude.exists()


def test_claude_registration_replaces_stale_entry_through_cli(home, fake_claude):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"graphify": {"type": "stdio", "command": "/old"}}}))
    assert harness.register_claude("/bin/wk-mcp") == "registered"
    calls = fake_claude.read_text().splitlines()
    assert calls[0] == "mcp remove --scope user graphify"
    assert calls[1].startswith("mcp add-json --scope user graphify ")
    assert json.loads(calls[1].split("graphify ", 1)[1]) == {
        "type": "stdio", "command": "/bin/wk-mcp", "args": [], "env": {}}


def test_claude_registration_reports_missing_cli(home, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert harness.register_claude("/bin/wk-mcp") == "manual"


# OMP skill discovery -------------------------------------------------------

def test_omp_discovers_skill_parent_in_custom_directories(home):
    skill = home / ".agents/skills/worktree-knowledge"
    config = f"skills:\n  customDirectories:\n    - {home}/.agents/skills\n"
    assert harness.omp_discovers(skill, config) is True
    assert harness.omp_discovers(skill, "skills:\n  customDirectories:\n    - /elsewhere\n") is False
    assert harness.omp_discovers(skill, "skills:\n  customDirectories:\n    - ~/.agents/skills\n") is True


# End to end ----------------------------------------------------------------

@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "harness").mkdir(parents=True)
    (repo / "harness/worktree-knowledge.md").write_text("{{HEADING}}\n\nLoad {{SKILL}} first.\n")
    (repo / "skills/worktree-knowledge").mkdir(parents=True)
    (repo / "skills/worktree-knowledge/SKILL.md").write_text("---\nname: worktree-knowledge\n---\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "graphify-worktree-mcp").write_text("#!/bin/sh\n")
    return repo, bin_dir


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def test_install_registers_every_present_harness_then_uninstall_reverts(home, tmp_path, checkout, fake_claude, monkeypatch):
    repo, bin_dir = checkout
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text('model = "x"\n')
    (home / ".omp/agent").mkdir(parents=True)
    (home / ".omp/agent/RULES.md").write_text("# Gate\n\nGate text.\n")
    (home / ".omp/agent/mcp.json").write_text(json.dumps({"mcpServers": {"linear": {"type": "http"}}}, indent=2) + "\n")
    old_skill = home / ".agents/skills/worktree-knowledge"
    old_skill.mkdir(parents=True)
    (old_skill / "SKILL.md").write_text("old skill")
    backup = tmp_path / "backup"
    command = str(bin_dir / "graphify-worktree-mcp")

    harness.install(repo, bin_dir, backup)

    skill = home / ".agents/skills/worktree-knowledge"
    assert skill.is_symlink() and (skill / "SKILL.md").read_text().startswith("---")
    assert (home / ".claude/skills/worktree-knowledge/SKILL.md").read_text().startswith("---")
    assert (backup / "skills/worktree-knowledge/SKILL.md").read_text() == "old skill"
    assert (home / ".claude/CLAUDE.md").read_text() == (
        f"{START}\n# Worktree knowledge\n\nLoad `~/.agents/skills/worktree-knowledge/SKILL.md` first.\n{END}\n")
    assert (home / ".codex/AGENTS.md").read_text().startswith(f"{START}\n# Worktree knowledge\n")
    assert (home / ".omp/agent/RULES.md").read_text() == (
        f"# Gate\n\nGate text.\n\n{START}\n## Worktree knowledge\n\nLoad `skill://worktree-knowledge` first.\n{END}\n")
    assert tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["graphify"]["command"] == command
    omp = json.loads((home / ".omp/agent/mcp.json").read_text())
    assert omp["mcpServers"]["graphify"] == {"type": "stdio", "command": command, "timeout": 600000}
    assert omp["mcpServers"]["linear"] == {"type": "http"}
    assert "mcp add-json --scope user graphify" in fake_claude.read_text()
    assert "graphify-out/\n" in (home / ".config/git/ignore").read_text()
    # Edited configuration files keep a pre-install copy.
    assert (backup / "files/.omp/agent/RULES.md").read_text() == "# Gate\n\nGate text.\n"

    # Reinstalling is a no-op for files (Claude is current only once its own CLI wrote it).
    before = snapshot(home)
    harness.install(repo, bin_dir, backup)
    assert snapshot(home) == before

    harness.uninstall(repo, bin_dir, tmp_path / "uninstall-backup")
    assert not skill.exists() and not (home / ".claude/skills/worktree-knowledge").exists()
    assert (home / ".omp/agent/RULES.md").read_text() == "# Gate\n\nGate text.\n"
    assert (home / ".claude/CLAUDE.md").read_text() == ""
    assert "graphify" not in tomllib.loads((home / ".codex/config.toml").read_text()).get("mcp_servers", {})
    assert json.loads((home / ".omp/agent/mcp.json").read_text())["mcpServers"] == {"linear": {"type": "http"}}
    assert fake_claude.read_text().splitlines()[-1] == "mcp remove --scope user graphify"
    # Backups and the shared ignore line are left for the user.
    assert (backup / "skills/worktree-knowledge/SKILL.md").read_text() == "old skill"
    assert "graphify-out/\n" in (home / ".config/git/ignore").read_text()


def test_install_skips_absent_harnesses(home, tmp_path, checkout, fake_claude):
    repo, bin_dir = checkout
    harness.install(repo, bin_dir, tmp_path / "backup")
    assert not (home / ".codex").exists() and not (home / ".omp").exists() and not (home / ".claude").exists()
    assert not fake_claude.exists()


# Session-start recall hook ---------------------------------------------------

RECALL = "/bin/graphify-worktree recall"
OURS = {"type": "command", "command": RECALL, "timeout": 5}
SUPERPOWERS = {"matcher": "startup", "hooks": [{"type": "command", "command": "other-session-hook"}]}


def test_session_hook_added_beside_existing_hooks_without_mutating_input():
    data = {"model": "x", "hooks": {"SessionStart": [SUPERPOWERS], "PreToolUse": [{"hooks": []}]}}
    result = harness.upsert_hook(harness.RECALL_HOOK, data, RECALL)
    assert result == {"model": "x", "hooks": {
        "SessionStart": [SUPERPOWERS, {"matcher": "startup|clear|compact", "hooks": [OURS]}],
        "PreToolUse": [{"hooks": []}]}}
    assert data["hooks"]["SessionStart"] == [SUPERPOWERS]


def test_session_hook_upsert_is_idempotent_and_repairs_our_entry_in_place():
    once = harness.upsert_hook(harness.RECALL_HOOK, {}, RECALL)
    assert harness.upsert_hook(harness.RECALL_HOOK, once, RECALL) == once
    drifted = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{**OURS, "timeout": 60}]}, SUPERPOWERS]}}
    assert harness.upsert_hook(harness.RECALL_HOOK, drifted, RECALL) == {"hooks": {"SessionStart": [
        {"matcher": "startup|clear|compact", "hooks": [OURS]}, SUPERPOWERS]}}


def test_session_hook_removal_keeps_other_handlers_and_drops_empty_containers():
    shared = {"matcher": "startup", "hooks": [OURS, {"type": "command", "command": "keep"}]}
    data = {"hooks": {"SessionStart": [shared, SUPERPOWERS]}}
    assert harness.remove_hook(harness.RECALL_HOOK, data, RECALL) == {"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": "keep"}]}, SUPERPOWERS]}}
    only_ours = harness.upsert_hook(harness.RECALL_HOOK, {"theme": "dark"}, RECALL)
    assert harness.remove_hook(harness.RECALL_HOOK, only_ours, RECALL) == {"theme": "dark"}


def test_session_hook_removal_leaves_other_installations_alone():
    other = harness.upsert_hook(harness.RECALL_HOOK, {}, "/elsewhere/graphify-worktree recall")
    assert harness.remove_hook(harness.RECALL_HOOK, other, RECALL) == other


def recall_hooks(path):
    data = json.loads(path.read_text()) if path.exists() else {}
    return [handler["command"] for group in data.get("hooks", {}).get("SessionStart", [])
            for handler in group["hooks"]]


def test_session_recall_is_registered_only_when_asked(home, tmp_path, checkout, fake_claude):
    repo, bin_dir = checkout
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".omp/agent").mkdir(parents=True)
    claude, codex = home / ".claude/settings.json", home / ".codex/hooks.json"
    command = f"{bin_dir}/graphify-worktree recall"
    messages = []

    harness.install(repo, bin_dir, tmp_path / "backup")
    assert recall_hooks(claude) == [] and not codex.exists()

    harness.install(repo, bin_dir, tmp_path / "backup", session_recall=True, report=messages.append)
    assert recall_hooks(claude) == [command] and recall_hooks(codex) == [command]
    assert any("trust" in line and "/hooks" in line for line in messages if line.startswith("codex"))
    assert any("session-start" in line and "not supported" in line for line in messages if line.startswith("omp"))

    harness.install(repo, bin_dir, tmp_path / "backup")
    assert recall_hooks(claude) == [command], "an install that does not ask leaves the hook alone"

    harness.install(repo, bin_dir, tmp_path / "backup", session_recall=False)
    assert recall_hooks(claude) == [] and recall_hooks(codex) == []


def test_uninstall_removes_the_session_recall_hook(home, tmp_path, checkout, fake_claude):
    repo, bin_dir = checkout
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text(json.dumps({"theme": "dark"}) + "\n")
    harness.install(repo, bin_dir, tmp_path / "backup", session_recall=True)
    harness.uninstall(repo, bin_dir, tmp_path / "uninstall-backup")
    assert json.loads((home / ".claude/settings.json").read_text()) == {"theme": "dark"}
    assert (tmp_path / "backup/files/.claude/settings.json").read_text() == '{"theme": "dark"}\n'


def test_session_recall_flag_from_the_command_line(home, tmp_path, checkout, fake_claude):
    repo, bin_dir = checkout
    (home / ".claude").mkdir()
    harness.main(["install", "--repo", str(repo), "--bin-dir", str(bin_dir),
                  "--backup-dir", str(tmp_path / "backup"), "--session-recall", "on"])
    assert recall_hooks(home / ".claude/settings.json") == [f"{bin_dir}/graphify-worktree recall"]
    harness.main(["install", "--repo", str(repo), "--bin-dir", str(bin_dir),
                  "--backup-dir", str(tmp_path / "backup"), "--session-recall", "off"])
    assert recall_hooks(home / ".claude/settings.json") == []


# Pre-tool nudge hook -------------------------------------------------------------

NUDGE = "/bin/graphify-worktree nudge"
NUDGE_MATCHER = "^(Grep|Glob|Read|Bash|mcp__graphify__.*)$"


def test_nudge_hook_is_a_pre_tool_use_entry_beside_other_hooks():
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}
    result = harness.upsert_hook(harness.NUDGE_HOOK, data, NUDGE)
    assert result["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]},
        {"matcher": NUDGE_MATCHER, "hooks": [{"type": "command", "command": NUDGE, "timeout": 5}]},
    ]
    assert harness.upsert_hook(harness.NUDGE_HOOK, result, NUDGE) == result
    assert harness.remove_hook(harness.NUDGE_HOOK, result, NUDGE) == data


def pre_tool_hooks(path):
    data = json.loads(path.read_text()) if path.exists() else {}
    return [handler["command"] for group in data.get("hooks", {}).get("PreToolUse", [])
            for handler in group["hooks"]]


def test_nudge_is_registered_only_when_asked_and_independently_of_recall(home, tmp_path, checkout, fake_claude):
    repo, bin_dir = checkout
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".omp/agent").mkdir(parents=True)
    claude, codex = home / ".claude/settings.json", home / ".codex/hooks.json"
    command = f"{bin_dir}/graphify-worktree nudge"
    messages = []

    harness.install(repo, bin_dir, tmp_path / "backup", session_recall=True)
    assert pre_tool_hooks(claude) == [] and pre_tool_hooks(codex) == []

    harness.install(repo, bin_dir, tmp_path / "backup", nudge=True, report=messages.append)
    assert pre_tool_hooks(claude) == [command] and pre_tool_hooks(codex) == [command]
    assert recall_hooks(claude) == [f"{bin_dir}/graphify-worktree recall"], "recall untouched"
    assert any("pre-tool nudge not supported" in line for line in messages if line.startswith("omp"))

    harness.install(repo, bin_dir, tmp_path / "backup", nudge=False)
    assert pre_tool_hooks(claude) == [] and pre_tool_hooks(codex) == []
    assert recall_hooks(claude) == [f"{bin_dir}/graphify-worktree recall"]

    harness.install(repo, bin_dir, tmp_path / "backup", nudge=True)
    harness.uninstall(repo, bin_dir, tmp_path / "uninstall-backup")
    assert pre_tool_hooks(claude) == [] and recall_hooks(claude) == []


def test_nudge_flag_from_the_command_line(home, tmp_path, checkout, fake_claude):
    repo, bin_dir = checkout
    (home / ".claude").mkdir()
    arguments = ["install", "--repo", str(repo), "--bin-dir", str(bin_dir), "--backup-dir", str(tmp_path / "b")]
    harness.main([*arguments, "--nudge", "on"])
    assert pre_tool_hooks(home / ".claude/settings.json") == [f"{bin_dir}/graphify-worktree nudge"]
    harness.main([*arguments, "--nudge", "off"])
    assert pre_tool_hooks(home / ".claude/settings.json") == []
