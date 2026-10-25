# Installing grovekeeper

## 1. Prerequisites

Check these first. `install.sh` checks the OS, uv and the Git version itself.

```sh
uname -s          # Darwin or Linux
uv --version      # https://docs.astral.sh/uv/getting-started/installation/
git --version     # 2.31 or newer
```

For Obsidian features, enable Obsidian's native command-line interface (see Obsidian's documentation) and make sure it is reachable at `~/.local/bin/obsidian`, or set `obsidian_cli` in the configuration file (section 3). Graph features work without Obsidian.

**Back up what the installer does not.** Before the first install on a machine you care about, copy these yourself (section 4 explains why):

```sh
cp -p ~/.claude.json ~/.claude.json.before-grovekeeper            # if it exists; Claude's CLI rewrites it
f="$(git config --global --path --get core.excludesFile || echo "${XDG_CONFIG_HOME:-$HOME/.config}/git/ignore")"
[ -e "$f" ] && cp -p "$f" "$f.before-grovekeeper"                 # the global Git ignore file
```

Record which of them did not exist, so a rollback knows to remove rather than restore them.

Use uv ≥0.13.4 and an up-to-date Python trust store before downloading dependencies; the project rejects older uv versions. The dependency floor `certifi>=2026.10.19` supplies the updated application CA bundle; it cannot repair the uv/Python trust used to install it. Never work around certificate errors by disabling verification.

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
- `WORKTREE_KNOWLEDGE_NUDGE=1` registers the optional pre-tool nudge hook; `=0` removes it; unset leaves it as it is.
- `WORKTREE_KNOWLEDGE_SESSION_RECALL=1` registers the optional session-start recall hook; `=0` removes it. Unset (the default) leaves it as it is, so a plain re-install never adds or removes it.

Vault locations are not installer options: a `WORKTREE_KNOWLEDGE_*` location variable exported while running `install.sh` affects only that run and is not saved for later CLI, hook or MCP processes. Use the configuration file below.

## 3. Configure locations

Skip this if the defaults (`~/Obsidian/Engineering`, `~/Obsidian/Graphify`, `~/.local/bin/obsidian`) suit you. Otherwise create `~/.config/grovekeeper/config.toml` (`$XDG_CONFIG_HOME/grovekeeper/config.toml` when `XDG_CONFIG_HOME` is an absolute path) with any of the three keys:

```toml
[paths]
memory_vault = "~/Documents/Obsidian/Engineering"
graph_vault_root = "~/Documents/Obsidian/Graphify"
obsidian_cli = "~/.local/bin/obsidian"
```

Neither the installer nor any command creates this configuration file. Reading it creates no directories; approved exports and memory writes create their own destination directories when needed. Every process reads it at call time, so the direct CLI, session-start recall and each harness's MCP server agree without per-harness `env` entries. A nonempty `WORKTREE_KNOWLEDGE_MEMORY_VAULT`, `WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT` or `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI` still overrides its key in the process that has it. A malformed file, an unknown key, or a value that is not an absolute or `~` path is an error rather than a silent fallback to another vault. The file only selects paths; it does not enroll a repository or grant any memory or cleanup permission. See README "Configuration" for the full rules.

## 4. What the installer changes

1. **Executables.** `uv tool install --force` installs the `grovekeeper` tool with the exact dependency versions in `uv.lock`, so `graphify-worktree` and `graphify-worktree-mcp` appear in `uv tool dir --bin` (normally `~/.local/bin`). If executables with those names already exist and are not this tool's entry points, they are copied to the backup directory first. That copy is of the executable entry itself (`cp -pP`, so a symlink is copied as a symlink), not of the uv tool environment it may point into: it is not an environment backup.
2. **Skill links.** `~/.agents/skills/worktree-knowledge` links to `skills/worktree-knowledge` in the checkout. If `~/.claude` exists, `~/.claude/skills/worktree-knowledge` links to it too. Anything already at those paths is moved into the backup directory. Codex discovers `~/.agents/skills` natively. OMP also discovers it with `skills.enableAgentsUser=true`; `skills.customDirectories` is only needed when native discovery is unavailable. The installer's reminder checks custom directories, not that native setting: verify discovery in a fresh OMP session before changing configuration.
3. **MCP registrations.** Each points at the absolute path of `graphify-worktree-mcp`. Harnesses whose configuration directory does not exist are skipped.
   - Claude Code: `claude mcp add-json --scope user graphify '{"type":"stdio","command":…,"args":[],"env":{}}'`. Claude's own CLI writes `~/.claude.json`, and the installer does not back that file up. Without the CLI, the installer prints the command to run.
   - Codex: `[mcp_servers.graphify]` in `~/.codex/config.toml` with `command` and `tool_timeout_sec = 600`. Other keys and `[mcp_servers.graphify.*]` subtables are preserved.
   - OMP: `mcpServers.graphify` in `~/.omp/agent/mcp.json` with `"type": "stdio"` and `"timeout": 600000`.
4. **Global agent rules.** The "Worktree knowledge" section is rendered from `harness/worktree-knowledge.md` into `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` (level-1 heading, skill path) and `~/.omp/agent/RULES.md` (level-2 heading, `skill://worktree-knowledge`). It sits between `<!-- worktree-knowledge:start -->` and `<!-- worktree-knowledge:end -->` markers. An existing unmarked "Worktree knowledge" section is adopted and replaced, and everything else in those files is left alone.
5. **Session-start recall hook (only with `WORKTREE_KNOWLEDGE_SESSION_RECALL=1`).** A `SessionStart` hook (matcher `startup|resume|clear|compact|fork`, timeout 5 s) runs `graphify-worktree recall` when a session starts, is resumed, cleared, compacted or forked (Codex has no `fork`). Claude Code: added to `hooks.SessionStart` in `~/.claude/settings.json`, beside any other hooks there. Codex: added to `~/.codex/hooks.json`; Codex skips it until you trust it once with `/hooks` in a Codex session. OMP: not registered, because OMP has no shell-command hooks (its hooks are TypeScript extensions). The hook prints accepted memory titles only in `obsidian` mode and is silent otherwise; both harnesses treat a failing or slow hook as non-blocking. `=0` removes only the hook entry that runs this installation's `graphify-worktree recall`. `=1` also updates the matcher of an existing entry; a plain install leaves it as it is.
6. **Pre-tool nudge hook (only with `WORKTREE_KNOWLEDGE_NUDGE=1`).** A `PreToolUse` hook (matcher `^(Grep|Glob|Read|Bash|mcp__graphify__.*)$`, timeout 5 s) runs `graphify-worktree nudge` in the same two files as the recall hook (Claude Code `~/.claude/settings.json`, Codex `~/.codex/hooks.json`, trusted once with `/hooks`). It is not registered for OMP. It only ever adds advisory context: it never returns a permission decision, always exits 0, and both harnesses treat a failing or slow hook as non-blocking. It costs one short Python process per matched tool call, about 50 ms when the call is not a search and about 90 ms when it is. `=0` removes only the entry that runs this installation's `graphify-worktree nudge`.
7. **Global Git ignore.** `graphify-out/` is appended to `core.excludesFile` (or `$XDG_CONFIG_HOME/git/ignore`, default `~/.config/git/ignore`) if it is not already listed. That file is not backed up. Graphs are never committed and repository ignore files are never edited.

The rules files, `~/.codex/config.toml`, `~/.omp/agent/mcp.json`, `~/.claude/settings.json` and `~/.codex/hooks.json` are copied to `<backup>/files/<path relative to HOME>` before the installer rewrites them, and anything replaced at a skill-link path is moved into the backup directory. Two changed files are not covered: `~/.claude.json` (written by Claude's CLI) and the global Git ignore file. Back them up yourself first (section 1). Re-running the installer changes nothing when everything is current.

The installer does not create `~/.config/grovekeeper/config.toml`, enroll any repository, install `orca.yaml` hooks or create a retirement grant.

Restart running Claude Code, Codex and OMP sessions afterwards so they start the new MCP server and reload the skill.

## 5. Verify

```sh
graphify-worktree --version          # grovekeeper and installed vs pinned Graphify/MCP versions
graphify-worktree policy .            # prints JSON; exit 0
graphify-worktree doctor .            # read-only report; "findings": [] when everything is wired up
                                      # "configuration": config path, exists, and each location's source
                                      # (environment, config or default)
PY="$(uv tool dir)/grovekeeper/bin/python"
"$PY" tests/smoke/smoke.py
"$PY" tests/smoke/memory_smoke.py
"$PY" tests/smoke/lifecycle_smoke.py
```

To confirm every process uses the same locations, compare `doctor`'s `locations` with the vault roots in the MCP server's startup line in each harness's MCP log; run `doctor` inside each harness's terminal too, since a variable set in one environment overrides the file only there. The MCP server writes one line to stderr when it starts (versions, vault roots, session mode), which harness MCP logs show. If a hook stays silent or a tool returns `graph_unavailable` or `memory_operation_failed` without an obvious reason, set `WORKTREE_KNOWLEDGE_DEBUG=1` in that environment to get the traceback on stderr. Each smoke prints `PASS:` lines and exits 0. They use disposable repositories and a disposable vault directory, and need no network or LLM. Then open a session in each harness and confirm the `graphify` MCP server lists `knowledge_session`, the seven `memory_*` tools, the two `learning_*` tools and the ten upstream graph/PR tools.

## 6. Upgrade

Pull the checkout and run `./install.sh` again. To move to a new Graphify or MCP release: change the pins in `pyproject.toml` and `PINS` in `src/graphify_worktree/upstream.py` and run `uv lock`. Then run `uv run pytest tests/test_upstream_contract.py` first: it checks every private upstream function, attribute and data format this package relies on, and a failure names the touchpoint that moved. After that, run `uv run pytest` and the smokes, then reinstall. Graphs built by an older Graphify become stale and are rebuilt on next use.

Security boundaries: repository consent and MCP `confirmed`/`confirmation` fields express the caller's approval; they do not authenticate a human independently of the host harness. Only grant this stdio server to a trusted client. Extraction is local and uses isolated Python imports, but is not an OS sandbox. PR tools invoke the authenticated `gh` CLI and contact GitHub. Repository files, generated graphs and retrieved memory remain untrusted context. Symlink checks reject pre-existing redirections, not concurrent malicious filesystem changes by a process running as the same user. Restart MCP sessions after upgrading when their active work is finished.

## 7. Uninstall

If you used retirement, first follow the retirement steps in section 8: the `retire` commands, including `restore`, go away with the tool.

```sh
# Hooks call the tool's Python. Remove them from every repository that has them first:
graphify-worktree hooks uninstall /path/to/repo
./uninstall.sh
```

This removes the rules sections, MCP registrations and the recall and nudge hooks (only entries pointing at this tool), the skill links (only links pointing at this checkout) and the uv tool. It leaves `graphify-out/` graphs, generated and authored vaults, memory notes, saved repository preferences and settings (`harness.knowledgeMode`, `harness.knowledgeIncremental`, `harness.generalMemory`), `~/.config/grovekeeper/config.toml`, the state directory `~/.local/state/grovekeeper/` (`$XDG_STATE_HOME/grovekeeper/` if set: the nudge's per-session counters, safe to delete, and the export records, candidates, grants, journals and receipts under `retirement/`, which are not), any `.grovekeeper-quarantine/` folders beside generated vaults, the global ignore line and all backups. Git hook shims left in a repository run only its original hooks once the tool is gone (shims installed before the Phase 2 restructure fail instead), so run `hooks uninstall` first as shown. Uninstalling is not a full rollback.

## 8. Roll back to a previous setup

**Retirement first**, while the lifecycle-capable `graphify-worktree` is still installed:

1. Stop coordinators from running retirement, and stop using the repository's `orca.yaml` archive hook (`retire prepare . --apply`) before removing the executable it calls.
2. Revoke each grant: `graphify-worktree retire policy revoke --grant GRANT_UUID --apply` (preview without `--apply`). Revocation does not move anything back.
3. For each export you want back, preview `graphify-worktree retire restore --candidate EXPORT_UUID`, then apply it with the preview's `--plan-sha256` and `--confirmation` once you have approved it. Restore never overwrites a path that has been reused; such conflicts stay in quarantine and are reported. Rolling back the code does not restore moved files.
4. Keep receipts and quarantine folders. Do not run an older grovekeeper's `export` against a vault written by this one as a shortcut: older versions do not respect the ownership records.

**Then the installation.** Backups are plain copies, so restore by hand: uninstall, then copy `<backup>/bin/*` back to `~/.local/bin` (this restores the executable entries only; a uv tool environment they pointed into must be reinstalled separately), move `<backup>/skills/worktree-knowledge` back to `~/.agents/skills/`, and restore any `<backup>/files/...` you want. Restore `~/.claude.json` and the global Git ignore file from your own copies (section 1), or remove just the `graphify` MCP entry and the `graphify-out/` line. Files the installer created had nothing to back up: remove them only if you are sure nothing else added content since. Restore each repository's previous `harness.knowledge*`/`harness.generalMemory` values (or their absence) yourself, and delete `~/.config/grovekeeper/config.toml` if you created it for this setup.
