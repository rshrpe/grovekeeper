# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Python package `worktree-knowledge` (`src/graphify_worktree`) with console scripts `graphify-worktree` and `graphify-worktree-mcp`. It requires Python ≥ 3.11 and pins `graphifyy[mcp,sql]==0.9.93` and `mcp==2.3.0` exactly.
- `upstream.py` is the only module that imports Graphify or the MCP SDK. Both entry points check the pinned versions at startup.
- `settings.py`: vault roots, the Obsidian CLI path and the skill directory can be overridden through `WORKTREE_KNOWLEDGE_*` environment variables, with defaults unchanged.
- `install.sh` / `uninstall.sh` and `python -m graphify_worktree.harness`: uv tool install from the lock file, skill links, MCP registration for Claude Code, Codex and OMP, global rules rendered from one template, and the global `graphify-out/` ignore line. All are idempotent and back up anything they replace.
- README, INSTALL, LICENSE (MIT), this changelog, and pytest unit tests.

### Changed (needs sign-off)
- **New startup refusal:** both executables exit 1 with a message when the installed `graphifyy` or `mcp` version differs from the pins. Before, a different version ran and could fail or misbehave inside private APIs. Consent, freshness and memory semantics are unchanged.
- **Global rules files gain marker comments:** the "Worktree knowledge" sections in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` and `~/.omp/agent/RULES.md` are wrapped in `<!-- worktree-knowledge:start/end -->` so the installer can update them in place. The text inside is byte-identical to the previous hand-maintained sections.

### Changed
- Graphify extraction runs as `python -m graphify` from the package's own environment, with the same pinned version, instead of `~/.local/bin/graphify`.
- New `hooks install` shims reference the installed package's `hooks.py`. The shim format is unchanged.
- The integration smokes moved to `tests/smoke/`. They run against the executables next to their Python (or `GRAPHIFY_WORKTREE_BIN`) and write vaults to a disposable directory instead of `~/Obsidian`.
- SKILL.md now lists all ten proxied upstream tools and discloses that the PR tools may run `gh` and use the network. It documents every CLI exit code and MCP error code, the `--once`/`allow_once` limits (overrides a saved `off`, never session off), `memory search --limit/--include-history`, per-clone memory identity, the global ignore as an install prerequisite, and the new install and verification steps.

### Removed
- User-specific shebangs, `sys.path` home-directory inserts and the sibling-file module loader.
- Shipped artifacts: screenshots of a private project's graph, `installation-result.json`, smoke result files, `__pycache__` and empty `assets/`/`references/` directories.
