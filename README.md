# grovekeeper

Consent-gated, per-worktree [Graphify](https://pypi.org/project/graphifyy/) code graphs and shared Obsidian project memory for coding agents. One managed CLI (`graphify-worktree`) and MCP server (`graphify-worktree-mcp`) are shared by Claude Code, Codex and OMP (oh-my-pi), together with a skill (`skills/worktree-knowledge/SKILL.md`) that tells each agent when it may use them.

grovekeeper is the project and package name. The interfaces your agents use keep their descriptive names: the `worktree-knowledge` skill, the `graphify-worktree` commands, the `graphify` MCP server and the `WORKTREE_KNOWLEDGE_*` variables.

It wraps upstream Graphify rather than replacing it. What it adds:

- **Opt-in per repository.** Nothing is scanned, built, exported or recalled until the user picks a mode for the repository.
- **One graph per worktree, never stale.** Every query checks a full content fingerprint (plus HEAD, branch, scope and Graphify version). It rebuilds under a per-worktree lock when needed and withholds the answer if sources or consent change while it runs. A rebuild is staged in a scratch directory and only a completed, re-verified extraction replaces the owned artifacts, so a failed build keeps the last good graph.
- **Structural extraction only.** `GRAPHIFY_BACKEND=none` is forced, so ambient API keys can never turn on LLM extraction or labelling.
- **Optional session-start recall.** Claude Code and Codex can inject a short list of accepted memory titles when a session starts, only in `obsidian` mode and silently otherwise (`WORKTREE_KNOWLEDGE_SESSION_RECALL=1 ./install.sh`).
- **Optional pre-tool nudge.** Before searches and source reads, Claude Code and Codex can get a short, capped suggestion to try the managed graph tools. It appears only where graph use is allowed, is never a block, and is silent otherwise (`WORKTREE_KNOWLEDGE_NUDGE=1 ./install.sh`).
- **Generated vs authored knowledge stay apart.** Each worktree exports to its own generated vault. Durable decisions, facts and lessons live as Markdown notes in an authored vault, and are saved only with explicit user approval, with provenance-preserving accept/supersede and no delete.

## Prerequisites

- macOS or Linux (POSIX `fcntl` locks and Git hook shims; `export --open` is macOS-tested)
- Python ≥ 3.11 (uv installs 3.12 by default)
- Git ≥ 2.31 (`rev-parse --path-format=absolute`)
- [uv](https://docs.astral.sh/uv/)
- For Obsidian features: [Obsidian](https://obsidian.md) with its native CLI enabled (the CLI is expected at `~/.local/bin/obsidian` unless `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI` says otherwise)
- At least one of Claude Code, Codex or OMP

## Install

```sh
git clone <this repository> ~/src/grovekeeper
cd ~/src/grovekeeper
./install.sh
```

See [INSTALL.md](INSTALL.md) for exactly what the installer changes, backups, verification, upgrades and uninstalling. The skill is linked from the checkout, so keep the checkout where you installed it from.

## Consent model

| Mode | Graph queries/builds | Obsidian memory and exports |
|---|---|---|
| unconfigured (default) | no | no |
| `off` | no | no |
| `graph` | yes | no |
| `obsidian` | yes | yes |

- The mode is saved as `harness.knowledgeMode` in the repository's common Git config (`graphify-worktree configure MODE PATH`). All worktrees and all three harnesses share it, and independent clones choose separately. Global config, includes, worktree-specific config, old graphs and existing vaults never imply consent.
- **Session opt-out** (`HARNESS_KNOWLEDGE_MODE=off`, or the MCP tool `knowledge_session` with `mode: off`) disables everything for that process without changing the saved mode.
- **One-off permission** (`--once` on the CLI, `allow_once: true` on MCP calls) allows exactly one operation even when the saved mode is `off` or unconfigured. It never overrides session opt-out and never repairs an invalid preference.
- Memory writes additionally require `confirmed: true` plus a description of the user's actual approval. There is no delete: superseded notes keep their content and audit history.

The skill is an operational guard on the managed tools, not a filesystem sandbox. It relies on the agent following the installed instructions.

## Things to know before sharing a vault

- **Memory identity is per clone and per machine.** A repository's memory namespace is derived from the absolute path of its common Git directory. Worktrees of one clone share notes. A second clone, or the same repository at a different path or on another machine, gets a separate namespace. If two engineers sync one Engineering vault, each sees the other's notes rejected as "belongs to another repository".
- **Git hooks reference the installed environment.** `graphify-worktree hooks install` (explicit, per repository) writes shims under `.git/graphify-worktree-hooks` for the four event hooks and for the hooks your original hooks directory already has. They call this tool's Python and its `hooks.py` by absolute path. If the uv tool environment disappears or moves, or its Python minor version changes, the shims fall back to running only your original hook: Git keeps working, but graph invalidation hints stop until you run `hooks install` again. Hooks added to your original directory after installation are not forwarded until you re-run `hooks install`; `hooks status` lists them as `unforwarded`. **If the repository itself moves or is renamed,** Git looks for hooks at the old absolute path, finds none and runs none of them, your own included. `hooks status` then reports `relocated` (exit 1); run `hooks install` to re-home the shims (your original hooks run again) or `hooks uninstall` to restore your original setting. Installs made by earlier versions shim every hook and have no fallback, so reinstall them with `hooks uninstall` then `hooks install`.
- Upstream versions are pinned exactly (`graphifyy[mcp,sql]==0.9.93`, `mcp==2.3.0`). Some upstream APIs this package uses are private, so it refuses to start on any other version.

## Configuration

| Variable | Default | Used for |
|---|---|---|
| `WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT` | `~/Obsidian/Graphify` | Parent of generated per-worktree vaults (`export`) |
| `WORKTREE_KNOWLEDGE_MEMORY_VAULT` | `~/Obsidian/Engineering` | Authored vault for `Projects/<repo>/Memory` notes |
| `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI` | `~/.local/bin/obsidian` | Native Obsidian CLI for `export --open` |
| `WORKTREE_KNOWLEDGE_SKILL_DIR` | `~/.agents/skills/worktree-knowledge` | Where the installer links the skill |
| `WORKTREE_KNOWLEDGE_STATE_DIR` | `~/.local/state/grovekeeper` | Per-session counters of the optional pre-tool nudge |
| `WORKTREE_KNOWLEDGE_GENERAL_MEMORY` | unset | Deprecated: use `configure --general-memory` (below). `1` is still honoured while the repository setting is unset |
| `WORKTREE_KNOWLEDGE_INCREMENTAL` | unset | Deprecated: use `configure --incremental` (below). `1` is still honoured while the repository setting is unset |
| `HARNESS_KNOWLEDGE_MODE` | `default` | `off` disables knowledge for the process |
| `WORKTREE_KNOWLEDGE_DEBUG` | unset | `1` prints to stderr the traceback behind a silent `recall`/`nudge` and behind MCP `graph_unavailable`/`memory_operation_failed` results |

Values must be absolute or start with `~`. Set vault overrides in the environment your harness starts the MCP server with, so the CLI and the server agree.

Two opt-in features are repository settings, saved like the mode in the common Git config so every worktree, harness and process agrees. `graphify-worktree policy` reports their effective values as `incremental` and `general_memory`:

| Setting | Git key | Turns on |
|---|---|---|
| `configure --incremental on\|off PATH` | `harness.knowledgeIncremental` | Incremental rebuilds (seeded upstream extract; full on any doubt) |
| `configure --general-memory on\|off PATH` | `harness.generalMemory` | General-scope lessons in `General/Memory/` for this repository, searched after its own notes |

**Migrating from the variables:** run `graphify-worktree configure --incremental on PATH` (or `--general-memory on`) in each repository that relied on them, then remove the variable. While a repository's key is unset, the variable still counts for one more release, and `policy` lists it under `deprecated_environment`.

## Usage

The agents follow `skills/worktree-knowledge/SKILL.md`. By hand:

```sh
graphify-worktree policy .                 # read-only consent check
graphify-worktree configure graph .        # save a mode for the repository
graphify-worktree configure --incremental on .   # a setting; never changes the mode
graphify-worktree ensure .                 # build or reuse a fresh graph
graphify-worktree status .                 # 0 fresh, 3 stale, 4 missing, 5 denied
graphify-worktree diff ../main .           # structural differences between two worktrees' graphs
graphify-worktree export . --open          # obsidian mode: generated vault
graphify-worktree export . --wiki          # ... plus upstream's wiki articles in _graphify-worktree/wiki/
graphify-worktree memory search . --query "retry policy"
graphify-worktree recall .                 # what the optional session-start hook injects (obsidian mode only)
graphify-worktree dashboard .              # obsidian mode: Obsidian Bases view of this repository's memory notes
graphify-worktree learning save-result . --input-file result.json   # upstream work memory, needs approval
graphify-worktree --version               # grovekeeper and installed vs pinned upstream versions
graphify-worktree --help
```

## Development

```sh
uv sync                             # .venv with the pinned dependencies and pytest
uv run pytest -m "not integration"  # unit tests, ~10 s
uv run pytest                       # plus smokes and CLI tests under a disposable HOME, ~1 min
PY="$(uv tool dir)/grovekeeper/bin/python"
"$PY" tests/smoke/smoke.py            # graph/policy integration smoke
"$PY" tests/smoke/memory_smoke.py     # memory integration smoke
"$PY" tests/smoke/lifecycle_smoke.py  # MCP lifecycle and Git hooks smoke
tests/e2e/install_uninstall.sh        # install.sh/uninstall.sh under a disposable HOME (as CI)
```

The smokes use the executables next to the Python running them (override with `GRAPHIFY_WORKTREE_BIN`). They create disposable repositories and write vaults only to a disposable directory.

Layout:

```
src/graphify_worktree/
  cli.py              graphify-worktree entry point: prints every JSON result, maps errors to exit codes
  mcp_server.py       graphify-worktree-mcp: consent-gated proxy of upstream tools + memory
  policy.py           harness.knowledgeMode, session opt-out, one-off permission
  errors.py           KnowledgeError hierarchy and exit-code table
  gitutil.py          the only Git runner; RepoIdentity shared by vaults and memory
  settings.py         environment-overridable locations
  upstream.py         the only module importing graphify/mcp; version pins
  artifacts.py        owned files under graphify-out/, lock, provenance, vault path
  freshness.py        Generation, content snapshot, classify(), inspect()
  extract.py          Graphify structural extraction and report into scratch
  publish.py          refresh: verify and publish a new generation
  incremental.py      opt-in incremental rebuilds: seed, plan, full on any doubt
  guard.py            guarded_graph/ensure: fresh graph in, result withheld if anything moved
  diff.py             compare two worktrees' fresh graphs by node id
  obsidian_export.py  generated vault export and `export --open`
  recall.py           session-start hook: accepted memory titles, silent unless obsidian mode
  dashboard.py        generated Obsidian Bases view (Memory.base) of one repository's memory
  nudge.py            pre-tool hook: capped, advisory graph suggestion, silent unless graph consent
  learning.py         upstream save-result/reflect work memory behind the graph gates
  memory/schema.py    the memory action registry (validation, CLI, MCP schemas)
  memory/store.py     note format and safe file I/O
  memory/service.py   search/get/save/accept/supersede
  hooks.py            opt-in Git hook chaining and generation invalidation
  harness.py          installer logic for Claude Code, Codex and OMP
harness/worktree-knowledge.md   single source for the three harness rule snippets
skills/worktree-knowledge/      the agent skill
tests/                          pytest unit and integration tests; tests/smoke: integration smokes
```

## License

MIT. See [LICENSE](LICENSE).
