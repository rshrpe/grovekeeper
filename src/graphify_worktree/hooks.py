"""Explicit repository-local Git hook chaining and lightweight graph invalidation."""

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

from graphify_worktree.policy import PolicyError, get_policy, git_env, require, resolve_root


EVENTS = frozenset(("post-checkout", "post-merge", "post-rewrite", "post-commit"))
HOOKS = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
    "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit",
    "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive",
    "update", "proc-receive", "post-receive", "post-update", "push-to-checkout",
    "pre-auto-gc", "post-rewrite", "sendemail-validate", "fsmonitor-watchman",
    "p4-changelist", "p4-prepare-changelist", "p4-post-changelist", "p4-pre-submit",
    "post-index-change", "reference-transaction",
)
STATE_DIR = "graphify-worktree-hooks"
MARKER = ".graphify-worktree-event.json"


class HookError(Exception):
    pass


def _git(root, *args, allowed=(0,)):
    result = subprocess.run(["git", "-C", str(root), *args], env=git_env(),
                            capture_output=True, text=True)
    if result.returncode not in allowed:
        raise HookError(result.stderr.strip() or "Git hook configuration failed")
    return result


def _common(root):
    return Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.rstrip("\n")).resolve(strict=True)


def _plain(path, directory=False):
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise HookError(f"refusing non-regular owned hook path: {path}")


def _values(root, config):
    result = _git(root, "config", "--file", str(config), "--no-includes", "--null",
                  "--get-all", "core.hooksPath", allowed=(0, 1))
    return result.stdout.removesuffix("\0").split("\0") if result.returncode == 0 else []


def _effective(root):
    result = _git(root, "config", "--path", "--null", "--get", "core.hooksPath", allowed=(0, 1))
    return result.stdout.removesuffix("\0") if result.returncode == 0 else None


def _supported(root):
    result = _git(root, "config", "--bool", "--get", "extensions.worktreeConfig", allowed=(0, 1))
    if result.stdout.strip() != "true":
        return
    common = _common(root)
    configs = [common / "config.worktree"]
    worktrees = common / "worktrees"
    if worktrees.exists() or worktrees.is_symlink():
        _plain(worktrees, directory=True)
        for entry in worktrees.iterdir():
            _plain(entry, directory=True)
            configs.append(entry / "config.worktree")
    for config in configs:
        if not config.exists() and not config.is_symlink():
            continue
        _plain(config)
        overrides = _values(root, config)
        includes = _git(root, "config", "--file", str(config), "--no-includes",
                        "--get-regexp", r"^include(if\..*)?\.path$", allowed=(0, 1))
        if overrides or includes.returncode == 0:
            raise HookError(f"per-worktree hooksPath override or ambiguous included configuration in {config}; no configuration changed")


def _script(state, hook, python, module):
    command = " ".join(shlex.quote(str(value)) for value in (python, module, "--dispatch", state, hook))
    return f'#!/bin/sh\nexec {command} "$@"\n'


def _load(state):
    _plain(state, directory=True)
    _plain(state / "manifest.json")
    with (state / "manifest.json").open(encoding="utf-8") as stream:
        saved = json.load(stream)
    if (not isinstance(saved, dict) or saved.get("schema") != 1
            or saved.get("common_dir") != str(state.parent)
            or saved.get("hooks_path") != str(state / "hooks")
            or not isinstance(saved.get("original_local"), list)
            or len(saved["original_local"]) > 1
            or not all(isinstance(value, str) for value in saved["original_local"])
            or not isinstance(saved.get("original_directory"), str)
            or not isinstance(saved.get("python"), str)
            or not isinstance(saved.get("module"), str)):
        raise HookError("invalid owned hook manifest")
    _plain(state / "hooks", directory=True)
    return saved


def _intact(state, saved):
    if set(os.listdir(state)) != {"manifest.json", "hooks"} or set(os.listdir(state / "hooks")) != set(HOOKS):
        raise HookError("owned hook directory has unexpected or missing files; refusing to modify it")
    for hook in HOOKS:
        path = state / "hooks" / hook
        _plain(path)
        if not os.access(path, os.X_OK) or path.read_text() != _script(state, hook, saved["python"], saved["module"]):
            raise HookError(f"owned hook shim was modified: {path}")


@contextmanager
def _config_lock(root, common):
    """Use Git's lock convention so comparison and replacement cannot lose edits."""
    config = common / "config"
    _plain(config)
    lock = common / "config.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(config.stat().st_mode))
    published = False

    def publish():
        nonlocal published
        os.replace(lock, config)
        published = True

    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(config.read_bytes())
        yield lock, publish
    finally:
        # After publication another Git process may already own a new lock.
        if not published:
            lock.unlink(missing_ok=True)


def _set_config(root, lock, values):
    if values:
        _git(root, "config", "--file", str(lock), "--no-includes", "--replace-all", "core.hooksPath", values[0])
    else:
        _git(root, "config", "--file", str(lock), "--no-includes", "--unset-all", "core.hooksPath", allowed=(0, 5))


def _active(root, common, saved):
    _supported(root)
    if (_values(root, common / "config") != [saved["hooks_path"]]
            or _effective(root) != saved["hooks_path"]):
        raise HookError("core.hooksPath changed outside this integration; refusing to overwrite it")


def install(path):
    root = resolve_root(path)
    require(root, "graph")
    common = _common(root)
    state = common / STATE_DIR
    with _config_lock(root, common) as (lock, publish):
        _supported(root)
        if state.exists() or state.is_symlink():
            saved = _load(state)
            _active(root, common, saved)
            _intact(state, saved)
            return {"state": "installed", "changed": False, "worktree_root": str(root), "hooks_path": saved["hooks_path"]}
        original = _values(root, lock)
        if len(original) > 1:
            raise HookError("multiple local core.hooksPath values are unsupported; no configuration changed")
        directory = _effective(root)
        if directory is None:
            directory = str(common / "hooks")
        candidate = Path(directory)
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.resolve().is_relative_to(state):
            raise HookError("original hooksPath points into integration state; refusing recursive delegation")
        saved = {
            "schema": 1, "common_dir": str(common), "hooks_path": str(state / "hooks"),
            "original_local": original, "original_directory": directory,
            "python": sys.executable, "module": str(Path(__file__).resolve()),
        }
        # Prepare the owned directory before switching hooksPath. Never touch the
        # original directory, including shared/global and Husky-managed hooks.
        state.mkdir(mode=0o700)
        try:
            (state / "hooks").mkdir(mode=0o700)
            for hook in HOOKS:
                target = state / "hooks" / hook
                target.write_text(_script(state, hook, saved["python"], saved["module"]), encoding="utf-8")
                target.chmod(0o700)
            (state / "manifest.json").write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
            _set_config(root, lock, [saved["hooks_path"]])
            effective = _git(root, "config", "--file", str(lock), "--includes",
                             "--path", "--null", "--get", "core.hooksPath").stdout.removesuffix("\0")
            if effective != saved["hooks_path"]:
                raise HookError("included configuration overrides local hooksPath; no configuration changed")
            publish()
        except BaseException:
            # Only files created above are removed; unexpected files are retained.
            for hook in HOOKS:
                (state / "hooks" / hook).unlink(missing_ok=True)
            (state / "manifest.json").unlink(missing_ok=True)
            (state / "hooks").rmdir()
            state.rmdir()
            raise
    return {"state": "installed", "changed": True, "worktree_root": str(root), "hooks_path": saved["hooks_path"]}


def status(path):
    root = resolve_root(path)
    common = _common(root)
    state = common / STATE_DIR
    result = {"state": "not-installed", "worktree_root": str(root), "hooks_path": _effective(root)}
    if state.exists() or state.is_symlink():
        try:
            saved = _load(state)
            _active(root, common, saved)
            _intact(state, saved)
            result.update(state="installed", original_local=saved["original_local"], original_directory=saved["original_directory"])
        except (HookError, OSError, ValueError) as error:
            result.update(state="conflict", reason=str(error))
    return result


def uninstall(path):
    root = resolve_root(path)
    common = _common(root)
    state = common / STATE_DIR
    with _config_lock(root, common) as (lock, publish):
        if not state.exists() and not state.is_symlink():
            return {"state": "not-installed", "changed": False, "worktree_root": str(root)}
        saved = _load(state)
        _active(root, common, saved)
        _intact(state, saved)
        _set_config(root, lock, saved["original_local"])
        publish()
        for hook in HOOKS:
            (state / "hooks" / hook).unlink()
        (state / "hooks").rmdir()
        (state / "manifest.json").unlink()
        state.rmdir()
    return {"state": "not-installed", "changed": True, "worktree_root": str(root)}


def mark_event(path, event):
    """No graph scan/build, no output creation, and no permission by hook install."""
    if event not in EVENTS:
        return False
    root = resolve_root(path)
    if get_policy(root)["effective_mode"] not in ("graph", "obsidian"):
        return False
    output = root / "graphify-out"
    if not output.exists():
        return False
    _plain(output, directory=True)
    # Directory-relative operations keep publication confined if output is moved.
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        for name in ("graph.json", ".graphify-worktree.json"):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(info.st_mode):
                raise HookError(f"refusing non-regular graph artifact: {name}")
        descriptor = os.open(".graphify-worktree.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            saved = json.load(stream)
        generation = saved.get("extracted_at") if isinstance(saved, dict) else None
        if not isinstance(generation, str) or not generation or saved.get("worktree_root") != str(root):
            return False
        try:
            info = os.stat(MARKER, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise HookError("refusing non-regular event marker")
        except FileNotFoundError:
            pass
        temporary = f"{MARKER}.{os.getpid()}.{os.urandom(8).hex()}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"event": event, "at": datetime.now(timezone.utc).isoformat(), "generation": generation}, stream)
            stream.write("\n")
        os.replace(temporary, MARKER, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        return True
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def dispatch(state_path, hook, arguments):
    """Inherit cwd/stdin/stdout/environment; knowledge failures never fail Git."""
    if hook not in HOOKS:
        return 1
    state = Path(state_path)
    try:
        if not state.is_absolute() or state.parent != state.parent.resolve():
            raise HookError("invalid owned hook state path")
        saved = _load(state)
        original = Path(saved["original_directory"]) / hook
        if original.resolve().is_relative_to(state):
            raise HookError("refusing recursive original hook")
    except (HookError, OSError, ValueError) as error:
        print(f"graphify-worktree hooks: {error}", file=sys.stderr)
        return 1  # Never silently bypass an original hook whose state is damaged.
    code = 0
    try:
        if original.is_file() and os.access(original, os.X_OK):
            command = [str(original.absolute()), *arguments]
            try:
                code = subprocess.run(command).returncode
            except OSError as error:
                if error.errno != errno.ENOEXEC:
                    raise
                # Git itself runs executable hooks without a shebang via sh.
                code = subprocess.run(["/bin/sh", *command]).returncode
    except OSError as error:
        print(f"graphify-worktree original hook: {error}", file=sys.stderr)
        code = 1
    if hook in EVENTS:
        try:
            mark_event(Path.cwd(), hook)
        except (HookError, PolicyError, OSError, ValueError):
            pass
    return code if code >= 0 else 128 - code


def add_parser(commands):
    parser = commands.add_parser("hooks", help="explicitly install, inspect or remove repository-local Git event hooks")
    actions = parser.add_subparsers(dest="hooks_action", required=True)
    for action in ("install", "status", "uninstall"):
        command = actions.add_parser(action)
        command.add_argument("path", nargs="?", default=".", help="directory in the worktree (default: cwd)")


def run(arguments):
    try:
        response = {"install": install, "status": status, "uninstall": uninstall}[arguments.hooks_action](arguments.path)
        print(json.dumps(response, indent=2))
        return 1 if response["state"] == "conflict" else 0
    except PolicyError as error:
        print(json.dumps({"state": "denied", "reason": str(error)}))
        return 5
    except (HookError, OSError, ValueError) as error:
        print(json.dumps({"state": "error", "reason": str(error)}))
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--dispatch":
        raise SystemExit(dispatch(sys.argv[2], sys.argv[3], sys.argv[4:]))
    raise SystemExit("Use graphify-worktree hooks install|status|uninstall PATH")
