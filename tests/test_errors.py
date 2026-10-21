"""One table maps domain errors to CLI exit codes; unexpected bugs are not domain errors."""
import pytest

from graphify_worktree import errors


@pytest.mark.parametrize("error, code", [
    (errors.PolicyError("denied"), 5),
    (errors.GraphError("stale"), 1),
    (errors.ProvenanceError("bad provenance"), 1),
    (errors.GitError("git failed"), 1),
    (errors.MemoryRequestError("bad note"), 1),
    (errors.HookError("conflict"), 1),
    (errors.SettingsError("relative"), 1),
    (errors.UpstreamVersionError("pins"), 1),
    (OSError("disk"), 1),
])
def test_expected_failures_have_documented_exit_codes(error, code):
    assert errors.exit_code(error) == code


@pytest.mark.parametrize("bug", [KeyError("x"), TypeError("x"), ValueError("x"), AttributeError("x")])
def test_programming_errors_are_not_mapped(bug):
    assert errors.exit_code(bug) is None


def test_policy_error_keeps_policy_and_repository_flag():
    error = errors.PolicyError("no", {"state": "off"}, not_repository=True)
    assert (str(error), error.policy, error.not_repository) == ("no", {"state": "off"}, True)


def test_provenance_problems_are_graph_problems():
    assert issubclass(errors.ProvenanceError, errors.GraphError)
