#!/usr/bin/env bash
# End to end: ./install.sh, a no-op reinstall, then ./uninstall.sh, all under a
# disposable HOME, uv tool directory and bin directory with fake Claude Code,
# Codex and OMP configuration directories. Never touches the caller's HOME:
# only uv's download cache and managed Pythons may be shared, through
# UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR if the caller sets them.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" UV_TOOL_DIR="$WORK/uv-tools" UV_TOOL_BIN_DIR="$WORK/bin"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$WORK/uv-cache}" UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$WORK/uv-python}"
unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME GIT_CONFIG_GLOBAL CLAUDE_CONFIG_DIR CODEX_HOME HARNESS_KNOWLEDGE_MODE
for variable in $(env | sed -n 's/^\(WORKTREE_KNOWLEDGE_[A-Z_]*\)=.*/\1/p'); do unset "$variable"; done
mkdir -p "$HOME/.claude" "$HOME/.codex" "$HOME/.omp/agent" "$WORK/fake-bin" "$UV_TOOL_BIN_DIR"

# A `claude` that edits $HOME/.claude.json the way `claude mcp add-json/remove` does.
cat > "$WORK/fake-bin/claude" <<'PY'
#!/usr/bin/env python3
import json, os, pathlib, sys
config = pathlib.Path(os.environ["HOME"]) / ".claude.json"
data = json.loads(config.read_text()) if config.exists() else {}
servers = data.setdefault("mcpServers", {})
if sys.argv[1:3] == ["mcp", "add-json"]:
    servers[sys.argv[-2]] = json.loads(sys.argv[-1])
elif sys.argv[1:3] == ["mcp", "remove"] and servers.pop(sys.argv[-1], None) is None:
    sys.exit(1)
config.write_text(json.dumps(data))
PY
chmod +x "$WORK/fake-bin/claude"
export PATH="$WORK/fake-bin:$UV_TOOL_BIN_DIR:$PATH"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
snapshot() { (cd "$HOME" && find . -path ./.local/share -prune -o -type f -print | sort | xargs shasum); }

WORKTREE_KNOWLEDGE_SESSION_RECALL=1 WORKTREE_KNOWLEDGE_NUDGE=1 "$REPO/install.sh" > "$WORK/install.log" 2>&1 \
  || { cat "$WORK/install.log"; fail "install.sh exited non-zero"; }
PY="$UV_TOOL_DIR/grovekeeper/bin/python"
[ -x "$UV_TOOL_BIN_DIR/graphify-worktree" ] && [ -x "$UV_TOOL_BIN_DIR/graphify-worktree-mcp" ] || fail "executables missing"
graphify-worktree --version | grep -q '^graphify-worktree (grovekeeper ' || fail "--version"
git init -q "$WORK/repo" && graphify-worktree policy "$WORK/repo" | grep -q '"state": "unconfigured"' || fail "policy"
pass "uv tool installed into the disposable UV_TOOL_DIR; executables run"

"$PY" - "$REPO" "$UV_TOOL_BIN_DIR" <<'PY' || fail "registrations after install"
import json, os, pathlib, sys, tomllib
repo, bin_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
home = pathlib.Path(os.environ["HOME"])
mcp, cli = str(bin_dir / "graphify-worktree-mcp"), str(bin_dir / "graphify-worktree")
skill = home / ".agents/skills/worktree-knowledge"
assert skill.is_symlink() and skill.resolve() == (repo / "skills/worktree-knowledge").resolve(), "skill link"
assert (home / ".claude/skills/worktree-knowledge").is_symlink(), "claude skill link"
for rules in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".omp/agent/RULES.md"):
    assert "<!-- worktree-knowledge:start -->" in (home / rules).read_text(), rules
assert json.loads((home / ".claude.json").read_text())["mcpServers"]["graphify"]["command"] == mcp, "claude mcp"
assert tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["graphify"]["command"] == mcp, "codex mcp"
assert json.loads((home / ".omp/agent/mcp.json").read_text())["mcpServers"]["graphify"]["command"] == mcp, "omp mcp"
for hooks in (".claude/settings.json", ".codex/hooks.json"):
    data = json.loads((home / hooks).read_text())["hooks"]
    commands = {event: [h["command"] for group in data[event] for h in group["hooks"]] for event in data}
    assert commands == {"SessionStart": [f"{cli} recall"], "PreToolUse": [f"{cli} nudge"]}, (hooks, commands)
assert "graphify-out/" in (home / ".config/git/ignore").read_text().splitlines(), "global ignore"
PY
pass "skill links, rules, MCP registrations (Claude, Codex, OMP), recall and nudge hooks, global ignore"

before="$(snapshot)"
"$REPO/install.sh" > "$WORK/reinstall.log" 2>&1 || { cat "$WORK/reinstall.log"; fail "reinstall"; }
[ "$before" = "$(snapshot)" ] || fail "a plain reinstall changed configuration files"
pass "a plain reinstall changes no configuration file and keeps both hooks"

"$REPO/uninstall.sh" > "$WORK/uninstall.log" 2>&1 || { cat "$WORK/uninstall.log"; fail "uninstall.sh exited non-zero"; }
[ ! -e "$UV_TOOL_BIN_DIR/graphify-worktree" ] && [ ! -e "$UV_TOOL_BIN_DIR/graphify-worktree-mcp" ] || fail "executables left"
[ ! -e "$UV_TOOL_DIR/grovekeeper" ] || fail "uv tool environment left"
python3 - "$UV_TOOL_BIN_DIR" <<'PY' || fail "registrations after uninstall"
import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
assert not (home / ".agents/skills/worktree-knowledge").exists(), "skill link"
assert not (home / ".claude/skills/worktree-knowledge").exists(), "claude skill link"
for rules in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".omp/agent/RULES.md"):
    assert "worktree-knowledge:start" not in (home / rules).read_text(), rules
assert "graphify" not in json.loads((home / ".claude.json").read_text()).get("mcpServers", {}), "claude mcp"
assert "mcp_servers.graphify" not in (home / ".codex/config.toml").read_text(), "codex mcp"
assert "graphify" not in json.loads((home / ".omp/agent/mcp.json").read_text()).get("mcpServers", {}), "omp mcp"
for hooks in (".claude/settings.json", ".codex/hooks.json"):
    assert "hooks" not in json.loads((home / hooks).read_text()), hooks
assert "graphify-out/" in (home / ".config/git/ignore").read_text().splitlines(), "global ignore kept"
PY
pass "uninstall removed the tool, links, rules, MCP registrations and hooks; kept the ignore line"
