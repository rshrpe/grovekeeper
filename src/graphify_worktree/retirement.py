"""Retire the generated exports of removed linked worktrees, recoverably.

Every command previews unless --apply is given, and a preview creates no
state, lock or quarantine. Grovekeeper never fetches, prunes, deletes branches
or worktrees, or stops workers: the coordinator removes checkouts. This module
records a checkout and guards its local graph learning before removal
(prepare), proves afterwards that the recorded checkout is really gone, and
moves only unchanged manifest-owned generated files into a same-filesystem
quarantine, journal first, so an interrupted run resumes and every move can be
restored into absent paths. Lock order is graph (prepare only), lifecycle,
then export; a removed checkout's graph lock is never recreated. POSIX locks
are advisory, not a boundary against arbitrary same-user editors.
"""

from contextlib import ExitStack
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from graphify_worktree import artifacts, export_state, gitutil, incremental, upstream
from graphify_worktree.errors import GitError, GraphError, KnowledgeError, LifecycleError, PolicyError
# Descriptor-relative, no-follow primitives shared with export publication.
from graphify_worktree.export_state import (
    EXPORT_LOCK, _directory, _fingerprint, _host, _identity, _locked, _now, _prune_directories,
    _read_json, _write_json)


SCHEMA = 1
QUARANTINE = ".grovekeeper-quarantine"
DISPOSABLE = frozenset((*artifacts.OWNED, artifacts.LOCK, artifacts.EVENT))
LEARNING = ("memory", "reflections")
APPLICABLE = ("eligible", "interrupted")
STOP = ("conflict", "missing", "changed", "error")
SAMPLE = 50
SCOPE = "unchanged manifest-owned generated files"
ACTION = "recoverable quarantine"
COVERAGE = ("this guard covers graphify-out only; the coordinator still checks the whole checkout (dirty, untracked "
            "and needed ignored files, unsaved buffers, active workers, children) and rechecks after quiescing")
_progress = None  # the journal of a move in progress, reported if the command is interrupted


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _sample(items):
    return {"count": len(items), "items": items[:SAMPLE]}


def _denied(command, reason, **details):
    return PolicyError(reason, {"state": "denied", "command": f"retire {command}", **details})


def _session(command):
    """Session opt-out denies lifecycle commands too; repository knowledge mode is not required."""
    mode = os.environ.get("HARNESS_KNOWLEDGE_MODE", "default")
    if mode == "off":
        raise _denied(command, "knowledge is disabled for this session; lifecycle commands are denied")
    if mode != "default":
        raise _denied(command, "invalid HARNESS_KNOWLEDGE_MODE; use off or default")


def _state_path(*parts):
    return str(export_state.retirement_path().joinpath(*parts))


def _lstat(fd, name):
    if fd is None:
        return None
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _hash(fd, name, links=1):
    """(fingerprint, identity) of a regular file with exactly `links` links; never follows symlinks."""
    info = _lstat(fd, name)
    if info is None:
        return None, None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != links:
        raise GraphError(f"{name} must be a regular file with {links} link(s), not a symlink or special file")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(info):
            raise GraphError(f"{name} changed while opening")
        fingerprint = {"size": opened.st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
        after = os.fstat(stream.fileno())
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise GraphError(f"{name} changed while reading")
    return fingerprint, _identity(info)


def _exists_dir(fd, relative):
    """Whether a directory is still there; unreadable counts as present."""
    try:
        with _directory(relative, parent=fd) as directory:
            return directory is not None
    except (OSError, GraphError):
        return True


def _valid_entry(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return False
    path, fingerprint = PurePosixPath(entry["path"]), entry.get("fingerprint")
    return (bool(path.parts) and not path.is_absolute() and ".." not in path.parts and path.as_posix() == entry["path"]
            and isinstance(fingerprint, dict) and type(fingerprint.get("size")) is int and _hex(fingerprint.get("sha256")))


# Moving files: link then unlink, so bytes stay reachable and no path is ever replaced.

def _classify(source, target, relative, expected):
    """Where one journalled file is now: pending, linked, done, preserved, conflict or missing.

    source and target are directory descriptors, None when absent.
    """
    path = PurePosixPath(relative)
    try:
        with ExitStack() as stack:
            src = stack.enter_context(_directory(path.parent, parent=source)) if source is not None else None
            dst = stack.enter_context(_directory(path.parent, parent=target)) if target is not None else None
            here, there = _lstat(src, path.name), _lstat(dst, path.name)
            if here is not None and there is not None:
                if _identity(here) != _identity(there):
                    return "conflict"
                return "linked" if _hash(dst, path.name, links=2)[0] == expected else "conflict"
            if there is not None:
                return "done" if _hash(dst, path.name)[0] == expected else "conflict"
            if here is not None:
                return "pending" if _hash(src, path.name)[0] == expected else "preserved"
            return "missing"
    except (OSError, GraphError):
        return "conflict"


def _move(source, target, relative, expected):
    """Move one file from source to target unless it changed; returns its outcome.

    A rerun recognises every intermediate state; "changed" means the bytes
    changed during the move and are preserved at the target for recovery.
    """
    state = _classify(source, target, relative, expected)
    if state not in ("pending", "linked"):
        return state
    path = PurePosixPath(relative)
    with _directory(path.parent, parent=source) as src, _directory(path.parent, parent=target, create=True) as dst:
        if src is None:
            return "conflict"
        if state == "pending":
            fingerprint, identity = _hash(src, path.name)
            if fingerprint != expected:
                return "preserved"
            try:
                os.link(path.name, path.name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
            except FileExistsError:
                return "conflict"
            os.fsync(dst)
            linked = _lstat(dst, path.name)
            if linked is None or _identity(linked) != identity:
                return "conflict"
        else:
            identity = _identity(_lstat(dst, path.name))
        current = _lstat(src, path.name)
        if current is None or _identity(current) != identity:
            return "conflict"
        os.unlink(path.name, dir_fd=src)
        os.fsync(src)
        return "done" if _hash(dst, path.name)[0] == expected else "changed"


def _execute(entries, source_of, target):
    """Move journal entries in order, stopping at the first conflict, loss or error."""
    outcomes = []
    for entry in entries:
        outcome = {"path": entry["path"]}
        if "operation_uuid" in entry:
            outcome["operation_uuid"] = entry["operation_uuid"]
        try:
            outcome["outcome"] = _move(source_of(entry), target, entry["path"], entry["fingerprint"])
        except (OSError, GraphError) as error:
            outcome.update(outcome="error", error=str(error))
        outcomes.append(outcome)
        if outcome["outcome"] in STOP:
            break
    return outcomes


def _residuals(fd, files, directories):
    """Vault paths the export does not own (custom notes, .obsidian, foreign files), not followed."""
    owned, found = set(directories), []

    def walk(directory, prefix):
        for name in sorted(os.listdir(directory)):
            relative = prefix + name
            if not prefix and name in (artifacts.PROVENANCE, EXPORT_LOCK):
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and relative in owned:
                with _directory(name, parent=directory) as child:
                    if child is not None:
                        walk(child, relative + "/")
            elif relative not in files:
                found.append(relative + "/" if stat.S_ISDIR(info.st_mode) else relative)

    try:
        walk(fd, "")
    except (OSError, GraphError) as error:
        found.append(f"(listing stopped: {error})")
    return found


# State files.

def _write_journal(retirement, journal):
    operation = f"exports/{journal['export_uuid']}/operations/{journal['operation_uuid']}"
    with _directory(operation, parent=retirement, create=True) as directory:
        _write_json(directory, "journal.json", journal)


def _write_receipt(retirement, receipt):
    parts = ("exports", receipt["export_uuid"], "operations", receipt["operation_uuid"])
    name = f"receipt-{receipt['attempt_uuid']}.json"
    with _directory("/".join(parts), parent=retirement) as directory:
        _write_json(directory, name, receipt)
    return _state_path(*parts, name)


def _operations(retirement, identifier):
    """Journalled operations of one export, oldest first, each with its attempt receipts."""
    result = []
    with _directory(f"exports/{identifier}/operations", parent=retirement) as operations:
        if operations is None:
            return result
        for name in sorted(os.listdir(operations)):
            if not export_state.is_uuid(name):
                raise GraphError(f"unexpected entry in the operations of {identifier}: {name}")
            with _directory(name, parent=operations) as directory:
                journal = _read_json(directory, "journal.json")
                if journal is None:
                    continue  # interrupted before its journal existed, so nothing moved
                if (not isinstance(journal, dict) or journal.get("schema") != SCHEMA
                        or journal.get("operation_uuid") != name or journal.get("export_uuid") != identifier
                        or journal.get("kind") not in ("quarantine", "restore")
                        or not isinstance(journal.get("created_at"), str) or not isinstance(journal.get("entries"), list)
                        or not all(_valid_entry(entry) for entry in journal["entries"])):
                    raise GraphError(f"invalid operation journal {identifier}/{name}")
                receipts = [_read_json(directory, entry) for entry in sorted(os.listdir(directory))
                            if entry.startswith("receipt-") and entry.endswith(".json")]
                receipts = [receipt for receipt in receipts if isinstance(receipt, dict)]
                result.append({"journal": journal, "receipts": receipts,
                               "journal_sha256": _fingerprint(directory, "journal.json")["sha256"],
                               "complete": next((receipt for receipt in receipts if receipt.get("state") == "complete"), None)})
    return sorted(result, key=lambda operation: operation["journal"]["created_at"])


# C3: prepare a checkout and guard local graph learning before removal.

def _checkout(root):
    common, admin = gitutil.common_dir(root), gitutil.admin_dir(root)
    identities = {}
    for key, path in (("worktree_root", root), ("parent", root.parent), ("common_git_dir", common), ("git_admin_dir", admin)):
        with _directory(path) as fd:
            if fd is None:
                raise LifecycleError(f"checkout directory is missing: {path}")
            identities[key] = _identity(os.fstat(fd))
    return {"worktree_root": str(root), "common_git_dir": str(common), "git_admin_dir": str(admin),
            "linked": admin != common, "directory_identities": identities}


def _candidate_name(root, identity):
    """One prepared candidate per checkout incarnation, so a reused path never overwrites it."""
    return export_state.digest({"worktree_root": root, "identity": identity}) + ".json"


def _kind(parts, directory):
    """disposable (regenerable graph output), learning, or unknown local data."""
    first = parts[0]
    if first == incremental.SEEDS:
        if len(parts) == 1:
            return "disposable" if directory else "unknown"
        return "disposable" if incremental.TOKEN.fullmatch(parts[1]) and (directory or len(parts) > 2) else "unknown"
    if len(parts) == 1 and not directory and first in DISPOSABLE:
        return "disposable"
    if first in LEARNING or (len(parts) == 1 and first == upstream.LEARNING_SIDECAR):
        return "learning"
    return "unknown"


def _readable(fd, name, info):
    """Open a regenerable file without following links and read one byte: unreadable data still blocks."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        if _identity(os.fstat(descriptor)) != _identity(info):
            raise GraphError("changed while being inventoried")
        os.read(descriptor, 1)
    finally:
        os.close(descriptor)


def _local_inventory(root):
    """(entries, unsafe, disposable count) for every graphify-out descendant, by path/type/size/hash only."""
    entries, unsafe, disposable = [], [], 0

    def walk(fd, parts):
        nonlocal disposable
        for name in sorted(os.listdir(fd)):
            path = (*parts, name)
            relative = "/".join(path)
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    kind = _kind(path, True)
                    if kind != "disposable":
                        entries.append({"path": relative, "type": "directory", "class": kind})
                    with _directory(name, parent=fd) as child:
                        if child is None:
                            raise GraphError("disappeared while being inventoried")
                        walk(child, path)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    unsafe.append(f"{relative}: symlink, hard link or special file")
                elif (kind := _kind(path, False)) == "disposable":
                    _readable(fd, name, info)
                    disposable += 1
                else:
                    fingerprint, _ = _hash(fd, name)
                    if fingerprint is None:
                        raise GraphError("disappeared while being inventoried")
                    entries.append({"path": relative, "type": "file", "class": kind, **fingerprint})
            except (OSError, GraphError) as error:
                unsafe.append(f"{relative}: {error}")

    with _directory(root) as root_fd:
        if root_fd is None:
            raise LifecycleError(f"worktree is missing: {root}")
        info = _lstat(root_fd, "graphify-out")
        if info is not None and not stat.S_ISDIR(info.st_mode):
            unsafe.append("graphify-out: symlink, hard link or special file")
        elif info is not None:
            try:
                with _directory("graphify-out", parent=root_fd) as output:
                    walk(output, ())
            except (OSError, GraphError) as error:
                unsafe.append(f"graphify-out: {error}")
    return entries, unsafe, disposable


def _approval(path):
    location = Path(os.path.abspath(path))
    try:
        with _directory(location.parent) as directory:
            if directory is None:
                raise FileNotFoundError(path)
            data = _read_json(directory, location.name)
    except (OSError, GraphError) as error:
        raise LifecycleError(f"cannot read local-data approval {path}: {error}") from error
    if (not isinstance(data, dict) or not _hex(data.get("inventory_sha256"))
            or data.get("decision") not in ("discard", "retained")
            or not isinstance(data.get("approval_reference"), str) or not data["approval_reference"].strip()
            or (data["decision"] == "retained") != isinstance(data.get("retained_at"), str)):
        raise LifecycleError("local-data approval needs inventory_sha256, decision (discard or retained), "
                             "approval_reference and, only for retained, retained_at")
    return data


def _retained(root, target, entries):
    """Inventory paths whose retained copy is missing or different (empty when all match)."""
    if not target.is_absolute() or target == root or root in target.parents:
        return [f"{target} must be an absolute path outside the worktree"]
    problems = []
    try:
        with _directory(target) as fd:
            if fd is None:
                return [f"{target} is missing"]
            for entry in entries:
                try:
                    if entry["type"] == "directory":
                        matches = _exists_dir(fd, entry["path"])
                    else:
                        matches = _fingerprint(fd, entry["path"]) == {"size": entry["size"], "sha256": entry["sha256"]}
                except (OSError, GraphError):
                    matches = False
                if not matches:
                    problems.append(entry["path"])
    except (OSError, GraphError) as error:
        return [f"{target}: {error}"]
    return problems


def _guard(root, entries, unsafe, approval):
    guard = {"inventory_sha256": export_state.digest(entries), "unsafe": unsafe,
             "learning": sum(entry["class"] == "learning" for entry in entries),
             "unknown": sum(entry["class"] == "unknown" for entry in entries)}
    if unsafe:
        return {**guard, "state": "blocked", "reason": "unsafe or unreadable local data must be resolved first"}
    if not entries:
        return {**guard, "state": "clear", "reason": "graphify-out holds only regenerable output"}
    if approval is None:
        return {**guard, "state": "blocked", "reason": "local learning or unowned data needs verified retention outside "
                "the worktree or loss approval bound to inventory_sha256 (--local-data-approval)"}
    if approval["inventory_sha256"] != guard["inventory_sha256"]:
        return {**guard, "state": "blocked", "reason": "the approval names a different inventory; new or changed local data invalidates it"}
    approved = {"decision": approval["decision"], "approval_reference": approval["approval_reference"]}
    if approval["decision"] == "discard":
        return {**guard, "state": "loss_approved", "approval": approved, "reason": "loss of exactly this inventory was approved"}
    approved["retained_at"] = approval["retained_at"]
    problems = _retained(root, Path(approval["retained_at"]), entries)
    if problems:
        return {**guard, "state": "blocked", "approval": approved,
                "reason": "retained copy is missing or differs: " + "; ".join(problems[:SAMPLE])}
    return {**guard, "state": "retained", "approval": approved, "reason": "every inventoried entry is retained outside the worktree"}


def _prepare(root, approval, retirement, *, write):
    checkout = _checkout(root)
    entries, unsafe, disposable = _local_inventory(root)
    guard = _guard(root, entries, unsafe, approval)
    blockers = [] if checkout["linked"] else ["main_worktree: the main worktree is never a retirement candidate"]
    exports, unreadable = [], 0
    for identifier in export_state.export_ids(retirement) if retirement is not None else ():
        try:
            record, _ = export_state.read_record(retirement, identifier)
        except (OSError, GraphError):
            unreadable += 1
            continue
        if (record is not None and record["worktree_root"] == checkout["worktree_root"]
                and record["directory_identities"]["worktree_root"] == checkout["directory_identities"]["worktree_root"]):
            exports.append({"export_uuid": identifier, "vault_path": record["vault_path"], "state": record.get("state")})
    ready = not blockers and guard["state"] in ("clear", "loss_approved", "retained")
    name = _candidate_name(checkout["worktree_root"], checkout["directory_identities"]["worktree_root"])
    if write:
        candidate = {"schema": SCHEMA, **checkout, "host": _host(), "prepared_at": _now(), "ready": ready,
                     "guard": guard, "inventory": entries, "export_uuids": [entry["export_uuid"] for entry in exports]}
        with _directory("candidates", parent=retirement, create=True) as candidates:
            _write_json(candidates, name, candidate)
    report = {"state": "prepared" if write else "preview", "ready": ready, **checkout, "blockers": blockers,
              "guard": {**guard, "disposable_files": disposable, "inventory": _sample(entries)},
              "exports": exports, "candidate": _state_path("candidates", name) if write else None, "coverage": COVERAGE}
    if unreadable:
        report["unreadable_export_records"] = unreadable
    return report


def prepare(path, *, apply=False, approval=None):
    """Record the checkout and its local-data guard; never removes or rebuilds anything.

    Applied, exits nonzero while the guard or a blocker stands, so a pre-removal hook stops removal.
    """
    _session("prepare")
    approved = _approval(approval) if approval is not None else None
    root = gitutil.resolve_root(path)
    if not apply:
        with export_state.retirement_directory() as retirement:
            return _prepare(root, approved, retirement, write=False), 0
    try:
        with artifacts.locked(root), export_state.lifecycle() as retirement:
            report = _prepare(root, approved, retirement, write=True)
    except GraphError as error:
        raise LifecycleError(f"preparation blocked: {error}", {"state": "blocked", "worktree_root": str(root)}) from error
    return report, 0 if report["ready"] else 1


# C4: prove that the recorded checkout is retired, not merely absent.

def _read_candidate(retirement, record):
    identities = record["directory_identities"]
    name = _candidate_name(record["worktree_root"], identities["worktree_root"])
    with _directory("candidates", parent=retirement) as candidates:
        candidate = None if candidates is None else _read_json(candidates, name)
    if candidate is None:
        if isinstance(identities.get("parent"), dict):
            return {"directory_identities": identities, "local_data_uninventoried": True}, None
        return None, ("parent_identity_unrecorded: neither export nor `retire prepare --apply` recorded this checkout's original "
                      "parent, so its absence cannot prove retirement")
    saved = candidate.get("directory_identities") if isinstance(candidate, dict) else None
    if (not isinstance(saved, dict) or not isinstance(saved.get("parent"), dict)
            or any(saved.get(key) != identities[key] for key in ("worktree_root", "common_git_dir", "git_admin_dir"))
            or any(candidate.get(key) != record[key] for key in ("worktree_root", "common_git_dir", "git_admin_dir"))):
        return None, "candidate_mismatch: the prepared candidate describes a different checkout incarnation"
    return candidate, None


def _proof(retirement, record, report):
    """(reasons the checkout is not provably retired, whether it is still active)."""
    identities = record["directory_identities"]
    root, common, admin = (Path(record[key]) for key in ("worktree_root", "common_git_dir", "git_admin_dir"))
    host = _host()
    if not host["machine_id"] or record["host"] != host:
        return ["foreign_host: recorded on another host, or this host has no machine-id"], False
    if admin == common:
        return ["main_worktree: the main worktree is never a retirement candidate"], False
    if not root.is_absolute() or root.parent == root or admin.parent != common / "worktrees":
        return ["unexpected_layout: the recorded directories are not a linked worktree of this clone"], False
    try:
        with _directory(root.parent) as parent:
            if parent is None:
                return [f"parent_unavailable: {root.parent} is missing or unmounted"], False
            present, parent_identity = _lstat(parent, root.name), _identity(os.fstat(parent))
    except (OSError, GraphError) as error:
        return [f"parent_unavailable: {error}"], False
    if present is not None:
        if _identity(present) == identities["worktree_root"]:
            return ["worktree_present: the checkout still exists"], True
        return ["path_reused: a different entry now occupies the worktree path"], False
    reasons = []
    candidate, problem = _read_candidate(retirement, record)
    if problem:
        reasons.append(problem)
    else:
        if candidate.get("local_data_uninventoried"):
            report["warnings"] = ["local graphify-out learning was not inventoried: no `retire prepare --apply` candidate exists"]
        elif not candidate.get("ready"):
            report["warnings"] = ["local graphify-out learning protection was not approved: "
                                  "the worktree was removed despite a blocked preparation guard"]
        if candidate["directory_identities"]["parent"] != parent_identity:
            reasons.append("parent_changed: the worktree's parent is not the recorded directory")
    try:
        with _directory(common) as fd:
            if fd is None:
                return reasons + [f"common_git_dir_unavailable: {common}"], False
            if _identity(os.fstat(fd)) != identities["common_git_dir"]:
                return reasons + [f"common_git_dir_changed: {common} is a different directory now"], False
            with _directory("worktrees", parent=fd) as worktrees:
                if _lstat(worktrees, admin.name) is not None:
                    reasons.append("git_admin_dir_present: Git still holds the worktree's administrative directory")
    except (OSError, GraphError) as error:
        return reasons + [f"common_git_dir_unavailable: {error}"], False
    try:
        if any(os.path.normpath(path) == str(root) for path in gitutil.registered_worktrees(common)):
            reasons.append("git_registration_present: Git still registers the worktree (removal incomplete or forget-only)")
    except (GitError, OSError) as error:
        reasons.append(f"git_unavailable: {error}")
    return reasons, False


# C5: plan, quarantine and resume.

def _fresh_plan(identifier, record, vault_fd):
    marker = _read_json(vault_fd, artifacts.PROVENANCE)
    manifest = marker.get("export_manifest") if isinstance(marker, dict) else None
    if not (isinstance(manifest, dict) and marker.get("worktree_root") == record["worktree_root"]
            and manifest.get("export_uuid") == identifier and manifest.get("generation") == record["generation"]
            and manifest.get("files") == record["files"] and manifest.get("directories") == record["directories"]):
        return None, ["marker_mismatch: the vault marker does not carry this export's recorded manifest"]
    moves, preserved, absent = [], [], []
    for relative, expected in sorted(record["files"].items()):
        try:
            current = _fingerprint(vault_fd, relative)
        except (OSError, GraphError) as error:
            preserved.append({"path": relative, "reason": f"unsafe: {error}"})
            continue
        if current is None:
            absent.append(relative)
        elif current == expected:
            moves.append({"path": relative, "fingerprint": expected})
        else:
            preserved.append({"path": relative, "reason": "modified"})
    if absent:
        # Owned bytes vanished outside any journalled move: unexplained loss is never retired as success.
        return None, [f"missing_owned_file: {relative}" for relative in absent[:SAMPLE]]
    marker_entry = {"path": artifacts.PROVENANCE, "fingerprint": _fingerprint(vault_fd, artifacts.PROVENANCE), "marker": True}
    return {"entries": [*moves, marker_entry], "preserved": preserved, "absent": absent}, []


def _resume_plan(operation, vault_fd, quarantine_root, held):
    journal = operation["journal"]
    if journal.get("quarantine_path") != str(quarantine_root / journal["operation_uuid"]):
        return None, ["journal_invalid: the journal's quarantine path is not this export's sibling quarantine"]
    quarantine = held.enter_context(_directory(quarantine_root / journal["operation_uuid"]))
    states = [[entry["path"], _classify(vault_fd, quarantine, entry["path"], entry["fingerprint"])]
              for entry in journal["entries"]]
    problems = [f"{state}: {path}" for path, state in states if state in ("conflict", "missing")]
    preserved = [{"path": path, "reason": "modified after the journal"} for path, state in states if state == "preserved"]
    return {"journal": journal, "entries": journal["entries"], "states": states, "preserved": preserved, "absent": []}, problems


def _assess(retirement, identifier, stack=None):
    """(report, context) for one recorded export; context is None without a readable record.

    With stack (apply, lifecycle lock held) the export lock is taken and the
    descriptors stay open for _quarantine; otherwise nothing is created or locked.
    """
    report = {"export_uuid": identifier}
    try:
        record, record_hash = export_state.read_record(retirement, identifier)
    except (OSError, KnowledgeError) as error:
        return {**report, "state": "invalid", "reasons": [str(error)]}, None
    if record is None:
        return {**report, "state": "unknown", "reasons": ["no export record with this UUID"]}, None
    report.update(worktree_root=record["worktree_root"], vault_path=record["vault_path"], generation=record["generation"])
    context = {"record": record, "restored": False}
    try:
        return _assess_record(retirement, identifier, record, record_hash, report, context, stack), context
    except (OSError, KnowledgeError) as error:
        return {**report, "state": "blocked", "reasons": [f"error: {error}"]}, context


def _assess_record(retirement, identifier, record, record_hash, report, context, stack):
    operations = _operations(retirement, identifier)
    latest = operations[-1] if operations else None
    kind = latest["journal"]["kind"] if latest else None
    context["restored"] = restored = kind == "restore" and latest["complete"] is not None
    if kind == "quarantine" and latest["complete"]:
        receipt = latest["complete"]
        return {**report, "state": "retired", "operation_uuid": latest["journal"]["operation_uuid"],
                "completed_at": receipt.get("completed_at"), "vault_removed": receipt.get("vault_removed"),
                "residuals": receipt.get("residuals")}
    if kind == "restore" and not latest["complete"]:
        return {**report, "state": "blocked", "reasons": [f"restore_incomplete: finish it with `retire restore --candidate {identifier}`"]}
    resume = latest if kind == "quarantine" else None
    if record.get("state") != "complete":
        return {**report, "state": "blocked", "reasons": ["publication_incomplete: an interrupted export is never retired"]}
    reasons, active = _proof(retirement, record, report)
    if reasons:
        return {**report, "state": "active" if active else "blocked", "reasons": reasons}
    vault = Path(record["vault_path"])
    quarantine_root = vault.parent / QUARANTINE / identifier
    expected = latest["complete"].get("vault_identity") if restored else record["directory_identities"]["vault_path"]
    with ExitStack() as held:
        vault_fd = held.enter_context(_directory(vault))
        if vault_fd is not None and _identity(os.fstat(vault_fd)) != expected:
            return {**report, "state": "blocked", "reasons": ["vault_identity_changed: the vault path holds a different directory"]}
        lock = held.enter_context(_locked(vault_fd, EXPORT_LOCK)) if stack is not None and vault_fd is not None else None
        if resume is not None:
            plan, problems = _resume_plan(resume, vault_fd, quarantine_root, held)
        elif vault_fd is None:
            return {**report, "state": "blocked", "reasons": ["vault_missing: the recorded vault is gone without a quarantine receipt"]}
        else:
            plan, problems = _fresh_plan(identifier, record, vault_fd)
        if problems:
            return {**report, "state": "blocked", "reasons": problems}
        if resume is not None:
            core = {"kind": "resume", "export_uuid": identifier, "operation_uuid": resume["journal"]["operation_uuid"],
                    "journal_sha256": resume["journal_sha256"], "record_sha256": record_hash["sha256"],
                    "vault_present": vault_fd is not None, "states": plan["states"]}
            moves = sum(state in ("pending", "linked") for _, state in plan["states"])
            destination = str(quarantine_root / resume["journal"]["operation_uuid"])
        else:
            core = {"kind": "quarantine", "export_uuid": identifier, "record_sha256": record_hash["sha256"],
                    "generation": record["generation"], "vault_path": str(vault), "vault_identity": expected,
                    "quarantine_root": str(quarantine_root), "entries": plan["entries"],
                    "preserved": [item["path"] for item in plan["preserved"]], "absent": plan["absent"],
                    "directories": record["directories"], "restored": restored}
            moves = len(plan["entries"]) - 1
            destination = str(quarantine_root / "<new operation UUID>")
        if stack is None:
            _, again = export_state.read_record(retirement, identifier)
            marker = plan["entries"][-1]["fingerprint"] if resume is None else None
            if again != record_hash or (resume is None and _fingerprint(vault_fd, artifacts.PROVENANCE) != marker):
                return {**report, "state": "blocked", "reasons": ["snapshot_changed: the export changed during the preview; preview again"]}
        plan_sha256 = export_state.digest(core)
        report.update(
            state="interrupted" if resume is not None else "eligible", plan_sha256=plan_sha256,
            required_confirmation=f"retire export {identifier} plan {plan_sha256[:12]}",
            quarantine_path=destination, moves=moves, preserved=_sample(plan["preserved"]), absent=_sample(plan["absent"]),
            residuals=_sample(_residuals(vault_fd, record["files"], record["directories"]) if vault_fd is not None else []),
            authorization="--confirmation only: restored explicitly" if restored else "a matching --grant or --confirmation")
        context.update(plan, vault_fd=vault_fd, lock=lock, quarantine_root=quarantine_root, journal=plan.get("journal"))
        if stack is not None:
            stack.enter_context(held.pop_all())
        return report


def _grant_core(grant):
    return {key: grant.get(key) for key in ("kind", "host", "repository", "export_roots", "scope", "action")}


def _current_identity(path):
    try:
        with _directory(Path(path)) as fd:
            return None if fd is None else _identity(os.fstat(fd))
    except (OSError, GraphError):
        return None


def _grant_reasons(grant, record):
    """Why a grant does not cover a recorded export (empty when it does)."""
    if export_state.digest(_grant_core(grant)) != grant.get("plan_sha256"):
        return ["grant_invalid"]
    reasons = []
    try:
        if grant.get("revoked_at"):
            reasons.append("grant_revoked")
        if grant["host"] != _host():
            reasons.append("grant_foreign_host")
        repository = grant["repository"]
        if (repository.get("common_git_dir") != record["common_git_dir"]
                or repository.get("identity") != record["directory_identities"]["common_git_dir"]):
            reasons.append("grant_other_repository")
        vault = PurePosixPath(record["vault_path"])
        roots = [root for root in grant["export_roots"] if PurePosixPath(root["path"]) in vault.parents]
        if not roots:
            reasons.append("grant_root_mismatch")
        elif not any(_current_identity(root["path"]) == root["identity"] for root in roots):
            reasons.append("grant_root_changed")
    except (KeyError, TypeError, AttributeError):
        return ["grant_invalid"]
    return reasons


def _grants(retirement):
    grants = []
    for identifier in export_state.grant_ids(retirement):
        try:
            grant = export_state.read_grant(retirement, identifier)
        except (OSError, GraphError):
            continue
        if grant is not None:
            grants.append(grant)
    return grants


def _authorize(retirement, report, context, grant, confirmation):
    identifier = report["export_uuid"]
    if grant is not None:
        if context["restored"]:
            raise _denied("reconcile", "this export was restored explicitly; only --confirmation may quarantine it again",
                          export_uuid=identifier, grant_uuid=grant)
        stored = export_state.read_grant(retirement, grant)
        if stored is None:
            raise _denied("reconcile", f"grant not found: {grant}", export_uuid=identifier, grant_uuid=grant)
        reasons = _grant_reasons(stored, context["record"])
        if reasons:
            raise _denied("reconcile", "grant does not authorize this export: " + ", ".join(reasons),
                          export_uuid=identifier, grant_uuid=grant)
        return {"source": "grant", "grant_uuid": grant, "grant_sha256": export_state.grant_digest(stored),
                "approval_reference": stored.get("approval_reference")}
    if confirmation != report["required_confirmation"]:
        raise _denied("reconcile", "confirmation does not match this plan", export_uuid=identifier,
                      required_confirmation=report["required_confirmation"])
    return {"source": "confirmation", "approval_reference": confirmation}


def _quarantine(retirement, report, context, authorization):
    global _progress
    identifier, vault, vault_fd = report["export_uuid"], Path(report["vault_path"]), context["vault_fd"]
    journal = context["journal"]
    if journal is None:
        operation = str(uuid.uuid4())
        journal = {"schema": SCHEMA, "kind": "quarantine", "operation_uuid": operation, "export_uuid": identifier,
                   "created_at": _now(), "plan_sha256": report["plan_sha256"], "authorization": authorization,
                   "vault_path": str(vault), "quarantine_path": str(context["quarantine_root"] / operation),
                   "entries": context["entries"]}
        _write_journal(retirement, journal)
    operation = journal["operation_uuid"]
    _progress = {"kind": "quarantine", "export_uuid": identifier, "operation_uuid": operation,
                 "journal": _state_path("exports", identifier, "operations", operation, "journal.json"),
                 "quarantine_path": journal["quarantine_path"]}
    started, removed, vault_removed, residuals = _now(), [], False, []
    try:
        with ExitStack() as stack:
            parent = stack.enter_context(_directory(vault.parent))
            if parent is None:
                raise GraphError(f"vault parent disappeared: {vault.parent}")
            if vault_fd is not None:
                current = _lstat(parent, vault.name)
                if current is None or _identity(current) != _identity(os.fstat(vault_fd)):
                    raise GraphError("the vault moved during retirement")
            quarantine = stack.enter_context(_directory(PurePosixPath(QUARANTINE, identifier, operation), parent=parent, create=True))
            if vault_fd is None:
                outcomes = [{"path": path, "outcome": state} for path, state in context["states"]]
            elif os.fstat(quarantine).st_dev != os.fstat(vault_fd).st_dev:
                raise GraphError("quarantine would cross filesystems; copy-and-delete is never used")
            else:
                outcomes = _execute(journal["entries"], lambda entry: vault_fd, quarantine)
            stopped = any(outcome["outcome"] in STOP for outcome in outcomes)
            if not stopped and vault_fd is not None:
                lock, current = context["lock"], _lstat(vault_fd, EXPORT_LOCK)
                if lock is not None and current is not None and _identity(current) == _identity(lock):
                    os.unlink(EXPORT_LOCK, dir_fd=vault_fd)
                    os.fsync(vault_fd)
                directories = context["record"]["directories"]
                _prune_directories(vault_fd, directories)
                removed = [relative for relative in directories if not _exists_dir(vault_fd, relative)]
                current = _lstat(parent, vault.name)
                if current is not None and _identity(current) == _identity(os.fstat(vault_fd)):
                    try:
                        os.rmdir(vault.name, dir_fd=parent)
                        os.fsync(parent)
                        vault_removed = True
                    except OSError as error:
                        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                            raise
                if not vault_removed:
                    residuals = _residuals(vault_fd, context["record"]["files"], directories)
    except (OSError, GraphError) as error:
        raise LifecycleError(f"quarantine stopped: {error}; the journal keeps it recoverable",
                             {"state": "interrupted", **_progress}) from error
    receipt = {"schema": SCHEMA, "attempt_uuid": str(uuid.uuid4()), "kind": "quarantine", "operation_uuid": operation,
               "export_uuid": identifier, "plan_sha256": report["plan_sha256"], "authorization": authorization,
               "started_at": started, "completed_at": _now(), "state": "partial" if stopped else "complete",
               "outcomes": outcomes, "directories_removed": removed, "vault_removed": vault_removed, "residuals": residuals}
    path = _write_receipt(retirement, receipt)
    _progress = None
    preserved = context["preserved"] + [{"path": outcome["path"], "reason": "modified during the move"}
                                        for outcome in outcomes if outcome["outcome"] == "preserved"]
    result = {"state": "partial" if stopped else "quarantined", "export_uuid": identifier, "operation_uuid": operation,
              "receipt": path, "quarantine_path": journal["quarantine_path"], "authorization": authorization,
              "moved": sum(outcome["outcome"] == "done" for outcome in outcomes),
              "conflicts": [outcome for outcome in outcomes if outcome["outcome"] in STOP],
              "preserved": _sample(preserved), "directories_removed": len(removed), "vault_removed": vault_removed,
              "residuals": _sample(residuals)}
    if "warnings" in report:
        result["warnings"] = report["warnings"]
    return result, 1 if stopped else 0


def _summary(reports):
    counts = {}
    for report in reports:
        counts[report["state"]] = counts.get(report["state"], 0) + 1
    return {"state": "preview", "candidates": reports, "counts": counts}


def reconcile(candidate=None, *, grant=None, apply=False, digest=None, confirmation=None):
    """Discover readiness from records and actual Git/filesystem state; apply one plan at a time."""
    _session("reconcile")
    if apply:
        with ExitStack() as stack:
            retirement = stack.enter_context(export_state.lifecycle())
            report, context = _assess(retirement, candidate, stack)
            if report["state"] == "retired":
                return {**report, "applied": False}, 0
            if report["state"] not in APPLICABLE:
                return {**report, "applied": False}, 1
            if digest != report["plan_sha256"]:
                return {**report, "state": "blocked", "applied": False,
                        "reasons": ["plan_changed: the current plan differs from --plan-sha256; preview again"]}, 1
            return _quarantine(retirement, report, context, _authorize(retirement, report, context, grant, confirmation))
    with export_state.retirement_directory() as retirement:
        if retirement is None:
            reports = [] if candidate is None else [{"export_uuid": candidate, "state": "unknown",
                                                     "reasons": ["no lifecycle state on this host"]}]
            return _summary(reports), 0
        scope = export_state.read_grant(retirement, grant) if grant is not None else None
        if grant is not None and scope is None:
            raise LifecycleError(f"grant not found: {grant}")
        grants = _grants(retirement) if scope is None else []
        reports = []
        for identifier in [candidate] if candidate is not None else export_state.export_ids(retirement):
            report, context = _assess(retirement, identifier)
            record = context["record"] if context else None
            if scope is not None and record is not None:
                reasons = _grant_reasons(scope, record)
                if candidate is None and {"grant_other_repository", "grant_root_mismatch"} & set(reasons):
                    continue
                if context["restored"]:
                    reasons.append("restored_explicitly")
                report["grant"] = {"grant_uuid": grant, "authorized": not reasons, "reasons": reasons}
            elif record is not None and report["state"] in APPLICABLE:
                report["authorized_by"] = [] if context["restored"] else [
                    stored["grant_uuid"] for stored in grants if not _grant_reasons(stored, record)]
            reports.append(report)
    return _summary(reports), 0


# Restore: quarantined bytes back into absent paths only.

def _restore_plan(retirement, identifier, stack=None):
    report = {"export_uuid": identifier}
    record, _ = export_state.read_record(retirement, identifier)
    if record is None:
        return {**report, "state": "unknown", "reasons": ["no export record with this UUID"]}, None
    vault = Path(record["vault_path"])
    report.update(worktree_root=record["worktree_root"], vault_path=str(vault))
    operations = _operations(retirement, identifier)
    latest = operations[-1] if operations else None
    resume = latest if latest and latest["journal"]["kind"] == "restore" and not latest["complete"] else None
    quarantines = [operation for operation in operations if operation["journal"]["kind"] == "quarantine"]
    restores = [operation for operation in operations if operation["journal"]["kind"] == "restore"]
    restored = {(outcome.get("operation_uuid"), outcome.get("path")) for operation in restores
                for receipt in operation["receipts"] for outcome in receipt.get("outcomes", [])
                if isinstance(outcome, dict) and outcome.get("outcome") == "done"}
    known = [record["directory_identities"]["vault_path"]] + [
        receipt.get("vault_identity") for operation in restores for receipt in operation["receipts"]]
    if resume is not None:
        known.append(resume["journal"].get("vault_identity"))
    with ExitStack() as held:
        parent = held.enter_context(_directory(vault.parent))
        if parent is None:
            return {**report, "state": "blocked", "reasons": [f"vault_parent_unavailable: {vault.parent}"]}, None
        present, vault_fd, lock = _lstat(parent, vault.name), None, None
        if present is not None:
            if not stat.S_ISDIR(present.st_mode):
                return {**report, "state": "blocked", "reasons": ["vault_path_reused: a non-directory occupies the vault path"]}, None
            vault_fd = held.enter_context(_directory(vault.name, parent=parent))
            if _identity(present) not in known and os.listdir(vault_fd):
                return {**report, "state": "blocked", "reasons": ["vault_path_reused: a different, non-empty directory occupies the vault path"]}, None
            marker = _read_json(vault_fd, artifacts.PROVENANCE)
            manifest = marker.get("export_manifest") if isinstance(marker, dict) else None
            if marker is not None and (not isinstance(manifest, dict) or manifest.get("export_uuid") != identifier):
                return {**report, "state": "blocked", "reasons": ["vault_path_reused: the vault marker names another export"]}, None
            if stack is not None:
                lock = held.enter_context(_locked(vault_fd, EXPORT_LOCK))
        sources = {}
        for operation in quarantines:
            journal = operation["journal"]
            if journal.get("quarantine_path") != str(vault.parent / QUARANTINE / identifier / journal["operation_uuid"]):
                return {**report, "state": "blocked", "reasons": [f"journal_invalid: {journal['operation_uuid']}"]}, None
            sources[journal["operation_uuid"]] = held.enter_context(
                _directory(PurePosixPath(QUARANTINE, identifier, journal["operation_uuid"]), parent=parent))
        if resume is not None:
            candidates = resume["journal"]["entries"]
        else:
            candidates = [{**entry, "operation_uuid": operation["journal"]["operation_uuid"]} for operation in quarantines
                          for entry in operation["journal"]["entries"]
                          if (operation["journal"]["operation_uuid"], entry["path"]) not in restored]
        restorable, preserved, missing = [], [], []
        for entry in candidates:
            source = sources.get(entry.get("operation_uuid"))
            path, quarantined = PurePosixPath(entry["path"]), False
            if source is not None:
                try:
                    with _directory(path.parent, parent=source) as directory:
                        quarantined = _lstat(directory, path.name) is not None
                except (OSError, GraphError):
                    quarantined = True  # unreadable: classification reports the conflict
            if not quarantined:
                if _classify(None, vault_fd, entry["path"], entry["fingerprint"]) == "missing":
                    missing.append(entry["path"])
                continue
            state = _classify(source, vault_fd, entry["path"], entry["fingerprint"])
            if state in ("pending", "linked"):
                restorable.append(entry)
            else:
                preserved.append({"path": entry["path"], "operation_uuid": entry.get("operation_uuid"),
                                  "reason": "quarantine_changed" if state == "preserved" else "destination_exists"})
        restorable.sort(key=lambda entry: bool(entry.get("marker")))
        directories = [relative for relative in record["directories"] if vault_fd is None or not _exists_dir(vault_fd, relative)]
        report.update(preserved=_sample(preserved), missing=_sample(missing))
        incomplete = [f"destination_exists_or_changed: {item['path']}" for item in preserved[:SAMPLE]]
        incomplete += [f"missing: {path}" for path in missing[:SAMPLE]]
        if not restorable and resume is None:
            if incomplete:
                return {**report, "state": "blocked", "reasons": incomplete}, None
            return {**report, "state": "nothing_to_restore"}, None
        report["incomplete"] = incomplete
        core = {"kind": "restore", "export_uuid": identifier, "resume": resume["journal"]["operation_uuid"] if resume else None,
                "vault": _identity(present) if present is not None else None, "directories": directories,
                "entries": [[entry["operation_uuid"], entry["path"], entry["fingerprint"]] for entry in restorable]}
        plan_sha256 = export_state.digest(core)
        report.update(state="restorable", plan_sha256=plan_sha256, files=len(restorable), directories=directories,
                      required_confirmation=f"restore export {identifier} plan {plan_sha256[:12]}",
                      note="restores into absent paths only; never overwrites or recreates a Git worktree")
        context = {"parent": parent, "vault_fd": vault_fd, "lock": lock, "sources": sources, "entries": restorable,
                   "directories": directories, "journal": resume["journal"] if resume else None}
        if stack is not None:
            stack.enter_context(held.pop_all())
        return report, context


def _restore(retirement, report, context, authorization):
    global _progress
    identifier, vault = report["export_uuid"], Path(report["vault_path"])
    parent, vault_fd = context["parent"], context["vault_fd"]
    journal = context["journal"]
    if journal is None:
        journal = {"schema": SCHEMA, "kind": "restore", "operation_uuid": str(uuid.uuid4()), "export_uuid": identifier,
                   "created_at": _now(), "plan_sha256": report["plan_sha256"], "authorization": authorization,
                   "vault_path": str(vault), "vault_identity": None if vault_fd is None else _identity(os.fstat(vault_fd)),
                   "entries": context["entries"]}
        _write_journal(retirement, journal)
    operation = journal["operation_uuid"]
    _progress = {"kind": "restore", "export_uuid": identifier, "operation_uuid": operation,
                 "journal": _state_path("exports", identifier, "operations", operation, "journal.json")}
    started = _now()
    try:
        with ExitStack() as stack:
            if vault_fd is None:
                os.mkdir(vault.name, mode=0o700, dir_fd=parent)
                os.fsync(parent)
                vault_fd = stack.enter_context(_directory(vault.name, parent=parent))
                journal["vault_identity"] = _identity(os.fstat(vault_fd))
                _write_journal(retirement, journal)
                stack.enter_context(_locked(vault_fd, EXPORT_LOCK))
            for relative in sorted(context["directories"]):
                with _directory(relative, parent=vault_fd, create=True):
                    pass
            outcomes = _execute(journal["entries"], lambda entry: context["sources"].get(entry["operation_uuid"]), vault_fd)
            vault_identity = _identity(os.fstat(vault_fd))
    except (OSError, GraphError) as error:
        raise LifecycleError(f"restore stopped: {error}; the journal keeps it recoverable",
                             {"state": "interrupted", **_progress}) from error
    stopped = any(outcome["outcome"] in STOP for outcome in outcomes)
    # The journal's own moves can complete while skipped collisions or lost bytes still leave the restore partial.
    partial = stopped or bool(report["incomplete"]) or any(outcome["outcome"] == "preserved" for outcome in outcomes)
    receipt = {"schema": SCHEMA, "attempt_uuid": str(uuid.uuid4()), "kind": "restore", "operation_uuid": operation,
               "export_uuid": identifier, "plan_sha256": report["plan_sha256"], "authorization": authorization,
               "started_at": started, "completed_at": _now(), "state": "partial" if stopped else "complete",
               "result": "partial" if partial else "restored", "outcomes": outcomes, "skipped": report["incomplete"],
               "vault_identity": vault_identity}
    path = _write_receipt(retirement, receipt)
    _progress = None
    return {"state": "partial" if partial else "restored", "export_uuid": identifier, "operation_uuid": operation,
            "receipt": path, "restored": sum(outcome["outcome"] == "done" for outcome in outcomes),
            "conflicts": [outcome for outcome in outcomes if outcome["outcome"] in STOP],
            "skipped": report["incomplete"], "preserved": report["preserved"], "missing": report["missing"],
            "note": "existing destinations were never overwritten; a later reconcile of this export needs "
                    "--confirmation, as a standing grant no longer applies"}, 1 if partial else 0


def restore(candidate, *, apply=False, digest=None, confirmation=None):
    _session("restore")
    if not apply:
        with export_state.retirement_directory() as retirement:
            if retirement is None:
                return {"export_uuid": candidate, "state": "unknown", "reasons": ["no lifecycle state on this host"]}, 0
            return _restore_plan(retirement, candidate)[0], 0
    with ExitStack() as stack:
        retirement = stack.enter_context(export_state.lifecycle())
        report, context = _restore_plan(retirement, candidate, stack)
        if context is None:
            return {**report, "applied": False}, 0 if report["state"] == "nothing_to_restore" else 1
        if digest != report["plan_sha256"]:
            return {**report, "state": "blocked", "applied": False,
                    "reasons": ["plan_changed: the current plan differs from --plan-sha256; preview again"]}, 1
        if confirmation != report["required_confirmation"]:
            raise _denied("restore", "confirmation does not match this plan", export_uuid=candidate,
                          required_confirmation=report["required_confirmation"])
        return _restore(retirement, report, context, {"source": "confirmation", "approval_reference": confirmation})


# C2: the generated-export grant (worktree removal policy is the coordinator's).

def _grant_plan(repo, roots):
    root = gitutil.resolve_root(repo)
    identity = gitutil.RepoIdentity.of(root)
    common = _current_identity(identity.common_git_dir)
    if common is None:
        raise LifecycleError(f"common Git directory is not accessible without symlinks: {identity.common_git_dir}")
    host, reasons, export_roots = _host(), [], []
    if not host["machine_id"]:
        reasons.append("host_unidentified: /etc/machine-id is missing, so a grant cannot be bound to this host")
    home = Path.home()
    for raw in dict.fromkeys(roots):
        path = Path(raw)
        if not path.is_absolute() or os.path.normpath(raw) != raw:
            reasons.append(f"{raw}: must be an absolute normalized path")
        elif path == home or path in home.parents:
            reasons.append(f"{raw}: too broad; name the generated export root")
        elif (current := _current_identity(path)) is None:
            reasons.append(f"{raw}: missing, or reached through a symlink")
        else:
            export_roots.append({"path": raw, "identity": current})
    core = {"kind": "grant", "host": host, "export_roots": export_roots, "scope": SCOPE, "action": ACTION,
            "repository": {"common_git_dir": str(identity.common_git_dir), "identity": common,
                           "id": identity.id, "name": identity.name}}
    return core, reasons


def _describe(grant):
    try:
        valid = export_state.digest(_grant_core(grant)) == grant.get("plan_sha256")
        status = ("invalid" if not valid else "revoked" if grant.get("revoked_at")
                  else "foreign_host" if grant.get("host") != _host() else "active")
    except (TypeError, AttributeError):
        status = "invalid"
    return {**grant, "grant_sha256": export_state.grant_digest(grant), "status": status}


def grant(repo, roots, *, apply=False, digest=None, confirmation=None):
    _session("policy grant")
    core, reasons = _grant_plan(repo, roots)
    plan_sha256 = export_state.digest(core)
    required = f"grant {core['repository']['name']} roots {len(core['export_roots'])} plan {plan_sha256[:12]}"
    report = {"state": "blocked" if reasons else "preview", "plan_sha256": plan_sha256, "required_confirmation": required,
              "reasons": reasons, **core,
              "note": "authorizes recoverable quarantine of unchanged manifest-owned generated files only; "
                      "not worktree removal, local-data loss or memory promotion"}
    if not apply:
        return report, 0
    if reasons:
        return {**report, "applied": False}, 1
    if digest != plan_sha256:
        return {**report, "state": "blocked", "applied": False, "reasons": ["plan_changed: preview again"]}, 1
    if confirmation != required:
        raise _denied("policy grant", "confirmation does not match the previewed grant", required_confirmation=required)
    with export_state.lifecycle() as retirement:
        current, reasons = _grant_plan(repo, roots)
        if reasons or export_state.digest(current) != plan_sha256:
            return {**report, "state": "blocked", "applied": False, "reasons": reasons or ["plan_changed: preview again"]}, 1
        stored = {"schema": SCHEMA, "grant_uuid": str(uuid.uuid4()), "created_at": _now(), **core,
                  "plan_sha256": plan_sha256, "approval_reference": confirmation, "revoked_at": None}
        export_state.write_grant(retirement, stored)
    return {**_describe(stored), "state": "granted", "path": _state_path("grants", stored["grant_uuid"] + ".json")}, 0


def revoke(identifier, *, apply=False):
    _session("policy revoke")
    effect = "prevents later operations and resumptions; completed moves stay quarantined"
    if not apply:
        with export_state.retirement_directory() as retirement:
            stored = None if retirement is None else export_state.read_grant(retirement, identifier)
        if stored is None:
            raise LifecycleError(f"grant not found: {identifier}")
        return {**_describe(stored), "state": "already_revoked" if stored.get("revoked_at") else "preview", "effect": effect}, 0
    with export_state.lifecycle() as retirement:
        stored = export_state.read_grant(retirement, identifier)
        if stored is None:
            raise LifecycleError(f"grant not found: {identifier}")
        state = "already_revoked" if stored.get("revoked_at") else "revoked"
        if state == "revoked":
            stored["revoked_at"] = _now()
            export_state.write_grant(retirement, stored)
    return {**_describe(stored), "state": state, "effect": effect}, 0


def show(repo=None, grant_id=None):
    _session("policy show")
    with export_state.retirement_directory() as retirement:
        if grant_id is not None:
            stored = None if retirement is None else export_state.read_grant(retirement, grant_id)
            if stored is None:
                raise LifecycleError(f"grant not found: {grant_id}")
            return {**_describe(stored), "state": "grant"}, 0
        root = gitutil.resolve_root(repo)
        identity = gitutil.RepoIdentity.of(root)
        grants = []
        for identifier in export_state.grant_ids(retirement) if retirement is not None else ():
            try:
                stored = export_state.read_grant(retirement, identifier)
            except (OSError, GraphError) as error:
                grants.append({"grant_uuid": identifier, "status": "invalid", "error": str(error)})
                continue
            if stored is not None and (stored.get("repository") or {}).get("common_git_dir") == str(identity.common_git_dir):
                grants.append(_describe(stored))
    return {"state": "policy", "repository": {"common_git_dir": str(identity.common_git_dir), "name": identity.name},
            "grants": grants, "default": "report-only unless an active grant or an explicit --confirmation authorizes a plan",
            "worktree_removal": "enforced by the coordinator's approved policy, never by Grovekeeper"}, 0


# Command line.

def interrupted():
    """What an interrupted lifecycle command leaves behind, for the CLI to report."""
    if _progress is None:
        return {"state": "interrupted", "lifecycle": "no lifecycle move was in progress; nothing needs recovery"}
    return {"state": "interrupted", **_progress,
            "recovery": "the journal is durable: preview again to resume, or restore what was quarantined"}


def add_parser(commands):
    parser = commands.add_parser("retire", help="retire generated exports of removed linked worktrees: read-only previews "
                                 "by default; recoverable quarantine only with --apply and explicit authorization")
    actions = parser.add_subparsers(dest="retire_action", required=True)

    def modes(command, *, plan=False):
        group = command.add_mutually_exclusive_group()
        group.add_argument("--dry-run", action="store_true", help="preview only; the default")
        group.add_argument("--apply", action="store_true", help="carry out the previewed operation")
        if plan:
            command.add_argument("--plan-sha256", metavar="DIGEST", help="plan_sha256 of the preview being applied")
            command.add_argument("--confirmation", metavar="TEXT", help="the preview's exact required_confirmation")
        command.set_defaults(retire_parser=command)

    prepare_parser = actions.add_parser("prepare", help="record a linked worktree and guard its graphify-out learning before removal; never removes anything")
    prepare_parser.add_argument("worktree", metavar="WORKTREE")
    prepare_parser.add_argument("--local-data-approval", metavar="FILE",
                                help="JSON {inventory_sha256, decision: discard|retained, approval_reference, retained_at?}")
    modes(prepare_parser)
    reconcile_parser = actions.add_parser("reconcile", help="prove retirement from records and Git/filesystem state; quarantine one export per apply")
    target = reconcile_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--candidate", metavar="EXPORT_UUID")
    target.add_argument("--all", action="store_true", help="preview every recorded export (never with --apply)")
    reconcile_parser.add_argument("--grant", metavar="GRANT_UUID", help="authorize an apply, or filter a preview to this grant's scope")
    modes(reconcile_parser, plan=True)
    restore_parser = actions.add_parser("restore", help="restore quarantined files into absent paths only")
    restore_parser.add_argument("--candidate", metavar="EXPORT_UUID", required=True)
    modes(restore_parser, plan=True)
    policy_parser = actions.add_parser("policy", help="show, grant or revoke standing generated-export cleanup authorization")
    policies = policy_parser.add_subparsers(dest="policy_action", required=True)
    show_parser = policies.add_parser("show", help="grants of a repository, or one grant")
    shown = show_parser.add_mutually_exclusive_group(required=True)
    shown.add_argument("repo", nargs="?", metavar="REPO_PATH")
    shown.add_argument("--grant", metavar="GRANT_UUID")
    show_parser.set_defaults(retire_parser=show_parser)
    grant_parser = policies.add_parser("grant", help="bind a grant to this host, clone and explicit generated export roots")
    grant_parser.add_argument("repo", metavar="REPO_PATH")
    grant_parser.add_argument("--export-root", dest="export_roots", action="append", required=True, metavar="ABSOLUTE_ROOT",
                              help="repeat to cover several roots in one previewed grant")
    modes(grant_parser, plan=True)
    revoke_parser = policies.add_parser("revoke", help="stop a grant authorizing later operations; moved files stay quarantined")
    revoke_parser.add_argument("--grant", metavar="GRANT_UUID", required=True)
    modes(revoke_parser)


def run(arguments):
    """Validate the argument combination, then run one retire command: (JSON result, exit code)."""
    parser, action = arguments.retire_parser, arguments.retire_action
    apply = getattr(arguments, "apply", False)
    digest, confirmation = getattr(arguments, "plan_sha256", None), getattr(arguments, "confirmation", None)
    for name in ("candidate", "grant"):
        value = getattr(arguments, name, None)
        if value is not None and not export_state.is_uuid(value):
            parser.error(f"--{name} must be a lowercase UUID")
    if digest is not None and not _hex(digest):
        parser.error("--plan-sha256 must be a lowercase 64-character SHA-256 digest")
    if not apply and (digest is not None or confirmation is not None):
        parser.error("--plan-sha256 and --confirmation are only used with --apply")
    if action == "prepare":
        return prepare(arguments.worktree, apply=apply, approval=arguments.local_data_approval)
    if action == "reconcile":
        if apply and arguments.all:
            parser.error("--all is preview-only; apply one --candidate EXPORT_UUID at a time")
        if apply and digest is None:
            parser.error("--apply needs --plan-sha256 from the preview")
        if apply and (arguments.grant is None) == (confirmation is None):
            parser.error("--apply needs exactly one authorization: --grant or --confirmation")
        return reconcile(arguments.candidate, grant=arguments.grant, apply=apply, digest=digest, confirmation=confirmation)
    if action == "restore":
        if apply and (digest is None or confirmation is None):
            parser.error("--apply needs --plan-sha256 and --confirmation from the preview")
        return restore(arguments.candidate, apply=apply, digest=digest, confirmation=confirmation)
    if arguments.policy_action == "show":
        if (arguments.repo is None) == (arguments.grant is None):
            parser.error("give REPO_PATH or --grant")
        return show(arguments.repo, arguments.grant)
    if arguments.policy_action == "grant":
        if apply and (digest is None or confirmation is None):
            parser.error("--apply needs --plan-sha256 and --confirmation from the preview")
        return grant(arguments.repo, arguments.export_roots, apply=apply, digest=digest, confirmation=confirmation)
    return revoke(arguments.grant, apply=apply)
