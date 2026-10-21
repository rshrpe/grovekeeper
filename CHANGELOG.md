# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Python package `worktree-knowledge` (`src/graphify_worktree`) with console scripts `graphify-worktree` and `graphify-worktree-mcp`. It requires Python ≥ 3.11 and pins `graphifyy[mcp,sql]==0.9.93` and `mcp==2.3.0` exactly.
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
