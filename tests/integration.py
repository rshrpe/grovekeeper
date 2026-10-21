"""Helpers for tests that run real executables under a disposable HOME."""
import os


def isolated_env(home, **extra):
    """Process environment with HOME redirected and every knowledge override removed."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("WORKTREE_KNOWLEDGE_", "GIT_")) and key not in (
               "HARNESS_KNOWLEDGE_MODE", "XDG_CONFIG_HOME", "GRAPHIFY_OUT")}
    env["HOME"] = str(home)
    env.update(extra)
    return env
