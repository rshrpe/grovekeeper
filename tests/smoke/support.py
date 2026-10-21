"""Shared scaffolding for the integration smokes.

The smokes exercise real executables: by default the ones next to the Python
running the smoke (a uv tool environment or the development venv), or
GRAPHIFY_WORKTREE_BIN. Both vault roots always point at a disposable directory.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from mcp import StdioServerParameters
from mcp.client.stdio import get_default_environment

BIN = Path(os.environ.get('GRAPHIFY_WORKTREE_BIN') or Path(sys.executable).parent)
WRAPPER = BIN / 'graphify-worktree'
MCP = BIN / 'graphify-worktree-mcp'
VAULT_VARIABLES = ('WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT', 'WORKTREE_KNOWLEDGE_MEMORY_VAULT')


def command(*args, expected=0, env=None, input=None, cwd=None, timeout=180):
    """Run a command; assert its exit code unless expected is None."""
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            timeout=timeout, env=env, input=input, cwd=cwd)
    assert expected is None or result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result


def git(root, *args, **kwargs):
    return command('git', '-C', root, *args, **kwargs)


def init_repo(root, name):
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.email', f'{name}@localhost')
    git(root, 'config', 'user.name', name)


def wrapper_json(*args, expected=0, env=None):
    return json.loads(command(WRAPPER, *args, expected=expected, env=env).stdout)


def server():
    """MCP stdio parameters that carry the disposable vault locations."""
    vaults = {key: os.environ[key] for key in VAULT_VARIABLES}
    return StdioServerParameters(command=str(MCP), env={**get_default_environment(), **vaults})


class Smoke:
    def __init__(self, name):
        self.name = name
        self.checks = []
        directory = Path(os.environ.get('WORKTREE_KNOWLEDGE_SMOKE_RESULTS') or Path(__file__).parent)
        self.result = directory / f'{name}-result.json'
        self.vaults = None

    def passed(self, check):
        self.checks.append(check)
        print('PASS:', check, flush=True)

    def run(self, exercise, prefix):
        """Run exercise(base_directory) in a disposable directory and write the report."""
        report = {'started_at': datetime.now(timezone.utc).isoformat(), 'checks': self.checks}
        try:
            with tempfile.TemporaryDirectory(prefix=prefix) as folder, \
                    tempfile.TemporaryDirectory(prefix='worktree-knowledge-vaults-') as vaults:
                # Outside every disposable repository so vault files never enter a scan.
                self.vaults = Path(vaults).resolve()
                os.environ['WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT'] = str(self.vaults / 'Graphify')
                os.environ['WORKTREE_KNOWLEDGE_MEMORY_VAULT'] = str(self.vaults / 'Engineering')
                asyncio.run(exercise(Path(folder).resolve()))
            report['passed'] = True
        except BaseException as error:
            report.update(passed=False, error=repr(error))
            raise
        finally:
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            self.result.write_text(json.dumps(report, indent=2) + '\n')
            print('Artifact:', self.result)
