"""Explicit repository-local Git hook chaining and lightweight graph invalidation.

This file is also executed directly by the installed hook shims
(`python -P hooks.py --dispatch STATE HOOK ...`), so it must keep working as a
script.
"""

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

from graphify_worktree import artifacts, errors, gitutil
from graphify_worktree.errors import GraphError, HookError, KnowledgeError, PolicyError
from graphify_worktree.policy import get_policy, require, resolve_root


EVENTS = ("post-checkout", "post-commit", "post-merge", "post-rewrite")
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
SCHEMA = 2  # 1: legacy, every Git hook shimmed with a shim that needs the tool
EVENT_LOCK_WAIT = 5.0


def _git(root, *args, allowed=(0,)):
    result = gitutil.run(root, *args)
    if result.returncode not in allowed:
        raise HookError(result.stderr.strip() or "Git hook configuration failed")
    return result


def _common(root):
    try:
        return gitutil.common_dir(root)
    except errors.GitError as error:
        raise HookError(str(error)) from error


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


# Shims ----------------------------------------------------------------------

def shim_script(state, hook, saved):
    """The shell shim Git runs for hook; depends on the manifest's schema."""
    python, module = saved["python"], saved["module"]
    if saved["schema"] == 1:
        command = " ".join(shlex.quote(str(value)) for value in (python, module, "--dispatch", state, hook))
        return f'#!/bin/sh\nexec {command} "$@"\n'
    original = shlex.quote(str(Path(saved["original_directory"]) / hook))
    q = shlex.quote
    return (
        "#!/bin/sh\n"
        "# graphify-worktree hook shim. Without the tool, only the original hook runs.\n"
        f"if [ -x {q(str(python))} ] && [ -f {q(str(module))} ]; then\n"
        f"  exec {q(str(python))} -P {q(str(module))} --dispatch {q(str(state))} {q(hook)} \"$@\"\n"
        "fi\n"
        f"original={original}\n"
        'if [ -f "$original" ] && [ -x "$original" ]; then\n'
        '  exec "$original" "$@"\n'
        "fi\n"
        "exit 0\n"
    )


def _shimmed(saved):
    return tuple(saved.get("hooks", HOOKS))


def _wanted(root, saved):
    """Event hooks plus every hook the original directory already provides."""
    directory = Path(saved["original_directory"])
    directory = directory if directory.is_absolute() else root / directory
    present = {hook for hook in HOOKS if (directory / hook).is_file()}
    return tuple(hook for hook in HOOKS if hook in EVENTS or hook in present)


def _moved_from(state, saved):
    """The common Git directory a manifest was written in, if the repository moved since.

    Only a manifest whose paths differ from this state directory by the common
    directory prefix alone counts; anything else stays an invalid manifest.
    """
    if not isinstance(saved, dict) or not isinstance(saved.get("common_dir"), str):
        return None
    previous = Path(saved["common_dir"])
    if (str(previous) == str(state.parent) or not previous.is_absolute()
            or saved.get("hooks_path") != str(previous / state.name / "hooks")):
        return None
    return previous


def _rehome(value, previous, common):
    """A path recorded under the previous common directory, moved under the current one."""
    path = Path(value)
    if path.is_absolute() and path.is_relative_to(previous):
        return str(common / path.relative_to(previous))
    return value


def _load(state):
    """The manifest, and the common directory it was written in if the repository moved since."""
    _plain(state, directory=True)
    _plain(state / "manifest.json")
    try:
        with (state / "manifest.json").open(encoding="utf-8") as stream:
            saved = json.load(stream)
    except ValueError as error:
        raise HookError(f"invalid owned hook manifest: {error}") from error
    previous = _moved_from(state, saved)
    written = state if previous is None else previous / state.name
    if (not isinstance(saved, dict) or saved.get("schema") not in (1, SCHEMA)
            or saved.get("common_dir") != str(written.parent)
            or saved.get("hooks_path") != str(written / "hooks")
            or not isinstance(saved.get("original_local"), list)
            or len(saved["original_local"]) > 1
            or not all(isinstance(value, str) for value in saved["original_local"])
            or not isinstance(saved.get("original_directory"), str)
            or not isinstance(saved.get("python"), str)
            or not isinstance(saved.get("module"), str)
            or (saved["schema"] == SCHEMA and (not isinstance(saved.get("hooks"), list)
                                               or not set(saved["hooks"]) <= set(HOOKS)))):
        raise HookError("invalid owned hook manifest")
    _plain(state / "hooks", directory=True)
    return saved, previous


def _intact(state, saved, previous=None):
    """The shims are exactly as written, at the state path they were written for."""
    written = state if previous is None else previous / state.name
    expected = set(_shimmed(saved))
    if set(os.listdir(state)) != {"manifest.json", "hooks"} or set(os.listdir(state / "hooks")) != expected:
        raise HookError("owned hook directory has unexpected or missing files; refusing to modify it")
    for hook in expected:
        path = state / "hooks" / hook
        _plain(path)
        if not os.access(path, os.X_OK) or path.read_text() != shim_script(written, hook, saved):
            raise HookError(f"owned hook shim was modified: {path}")


def _write_shims(state, saved, names):
    for hook in names:
        target = state / "hooks" / hook
        target.write_text(shim_script(state, hook, saved), encoding="utf-8")
        target.chmod(0o700)


def _write_manifest(state, saved):
    temporary = state / "manifest.json.tmp"
    temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, state / "manifest.json")


# Configuration lock ------------------------------------------------------------

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


def _check_includes(root, lock, hooks_path):
    effective = _git(root, "config", "--file", str(lock), "--includes",
                     "--path", "--null", "--get", "core.hooksPath").stdout.removesuffix("\0")
    if effective != hooks_path:
        raise HookError("included configuration overrides local hooksPath; no configuration changed")


def _repair(root, state, lock, publish, saved, previous):
    """Re-home a moved repository: shims, manifest and core.hooksPath for its new location.

    Git found no hooks at the old absolute hooksPath, so until this runs none
    of the repository's hooks, including the user's own, were running.
    """
    common = state.parent
    moved = {**saved, "common_dir": str(common), "hooks_path": str(state / "hooks"),
             "original_local": [_rehome(value, previous, common) for value in saved["original_local"]],
             "original_directory": _rehome(saved["original_directory"], previous, common)}
    if saved["schema"] == SCHEMA:
        moved["hooks"] = [hook for hook in HOOKS if hook in set(saved["hooks"]) | set(_wanted(root, moved))]
    _set_config(root, lock, [moved["hooks_path"]])
    _check_includes(root, lock, moved["hooks_path"])
    try:
        _write_shims(state, moved, _shimmed(moved))
        _write_manifest(state, moved)
        publish()
    except BaseException:
        # Put back exactly what was there, so the next attempt recognises it again.
        for hook in set(_shimmed(moved)) - set(_shimmed(saved)):
            (state / "hooks" / hook).unlink(missing_ok=True)
        for hook in _shimmed(saved):
            (state / "hooks" / hook).write_text(shim_script(previous / state.name, hook, saved), encoding="utf-8")
        _write_manifest(state, saved)
        raise


# Commands ----------------------------------------------------------------------

def install(path):
    root = resolve_root(path)
    require(root, "graph")
    common = _common(root)
    state = common / STATE_DIR
    with _config_lock(root, common) as (lock, publish):
        _supported(root)
        if state.exists() or state.is_symlink():
            saved, previous = _load(state)
            _active(root, common, saved)
            _intact(state, saved, previous)
            if previous is not None:
                _repair(root, state, lock, publish, saved, previous)
                return {"state": "installed", "changed": True, "worktree_root": str(root),
                        "hooks_path": str(state / "hooks"), "moved_from": str(previous)}
            added = [hook for hook in _wanted(root, saved) if hook not in _shimmed(saved)]
            if added and saved["schema"] == SCHEMA:
                saved["hooks"] = [hook for hook in HOOKS if hook in set(saved["hooks"]) | set(added)]
                _write_shims(state, saved, added)
                _write_manifest(state, saved)
            changed = bool(added) and saved["schema"] == SCHEMA
            return {"state": "installed", "changed": changed, "worktree_root": str(root), "hooks_path": saved["hooks_path"]}
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
            "schema": SCHEMA, "common_dir": str(common), "hooks_path": str(state / "hooks"),
            "original_local": original, "original_directory": directory,
            "python": sys.executable, "module": str(Path(__file__).resolve()),
        }
        saved["hooks"] = list(_wanted(root, saved))
        # Prepare the owned directory before switching hooksPath. Never touch the
        # original directory, including shared/global and Husky-managed hooks.
        state.mkdir(mode=0o700)
        try:
            (state / "hooks").mkdir(mode=0o700)
            _write_shims(state, saved, saved["hooks"])
            (state / "manifest.json").write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
            _set_config(root, lock, [saved["hooks_path"]])
            _check_includes(root, lock, saved["hooks_path"])
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
            saved, previous = _load(state)
            _active(root, common, saved)
            _intact(state, saved, previous)
            if previous is not None:
                result.update(state="relocated", moved_from=str(previous),
                              reason="the repository moved since hooks install, so Git runs none of its hooks; "
                                     "run hooks install to repair them or hooks uninstall to remove them")
                return result
            result.update(state="installed", original_local=saved["original_local"],
                          original_directory=saved["original_directory"],
                          unforwarded=[hook for hook in _wanted(root, saved) if hook not in _shimmed(saved)])
        except (HookError, OSError) as error:
            result.update(state="conflict", reason=str(error))
    return result


def uninstall(path):
    root = resolve_root(path)
    common = _common(root)
    state = common / STATE_DIR
    with _config_lock(root, common) as (lock, publish):
        if not state.exists() and not state.is_symlink():
            return {"state": "not-installed", "changed": False, "worktree_root": str(root)}
        saved, previous = _load(state)
        _active(root, common, saved)
        _intact(state, saved, previous)
        original = saved["original_local"]
        if previous is not None:
            original = [_rehome(value, previous, common) for value in original]
        _set_config(root, lock, original)
        publish()
        for hook in _shimmed(saved):
            (state / "hooks" / hook).unlink()
        (state / "hooks").rmdir()
        (state / "manifest.json").unlink()
        state.rmdir()
    return {"state": "not-installed", "changed": True, "worktree_root": str(root)}


def mark_event(path, event, *, wait=EVENT_LOCK_WAIT):
    """Tag the published generation as invalidated by a Git event.

    No graph scan/build, no output creation, and no permission by hook install.
    Takes the worktree lock so the tag cannot interleave with a publish; if a
    refresh or query holds it longer than wait seconds the hint is skipped,
    which is safe because every query re-checks source content anyway.
    """
    if event not in EVENTS:
        return False
    root = resolve_root(path)
    if get_policy(root)["effective_mode"] not in ("graph", "obsidian"):
        return False
    output = root / "graphify-out"
    if not output.exists():
        return False
    _plain(output, directory=True)
    try:
        with artifacts.locked(root, timeout=wait):
            return _write_event(root, output, event)
    except GraphError:
        return False


def _write_event(root, output, event):
    # Directory-relative operations keep publication confined if output is moved.
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        for name in ("graph.json", artifacts.PROVENANCE):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(info.st_mode):
                raise HookError(f"refusing non-regular graph artifact: {name}")
        descriptor = os.open(artifacts.PROVENANCE, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            saved = json.load(stream)
        generation = saved.get("extracted_at") if isinstance(saved, dict) else None
        if not isinstance(generation, str) or not generation or saved.get("worktree_root") != str(root):
            return False
        try:
            info = os.stat(artifacts.EVENT, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise HookError("refusing non-regular event marker")
        except FileNotFoundError:
            pass
        temporary = f"{artifacts.EVENT}.{os.getpid()}.{os.urandom(8).hex()}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"event": event, "at": datetime.now(timezone.utc).isoformat(), "generation": generation}, stream)
            stream.write("\n")
        os.replace(temporary, artifacts.EVENT, src_dir_fd=directory, dst_dir_fd=directory)
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
        saved, previous = _load(state)
        if previous is not None:
            raise HookError("the repository moved since hooks install; run graphify-worktree hooks install")
        original = Path(saved["original_directory"]) / hook
        if original.resolve().is_relative_to(state):
            raise HookError("refusing recursive original hook")
    except (HookError, OSError) as error:
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
        except (KnowledgeError, OSError, ValueError):
            pass
    return code if code >= 0 else 128 - code


def add_parser(commands):
    parser = commands.add_parser("hooks", help="explicitly install, inspect or remove repository-local Git event hooks")
    actions = parser.add_subparsers(dest="hooks_action", required=True)
    for action in ("install", "status", "uninstall"):
        command = actions.add_parser(action)
        command.add_argument("path", nargs="?", default=".", help="directory in the worktree (default: cwd)")


def run(arguments):
    """(result JSON, exit code) for a hooks command; failures are results too."""
    try:
        response = {"install": install, "status": status, "uninstall": uninstall}[arguments.hooks_action](arguments.path)
        return response, 1 if response["state"] in ("conflict", "relocated") else 0
    except PolicyError as error:
        return {"state": "denied", "reason": str(error)}, errors.exit_code(error)
    except (KnowledgeError, OSError) as error:
        return {"state": "error", "reason": str(error)}, errors.exit_code(error)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--dispatch":
        raise SystemExit(dispatch(sys.argv[2], sys.argv[3], sys.argv[4:]))
    raise SystemExit("Use graphify-worktree hooks install|status|uninstall PATH")
