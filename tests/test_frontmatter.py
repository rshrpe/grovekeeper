"""Memory note frontmatter: the subset of YAML that Obsidian edits produce."""
import pytest

from graphify_worktree.errors import MemoryRequestError
from graphify_worktree.memory.store import parse_frontmatter as parse


def test_json_scalars_lists_and_body_round_trip():
    metadata, body = parse('---\nid: "abc"\nschema_version: 1\nflag: true\npr: null\n'
                           'audit_action: ["save", "accept"]\n---\nBody text\n')
    assert metadata == {"id": "abc", "schema_version": 1, "flag": True, "pr": None,
                        "audit_action": ["save", "accept"]}
    assert body == "Body text\n"


def test_obsidian_style_edits_are_accepted():
    metadata, _ = parse("---\n# comment\ntitle: Plain title\nquoted: 'it''s'\nempty:\ntilde: ~\n"
                        "audit_branch:\n  - main\n  - null\n---\n")
    assert metadata == {"title": "Plain title", "quoted": "it's", "empty": None, "tilde": None,
                        "audit_branch": ["main", None]}


@pytest.mark.parametrize("text, message", [
    ("no frontmatter\n", "missing Markdown YAML frontmatter"),
    ("---\ntitle: a\n", "missing Markdown YAML frontmatter"),
    ("---\ntitle: a\ntitle: b\n---\n", "invalid or duplicate frontmatter property"),
    ("---\nTitle: a\n---\n", "invalid or duplicate frontmatter property"),
    ("---\n- orphan\n---\n", "invalid frontmatter list"),
    ("---\ntitle: a: b\n---\n", "unsupported frontmatter value"),
    ("---\ntitle: |\n---\n", "unsupported frontmatter value"),
    ("---\ntitle: value # trailing comment\n---\n", "unsupported frontmatter value"),
])
def test_unsupported_yaml_is_rejected_explicitly(text, message):
    with pytest.raises(MemoryRequestError, match=message):
        parse(text)
