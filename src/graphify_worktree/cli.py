"""Worktree-local, code-only Graphify refresh and generated Obsidian export."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from graphify_worktree import hooks as knowledge_hooks
from graphify_worktree import memory_cli, settings, upstream
from graphify_worktree.policy import PolicyError, configure, get_policy, git_env, require, resolve_root


PROVENANCE = ".graphify-worktree.json"
EVENT = ".graphify-worktree-event.json"
SCHEMA = 1


class Error(Exception):
    pass


def git(path, *args, optional=False):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, env=git_env(),
    )
    if result.returncode:
        if optional and result.returncode == 1:
            return None
        raise Error(result.stderr.strip() or "Git command failed")
    return result.stdout.rstrip("\n")


def root_for(path):
    try:
        return resolve_root(path)
    except PolicyError as error:
        raise Error(str(error)) from error


def resolve_scope(root, scope):
    relative = Path(scope)
    if relative.is_absolute():
        raise Error("--scope must be relative to the worktree root")
    scoped = (root / relative).resolve(strict=True)
    if not scoped.is_relative_to(root):
        raise Error("scope escapes the worktree")
    if not scoped.is_dir() or root_for(scoped) != root:
        raise Error("scope must be a directory in this worktree, not a nested checkout")
    if "graphify-out" in scoped.relative_to(root).parts:
        raise Error("generated graphify-out directories cannot be a source scope")
    return scoped


def vault_for(root):
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common.name == ".git":
        project = common.parent.name
    else:
        entries = git(root, "worktree", "list", "--porcelain", "-z").split("\0")
        primary = next(entry[9:] for entry in entries if entry.startswith("worktree "))
        project = Path(primary).name.removesuffix(".git")
    suffix = hashlib.sha256(os.fsencode(root)).hexdigest()[:10]
    return settings.graph_vault_root() / project / f"{root.name}-{suffix}"


def load_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_state(root):
    return {
        "branch": git(root, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True),
        "head": git(root, "rev-parse", "--verify", "HEAD", optional=True),
    }


def exclusions(scoped):
    # Reuse Graphify's traversal rules, adding hard checkout boundaries that its
    # native walker otherwise crosses when a nested checkout is not gitignored.
    rules = upstream.ignore_rules()
    patterns = rules.load_graphifyignore(scoped)
    cache = {}
    excluded = []

    def walk_error(error):
        raise error

    for directory, names, _ in os.walk(scoped, onerror=walk_error):
        parent = Path(directory)
        if parent != scoped:
            patterns.extend(rules.load_dir_own_ignore(parent))
        keep = []
        for name in names:
            child = parent / name
            if child.is_symlink() or rules.is_noise_dir(name, parent):
                continue
            if (child / ".git").exists():
                literal = glob.escape(child.relative_to(scoped).as_posix()).replace("#", "[#]")
                excluded.append(f"/{literal}/")
            elif not rules.is_ignored(child, scoped, patterns, _cache=cache):
                keep.append(name)
        names[:] = keep
    return sorted(excluded)


def snapshot(root, scoped, scratch):
    state = git_state(root)
    excluded = exclusions(scoped)
    detected = upstream.detect(scoped, excludes=excluded, scratch=scratch)
    if detected.get("walk_errors"):
        raise Error("source enumeration was incomplete; refusing a partial graph")
    manifest = {}
    for name in detected["files"]["code"]:
        source = Path(name)
        if not source.resolve().is_relative_to(scoped):
            raise Error(f"source escaped scope: {source}")
        before = source.stat()
        content_hash = digest(source)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise Error(f"source changed while hashing: {source}")
        manifest[source.relative_to(root).as_posix()] = content_hash
    if git_state(root) != state:
        raise Error("HEAD or branch changed while inspecting sources")
    fingerprint = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        **state,
        "worktree_root": str(root),
        "scope": scoped.relative_to(root).as_posix(),
        "graphify_version": upstream.graphify_version(),
        "manifest": manifest,
        "fingerprint": fingerprint,
        "excludes": excluded,
    }


@contextmanager
def locked(root, create=False):
    output = root / "graphify-out"
    if output.is_symlink() or output.resolve() != output:
        raise Error("graphify-out must not traverse symlinks; each worktree owns its output")
    if create:
        output.mkdir(exist_ok=True)
    if not output.exists():
        yield output
        return
    lock = output / ".graphify-worktree.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield output

@contextmanager
def workspace(output):
    # Detection and extraction stay structural-only and same-filesystem staged.
    # Restore the process environment before the MCP upstream query runs.
    parent = output if output.is_dir() else None
    with tempfile.TemporaryDirectory(prefix=".graphify-worktree-", dir=parent) as temporary:
        scratch = Path(temporary).resolve()
        environment = {
            "GRAPHIFY_OUT": str(scratch / "graphify-out"),
            "GRAPHIFY_GOOGLE_WORKSPACE": "0",
            "GRAPHIFY_BACKEND": "none",
        }
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            yield scratch
        finally:
            upstream.release_stat_index(scratch)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def check_artifacts(output):
    for name in ("graph.json", "GRAPH_REPORT.md", PROVENANCE, EVENT):
        path = output / name
        if path.is_symlink() or path.resolve() != path:
            raise Error(f"{path} must be worktree-local, not a symlink")



def inspect(root, output, scratch):
    check_artifacts(output)
    result = {
        "state": "missing",
        "worktree_root": str(root),
        "graph_path": str(output / "graph.json"),
        "vault_path": str(vault_for(root)),
        "scope": None,
        "reason": "graph.json does not exist",
    }
    graph = output / "graph.json"
    if not graph.is_file():
        return result
    result["state"] = "stale"
    try:
        saved = load_json(output / PROVENANCE)
        if not isinstance(saved, dict) or saved.get("schema") != SCHEMA:
            raise Error("missing or unsupported provenance schema")
        result["scope"] = saved["scope"]
        result["extracted_at"] = saved["extracted_at"]
        result["generation"] = saved["extracted_at"]
        result["fingerprint"] = saved["fingerprint"]
        result["graph_sha256"] = saved["graph_sha256"]
        if saved["worktree_root"] != str(root):
            raise Error("graph belongs to a different worktree")
        if graph.is_symlink() or digest(graph) != saved["graph_sha256"]:
            raise Error("graph changed outside graphify-worktree")
        if digest(output / "GRAPH_REPORT.md") != saved["report_sha256"]:
            raise Error("graph report is missing or changed")
        current = snapshot(root, resolve_scope(root, saved["scope"]), scratch)
        differences = [key for key, value in current.items() if saved.get(key) != value]
        if differences:
            raise Error("changed: " + ", ".join(differences))
        try:
            event = load_json(output / EVENT)
        except (OSError, ValueError):
            event = None
        if isinstance(event, dict) and event.get("generation") == saved["extracted_at"]:
            raise Error(f"Git event invalidated this generation: {event.get('event', 'unknown')}")
        result.update(state="fresh", reason="source content, scope, HEAD and branch match provenance")
        result["branch"] = saved["branch"]
        result["head"] = saved["head"]
        result["code_files"] = len(saved["manifest"])
    except (Error, OSError, ValueError, KeyError, TypeError) as error:
        result["reason"] = str(error)
    return result


def run_graphify(arguments, scoped):
    result = subprocess.run(
        [*upstream.graphify_command(), *map(str, arguments)], cwd=scoped,
        stdout=sys.stderr,
    )
    if result.returncode:
        raise Error(f"Graphify {arguments[0]} failed (exit {result.returncode}); previous graph retained")


def report_graph(staged, root, scoped, before):
    # Extraction already clustered. Reuse its analysis rather than clustering
    # a large checkout twice; hub naming and report generation are entirely local.
    graph_path = staged / "graph.json"
    data = load_json(graph_path)
    if not data.get("nodes"):
        raise Error("Graphify produced an empty graph")
    # Native extraction makes citations scope-relative. MCP project_path is the
    # worktree root, so published citations must use that same coordinate system.
    def rebase(value):
        if isinstance(value, dict):
            source = value.get("source_file")
            if isinstance(source, str) and source:
                absolute = (scoped / source).resolve()
                if absolute.is_relative_to(root):
                    value["source_file"] = absolute.relative_to(root).as_posix()
            for child in value.values():
                rebase(child)
        elif isinstance(value, list):
            for child in value:
                rebase(child)

    rebase(data)
    analysis = load_json(staged / ".graphify_analysis.json")
    graph = upstream.build_graph(data)
    communities = {int(key): members for key, members in analysis["communities"].items()}
    labels = upstream.label_communities(graph, communities)
    questions = upstream.suggest_questions(graph, communities, labels)
    report = upstream.generate_report(
        graph, communities,
        {int(key): value for key, value in analysis["cohesion"].items()},
        labels, analysis["gods"], analysis["surprises"],
        {"warning": f"Code-only scope: {before['scope']}; {len(before['manifest'])} supported code files. Documents and semantic extraction excluded."},
        {"input": 0, "output": 0}, str(root),
        suggested_questions=questions, built_at_commit=before["head"],
    )
    report = report.replace("`graphify update .`", "`graphify-worktree refresh .`")
    for node in data["nodes"]:
        if node.get("community") is not None:
            node["community_name"] = labels.get(int(node["community"]), "")
    write_json(graph_path, data)
    analysis["questions"] = questions
    write_json(staged / ".graphify_analysis.json", analysis)
    write_json(staged / ".graphify_labels.json", labels)
    (staged / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")


def refresh(root, output, scratch, requested_scope, *, emit=True, authorize=None):
    check_artifacts(output)
    saved_path = output / PROVENANCE
    saved = load_json(saved_path) if saved_path.exists() else {}
    scope = requested_scope if requested_scope is not None else saved.get("scope", ".")
    scoped = resolve_scope(root, scope)
    before = snapshot(root, scoped, scratch)
    if not before["manifest"]:
        raise Error("scope has no supported code files; previous graph retained")
    arguments = ["extract", scoped, "--code-only", "--force", "--out", scratch, "--max-workers", "4"]
    for excluded in before["excludes"]:
        arguments.extend(["--exclude", excluded])
    run_graphify(arguments, scoped)
    staged = scratch / "graphify-out"
    report_graph(staged, root, scoped, before)
    if before != snapshot(root, scoped, scratch):
        raise Error("sources, HEAD, branch or scope changed during extraction; previous graph retained")
    if authorize is not None:
        authorize()
    extracted_at = datetime.now(timezone.utc)
    if saved.get("extracted_at"):
        previous = datetime.fromisoformat(saved["extracted_at"])
        extracted_at = max(extracted_at, previous + timedelta(microseconds=1))
    provenance = {
        "schema": SCHEMA,
        **before,
        "extracted_at": extracted_at.isoformat(),
        "graph_sha256": digest(staged / "graph.json"),
        "report_sha256": digest(staged / "GRAPH_REPORT.md"),
        "vault_path": str(vault_for(root)),
    }
    write_json(staged / PROVENANCE, provenance)
    # Upstream's graph cache keys on mtime/size. Published generations must have
    # distinct keys even for equal-sized graphs or a clock moving backwards.
    graph = output / "graph.json"
    modified = max(time.time_ns(), graph.stat().st_mtime_ns + 1 if graph.exists() else 0)
    os.utime(staged / "graph.json", ns=(modified, modified))
    if not saved_path.exists() and (output / "graph.json").exists():
        archive = output / ("pre-worktree-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        archive.mkdir()
        for name in ("graph.json", "GRAPH_REPORT.md", ".graphify_analysis.json", ".graphify_labels.json", "manifest.json"):
            previous = output / name
            if previous.is_file():
                shutil.copy2(previous, archive / name)
        print(f"Archived pre-wrapper graph: {archive}", file=sys.stderr)
    # Only replace owned artifacts. Memory, reflections and unrelated files stay
    # untouched. graph.json is the final atomic publication/reader commit point.
    for name in ("GRAPH_REPORT.md", ".graphify_analysis.json", ".graphify_labels.json", PROVENANCE, "graph.json"):
        os.replace(staged / name, output / name)
    if emit:
        print(json.dumps({"state": "fresh", "graph_path": str(output / "graph.json"),
                          "scope": before["scope"], "code_files": len(before["manifest"]),
                          "vault_path": provenance["vault_path"]}, indent=2))
    return 0


@contextmanager
def guarded_graph(root, *, once=False, session_mode=None):
    """Ensure under the worktree lock, then validate the caller's graph read.

    session_mode may be a provider so an in-flight MCP session opt-out is seen.
    Graph/source/provenance failures raise Error; consent failures PolicyError.
    """
    def authorize():
        mode = session_mode() if callable(session_mode) else session_mode
        return require(root, capability="graph", once=once, session_mode=mode)

    policy = authorize()  # Denials must precede artifact IO and lock creation.
    root = Path(policy["worktree_root"])
    with locked(root, create=True) as output:
        authorize()  # A queued client must not inherit earlier consent/freshness.
        with workspace(output) as scratch:
            status = inspect(root, output, scratch)
            rebuilt = status["state"] != "fresh"
            if rebuilt:
                refresh(root, output, scratch, None, emit=False, authorize=authorize)
                status = inspect(root, output, scratch)
            if status["state"] != "fresh":
                raise Error("graph unavailable after refresh: " + status["reason"])
        authorize()
        status["rebuilt"] = rebuilt
        expected = (status["generation"], status["fingerprint"], status["graph_sha256"], status["scope"])
        yield status
        authorize()
        with workspace(output) as scratch:
            current = inspect(root, output, scratch)
        authorize()
        actual = tuple(current.get(key) for key in ("generation", "fingerprint", "graph_sha256", "scope"))
        if current["state"] != "fresh" or actual != expected:
            raise Error("graph changed during query; result withheld: " + current["reason"])


def ensure(root, *, once=False, session_mode=None):
    """Return fresh JSON-compatible status, rebuilding at most once."""
    with guarded_graph(root, once=once, session_mode=session_mode) as status:
        return status


def export_canvas(vault, graph, communities, labels):
    if graph.number_of_nodes() <= 5000:
        upstream.to_canvas(graph, communities, str(vault / "graph.canvas"), community_labels=labels)
        mode = "symbols"
        keep = set()
    else:
        # Obsidian timed out on the 96k-node checkout. Page community links
        # instead of rendering every symbol; all native notes and MCP data remain.
        targets = sorted(name for name in load_json(vault / ".graphify_obsidian_manifest.json")["files"]
                         if name.startswith("_COMMUNITY_") and name.endswith(".md"))
        keep = set()

        def page(path, links, overview=False):
            nodes = [
                {"id": hashlib.sha256(link.encode()).hexdigest()[:16],
                 "type": "text", "text": "[[" + link + "]]",
                 "x": (index % 10) * 340, "y": (index // 10) * 180,
                 "width": 300, "height": 140}
                for index, link in enumerate(links)
            ]
            if overview:
                nodes.insert(0, {"id": "0000000000000000", "type": "text",
                                "text": "# Community index\n\nOpen a page, then a community note to follow its symbol links. "
                                        "The full graph remains available through Graphify MCP.",
                                "x": 0, "y": -240, "width": 660, "height": 180})
            temporary = path.with_suffix(".canvas.tmp")
            try:
                write_json(temporary, {"nodes": nodes, "edges": []})
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

        level = 0
        while True:
            links = []
            for index, start in enumerate(range(0, len(targets), 100)):
                relative = f"_graphify-worktree/communities-{level}-{index + 1:04}.canvas"
                destination = vault / relative
                page(destination, targets[start:start + 100])
                keep.add(destination)
                links.append(relative)
            if len(links) <= 100:
                page(vault / "graph.canvas", links, overview=True)
                break
            targets = links
            level += 1
        mode = "community-index"
    for previous in (vault / "_graphify-worktree").glob("communities-*.canvas"):
        if previous not in keep:
            previous.unlink()
    return mode


def export(root, output, scratch, open_vault):
    status = inspect(root, output, scratch)
    if status["state"] != "fresh":
        print(json.dumps(status, indent=2))
        return 4 if status["state"] == "missing" else 3
    vault = Path(status["vault_path"])
    if vault.is_symlink() or vault.resolve() != vault:
        raise Error("generated vault path must not traverse symlinks")
    if vault.exists() and any(vault.iterdir()):
        try:
            owner = load_json(vault / PROVENANCE)
        except (OSError, ValueError):
            raise Error(f"refusing to overwrite an unrelated vault: {vault}") from None
        if not isinstance(owner, dict) or owner.get("worktree_root") != str(root):
            raise Error(f"vault belongs to another worktree: {vault}")
    vault.mkdir(parents=True, exist_ok=True)
    marker = vault / PROVENANCE
    if not marker.exists():
        # Claim an empty generated vault before export so a failed native export
        # remains safely retryable, without claiming it completed.
        write_json(marker, {"schema": SCHEMA, "worktree_root": str(root), "export_state": "pending"})
    report_dir = vault / "_graphify-worktree"
    if report_dir.is_symlink():
        raise Error("vault report directory must not be a symlink")
    report_dir.mkdir(exist_ok=True)
    data = load_json(output / "graph.json")
    graph = upstream.build_graph(data)
    analysis = load_json(output / ".graphify_analysis.json")
    communities = {int(key): members for key, members in analysis["communities"].items()}
    labels = {int(key): value for key, value in load_json(output / ".graphify_labels.json").items()}
    upstream.to_obsidian(graph, communities, str(vault), community_labels=labels,
                cohesion={int(key): value for key, value in analysis["cohesion"].items()})
    canvas_mode = export_canvas(vault, graph, communities, labels)
    if (report_dir / "GRAPH_REPORT.md").is_symlink():
        raise Error("vault report must not be a symlink")
    shutil.copyfile(output / "GRAPH_REPORT.md", report_dir / "GRAPH_REPORT.md")
    # Native exporter tracks and prunes only its generated files; it preserves
    # authored notes and .obsidian settings in this already-owned vault.
    provenance = load_json(output / PROVENANCE)
    provenance["exported_at"] = datetime.now(timezone.utc).isoformat()
    descriptor, temporary = tempfile.mkstemp(dir=vault, prefix=".graphify-worktree-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(provenance, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, marker)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({"state": "exported", "vault_path": str(vault), "canvas_mode": canvas_mode}, indent=2))
    if open_vault:
        # vault-message uses Obsidian's native startup queue. A second CLI call
        # can arrive before a new vault's CLI handlers have registered.
        code = ('(()=>{const ipc=require("electron").ipcRenderer;const path=' + json.dumps(str(vault)) +
                ';const opened=ipc.sendSync("vault-open",path,false);'
                'if(opened!==true)throw Error(String(opened));'
                'ipc.sendSync("vault-message",path,{action:"open",file:"_graphify-worktree/GRAPH_REPORT.md"});return true})()')
        registration = subprocess.run(
            [str(settings.obsidian_cli()), "eval", "code=" + code],
            capture_output=True, text=True,
        )
        if registration.returncode or registration.stdout.strip() != "=> true":
            raise Error("vault exported, but Obsidian registration failed: " + (registration.stderr or registration.stdout).strip())
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Local code-only Graphify graphs and isolated generated Obsidian vaults per Git worktree.",
        epilog="Exit codes: 0 success/fresh, 1 operation error, 2 usage error, 3 stale, 4 missing, 5 policy denied. No LLM, daemon or global graph merge.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    policy_parser = commands.add_parser("policy", help="read repository knowledge preference without inspecting artifacts")
    policy_parser.add_argument("path", nargs="?", default=".")
    configure_parser = commands.add_parser("configure", help="save shared repository preference; never build, export, open, or delete artifacts")
    configure_parser.add_argument("mode", choices=("off", "graph", "obsidian"))
    configure_parser.add_argument("path", nargs="?", default=".")
    refresh_parser = commands.add_parser("refresh", help="atomically rebuild this worktree's code graph")
    refresh_parser.add_argument("path", nargs="?", default=".", help="directory in the worktree (default: cwd)")
    refresh_parser.add_argument("--scope", metavar="RELATIVE_DIR", help="scan within the worktree; omitted reuses saved scope, initially '.'")
    status_parser = commands.add_parser("status", help="report missing/stale/fresh as JSON without rebuilding")
    status_parser.add_argument("path", nargs="?", default=".")
    ensure_parser = commands.add_parser("ensure", help="reuse a fresh graph or rebuild once before returning fresh JSON")
    ensure_parser.add_argument("path", nargs="?", default=".")
    export_parser = commands.add_parser("export", help="export only a fresh graph to its unique generated vault")
    export_parser.add_argument("path", nargs="?", default=".")
    export_parser.add_argument("--open", action="store_true", help="register the generated vault and open its report in Obsidian")
    for command_parser in (refresh_parser, status_parser, ensure_parser, export_parser):
        command_parser.add_argument("--once", action="store_true", help="permit only this operation without changing repository preference; session off still wins")
    memory_cli.add_parser(commands)
    knowledge_hooks.add_parser(commands)
    arguments = parser.parse_args()
    if arguments.command == "memory":
        return memory_cli.run(arguments)
    if arguments.command == "hooks":
        return knowledge_hooks.run(arguments)
    if arguments.command == "policy":
        try:
            policy = get_policy(arguments.path)
        except PolicyError as error:
            if not error.not_repository:
                raise
            policy = {"state": "not_repository", "saved_mode": None, "effective_mode": "off",
                      "worktree_root": None, "reason": str(error)}
        print(json.dumps(policy, indent=2))
        return 0
    if arguments.command == "configure":
        print(json.dumps(configure(arguments.path, arguments.mode), indent=2))
        return 0
    if arguments.command == "ensure":
        print(json.dumps(ensure(arguments.path, once=arguments.once), indent=2))
        return 0
    policy = require(arguments.path, capability="obsidian" if arguments.command == "export" else "graph",
                     once=arguments.once)
    root = Path(policy["worktree_root"])
    with locked(root, create=arguments.command == "refresh") as output:
        require(root, capability="obsidian" if arguments.command == "export" else "graph", once=arguments.once)
        with workspace(output) as scratch:
            if arguments.command == "refresh":
                return refresh(root, output, scratch, arguments.scope,
                               authorize=lambda: require(root, capability="graph", once=arguments.once))
            if arguments.command == "export":
                return export(root, output, scratch, arguments.open)
            status = inspect(root, output, scratch)
            print(json.dumps(status, indent=2))
            return {"fresh": 0, "stale": 3, "missing": 4}[status["state"]]


def entrypoint():
    try:
        upstream.assert_versions()
        sys.exit(main())
    except KeyboardInterrupt:
        print("graphify-worktree: interrupted; unpublished graph discarded", file=sys.stderr)
        sys.exit(130)
    except PolicyError as error:
        policy = error.policy or {"state": "not_repository" if error.not_repository else "unconfigured",
                                  "saved_mode": None, "effective_mode": "off", "worktree_root": None}
        print(json.dumps({**policy, "reason": str(error)}, indent=2))
        sys.exit(5)
    except (Error, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f"graphify-worktree: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
