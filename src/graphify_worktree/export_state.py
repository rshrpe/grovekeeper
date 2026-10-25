"""External export identities and hash-verified, descriptor-relative publication."""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import stat
import uuid

from graphify_worktree import artifacts, gitutil, settings
from graphify_worktree.errors import GitError, GraphError


SCHEMA = 1
LIFECYCLE_LOCK = "lifecycle.lock"
EXPORT_LOCK = ".graphify-worktree-export.lock"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ANY = object()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identity(info):
    return {"dev": info.st_dev, "ino": info.st_ino}


@contextmanager
def _directory(path, *, parent=None, create=False):
    path = Path(path)
    if ".." in path.parts or (parent is None and not path.is_absolute()):
        raise GraphError(f"invalid export directory: {path}")
    if parent is not None and path.is_absolute():
        raise GraphError(f"export path must be relative: {path}")
    fd = os.open(path.anchor, _DIRECTORY_FLAGS) if parent is None else os.dup(parent)
    try:
        for part in path.parts[1:] if parent is None else path.parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                raise
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise GraphError(f"directory must not traverse symlinks or non-directories: {path}") from error
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _regular(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GraphError(f"{name} must be a regular file with one link, not a symlink or special file")
    return info


@contextmanager
def _reader(fd, name):
    before = _regular(fd, name)
    if before is None:
        raise FileNotFoundError(name)
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _identity(opened) != _identity(before):
            raise GraphError(f"export file changed while opening: {name}")
        yield stream
        after = os.fstat(stream.fileno())
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise GraphError(f"export file changed while reading: {name}")


def _fingerprint(fd, relative):
    path = PurePosixPath(relative)
    with _directory(path.parent, parent=fd) as parent:
        if parent is None or _regular(parent, path.name) is None:
            return None
        with _reader(parent, path.name) as stream:
            size = os.fstat(stream.fileno()).st_size
            return {"size": size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def _read_json(fd, name):
    if _regular(fd, name) is None:
        return None
    try:
        with _reader(fd, name) as stream:
            return json.load(stream)
    except (ValueError, UnicodeError) as error:
        raise GraphError(f"invalid export metadata {name}: {error}") from error


@contextmanager
def _atomic_file(fd, name, *, expected=_ANY):
    temporary = f".graphify-worktree-{uuid.uuid4()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        _regular(fd, name)
        if expected is not _ANY and _fingerprint(fd, name) != expected:
            raise GraphError(f"export destination changed during publication: {name}")
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _write_json(fd, name, value, *, expected=_ANY):
    with _atomic_file(fd, name, expected=expected) as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@contextmanager
def _locked(fd, name):
    _regular(fd, name)
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise GraphError(f"export lock must be a regular file with one link: {name}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = _regular(fd, name)
        if current is None or _identity(current) != _identity(info):
            raise GraphError(f"export lock changed while waiting: {name}")
        yield info
    finally:
        os.close(descriptor)


@contextmanager
def staging(vault):
    """Render beside the vault, without holding the lifecycle lock."""
    try:
        with _directory(vault.parent, create=True) as parent:
            name = f".graphify-worktree-staging-{uuid.uuid4()}"
            os.mkdir(name, mode=0o700, dir_fd=parent)
            try:
                yield vault.parent / name
            finally:
                shutil.rmtree(name, dir_fd=parent)
                os.fsync(parent)
    except (OSError, UnicodeError) as error:
        raise GraphError(f"export staging error: {error}") from error


def _inventory(fd):
    files, directories = {}, []

    def walk(directory, prefix=""):
        for name in sorted(os.listdir(directory)):
            relative = prefix + name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                with _directory(name, parent=directory) as child:
                    if child is None:
                        raise GraphError(f"staged directory disappeared: {relative}")
                    walk(child, relative + "/")
            else:
                files[relative] = _fingerprint(directory, name)
                if files[relative] is None:
                    raise GraphError(f"staged file disappeared: {relative}")

    walk(fd)
    return files, directories


def _owned_path(relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise GraphError("invalid export inventory path")
    path = PurePosixPath(relative)
    if (not path.parts or path.is_absolute() or path.as_posix() != relative or ".." in path.parts
            or path.parts[0] in (".obsidian", artifacts.PROVENANCE, EXPORT_LOCK)):
        raise GraphError(f"invalid export inventory path: {relative}")


def _validate_files(files):
    if not isinstance(files, dict):
        raise GraphError("export inventory files must be an object")
    for relative, fingerprint in files.items():
        _owned_path(relative)
        if not isinstance(fingerprint, dict) or set(fingerprint) != {"size", "sha256"}:
            raise GraphError(f"invalid export fingerprint: {relative}")
        size, digest = fingerprint["size"], fingerprint["sha256"]
        if (type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise GraphError(f"invalid export fingerprint: {relative}")


def _validate_inventory(inventory):
    if not isinstance(inventory, dict):
        raise GraphError("export inventory must be an object")
    _validate_files(inventory.get("files"))
    directories = inventory.get("directories")
    if not isinstance(directories, list):
        raise GraphError("export inventory directories must be a list")
    for relative in directories:
        _owned_path(relative)
        if relative in inventory["files"]:
            raise GraphError(f"export inventory lists both a file and directory: {relative}")


def _host():
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeError):
        machine_id = None
    return {"hostname": socket.gethostname(), "machine_id": machine_id}


def _checkout(root, vault, vault_fd):
    paths = {"worktree_root": root, "common_git_dir": gitutil.common_dir(root),
             "git_admin_dir": Path(gitutil.output(root, "rev-parse", "--absolute-git-dir"))}
    identities = {"vault_path": _identity(os.fstat(vault_fd))}
    for key, path in (*paths.items(), ("parent", root.parent)):
        with _directory(path) as fd:
            if fd is None:
                raise GraphError(f"export checkout directory is missing: {path}")
            identities[key] = _identity(os.fstat(fd))
    return {**{key: str(path) for key, path in paths.items()}, "vault_path": str(vault),
            "directory_identities": identities}


def _owner(fd, root, vault):
    marker = _read_json(fd, artifacts.PROVENANCE)
    if marker is None and _regular(fd, artifacts.PROVENANCE) is None:
        if set(os.listdir(fd)) - {EXPORT_LOCK}:
            raise GraphError(f"refusing to overwrite an unrelated vault: {vault}")
        return None
    if not isinstance(marker, dict) or marker.get("worktree_root") != str(root):
        raise GraphError(f"vault belongs to another worktree: {vault}")
    return marker


def _record(exports, marker, checkout):
    manifest = marker.get("export_manifest") if marker is not None else None
    if marker is not None and "export_manifest" not in marker and "export_uuid" in marker:
        if marker.get("export_state") != "pending":
            raise GraphError("export UUID outside a pending vault claim")
        manifest = {"schema": SCHEMA, "export_uuid": marker["export_uuid"], "generation": 0}
    if manifest is None and (marker is None or "export_manifest" not in marker):
        now = _now()
        return {"schema": SCHEMA, "export_uuid": str(uuid.uuid4()), **checkout, "host": _host(),
                "state": "complete", "generation": 0, "files": {}, "directories": [],
                "created_at": now, "updated_at": now, "completed_at": None}
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise GraphError("vault export_manifest has an invalid or unsupported schema")
    identifier = manifest.get("export_uuid")
    try:
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError
    except ValueError:
        raise GraphError("vault export_manifest has an invalid export UUID") from None
    with _directory(identifier, parent=exports) as directory:
        saved = _read_json(directory, "record.json") if directory is not None else None
    if saved is None:
        raise GraphError(f"external export record is missing for {identifier}; refusing adoption")
    if not isinstance(saved, dict) or saved.get("schema") != SCHEMA or saved.get("export_uuid") != identifier:
        raise GraphError(f"invalid external export record for {identifier}")
    saved_identities = saved.get("directory_identities")
    if (any(saved.get(key) != value for key, value in checkout.items() if key != "directory_identities")
            or not isinstance(saved_identities, dict)
            or any(saved_identities.get(key) != value for key, value in checkout["directory_identities"].items()
                   if key != "parent" or key in saved_identities)):
        raise GraphError(f"export {identifier} belongs to a different checkout or vault incarnation; retirement is required")
    if (saved.get("state") not in ("publishing", "complete") or type(saved.get("generation")) is not int
            or saved["generation"] < 0):
        raise GraphError(f"invalid export state for {identifier}")
    generation = manifest.get("generation")
    allowed = (saved["generation"], saved["generation"] + 1) if saved["state"] == "publishing" else (saved["generation"],)
    if type(generation) is not int or generation not in allowed:
        raise GraphError(f"marker and external record generations disagree for {identifier}")
    _validate_inventory(saved)
    if saved["state"] == "publishing":
        _validate_inventory(saved.get("pending"))
        _validate_files(saved["pending"].get("previous_files", {}))
    elif "pending" in saved:
        raise GraphError(f"complete export {identifier} still has pending publication metadata")
    return saved


def _trusted(record):
    pending = record.get("pending", {})
    return (record["files"], pending.get("files", {}), pending.get("previous_files", {}))


def _preflight(fd, files, directories, record, seed):
    trusted = _trusted(record)
    old_files = set().union(*(inventory.keys() for inventory in trusted))
    old_directories = set(record["directories"]) | set(record.get("pending", {}).get("directories", []))
    owned_directories = set(old_directories)
    snapshots, conflicts = {}, []
    for relative in sorted(set(directories) | old_directories | {".obsidian"}):
        try:
            with _directory(relative, parent=fd) as directory:
                if directory is None and relative in directories:
                    owned_directories.add(relative)
        except (OSError, GraphError) as error:
            conflicts.append(f"{relative} ({error})")
    for relative in sorted(set(files) | old_files):
        try:
            current = _fingerprint(fd, relative)
            snapshots[relative] = current
            allowed = [inventory[relative] for inventory in trusted if relative in inventory]
            if relative in files:
                allowed.append(files[relative])
            if current is not None and current not in allowed:
                conflicts.append(relative)
        except (OSError, GraphError) as error:
            conflicts.append(f"{relative} ({error})")
    seed_missing = False
    if seed:
        try:
            with _directory(".obsidian", parent=fd) as directory:
                seed_missing = directory is None or _regular(directory, "graph.json") is None
        except (OSError, GraphError) as error:
            conflicts.append(f".obsidian/graph.json ({error})")
    if conflicts:
        raise GraphError("refusing export; protected or modified paths: " + ", ".join(conflicts))
    return snapshots, owned_directories, old_directories - set(directories), seed_missing


def _copy(stage, vault, relative, fingerprint, expected):
    path = PurePosixPath(relative)
    with _directory(path.parent, parent=stage) as source, _directory(path.parent, parent=vault, create=True) as target:
        if source is None:
            raise GraphError(f"staged directory disappeared: {relative}")
        with _reader(source, path.name) as incoming, _atomic_file(target, path.name, expected=expected) as outgoing:
            digest = hashlib.sha256()
            size = 0
            while chunk := incoming.read(1024 * 1024):
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if {"size": size, "sha256": digest.hexdigest()} != fingerprint:
                raise GraphError(f"staged file changed during publication: {relative}")


def _prune(fd, relative, expected):
    path = PurePosixPath(relative)
    with _directory(path.parent, parent=fd) as parent:
        if parent is None or _fingerprint(parent, path.name) != expected:
            raise GraphError(f"export destination changed during pruning: {relative}")
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)


def _prune_directories(fd, directories):
    for relative in sorted(directories, key=lambda name: (name.count("/"), name), reverse=True):
        path = PurePosixPath(relative)
        with _directory(path, parent=fd) as directory:
            if directory is None:
                continue
        with _directory(path.parent, parent=fd) as parent:
            if parent is None:
                continue
            try:
                os.rmdir(path.name, dir_fd=parent)
                os.fsync(parent)
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                    raise


@contextmanager
def _publication(root, vault):
    try:
        with ExitStack() as stack:
            retirement = stack.enter_context(_directory(settings.state_dir() / "retirement", create=True))
            stack.enter_context(_locked(retirement, LIFECYCLE_LOCK))
            exports = stack.enter_context(_directory("exports", parent=retirement, create=True))
            vault_fd = stack.enter_context(_directory(vault, create=True))
            _owner(vault_fd, root, vault)
            stack.enter_context(_locked(vault_fd, EXPORT_LOCK))
            marker = _owner(vault_fd, root, vault)
            record = _record(exports, marker, _checkout(root, vault, vault_fd))
            yield vault_fd, exports, marker, record
    except (OSError, UnicodeError, GitError) as error:
        raise GraphError(f"export publication error: {error}") from error


def claim(root, vault):
    """Keep a first export retryable, even when rendering is interrupted."""
    with _publication(root, vault) as (vault_fd, exports, marker, record):
        if marker is not None and (marker.get("export_state") != "pending"
                                   or "export_uuid" in marker or "export_manifest" in marker):
            return
        marker_hash = _fingerprint(vault_fd, artifacts.PROVENANCE)
        record.update(state="publishing", updated_at=_now(),
                      pending={"files": {}, "directories": [], "previous_files": {}})
        with _directory(record["export_uuid"], parent=exports, create=True) as record_fd:
            _write_json(record_fd, "record.json", record)
        _write_json(vault_fd, artifacts.PROVENANCE,
                    {"schema": artifacts.SCHEMA, "worktree_root": str(root), "export_state": "pending",
                     "export_uuid": record["export_uuid"]}, expected=marker_hash)


def publish(root, vault, staged, provenance):
    """Publish under graph -> lifecycle -> export locks; the caller holds graph."""
    try:
        with ExitStack() as stack:
            stage_fd = stack.enter_context(_directory(staged))
            if stage_fd is None:
                raise GraphError("export staging directory is missing")
            rendered, rendered_directories = _inventory(stage_fd)
            files = {name: value for name, value in rendered.items() if not name.startswith(".obsidian/")}
            directories = [name for name in rendered_directories if name != ".obsidian" and not name.startswith(".obsidian/")]
            _validate_inventory({"files": files, "directories": directories})
            vault_fd, exports, marker, record = stack.enter_context(_publication(root, vault))
            parent_identity = record["directory_identities"].get("parent")
            if parent_identity is None:
                with _directory(root.parent) as parent:
                    if parent is None:
                        raise GraphError(f"export checkout parent is missing: {root.parent}")
                    parent_identity = _identity(os.fstat(parent))
            if os.fstat(stage_fd).st_dev != os.fstat(vault_fd).st_dev:
                raise GraphError("export staging directory and vault must share a filesystem")
            marker_hash = _fingerprint(vault_fd, artifacts.PROVENANCE)
            seed = rendered.get(".obsidian/graph.json")
            snapshots, owned_directories, stale_directories, seed_missing = _preflight(
                vault_fd, files, directories, record, seed)
            record_fd = stack.enter_context(_directory(record["export_uuid"], parent=exports, create=True))
            record.update(state="publishing", updated_at=_now(), pending={
                "files": files, "directories": sorted(owned_directories),
                "previous_files": {name: value for name, value in snapshots.items() if value is not None},
            })
            _write_json(record_fd, "record.json", record)
            if marker is None or ("export_manifest" not in marker and "export_uuid" not in marker):
                _write_json(vault_fd, artifacts.PROVENANCE,
                            {**(marker or {"schema": artifacts.SCHEMA, "worktree_root": str(root)}),
                             "export_state": "pending", "export_uuid": record["export_uuid"]}, expected=marker_hash)
                marker_hash = _fingerprint(vault_fd, artifacts.PROVENANCE)
            for relative, fingerprint in files.items():
                if snapshots[relative] != fingerprint:
                    _copy(stage_fd, vault_fd, relative, fingerprint, snapshots[relative])
            if seed_missing:
                _copy(stage_fd, vault_fd, ".obsidian/graph.json", seed, None)
            for relative in sorted(snapshots.keys() - files.keys()):
                if snapshots[relative] is not None:
                    _prune(vault_fd, relative, snapshots[relative])
            _prune_directories(vault_fd, stale_directories)
            remaining = []
            for relative in sorted(owned_directories):
                with _directory(relative, parent=vault_fd) as directory:
                    if directory is not None:
                        remaining.append(relative)
            generation = record["generation"] + 1
            manifest = {"schema": SCHEMA, "export_uuid": record["export_uuid"], "generation": generation,
                        "files": files, "directories": remaining}
            completed_at = _now()
            _write_json(vault_fd, artifacts.PROVENANCE,
                        {**provenance, "exported_at": completed_at, "export_manifest": manifest}, expected=marker_hash)
            record.update(state="complete", generation=generation, files=files, directories=remaining,
                          updated_at=completed_at, completed_at=completed_at)
            record["directory_identities"]["parent"] = parent_identity
            del record["pending"]
            _write_json(record_fd, "record.json", record)
            return {"export_uuid": record["export_uuid"], "generation": generation}
    except (OSError, UnicodeError, GitError) as error:
        raise GraphError(f"export publication error: {error}") from error


# Read-only registry access and grant storage for retirement.py. Readers never
# create state or locks; writers expect the caller to hold lifecycle().

GRANT_MUTABLE = ("revoked_at",)


def digest(value):
    """SHA-256 of canonical JSON, the identity of plans and grants."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def retirement_path():
    return settings.state_dir() / "retirement"


@contextmanager
def retirement_directory():
    """The lifecycle state directory, or None when absent; nothing is created or locked."""
    with _directory(retirement_path()) as fd:
        yield fd


@contextmanager
def lifecycle():
    """Hold the lifecycle lock, creating the state directory; yields its descriptor."""
    with _directory(retirement_path(), create=True) as fd, _locked(fd, LIFECYCLE_LOCK):
        yield fd


def export_ids(retirement):
    """UUIDs of every registered export."""
    with _directory("exports", parent=retirement) as exports:
        return [] if exports is None else sorted(name for name in os.listdir(exports) if is_uuid(name))


def read_record(retirement, identifier):
    """(record, fingerprint of record.json) of one registered export, or (None, None)."""
    if not is_uuid(identifier):
        raise GraphError(f"invalid export UUID: {identifier}")
    with _directory(f"exports/{identifier}", parent=retirement) as directory:
        if directory is None or _regular(directory, "record.json") is None:
            return None, None
        fingerprint = _fingerprint(directory, "record.json")
        record = _read_json(directory, "record.json")
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or record.get("export_uuid") != identifier:
        raise GraphError(f"invalid external export record for {identifier}")
    paths = ("worktree_root", "common_git_dir", "git_admin_dir", "vault_path")
    identities = record.get("directory_identities")
    if (any(not isinstance(record.get(key), str) for key in paths) or not isinstance(identities, dict)
            or any(not isinstance(identities.get(key), dict) for key in (*paths[:3], "vault_path"))
            or not isinstance(record.get("host"), dict) or type(record.get("generation")) is not int):
        raise GraphError(f"incomplete external export record for {identifier}")
    _validate_inventory(record)
    return record, fingerprint


def grant_digest(grant):
    """Digest of a grant's immutable content; revocation does not change it."""
    return digest({key: value for key, value in grant.items() if key not in GRANT_MUTABLE})


def grant_ids(retirement):
    with _directory("grants", parent=retirement) as grants:
        if grants is None:
            return []
        return sorted(name.removesuffix(".json") for name in os.listdir(grants)
                      if name.endswith(".json") and is_uuid(name.removesuffix(".json")))


def read_grant(retirement, identifier):
    """One stored generated-export grant, or None."""
    if not is_uuid(identifier):
        raise GraphError(f"invalid grant UUID: {identifier}")
    with _directory("grants", parent=retirement) as grants:
        grant = None if grants is None else _read_json(grants, f"{identifier}.json")
    if grant is not None and (not isinstance(grant, dict) or grant.get("schema") != SCHEMA
                              or grant.get("grant_uuid") != identifier):
        raise GraphError(f"invalid grant {identifier}")
    return grant


def write_grant(retirement, grant):
    with _directory("grants", parent=retirement, create=True) as grants:
        _write_json(grants, f"{grant['grant_uuid']}.json", grant)
