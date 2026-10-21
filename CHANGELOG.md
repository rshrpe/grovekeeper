# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- **`install.sh` could install stale code.** uv reuses its cached wheel of a local project until `pyproject.toml` changes, so re-running the installer after a source-only change kept the previous code. It now passes `--reinstall-package grovekeeper`, which rebuilds the package on every install. Dependencies are still taken from the lock file and are not re-resolved.

### Added in Phase 3: consent-aware pre-tool nudge (needs sign-off)
- **`graphify-worktree nudge`** reads a Claude Code or Codex `PreToolUse` payload on stdin. It is modelled on upstream `graphify hook-guard` and is advisory only.
  - **When it speaks:** before a search (Grep, Glob, or a Bash command that actually runs grep/rg/find/fd/ag/...; upstream's own parser through `upstream.py`) or a Read of a source file that code-only extraction indexes (upstream's code extensions, not its docs list), it may add `additionalContext` suggesting the managed graph tools with the worktree's `project_path`.
  - **Softer when stale:** the wording is softer when no graph is built or it looks out of date (HEAD or branch moved, Git event marker, or the read file is newer than the build). These checks are cheap: no lock, no content scan.
  - **Never strict:** it never returns a permission decision, never says "mandatory", and always exits 0 (exempt from the version-pin refusal, like `recall`).
- **Consent:** silent unless the effective mode is `graph` or `obsidian`: unconfigured, off, invalid, non-repository and `HARNESS_KNOWLEDGE_MODE=off` are silent. It observes `mcp__graphify__knowledge_session` calls, so an MCP session opt-out silences it until `mode: default`.
- **Budget, as upstream:** at most 5 per agent per session (session id plus `agent_id`, so subagents count separately), and silent for 1800 s after that agent called a managed graph tool. Without a session id it is silent.
- **Fail-open:** malformed payloads, a busy or unwritable state file, or any error mean silence.
- **Where its state lives:** `~/.local/state/grovekeeper/nudge/`, set by the new `WORKTREE_KNOWLEDGE_STATE_DIR`, cleaned after 24 h. Unlike upstream it never writes into the repository and never creates `graphify-out`.
- **Opt-in registration:** `WORKTREE_KNOWLEDGE_NUDGE=1 ./install.sh` (or `--nudge on`) adds a `PreToolUse` hook (matcher `^(Grep|Glob|Read|Bash|mcp__graphify__.*)$`, timeout 5 s) to `~/.claude/settings.json` and `~/.codex/hooks.json`. `=0` removes it, and unset leaves it unchanged. `uninstall.sh` removes it. Not registered for OMP, which has no shell-command hooks.
- **Cost:** measured with process start, about 50 ms per non-search call and about 90 ms per nudge-eligible call.
- Internal: the installer's hook helpers are now one generic `HookSpec`-based pair (`upsert_hook`/`remove_hook`) shared by the recall and nudge hooks. Recall registration is unchanged.

### Added in Phase 3: upstream work memory (needs sign-off)
- **`learning_save_result` and `learning_reflect` MCP tools; `graphify-worktree learning save-result|reflect PATH --input-file FILE [--once]`.** They route upstream `graphify save-result` and `reflect --if-stale` (called in-process through `upstream.py`) behind the graph tools' gates: graph consent or `allow_once`/`--once`, session off wins, worktree lock, fresh graph ensured first. Both are writes and require `confirmed: true` plus a `confirmation`. Saving a result also refreshes the lessons. Reflect skips when `LESSONS.md` is newer than the saved results and the graph and the overlay exists.
- Work memory is per worktree: `graphify-out/memory/`, `graphify-out/reflections/LESSONS.md`, `graphify-out/.graphify_learning.json`. Writes refuse symlinked paths. A test verifies that publishing a rebuilt graph leaves all three byte- and mtime-identical.
- Managed `query_graph` shows the overlay as `learning=<status>[:stale]`. Two changes make that reliable:
  - **After reflecting, graph.json's mtime is advanced** (content unchanged, so freshness is unaffected). Upstream servers cache a graph by mtime and size and read the overlay only on reload. Without this, a running server, or one in another process after a CLI write, never shows new lessons.
  - **`upstream.py` carries the overlay through upstream's context filter.** In 0.9.93 `serve._filter_graph_by_context` (`serve.py:994-1008`) rebuilds the graph without its attributes, so any question implying a filter ("who calls X", "imports") lost every `learning=` tag. Only that one attribute is copied; upstream behaviour is otherwise unchanged.
- New MCP error code `invalid_learning_request`. Consent failures stay `knowledge_policy_denied`, and build or freshness failures stay `graph_unavailable`. CLI exit codes follow the existing table: 0, 1 for invalid input or failure, 5 for denied.
- **MCP tool surface:** `tests/fixtures/mcp_tools.json` gains exactly these two tools, appended. The existing 16 entries are byte-identical.

### Added in Phase 3: session-start recall (needs sign-off)
- **`graphify-worktree recall [PATH]`** prints a list of accepted memory titles and IDs, newest first, for a session-start hook. It is capped at 1,000 characters (titles at 100) and framed as untrusted evidence that must be fetched with `memory_get` and verified. It runs the cheap policy check first and prints nothing unless the effective mode is `obsidian`: unconfigured, off, graph-only, invalid preferences, outside a repository and `HARNESS_KNOWLEDGE_MODE=off` are all silent. It also stays silent when nothing is accepted, when a writer holds the memory lock (it does not wait), or when listing takes over two seconds. It reads notes only and never creates directories. Proposed and superseded notes and malformed notes are left out.
- **Opt-in hook registration.** `WORKTREE_KNOWLEDGE_SESSION_RECALL=1 ./install.sh` (or `python -m graphify_worktree.harness install --session-recall on`) adds a `SessionStart` hook (matcher `startup|clear|compact`, timeout 5 s) to `~/.claude/settings.json` and `~/.codex/hooks.json`. `=0` / `off` removes it. Without the variable an install leaves any existing hook unchanged. Codex runs it only after the user trusts it with `/hooks`. OMP is not registered: it has no shell-command hooks, only TypeScript extensions.
- `./uninstall.sh` also removes the hook entries that run this installation's `graphify-worktree recall`.

### Changed in Phase 3 (needs sign-off)
- **`recall` is exempt from the version-pin startup refusal.** It uses no Graphify or MCP API, and a hook must never fail a session start, so with mismatched pins it still runs and exits 0. Every other command still exits 1 on a mismatch. `recall` exits 0 in every case, including unexpected errors.
- Unchanged: consent semantics, every other exit code, the MCP tool surface (`tests/fixtures/mcp_tools.json`), and existing registrations unless the variable is set.

### Added
- Project and package name **grovekeeper** (uv tool `grovekeeper`, backups under `~/.local/share/grovekeeper/`). The skill (`worktree-knowledge`), the commands (`graphify-worktree`, `graphify-worktree-mcp`), the MCP server id (`graphify`), the environment variables (`WORKTREE_KNOWLEDGE_*`) and the rules markers are unchanged. `install.sh` removes a uv tool installed under the earlier name `worktree-knowledge` before installing. Git hooks installed while the tool had that name fall back to running only the original hook until `hooks install` is re-run.
- GitHub Actions CI: unit and integration tests on Ubuntu and macOS with Python 3.11 and 3.12.
- Python package (`src/graphify_worktree`) (`src/graphify_worktree`) with console scripts `graphify-worktree` and `graphify-worktree-mcp`. It requires Python ≥ 3.11 and pins `graphifyy[mcp,sql]==0.9.93` and `mcp==2.3.0` exactly.
- `upstream.py` is the only module that imports Graphify or the MCP SDK. Both entry points check the pinned versions at startup.
- `settings.py`: vault roots, the Obsidian CLI path and the skill directory can be overridden through `WORKTREE_KNOWLEDGE_*` environment variables, with defaults unchanged.
- `install.sh` / `uninstall.sh` and `python -m graphify_worktree.harness`: uv tool install from the lock file, skill links, MCP registration for Claude Code, Codex and OMP, global rules rendered from one template, and the global `graphify-out/` ignore line. All are idempotent and back up anything they replace.
- README, INSTALL, LICENSE (MIT), this changelog.
- Tests: pytest unit tests (errors, gitutil, policy, freshness classification, frontmatter, memory registry and service, hooks, MCP serializer, installer) and integration tests. The integration tests run the three smokes under a disposable HOME, pin the served MCP tool surface to a fixture, and cover `--scope`, `ensure --once`, `export --open`, `memory --limit` and `hooks status`.
- Package layout from the review: `errors`, `gitutil` (one `RepoIdentity`), `artifacts`, `freshness`, `extract`, `publish`, `guard`, `obsidian_export`, `memory/{schema,store,service}`. One memory action registry drives validation, CLI flags and MCP schemas. `guarded_graph` is built around a `Generation` value and checks consent at five named boundaries; it no longer re-inspects after a rebuild. Upstream MCP handlers run on one persistent event-loop thread instead of a nested `asyncio.run` per call.

### Changed (needs sign-off)
- **New startup refusal:** both executables exit 1 with a message when the installed `graphifyy` or `mcp` version differs from the pins. Before, a different version ran and could fail or misbehave inside private APIs. Consent, freshness and memory semantics are unchanged.
- **Global rules files gain marker comments:** the "Worktree knowledge" sections in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` and `~/.omp/agent/RULES.md` are wrapped in `<!-- worktree-knowledge:start/end -->` so the installer can update them in place. The text inside is byte-identical to the previous hand-maintained sections.

### Changed in the Phase 2 restructure (needs sign-off)
Requested in the Phase 2 brief:
- **Hook shims only where needed.** `hooks install` shims the four event hooks (`post-checkout`, `post-commit`, `post-merge`, `post-rewrite`) plus each hook the original hooks directory already contains, instead of all 28 Git hooks. A hook added to the original directory later is not forwarded until `hooks install` is re-run, which adds the missing shims; `hooks status` lists such hooks under a new `unforwarded` key. Installations made by earlier versions are still recognised, kept and removable unchanged.
- **Shims fall back to the original hook.** When the tool's Python or `hooks.py` is gone, a shim runs only the original hook (or exits 0 if there is none). Before, it failed, and a failing `pre-commit` shim blocked commits.
- **Memory search tolerates a malformed note.** It skips the unreadable note and lists it in `warnings`, instead of failing the whole search with `invalid_memory_request`/exit 1. `memory get` of that note still fails.
- **Event marking takes the worktree lock.** `mark_event` waits up to five seconds for the lock and otherwise skips the hint. Skipping is safe because every query re-checks source content.
- **MCP cancellation.** A cancelled request returns immediately. Its worker stops at the next guard boundary (before publishing a rebuild or before running the upstream query) and releases the locks when it stops. Before, the worker ran the whole query and the lock was held until then.

Side effects of the restructure:
- **Generated vault folder name** is the repository name shared with the memory namespace (path-safe: characters outside letters, digits, `_`, `.` and `-` become `-`). It differs from before only for repository directories with other characters (for example spaces) and for `--separate-git-dir` checkouts, which now use the Git directory's name. An existing vault at the old path is left alone and the next export creates the new one. Memory namespaces are unchanged.
- **MCP memory tools:** an unexpected internal failure now returns `memory_operation_failed`. Before, anything other than OSError/RuntimeError escaped and was reported as `graph_unavailable`.
- **CLI:** programming errors (`KeyError`, `TypeError`, unexpected `ValueError`) now end with a traceback instead of a one-line message. The exit code stays 1. Expected failures and their exit codes are unchanged.
- **Wording of reasons only:** memory enum errors read "kind must be one of: decision, fact, lesson". A stale source change reports `changed: fingerprint` instead of `changed: manifest, fingerprint`. Missing or invalid provenance reports "missing or unsupported provenance schema" or "provenance field X is missing or invalid".

### Changed
- Graphify extraction runs as `python -m graphify` from the package's own environment, with the same pinned version, instead of `~/.local/bin/graphify`.
- New `hooks install` shims reference the installed package's `hooks.py` and run it with `python -P`, so the package directory is never put first on `sys.path`. The hook manifest is schema 2: it records which hooks are shimmed.
- The integration smokes moved to `tests/smoke/`. They run against the executables next to their Python (or `GRAPHIFY_WORKTREE_BIN`) and write vaults to a disposable directory instead of `~/Obsidian`.
- SKILL.md now lists all ten proxied upstream tools and discloses that the PR tools may run `gh` and use the network. It documents every CLI exit code and MCP error code, the `--once`/`allow_once` limits (overrides a saved `off`, never session off), `memory search --limit/--include-history`, per-clone memory identity, the global ignore as an install prerequisite, and the new install and verification steps.

### Removed
- User-specific shebangs, `sys.path` home-directory inserts and the sibling-file module loader.
- Shipped artifacts: screenshots of a private project's graph, `installation-result.json`, smoke result files, `__pycache__` and empty `assets/`/`references/` directories.
