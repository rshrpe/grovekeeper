"""Hand a caller a fresh graph and refuse its result if anything moved meanwhile."""

from contextlib import contextmanager
from pathlib import Path

from graphify_worktree import artifacts
from graphify_worktree.errors import GraphError
from graphify_worktree.freshness import Generation, inspect
from graphify_worktree.policy import require
from graphify_worktree.publish import refresh


@contextmanager
def guarded_graph(root, *, once=False, session_mode=None, cancelled=None):
    """Ensure a fresh graph under the worktree lock, then validate the caller's read.

    Consent is checked at each boundary where it could have changed: before
    any artifact IO or lock creation, after waiting for the lock, before a
    rebuild is published (inside refresh), before the caller reads, and after
    the caller finishes. After the read, a full content re-inspection must
    show the same Generation, or the result is withheld (GraphError).

    session_mode may be a provider so an in-flight MCP session opt-out is seen.
    cancelled is an optional threading.Event that aborts at the next boundary.
    """
    def authorize():
        if cancelled is not None and cancelled.is_set():
            raise GraphError("request cancelled; result withheld")
        mode = session_mode() if callable(session_mode) else session_mode
        return require(root, capability="graph", once=once, session_mode=mode)

    root = Path(authorize()["worktree_root"])
    with artifacts.locked(root, create=True) as output:
        authorize()
        with artifacts.workspace(output) as scratch:
            status = inspect(root, output, scratch)
            rebuilt = status["state"] != "fresh"
            if rebuilt:
                status = refresh(root, output, scratch, None, authorize=authorize)
        expected = Generation.of(status)
        authorize()
        yield {**status, "rebuilt": rebuilt}
        with artifacts.workspace(output) as scratch:
            current = inspect(root, output, scratch)
        authorize()
        if current["state"] != "fresh" or Generation.of(current) != expected:
            raise GraphError("graph changed during query; result withheld: " + current["reason"])


def ensure(root, *, once=False, session_mode=None):
    """Fresh JSON-compatible status, rebuilding at most once."""
    with guarded_graph(root, once=once, session_mode=session_mode) as status:
        return status
