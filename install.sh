#!/usr/bin/env bash
# Install worktree-knowledge as a uv tool, then register it with Claude Code,
# Codex and OMP. Safe to re-run; every replaced file is copied to a backup first.
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

BIN_DIR="$(uv tool dir --bin)"
TOOL_ENV="$(uv tool dir)/worktree-knowledge"
BACKUP="${WORKTREE_KNOWLEDGE_BACKUP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/worktree-knowledge/backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$BACKUP/bin"

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
uv tool install --force --python "$PYTHON" --constraints "$constraints" "$REPO"

"$TOOL_ENV/bin/python" -m graphify_worktree.harness install \
  --repo "$REPO" --bin-dir "$BIN_DIR" --backup-dir "$BACKUP"

find "$BACKUP" -depth -type d -empty -delete
[ -d "$BACKUP" ] && echo "Backups: $BACKUP"
echo "Done. Restart Claude Code, Codex and OMP sessions so they reload the skill and MCP server."
