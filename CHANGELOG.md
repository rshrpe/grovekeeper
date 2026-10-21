# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- **`install.sh` could install stale code.** uv reuses its cached wheel of a local project until `pyproject.toml` changes, so re-running the installer after a source-only change kept the previous code. It now passes `--reinstall-package grovekeeper`, which rebuilds the package on every install. Dependencies are still taken from the lock file and are not re-resolved.

### Added in Phase 3: memory dashboard (needs sign-off)
- **`graphify-worktree dashboard PATH [--once]`** writes `Projects/<repository>/Memory.base` in the authored memory vault, beside that repository's `Memory/` folder. Bases only query notes in their own vault.
  - It is scoped with `file.inFolder("Projects/<repository>/Memory")` and has five table views: Current (grouped by status), By kind, Proposals by branch, Needs review (a `review` formula over `verify_by`/`stale_after`), and History (superseded).
  - It needs Obsidian consent; `--once` works and session off wins.
  - It never creates the Memory folder or any note, and writes symlink-safely through directory descriptors.
  - A re-run reports `unchanged`. A copy that differs (for example after editing views in Obsidian) is never overwritten: the command exits 1 and keeps it. Delete the file to get the current template.
- CLI only and explicit. It isn't an MCP tool, so the MCP tool surface is unchanged.
- The generated YAML was validated with PyYAML, outside the project's dependencies: four top-level keys, every referenced formula defined. It was not opened in the Obsidian app.

### Changed in Phase 3: near-duplicate check on memory save (needs sign-off)
- **`memory_save` refuses a near-duplicate title** unless the request has `duplicate_ok: true` (new optional boolean, default false). The check runs under the save's exclusive memory lock, against every readable, non-superseded note of the repository, including proposals on other branches.
  - **Titles are compared word by word,** with the search scorer's tokenization. They match when the words are identical, or when at least two words are shared, the shared words are at least half of all words and at least three quarters of the shorter title. So "Retry ceiling is three" matches "Retry ceiling is 3", but "Use three retries" doesn't match "Use three workers", and "Retry policy" doesn't match "Retry policy for webhooks differs".
  - **A refusal writes nothing.** MCP returns `invalid_memory_request` with a new `duplicates` list (id, title, status, applicable, applicability, path). The CLI exits 1 and names them on stderr.
  - **With `duplicate_ok: true`** the note is saved and `warnings` names the matches.
  - Unreadable notes never block a save.
- **Behaviour change:** a save that used to succeed now fails when a current note has a nearly identical title. That includes the replacement in a save-then-supersede flow, which passes `duplicate_ok: true` for the note it replaces (as the memory smoke now does).
- **`memory_lint`** also reports each pair of current notes with nearly the same title (`near_duplicate`, with `duplicate_of`).
- **MCP tool surface:** `memory_save` gains only the optional `duplicate_ok` property.

### Added in Phase 3: memory review dates, verify and lint (needs sign-off)
- **Review dates.** `memory_save` takes optional `verify_by` and `stale_after` UTC dates (`YYYY-MM-DD`; not in the past; `stale_after` not before `verify_by`). They are stored as optional frontmatter properties of schema version 1 and are only written when set.
  - Search and get add `freshness` (`verify_by`, `stale_after`, `state`: `current`, `verify_due` or `stale`) only for notes that have a date, so results and files for existing notes are byte-identical.
  - Search still returns due and stale notes, flagged; ranking and applicability are unchanged.
  - An older grovekeeper reading a note that has these properties skips it with a warning, like any note it cannot read.
- **`memory_verify`** (CLI `memory verify PATH --input-file FILE`) is confirmation-gated, like the other writes. It records that the user re-verified a current note, sets exactly the given review dates (omitted ones are cleared) and appends a `verify` audit event. Content and provenance are never changed, and superseded notes cannot be verified.
- **`memory_lint`** (CLI `memory lint PATH`) is read-only, changes nothing and creates nothing. It reports:
  - unreadable notes;
  - `verify_due` and `stale` notes;
  - superseded notes whose replacement is missing, unreadable or not accepted;
  - proposals whose branch no longer exists.

  It needs Obsidian consent like every memory action, and exits 0 with the findings in JSON.
- **MCP tool surface:** `memory_verify` and `memory_lint` are added after `memory_supersede`, and `memory_save` gains only the two optional date properties. Every other tool entry is byte-identical.

### Added in Phase 3: opt-in incremental rebuilds (needs sign-off)
- **`WORKTREE_KNOWLEDGE_INCREMENTAL=1` makes rebuilds incremental.** It must be set for the CLI and in the environment the harness starts the MCP server with. Unset or any other value: rebuilds are the same full extractions as before, with no seed and no new provenance field.
- **How it works:**
  - Each build keeps upstream's raw output (`manifest.json`, AST cache, `graph.json` before citations are rebased) as a seed in `graphify-out/.graphify-worktree-seed/<token>/`, named in provenance `build.seed`.
  - The next rebuild moves that seed into the staged workspace and runs `graphify extract --code-only` without `--force`.
  - Upstream trusts an unchanged mtime (`detect.detect_incremental`), so before the run every file whose content hash differs between the seed's generation and now is dropped from the seed manifest. A test shows a same-mtime, same-size edit would otherwise be published as fresh with the old symbols.
- **Unchanged:** the content-fingerprint snapshot before and after extraction, consent checks and the staged owned-artifact publish.
- **Full extract on any doubt**, with the reason recorded in provenance `build.reason`:
  - the seed is missing or incomplete, or comes from another worktree;
  - the Graphify version, scope or nested-checkout exclusions changed;
  - the incremental run or its report failed;
  - nine incremental builds have run in a row.
- **Turning it off** removes the seed on the next rebuild. Seeds are about the size of upstream's AST cache (32 MB for 583 Python files).
- **Why not upstream `update`, as the review proposed:** in 0.9.93 it has no `--out`, `--exclude` or `--code-only` (`cli.py:2781-2800`). It ingests every Markdown file, including `graphify-out/memory/*.md` (+926 nodes on a 97-file test repo). It also doesn't write `.graphify_analysis.json`, which report and export need. Seeded `extract` keeps the graph code-only.
- **Measured** on a disposable copy of `networkx` (583 files, about 11,400 nodes), whole `graphify-worktree refresh` including both content snapshots:

  | Change | Incremental | Full |
  |---|---|---|
  | one file | 8.45 s | 10.28 s |
  | ten files | 8.95 s | 10.59 s |
  | branch switch (25 files) | 8.89 s | 10.46 s |

  The extract step alone takes 4.65 s instead of 7.87 s; snapshots, clustering and the report are unchanged.
- **Known limitation:** an incremental merge can give an unresolved external reference (for example `typing.Mapping` as a base class) in a re-extracted file a global node id (`mapping`) where a full build gives a file-scoped one, and its edge follows. Measured: identical graphs for one changed file; 2 of about 11,400 nodes and 2 edges for 10 and 25 changed files. No other node attribute differs. The periodic full extract bounds this.

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
