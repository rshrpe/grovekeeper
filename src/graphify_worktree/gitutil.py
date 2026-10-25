"""The one way this package runs Git, and the one repository identity."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess

from graphify_worktree.errors import GitError, PolicyError


def git_env():
    # Explicit paths must win over ambient repository/config redirection.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def run(path, *args):
    """Run git in path; the caller interprets the exit status."""
    return subprocess.run(["git", "-C", str(path), *args], env=git_env(), capture_output=True, text=True)


def output(path, *args, missing=()):
    """Stdout without the trailing newline; None for exit codes listed in missing."""
    result = run(path, *args)
    if result.returncode:
        if result.returncode in missing:
            return None
        raise GitError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.rstrip("\n")


def resolve_root(path):
    """Canonical top level of the worktree containing path; PolicyError otherwise."""
    try:
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise PolicyError(f"not a directory: {path}")
        result = run(path, "rev-parse", "--show-toplevel")
        if result.returncode:
            reason = result.stderr.strip() or "cannot resolve Git worktree"
            raise PolicyError(reason, not_repository="not a git repository" in reason.lower())
        return Path(result.stdout.rstrip("\n")).resolve(strict=True)
    except OSError as error:
        raise PolicyError(str(error)) from error


def common_dir(root):
    return Path(output(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)


def admin_dir(root):
    """The worktree's own Git directory; equal to the common directory only for the main worktree."""
    return Path(output(root, "rev-parse", "--absolute-git-dir")).resolve(strict=True)


def registered_worktrees(common):
    """Worktree paths a saved common Git directory still registers, prunable ones included.

    Read-only and usable after the worktree is gone: nothing is pruned or repaired.
    """
    result = subprocess.run(["git", "--git-dir", str(common), "worktree", "list", "--porcelain", "-z"],
                            cwd=common, env=git_env(), capture_output=True, text=True)
    if result.returncode:
        raise GitError(result.stderr.strip() or "git worktree list failed")
    return [Path(field.removeprefix("worktree ")) for field in result.stdout.split("\0")
            if field.startswith("worktree ")]


def checkout_state(root, *, allow_unborn=False):
    """Branch (None when detached) and HEAD commit (None only when unborn and allowed)."""
    return {
        "branch": output(root, "symbolic-ref", "--quiet", "--short", "HEAD",
                         missing=(1, 128) if allow_unborn else (1,)),
        "head": output(root, "rev-parse", "--verify", "HEAD",
                       missing=(1, 128) if allow_unborn else (1,)),
    }


@dataclass(frozen=True)
class RepoIdentity:
    """Shared by every worktree of one clone; distinct per clone and per machine.

    The id hashes the absolute common Git directory. The name is a readable,
    path-safe label used for both the generated-vault folder and the memory
    namespace, so they always agree.
    """

    common_git_dir: Path
    id: str
    name: str

    @classmethod
    def of(cls, root):
        common = common_dir(root)
        name = common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
        name = re.sub(r"[^\w.-]+", "-", name).strip(".-") or "repository"
        return cls(common, hashlib.sha256(os.fsencode(common)).hexdigest(), name)
