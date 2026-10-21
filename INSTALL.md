# Installing grovekeeper

## 1. Prerequisites

Check these first. `install.sh` checks the OS, uv and the Git version itself.

```sh
uname -s          # Darwin or Linux
uv --version      # https://docs.astral.sh/uv/getting-started/installation/
git --version     # 2.31 or newer
```

For Obsidian features, enable Obsidian's native command-line interface (see Obsidian's documentation) and make sure it is reachable at `~/.local/bin/obsidian`, or set `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI`. Graph features work without Obsidian.

## 2. Install

From the checkout (keep it in place afterwards; the skill is linked from it):

```sh
./install.sh
```

Options, all through the environment:

- `WORKTREE_KNOWLEDGE_PYTHON` sets the Python for the tool environment (default `3.12`; any version ≥ 3.11).
- `WORKTREE_KNOWLEDGE_BACKUP_DIR` sets where to keep copies of replaced files (default `~/.local/share/grovekeeper/backups/<UTC timestamp>`).
- `WORKTREE_KNOWLEDGE_SKILL_DIR` sets where to link the skill (default `~/.agents/skills/worktree-knowledge`).
- `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` are uv's own locations for tool environments and executables.
- `WORKTREE_KNOWLEDGE_SESSION_RECALL=1` registers the optional session-start recall hook; `=0` removes it. Unset (the default) leaves it as it is, so a plain re-install never adds or removes it.

## 3. What the installer changes

1. **Executables.** `uv tool install --force` installs the `grovekeeper` tool with the exact dependency versions in `uv.lock`, so `graphify-worktree` and `graphify-worktree-mcp` appear in `uv tool dir --bin` (normally `~/.local/bin`). If executables with those names already exist and are not this tool's entry points, they are copied to the backup directory first.
2. **Skill links.** `~/.agents/skills/worktree-knowledge` links to `skills/worktree-knowledge` in the checkout. If `~/.claude` exists, `~/.claude/skills/worktree-knowledge` links to it too. Anything already at those paths is moved into the backup directory. Codex discovers `~/.agents/skills` natively. OMP needs that directory in `skills.customDirectories` of `~/.omp/agent/config.yml`; the installer prints a reminder if it is missing.
3. **MCP registrations.** Each points at the absolute path of `graphify-worktree-mcp`. Harnesses whose configuration directory does not exist are skipped.
   - Claude Code: `claude mcp add-json --scope user graphify '{"type":"stdio","command":…,"args":[],"env":{}}'`. Claude's own CLI writes `~/.claude.json`. Without the CLI, the installer prints the command to run.
   - Codex: `[mcp_servers.graphify]` in `~/.codex/config.toml` with `command` and `tool_timeout_sec = 600`. Other keys and `[mcp_servers.graphify.*]` subtables are preserved.
   - OMP: `mcpServers.graphify` in `~/.omp/agent/mcp.json` with `"type": "stdio"` and `"timeout": 600000`.
4. **Global agent rules.** The "Worktree knowledge" section is rendered from `harness/worktree-knowledge.md` into `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` (level-1 heading, skill path) and `~/.omp/agent/RULES.md` (level-2 heading, `skill://worktree-knowledge`). It sits between `<!-- worktree-knowledge:start -->` and `<!-- worktree-knowledge:end -->` markers. An existing unmarked "Worktree knowledge" section is adopted and replaced, and everything else in those files is left alone.
5. **Session-start recall hook (only with `WORKTREE_KNOWLEDGE_SESSION_RECALL=1`).** A `SessionStart` hook (matcher `startup|clear|compact`, timeout 5 s) runs `graphify-worktree recall`. Claude Code: added to `hooks.SessionStart` in `~/.claude/settings.json`, beside any other hooks there. Codex: added to `~/.codex/hooks.json`; Codex skips it until you trust it once with `/hooks` in a Codex session. OMP: not registered, because OMP has no shell-command hooks (its hooks are TypeScript extensions). The hook prints accepted memory titles only in `obsidian` mode and is silent otherwise; both harnesses treat a failing or slow hook as non-blocking. `=0` removes only the hook entry that runs this installation's `graphify-worktree recall`.
6. **Global Git ignore.** `graphify-out/` is added to `core.excludesFile` (or `~/.config/git/ignore`) if it is not already listed. Graphs are never committed and repository ignore files are never edited.

Every file the installer rewrites is first copied to `<backup>/files/<path relative to HOME>`. Re-running the installer changes nothing when everything is current.

Restart running Claude Code, Codex and OMP sessions afterwards so they start the new MCP server and reload the skill.

## 4. Verify

```sh
graphify-worktree policy .            # prints JSON; exit 0
PY="$(uv tool dir)/grovekeeper/bin/python"
"$PY" tests/smoke/smoke.py
"$PY" tests/smoke/memory_smoke.py
"$PY" tests/smoke/lifecycle_smoke.py
```

Each smoke prints `PASS:` lines and exits 0. They use disposable repositories and a disposable vault directory, and need no network or LLM. Then open a session in each harness and confirm the `graphify` MCP server lists `knowledge_session`, the five `memory_*` tools, the two `learning_*` tools and the ten upstream graph/PR tools.

## 5. Upgrade

Pull the checkout and run `./install.sh` again. To move to a new Graphify or MCP release: change the pins in `pyproject.toml` and `PINS` in `src/graphify_worktree/upstream.py`, run `uv lock`, `uv run pytest` and the smokes, then reinstall. Graphs built by an older Graphify become stale and are rebuilt on next use.

## 6. Uninstall

```sh
# Hooks call the tool's Python. Remove them from every repository that has them first:
graphify-worktree hooks uninstall /path/to/repo
./uninstall.sh
```

This removes the rules sections, MCP registrations and session-start recall hooks (only entries pointing at this tool), the skill links (only links pointing at this checkout) and the uv tool. It leaves `graphify-out/` graphs, generated and authored vaults, memory notes, saved `harness.knowledgeMode` preferences, the global ignore line and all backups.

## 7. Roll back to a previous setup

Backups are plain copies, so restore by hand: uninstall, then copy `<backup>/bin/*` back to `~/.local/bin`, move `<backup>/skills/worktree-knowledge` back to `~/.agents/skills/`, and restore any `<backup>/files/...` you want.
