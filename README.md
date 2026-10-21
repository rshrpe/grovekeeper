# worktree-knowledge

Consent-gated, per-worktree [Graphify](https://pypi.org/project/graphifyy/) code graphs and shared Obsidian project memory for coding agents. One managed CLI (`graphify-worktree`) and MCP server (`graphify-worktree-mcp`) are shared by Claude Code, Codex and OMP (oh-my-pi), together with a skill (`skills/worktree-knowledge/SKILL.md`) that tells each agent when it may use them.

It wraps upstream Graphify rather than replacing it. What it adds:

- **Opt-in per repository.** Nothing is scanned, built, exported or recalled until the user picks a mode for the repository.
- **One graph per worktree, never stale.** Every query checks a full content fingerprint (plus HEAD, branch, scope and Graphify version). It rebuilds under a per-worktree lock when needed and withholds the answer if sources or consent change while it runs. A rebuild is staged in a scratch directory and only a completed, re-verified extraction replaces the owned artifacts, so a failed build keeps the last good graph.
- **Structural extraction only.** `GRAPHIFY_BACKEND=none` is forced, so ambient API keys can never turn on LLM extraction or labelling.
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
git clone <this repository> ~/src/worktree-knowledge
cd ~/src/worktree-knowledge
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
- **Git hooks depend on the installed environment.** `graphify-worktree hooks install` (explicit, per repository) writes shims under `.git/graphify-worktree-hooks` that call this tool's Python and its `hooks.py` by absolute path. If the uv tool environment moves, or its Python minor version changes, those shims fail, and a failing `pre-commit` shim blocks commits. Run `graphify-worktree hooks uninstall PATH` before uninstalling or switching Python, and reinstall the hooks afterwards.
- Upstream versions are pinned exactly (`graphifyy[mcp,sql]==0.9.93`, `mcp==2.3.0`). Some upstream APIs this package uses are private, so it refuses to start on any other version.

## Configuration

| Variable | Default | Used for |
|---|---|---|
| `WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT` | `~/Obsidian/Graphify` | Parent of generated per-worktree vaults (`export`) |
| `WORKTREE_KNOWLEDGE_MEMORY_VAULT` | `~/Obsidian/Engineering` | Authored vault for `Projects/<repo>/Memory` notes |
| `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI` | `~/.local/bin/obsidian` | Native Obsidian CLI for `export --open` |
| `WORKTREE_KNOWLEDGE_SKILL_DIR` | `~/.agents/skills/worktree-knowledge` | Where the installer links the skill |
| `HARNESS_KNOWLEDGE_MODE` | `default` | `off` disables knowledge for the process |

Values must be absolute or start with `~`. Set vault overrides in the environment your harness starts the MCP server with, so the CLI and the server agree.

## Usage

The agents follow `skills/worktree-knowledge/SKILL.md`. By hand:

```sh
graphify-worktree policy .                 # read-only consent check
graphify-worktree configure graph .        # save a mode for the repository
graphify-worktree ensure .                 # build or reuse a fresh graph
graphify-worktree status .                 # 0 fresh, 3 stale, 4 missing, 5 denied
graphify-worktree export . --open          # obsidian mode: generated vault
graphify-worktree memory search . --query "retry policy"
graphify-worktree --help
```

## Development

```sh
uv sync                    # .venv with the pinned dependencies and pytest
uv run pytest              # unit tests (isolated HOME, no real vaults)
PY="$(uv tool dir)/worktree-knowledge/bin/python"
"$PY" tests/smoke/smoke.py            # graph/policy integration smoke
"$PY" tests/smoke/memory_smoke.py     # memory integration smoke
"$PY" tests/smoke/lifecycle_smoke.py  # MCP lifecycle and Git hooks smoke
```

The smokes use the executables next to the Python running them (override with `GRAPHIFY_WORKTREE_BIN`). They create disposable repositories and write vaults only to a disposable directory.

Layout:

```
src/graphify_worktree/
  cli.py          graphify-worktree entry point: refresh/status/ensure/export
  mcp_server.py   graphify-worktree-mcp: consent-gated proxy of upstream tools + memory
  policy.py       harness.knowledgeMode, session opt-out, one-off permission
  memory.py       Markdown memory store; memory_cli.py: its CLI and MCP schemas
  hooks.py        opt-in Git hook chaining and generation invalidation
  settings.py     environment-overridable locations
  upstream.py     the only module importing graphify/mcp; version pins
  harness.py      installer logic for Claude Code, Codex and OMP
harness/worktree-knowledge.md   single source for the three harness rule snippets
skills/worktree-knowledge/      the agent skill
tests/                          pytest unit tests; tests/smoke: integration smokes
```

## License

MIT. See [LICENSE](LICENSE).
