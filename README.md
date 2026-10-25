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
- **Protected exports, recoverable retirement.** Each export records exactly which generated files it owns, so re-exports never overwrite authored notes, settings or edited generated files. After a linked worktree is provably removed, `graphify-worktree retire` can move only its unchanged generated files into a restorable quarantine. It is report-only unless separately authorized, and it runs only when a command or coordinator invokes it.

## Prerequisites

- macOS or Linux for graphs and memory (POSIX `fcntl` locks and Git hook shims; `export --open` is macOS-tested). Retirement currently requires Linux's `/etc/machine-id`; an absent host identity fails closed.
- Python ≥ 3.11 (uv installs 3.12 by default)
- Git ≥ 2.31 (`rev-parse --path-format=absolute`); retirement requires Git ≥ 2.36 for NUL-delimited worktree enumeration.
- [uv](https://docs.astral.sh/uv/)
- For Obsidian features: [Obsidian](https://obsidian.md) with its native CLI enabled (the CLI is expected at `~/.local/bin/obsidian` unless `obsidian_cli` in the [configuration file](#configuration) or `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI` says otherwise)
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
- Upstream versions are pinned exactly (`graphifyy[mcp,sql]==0.9.93`, `mcp==2.3.0`). Some upstream APIs this package uses are private, so graph commands and the MCP server refuse to start on any other version. `recall`, `nudge`, `doctor`, `--version` and the metadata-only `retire` commands skip that check.

## Configuration

The three machine-specific locations live in one persistent file read by every process (direct CLI, session-start recall and the MCP server of each harness):

```toml
# ~/.config/grovekeeper/config.toml  ($XDG_CONFIG_HOME/grovekeeper/config.toml when XDG_CONFIG_HOME is absolute)
[paths]
memory_vault = "~/Documents/Obsidian/Engineering"
graph_vault_root = "~/Documents/Obsidian/Graphify"
obsidian_cli = "~/.local/bin/obsidian"
```

Each location resolves at call time, in this order: a nonempty `WORKTREE_KNOWLEDGE_*` environment variable, then the `[paths]` key, then the built-in default. An empty variable counts as unset.

| TOML key | Environment override | Default | Used for |
|---|---|---|---|
| `graph_vault_root` | `WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT` | `~/Obsidian/Graphify` | Parent of generated per-worktree vaults (`export`) |
| `memory_vault` | `WORKTREE_KNOWLEDGE_MEMORY_VAULT` | `~/Obsidian/Engineering` | Authored vault for `Projects/<repo>/Memory` notes |
| `obsidian_cli` | `WORKTREE_KNOWLEDGE_OBSIDIAN_CLI` | `~/.local/bin/obsidian` | Native Obsidian CLI for `export --open` |

- A missing file is normal: defaults (or environment overrides) apply. Every key is optional.
- The file is an error, never a reason to quietly pick another vault, when it is malformed TOML, has a top-level key other than `[paths]` or an unknown key inside it, has a value that is not a nonempty string or is neither absolute nor `~`-prefixed, or is not a regular file (a symlink to a regular file is followed; a dangling symlink or a directory is refused). Commands that need a location then fail with that error.
- `graphify-worktree doctor` reports the file's path, whether it exists and, for each location, whether its value came from `environment`, `config` or `default`, and lists a broken file under `findings`. It creates nothing and never opens Obsidian.
- Its bounded `retirement` summary lists up to 50 registered export publication states without touching vaults or creating state. It does not determine retirement eligibility; use `retire reconcile --all --dry-run` for that.
- Location settings choose paths only. They grant no repository participation, session access, general memory or cleanup permission.
- Prefer this file to per-harness MCP `env` blocks: an environment variable exported while running `install.sh` is not persisted, and a variable set only for the MCP server leaves the CLI and recall on different paths. A nonempty environment variable still wins in the process that has it.

Other variables:

| Variable | Default | Used for |
|---|---|---|
| `WORKTREE_KNOWLEDGE_SKILL_DIR` | `~/.agents/skills/worktree-knowledge` | Where the installer links the skill (environment only, not a TOML key) |
| `XDG_STATE_HOME` | `~/.local/state` | Grovekeeper's state directory is its `grovekeeper/`: per-session counters of the optional pre-tool nudge in `nudge/`, and export records, retirement candidates, grants and receipts in `retirement/` (the standard XDG variable; `WORKTREE_KNOWLEDGE_STATE_DIR` was removed) |
| `WORKTREE_KNOWLEDGE_GENERAL_MEMORY` | unset | Deprecated: use `configure --general-memory` (below). `1` is still honoured while the repository setting is unset |
| `WORKTREE_KNOWLEDGE_INCREMENTAL` | unset | Deprecated: use `configure --incremental` (below). `1` is still honoured while the repository setting is unset |
| `HARNESS_KNOWLEDGE_MODE` | `default` | `off` disables knowledge for the process |
| `WORKTREE_KNOWLEDGE_DEBUG` | unset | `1` prints to stderr the traceback behind a silent `recall`/`nudge` and behind MCP `graph_unavailable`/`memory_operation_failed` results |

Path values, in the file or the environment, must be absolute or start with `~`.

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
graphify-worktree doctor .                # read-only health report: versions, config file, vaults, harness registrations, hooks, settings
graphify-worktree retire reconcile --all --dry-run   # read-only: which removed worktrees' exports could be retired
graphify-worktree --help
```

## Generated exports and retirement

### Export ownership

Every export incarnation of a worktree gets an export UUID, independent of folder and branch names, and a record outside the checkout at `$XDG_STATE_HOME/grovekeeper/retirement/exports/<EXPORT_UUID>/record.json` (default state home: `~/.local/state`). The record holds the worktree, common Git and administrative directory paths with their directory identities (including the worktree's parent directory), this host's identity, the vault path, the exported generation, relative directory paths, and the relative path, size and SHA-256 of every generated file. The vault's marker carries the same inventory as `export_manifest`; the code-graph provenance schema is unchanged.

- Notes, Canvas, report and optional wiki are rendered into private staging first and inventoried there.
- An existing destination file is replaced or pruned only if it still has the bytes of a previous trusted export. A generated file you edited, or an unowned file at a generated path, blocks the export instead of being overwritten. Authored notes and `.obsidian` settings in the vault are left alone.
- Files are replaced individually and atomically, with a durable transaction record; the vault as a whole is not one atomic transaction. An interrupted publication stays recorded as incomplete and is never eligible for retirement.
- Exports made before this release have no inventory. Re-exporting one adopts only files byte-identical to the freshly generated output; if any generated path holds different bytes, the export is refused and nothing there is overwritten. Older generated files the new export does not produce stay in place, unowned. An old export that is never re-exported has no record, so retirement never touches it.

### Retirement commands

```text
graphify-worktree retire prepare WORKTREE [--dry-run | --apply]
    [--local-data-approval FILE]
graphify-worktree retire reconcile (--candidate EXPORT_UUID | --all)
    [--grant GRANT_UUID]
    [--dry-run | --apply --plan-sha256 DIGEST [--confirmation TEXT]]
graphify-worktree retire restore --candidate EXPORT_UUID
    [--dry-run | --apply --plan-sha256 DIGEST --confirmation TEXT]
graphify-worktree retire policy show (REPO_PATH | --grant GRANT_UUID)
graphify-worktree retire policy grant REPO_PATH --export-root ABSOLUTE_ROOT
    [--dry-run | --apply --plan-sha256 DIGEST --confirmation TEXT]
graphify-worktree retire policy revoke --grant GRANT_UUID [--dry-run | --apply]
```

- Every command previews by default and a preview creates no files, locks, records or quarantine. `--dry-run` and `--apply` are mutually exclusive.
- `reconcile --all --apply` is rejected. An apply names one export UUID and the exact `plan_sha256` of its current preview, authorized by exactly one of `--grant` or the preview's `required_confirmation` passed as `--confirmation`. On a preview, `--grant` only filters the scope.
- `restore` and `policy grant` always need the previewed digest and confirmation to apply. Repeating `--export-root` covers several roots in one grant; widening a grant's scope needs a new one.
- The `retire` commands read metadata, files and Git only, so they skip the Graphify/MCP version-pin check. They still refuse when the session is off (`HARNESS_KNOWLEDGE_MODE=off`, exit 5). They do not need the repository's knowledge mode.
- Exit codes: a reconcile preview can exit 0 even when candidates are blocked, so read each candidate's state. Operation errors (including an unknown grant) and blocked prepare guards exit 1, as do applied blocked or partial operations; usage errors exit 2, policy denials 5, and interruptions 130 with the journal or lifecycle state to resume from (no graph is discarded).

### Two independent permissions

| Permission | Enforced by | Covers | Never covers |
|---|---|---|---|
| Worktree removal policy | The coordinator | Removing clean, inactive, positively integrated linked worktrees of this clone through Orca, including Orca's safe local-branch deletion attempt | Force flags, remote branches, dirty or untracked files, the main worktree, live workers |
| Generated-export grant | Grovekeeper (`retire policy grant`) | Moving unchanged manifest-owned generated files of retired worktrees under the granted roots into recoverable quarantine; stored at `retirement/grants/<GRANT_UUID>.json`, bound to this host, the clone's common Git directory and the explicit roots | Edited or unowned files, whole-vault deletion, other clones or hosts |

Neither permission implies the other, and neither authorizes memory writes, acceptance or promotion into the authored vault, or loss of a worktree's local learning. Enrolling a repository and the location settings grant neither. The default is report-only: support for a standing grant does not create one. Every apply rechecks the grant, scope, session, readiness, plan digest and current file hashes under the lifecycle lock, and its receipt records the grant digest or approval reference, the plan and each file's outcome. Revoking a grant stops later operations and resumptions; it does not undo moves already made.

### Before a worktree is removed

`graphify-out/` is not only cache: `memory/`, `reflections/LESSONS.md` and the learning overlay hold approved local results that exist nowhere else. `retire prepare WORKTREE --apply` records the checkout (paths, directory identities including its parent, host) as a candidate and inventories local learning and unknown files under `graphify-out/` by path, type, size and hash, not content. It never removes or rebuilds anything.

- It exits 1 while the guard or a blocker stands, which stops a pre-removal hook: unreadable or unsafe entries, the main worktree, or nonempty learning or unknown data without an approval.
- `--local-data-approval FILE` takes JSON `{"inventory_sha256": …, "decision": "discard" | "retained", "approval_reference": …, "retained_at": …}` (`retained_at` only, and required, for `retained`, which is checked against a verified copy outside the worktree). The approval is bound to that inventory digest, so new data invalidates it. An export grant never substitutes for it.
- The guard covers `graphify-out/` only. The coordinator still checks the whole checkout (dirty, untracked and needed ignored files, unsaved buffers, active workers and unresolved children), quiesces writers and rechecks immediately before removal.

The coordinator, not Grovekeeper, proves integration into a freshly resolved `origin/main`: for a merge or fast-forward, the exact reviewed head must be an ancestor; for a squash or rebase, authoritative merged-review evidence must match the repository, base, reviewed head and resulting commit. A closed PR, a deleted branch, task completion, idle time or a successful `git branch -d` is not proof. Grovekeeper never fetches, prunes, deletes branches or worktrees, stops workers or schedules merges.

### Proof of retirement

`reconcile` decides from the recorded identities and the current Git and filesystem state, not from events. An export becomes eligible only when the original worktree path is absent under its recorded, still accessible and unchanged parent directory; the saved common Git directory is accessible and is the same directory; Git no longer registers the worktree or holds its administrative directory; and the vault marker carries the recorded manifest. An existing or partially removed checkout, a reused path, a moved worktree, an unavailable mount or common directory, a changed common directory or another host blocks it.

The parent directory's identity is what turns absence into proof:

- **New exports** record it, so their worktree can be retired even without a `prepare` candidate (for example after a skipped archive hook or a removal from Orca's UI). The preview then warns `local graphify-out learning was not inventoried`: nothing checked that worktree's local learning before it was removed.
- **Exports recorded before the parent was saved** gain it at their next completed export of the same, still existing worktree. One that is neither re-exported nor prepared with `retire prepare --apply` before removal stays blocked as `parent_identity_unrecorded`; absence alone never makes it eligible.
- If a worktree was removed despite a blocked preparation guard, reconciliation warns that protection of its local learning was not approved. External export quarantine cannot recover lost checkout-local data.
- Reusing a checkout path or replacing its vault directory blocks automatic retirement and re-export. Preserve the old export and resolve it manually; use a distinct worktree path for a new incarnation rather than weakening the identity checks.

### Quarantine and recovery limits

Only unchanged owned generated files move, into a sibling `.grovekeeper-quarantine/<EXPORT_UUID>/<OPERATION_UUID>/` on the same filesystem, with an operation journal written before any move and a receipt after. Edited generated files, custom notes and `.obsidian` stay where they are. There is no recursive deletion, no cross-device copy-and-delete and no timed purge.

- A retry tells source-only, already quarantined, duplicate or conflicting, and unexpectedly missing files apart; it stops on racing writes and never reports unexplained loss as success. POSIX locks are advisory, not a defence against another program running as the same user.
- Afterwards only verified-owned empty directories are removed, bottom-up with `rmdir`, including the export root if it is truly empty. Leftover notes or settings keep their directory and are reported. Obsidian's vault registration list is never edited; remove a stale registration in Obsidian.
- Quarantine keeps the bytes and their disk space. Deleting them for good is a separate, explicit decision.
- `retire restore` puts back only recorded quarantined files and owned empty directories, never over an existing path, and never recreates a Git worktree. A restored export can be quarantined again only with `--confirmation`, not by a standing grant.
- Revoking a grant or rolling back the code does not restore moved files; use `retire restore`.

### Orca integration and timing

The repository's `orca.yaml` provisions each Orca-created worktree and prepares it before removal:

```yaml
setupAgentStartupPolicy: wait-for-setup
scripts:
  setup: uv sync --frozen --python 3.12
  archive: graphify-worktree retire prepare . --apply
```

Use it only once the installed `graphify-worktree` has the `retire` commands, and confirm that Orca-created terminals resolve that executable and trust the hook. The archive hook runs before Orca stops workers and can be skipped, so it records candidates and early blockers only. Do not share `graphify-out` or `.venv` through Orca's shared-directory settings: each checkout keeps its own graph and environment.

The development workflow (`~/.agents/development-workflow.md`) is harness-neutral: whichever of Claude Code, Codex or OMP coordinates a run performs the same reconciliation at the same boundaries (startup or resume, after a completed worktree removal, and at task completion). It runs the read-only preview for enrolled repositories when the command is installed:

```sh
graphify-worktree retire reconcile --all --dry-run
```

and applies eligible plans one export at a time only under a recorded, approved, matching grant. Export-time records make worktrees whose archive hook was skipped, or that were removed in Orca's UI, visible at the next reconciliation. An Orca `worktree.removed` event can mean forget-only and is never treated as proof.

**Timing:** there is no daemon, scheduler or background service. Nothing runs while every coordinator is closed; cleanup waits for the next boundary of whichever coordinator runs next, however long that is. A standing grant authorizes moves; it does not schedule them.

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
  settings.py         locations: environment, then ~/.config/grovekeeper/config.toml, then defaults; state directory
  upstream.py         the only module importing graphify/mcp; version pins
  artifacts.py        owned files under graphify-out/, lock, provenance, vault path
  freshness.py        Generation, content snapshot, classify(), inspect()
  extract.py          Graphify structural extraction and report into scratch
  publish.py          refresh: verify and publish a new generation
  incremental.py      opt-in incremental rebuilds: seed, plan, full on any doubt
  guard.py            guarded_graph/ensure: fresh graph in, result withheld if anything moved
  diff.py             compare two worktrees' fresh graphs by node id
  doctor.py           read-only installation report (`graphify-worktree doctor`)
  obsidian_export.py  generated vault export and `export --open`
  export_state.py     export UUIDs, ownership records, staged hash-checked publication, grants
  retirement.py       `retire`: prepare, reconcile, quarantine, restore, policy
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
orca.yaml                       Orca setup and pre-removal (archive) scripts for this repository's worktrees
skills/worktree-knowledge/      the agent skill
tests/                          pytest unit and integration tests; tests/smoke: integration smokes
```

## License

MIT. See [LICENSE](LICENSE).
