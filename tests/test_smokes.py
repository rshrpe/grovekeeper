"""Run the integration smokes as subprocesses under a disposable HOME.

Any write to a default location (~/Obsidian, ~/.gitconfig, ...) lands in the
temporary HOME, so the smokes can never touch the developer's real vaults.
"""
from pathlib import Path
import subprocess
import sys

import pytest

from integration import isolated_env

SMOKES = Path(__file__).parent / "smoke"


@pytest.mark.integration
@pytest.mark.parametrize("script", ["smoke.py", "memory_smoke.py", "lifecycle_smoke.py"])
def test_smoke_passes_under_isolated_home(script, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = isolated_env(home, WORKTREE_KNOWLEDGE_SMOKE_RESULTS=str(tmp_path))
    result = subprocess.run([sys.executable, str(SMOKES / script)], env=env,
                            capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "PASS:" in result.stdout
    # Vaults went to the smoke's disposable directory, not to HOME defaults.
    assert not (home / "Obsidian").exists()
