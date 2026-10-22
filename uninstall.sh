#!/usr/bin/env bash
# Remove the harness registrations and the uv tool. Graphs, vaults, memory
# notes, saved repository preferences, nudge state and backups are left untouched.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v uv >/dev/null || { echo "uninstall: uv is required" >&2; exit 1; }

cat >&2 <<'NOTE'
Note: Git hooks installed with 'graphify-worktree hooks install' call this tool's
Python. Run 'graphify-worktree hooks uninstall PATH' in those repositories first.
Otherwise their shims run only the original hooks; shims installed before the
Phase 2 restructure fail instead, and a failing pre-commit shim blocks commits.
NOTE

BIN_DIR="$(uv tool dir --bin)"
TOOL_ENV="$(uv tool dir)/grovekeeper"
BACKUP="${WORKTREE_KNOWLEDGE_BACKUP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/grovekeeper/backups/uninstall-$(date -u +%Y%m%dT%H%M%SZ)}"

if [ -x "$TOOL_ENV/bin/python" ]; then
  "$TOOL_ENV/bin/python" -m graphify_worktree.harness uninstall \
    --repo "$REPO" --bin-dir "$BIN_DIR" --backup-dir "$BACKUP"
fi
uv tool uninstall grovekeeper || true

cat <<'NOTE'
Left in place: graphify-out/ directories, generated and authored Obsidian vaults,
memory notes, harness.knowledgeMode/knowledgeIncremental/generalMemory settings,
the nudge's counters in ~/.local/state/grovekeeper/ (or $XDG_STATE_HOME/grovekeeper/;
safe to delete), the global 'graphify-out/' ignore line, and earlier backups.
NOTE
