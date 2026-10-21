import pytest

from graphify_worktree import settings


SETTINGS = [
    # (accessor, environment variable, default relative to HOME)
    (settings.graph_vault_root, "WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT", "Obsidian/Graphify"),
    (settings.memory_vault, "WORKTREE_KNOWLEDGE_MEMORY_VAULT", "Obsidian/Engineering"),
    (settings.obsidian_cli, "WORKTREE_KNOWLEDGE_OBSIDIAN_CLI", ".local/bin/obsidian"),
    (settings.skill_dir, "WORKTREE_KNOWLEDGE_SKILL_DIR", ".agents/skills/worktree-knowledge"),
]


@pytest.mark.parametrize("accessor, variable, default", SETTINGS)
def test_default_is_under_current_home(home, accessor, variable, default):
    assert accessor() == home / default


@pytest.mark.parametrize("accessor, variable, default", SETTINGS)
def test_empty_override_falls_back_to_default(home, monkeypatch, accessor, variable, default):
    monkeypatch.setenv(variable, "")
    assert accessor() == home / default


@pytest.mark.parametrize("accessor, variable, default", SETTINGS)
def test_absolute_override_wins(home, monkeypatch, tmp_path, accessor, variable, default):
    monkeypatch.setenv(variable, str(tmp_path / "elsewhere"))
    assert accessor() == tmp_path / "elsewhere"


@pytest.mark.parametrize("accessor, variable, default", SETTINGS)
def test_tilde_override_expands_against_home(home, monkeypatch, accessor, variable, default):
    monkeypatch.setenv(variable, "~/Vaults/x")
    assert accessor() == home / "Vaults/x"


@pytest.mark.parametrize("accessor, variable, default", SETTINGS)
def test_relative_override_is_rejected(home, monkeypatch, accessor, variable, default):
    monkeypatch.setenv(variable, "Vaults/x")
    with pytest.raises(settings.SettingsError, match=variable):
        accessor()


def test_settings_error_is_a_value_error():
    # Existing CLI/MCP handlers map ValueError to exit 1 / request errors.
    assert issubclass(settings.SettingsError, ValueError)
