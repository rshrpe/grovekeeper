#!/usr/bin/env bash
# Install grovekeeper as a uv tool, then register it with Claude Code,
# Codex and OMP. See INSTALL.md for backup coverage and the required manual copies.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${WORKTREE_KNOWLEDGE_PYTHON:-3.12}"

fail() { echo "install: $*" >&2; exit 1; }

case "$(uname -s)" in Darwin|Linux) ;; *) fail "macOS or Linux is required (fcntl, POSIX hooks)";; esac
command -v uv >/dev/null || fail "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
command -v git >/dev/null || fail "git >= 2.31 is required"
git_version="$(git version | sed -E 's/^git version ([0-9]+)\.([0-9]+).*/\1 \2/')"
read -r git_major git_minor <<<"$git_version"
{ [ "$git_major" -gt 2 ] || { [ "$git_major" -eq 2 ] && [ "$git_minor" -ge 31 ]; }; } \
  || fail "git >= 2.31 is required (found $(git version))"

# Opt-in session-start recall hook: 1 registers it, 0 removes it, unset leaves it as it is.
case "${WORKTREE_KNOWLEDGE_SESSION_RECALL:-}" in
  "") RECALL="" ;;
  1) RECALL=on ;;
  0) RECALL=off ;;
  *) fail "WORKTREE_KNOWLEDGE_SESSION_RECALL must be 1 (register) or 0 (remove)" ;;
esac
# Opt-in pre-tool nudge hook, same convention.
case "${WORKTREE_KNOWLEDGE_NUDGE:-}" in
  "") NUDGE="" ;;
  1) NUDGE=on ;;
  0) NUDGE=off ;;
  *) fail "WORKTREE_KNOWLEDGE_NUDGE must be 1 (register) or 0 (remove)" ;;
esac

BIN_DIR="$(uv tool dir --bin)"
TOOL_ENV="$(uv tool dir)/grovekeeper"
BACKUP="${WORKTREE_KNOWLEDGE_BACKUP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/grovekeeper/backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$BACKUP/bin"

# Releases before the rename installed the same executables as the uv tool
# "worktree-knowledge". Remove that tool first so the two never share them.
if uv tool list 2>/dev/null | grep -q '^worktree-knowledge '; then
  uv tool uninstall worktree-knowledge
fi

# uv overwrites these with --force. Keep anything that is not already our entry point.
for name in graphify-worktree graphify-worktree-mcp; do
  target="$BIN_DIR/$name"
  if [ -e "$target" ] || [ -L "$target" ]; then
    case "$(readlink "$target" || true)" in
      "$TOOL_ENV"/*) ;;
      *) cp -pP "$target" "$BACKUP/bin/$name"; echo "backed up $target" ;;
    esac
  fi
done

# Install exactly the locked dependency set, not whatever resolves today.
constraints="$(mktemp)"
trap 'rm -f "$constraints"' EXIT
uv export --project "$REPO" --frozen --no-dev --no-hashes --no-emit-project \
  --format requirements-txt --output-file "$constraints" >/dev/null
# uv reuses a cached wheel of a local project until pyproject.toml changes;
# rebuild this package every time so source changes are never silently skipped.
uv tool install --force --reinstall-package grovekeeper --python "$PYTHON" --constraints "$constraints" "$REPO"

"$TOOL_ENV/bin/python" -m graphify_worktree.harness install \
  --repo "$REPO" --bin-dir "$BIN_DIR" --backup-dir "$BACKUP" ${RECALL:+--session-recall "$RECALL"} ${NUDGE:+--nudge "$NUDGE"}

# Drop only directories this script created and left unused; backed-up trees stay exact.
rmdir "$BACKUP/bin" 2>/dev/null || true
rmdir "$BACKUP" 2>/dev/null || true
if [ -d "$BACKUP" ]; then echo "Backups: $BACKUP"; fi
echo "Done. Restart Claude Code, Codex and OMP sessions so they reload the skill and MCP server."
