import ast
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from graphify_worktree import upstream


PACKAGE = Path(upstream.__file__).parent


def test_installed_pins_pass():
    upstream.assert_versions()


@pytest.mark.parametrize("package, found", [("graphifyy", "0.9.94"), ("mcp", "2.2.9")])
def test_mismatched_upstream_version_is_refused(monkeypatch, package, found):
    real = upstream.version
    monkeypatch.setattr(upstream, "version", lambda name: found if name == package else real(name))
    with pytest.raises(upstream.UpstreamVersionError) as raised:
        upstream.assert_versions()
    assert package in str(raised.value) and found in str(raised.value)


def test_missing_upstream_package_is_refused(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(upstream, "version", missing)
    with pytest.raises(upstream.UpstreamVersionError, match="not installed"):
        upstream.assert_versions()


def test_version_error_maps_to_cli_operation_error():
    from graphify_worktree.errors import exit_code
    assert exit_code(upstream.UpstreamVersionError("pins")) == 1


def _imported_roots(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_only_upstream_module_imports_graphify_or_mcp():
    offenders = {
        path.name for path in PACKAGE.glob("*.py")
        if path.name != "upstream.py" and {"graphify", "mcp"} & set(_imported_roots(path))
    }
    assert offenders == set()
