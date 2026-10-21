"""One registry drives memory validation, CLI flags and MCP input schemas."""
import argparse
import json
from pathlib import Path

import pytest

from graphify_worktree.errors import MemoryRequestError
from graphify_worktree.memory import schema

FIXTURE = {tool["name"]: tool for tool in json.loads((Path(__file__).parent / "fixtures/mcp_tools.json").read_text())}
NOTE_ID = "0b6f2a4e-8c1d-4f3a-9e2b-5d7c6a1b2c3d"
WRITE = {"source": "s", "recorded_by": "claude", "confirmed": True, "confirmation": "user said yes"}


@pytest.mark.parametrize("action", list(schema.ACTIONS))
def test_mcp_tool_definition_matches_captured_surface(action):
    tool = FIXTURE[f"memory_{action}"]
    definition = schema.ACTIONS[action]
    assert definition.mcp_description() == tool["description"]
    assert definition.input_schema() == tool["input_schema"]
    assert definition.read_only == tool["annotations"]["read_only_hint"]


@pytest.mark.parametrize("action, arguments, expected", [
    ("search", {"query": "retry"}, {"query": "retry", "limit": 5, "include_history": False}),
    ("get", {"id": NOTE_ID}, {"id": NOTE_ID}),
    ("save", {"kind": "fact", "title": "t", "content": "c", **WRITE},
     {"kind": "fact", "title": "t", "content": "c", **WRITE, "status": "proposed"}),
])
def test_defaults_are_applied(action, arguments, expected):
    assert schema.validate(action, arguments) == expected


@pytest.mark.parametrize("action, arguments, message", [
    ("search", {}, "missing fields: query"),
    ("search", {"query": "x", "extra": 1}, "unknown fields: extra"),
    ("search", {"query": "   "}, "query must be a nonempty string"),
    ("search", {"query": "a\0b"}, "query must be a nonempty string"),
    ("search", {"query": "x", "limit": 0}, "limit must be an integer between 1 and 20"),
    ("search", {"query": "x", "limit": 21}, "limit must be an integer between 1 and 20"),
    ("search", {"query": "x", "limit": "5"}, "limit must be an integer between 1 and 20"),
    ("search", {"query": "x", "limit": True}, "limit must be an integer between 1 and 20"),
    ("search", {"query": "x", "include_history": "yes"}, "include_history must be a boolean"),
    ("get", {"id": NOTE_ID.upper()}, "id must be a canonical lowercase UUID"),
    ("get", {"id": "../outside"}, "id must be a canonical lowercase UUID"),
    ("save", {"kind": "rumour", "title": "t", "content": "c", **WRITE}, "kind must be one of: decision, fact, lesson"),
    ("save", {"kind": "fact", "title": "t", "content": "c", **WRITE, "status": "superseded"},
     "status must be one of: proposed, accepted"),
    ("save", {"kind": "fact", "title": "t", "content": "c", **WRITE, "confirmed": "true"},
     "confirmed must be true only after explicit user approval"),
    ("accept", {"id": NOTE_ID, **WRITE, "recorded_by": "bot"}, "recorded_by must be one of: omp, claude, codex, manual"),
    ("supersede", {"id": NOTE_ID, **WRITE}, "missing fields: replacement_id"),
    ("purge", {}, "unknown memory action: purge"),
])
def test_invalid_requests_are_rejected(action, arguments, message):
    with pytest.raises(MemoryRequestError, match=message):
        schema.validate(action, arguments)


def test_arguments_must_be_an_object():
    with pytest.raises(MemoryRequestError, match="arguments must be an object"):
        schema.validate("search", ["query"])


def parse(*argv):
    parser = argparse.ArgumentParser()
    schema.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["memory", *argv])


def test_read_actions_take_flags_derived_from_their_fields():
    arguments = parse("search", "/repo", "--query", "retry policy", "--limit", "3", "--include-history", "--once")
    assert (arguments.path, arguments.once) == ("/repo", True)
    assert schema.payload_from_cli(arguments) == {"query": "retry policy", "limit": 3, "include_history": True}
    assert schema.payload_from_cli(parse("get", "--id", NOTE_ID)) == {"id": NOTE_ID}


def test_write_actions_read_json_input(tmp_path):
    payload = {"id": NOTE_ID, **WRITE}
    path = tmp_path / "accept.json"
    path.write_text(json.dumps(payload))
    assert schema.payload_from_cli(parse("accept", "--input-file", str(path))) == payload


def test_malformed_json_input_is_a_request_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(MemoryRequestError, match="input"):
        schema.payload_from_cli(parse("save", "--input-file", str(path)))


def test_write_actions_require_an_input_file():
    with pytest.raises(SystemExit):
        parse("save")
