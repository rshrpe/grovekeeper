"""Expected, user-facing failures and the single table mapping them to exit codes.

Anything that is not a KnowledgeError or OSError is a bug: it is deliberately
left unmapped so it surfaces with a traceback instead of a quiet exit 1.
"""


class KnowledgeError(Exception):
    """An expected failure with a reason fit to show the user or agent."""


class PolicyError(KnowledgeError):
    """Knowledge use is not permitted (consent, session opt-out, invalid preference)."""

    def __init__(self, reason, policy=None, *, not_repository=False):
        super().__init__(reason)
        self.policy = policy
        self.not_repository = not_repository


class GitError(KnowledgeError):
    pass


class GraphError(KnowledgeError):
    """A graph could not be built, verified, published or exported."""


class ProvenanceError(GraphError):
    """Saved provenance is missing, malformed or belongs elsewhere."""


class MemoryRequestError(KnowledgeError):
    """Invalid memory input, lifecycle transition, note or storage."""


class DuplicateNoteError(MemoryRequestError):
    """A save was refused because current notes have nearly the same title."""

    def __init__(self, reason, duplicates):
        super().__init__(reason)
        self.duplicates = duplicates


class LearningRequestError(KnowledgeError):
    """Invalid work-memory (save-result/reflect) input."""


class HookError(KnowledgeError):
    pass


class SettingsError(KnowledgeError):
    pass


class UpstreamVersionError(KnowledgeError):
    pass


EXIT_CODES = (
    (PolicyError, 5),
    (KnowledgeError, 1),
    (OSError, 1),
)
INTERRUPTED = 130


def exit_code(error):
    """Exit code for an expected failure, or None for a programming error."""
    return next((code for kind, code in EXIT_CODES if isinstance(error, kind)), None)
