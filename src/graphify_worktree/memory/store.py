"""Markdown memory notes on disk: frontmatter format, validation and safe I/O."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
import re
import stat
import uuid

from graphify_worktree.errors import MemoryRequestError
from graphify_worktree.memory.schema import ACTORS, KINDS, STATUSES, note_id, text


ORIGIN = ("date", "created_at", "repository", "common_git_dir", "worktree", "branch",
          "commit", "source", "recorded_by", "pr", "confirmation")
AUDIT = ("action", "at", "recorded_by", "source", "confirmation", "worktree", "branch", "commit")
FIELDS = {"schema_version", "id", "kind", "title", "status", "superseded_by", *ORIGIN,
          *("audit_" + field for field in AUDIT)}


@contextmanager
def directory(path, create, writing):
    """Walk using directory descriptors so symlink swaps cannot redirect vault I/O."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                raise
            os.close(fd)
            fd = child
        # The stable Memory directory itself is the per-repository lock: no lock
        # files or directories need to be created for a read of absent memory.
        fcntl.flock(fd, fcntl.LOCK_EX if writing else fcntl.LOCK_SH)
        yield fd
    finally:
        os.close(fd)


def _scalar(raw):
    if not raw or raw == "~":
        return None
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Permit ordinary scalar edits in Obsidian; complex YAML fails explicitly.
        if (raw[0] in "\"'[{&*!|>@`" or " #" in raw or ": " in raw):
            raise MemoryRequestError("unsupported frontmatter value; use JSON-quoted scalars or flat JSON lists")
        return raw


def parse_frontmatter(document):
    if not document.startswith("---\n") or "\n---\n" not in document[3:]:
        raise MemoryRequestError("missing Markdown YAML frontmatter")
    header, content = document[4:].split("\n---\n", 1)
    metadata = {}
    key = None
    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            if key is None or metadata[key] is not None and not isinstance(metadata[key], list):
                raise MemoryRequestError("invalid frontmatter list")
            if metadata[key] is None:
                metadata[key] = []
            metadata[key].append(_scalar(line.lstrip()[2:].strip()))
            continue
        key, separator, raw = line.partition(":")
        if not separator or not re.fullmatch(r"[a-z_]+", key) or key in metadata:
            raise MemoryRequestError("invalid or duplicate frontmatter property")
        metadata[key] = _scalar(raw.strip())
    return metadata, content


def validate(metadata, content, expected_id, repo):
    if metadata.keys() != FIELDS:
        raise MemoryRequestError("unexpected or missing frontmatter properties")
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise MemoryRequestError("unsupported memory schema version")
    if metadata["id"] != expected_id:
        raise MemoryRequestError("note id does not match filename")
    if metadata["repository"] != repo["id"] or metadata["common_git_dir"] != repo["common_git_dir"]:
        raise MemoryRequestError("note belongs to another repository")
    for key in ("title", "date", "created_at", "worktree", "source", "confirmation"):
        text(metadata[key], key)
    if metadata["kind"] not in KINDS or metadata["status"] not in STATUSES or metadata["recorded_by"] not in ACTORS:
        raise MemoryRequestError("invalid note kind, status, or recording harness")
    for key in ("branch", "commit", "pr"):
        if metadata[key] is not None:
            text(metadata[key], key)
    try:
        created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
        if created.utcoffset() is None or created.utcoffset().total_seconds() != 0 or metadata["date"] != created.date().isoformat():
            raise MemoryRequestError()
    except (MemoryRequestError, ValueError) as error:
        raise MemoryRequestError("invalid UTC creation date") from error
    replacement = metadata["superseded_by"]
    if (metadata["status"] == "superseded") != (replacement is not None):
        raise MemoryRequestError("superseded status and replacement link disagree")
    if replacement is not None and note_id(replacement, "superseded_by") == expected_id:
        raise MemoryRequestError("a note cannot supersede itself")
    lengths = set()
    for key in AUDIT:
        values = metadata["audit_" + key]
        if not isinstance(values, list) or not values:
            raise MemoryRequestError("audit properties must be nonempty flat lists")
        lengths.add(len(values))
        for value in values:
            if value is not None or key not in ("branch", "commit"):
                text(value, "audit_" + key)
    if len(lengths) != 1 or metadata["audit_action"][0] != "save":
        raise MemoryRequestError("inconsistent audit history")
    if any(actor not in ACTORS for actor in metadata["audit_recorded_by"]):
        raise MemoryRequestError("invalid audit recording harness")
    if any(action not in ("save", "accept", "supersede") for action in metadata["audit_action"]):
        raise MemoryRequestError("invalid audit action")
    text(content, "content")


def load(fd, identifier, repo):
    filename = note_id(identifier) + ".md"
    try:
        note_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(note_fd, "r", encoding="utf-8", newline="") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise MemoryRequestError("note must be a regular file")
            metadata, content = parse_frontmatter(stream.read())
        validate(metadata, content, identifier, repo)
        return metadata, content
    except (OSError, UnicodeError, MemoryRequestError) as error:
        raise MemoryRequestError(f"cannot read memory note {filename}: {error}") from error


def write(fd, metadata, content):
    serialized = "---\n" + "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in metadata.items()) + "---\n" + content
    filename = metadata["id"] + ".md"
    temporary = "." + str(uuid.uuid4()) + ".tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=fd)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        # Never overwrite a symlink, even though replace itself would not follow it.
        try:
            current = os.stat(filename, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise MemoryRequestError("note destination must be a regular file, not a symlink")
        except FileNotFoundError:
            pass
        os.replace(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def append_event(metadata, action, args, repo, now):
    entry = {"action": action, "at": now, "recorded_by": args["recorded_by"],
             "source": args["source"], "confirmation": args["confirmation"],
             **{key: repo[key] for key in ("worktree", "branch", "commit")}}
    for key, value in entry.items():
        metadata.setdefault("audit_" + key, []).append(value)
