"""graphify-worktree: consent, freshness, refresh, export, memory and hooks from the shell."""

import argparse
import json
import sys
from pathlib import Path

from graphify_worktree import artifacts, dashboard, diff, errors, upstream
from graphify_worktree import hooks, learning, nudge, recall
from graphify_worktree.errors import PolicyError
from graphify_worktree.freshness import inspect
from graphify_worktree.guard import ensure
from graphify_worktree.memory import handle as handle_memory
from graphify_worktree.memory import schema as memory_schema
from graphify_worktree.obsidian_export import export, open_in_obsidian
from graphify_worktree.policy import MODES, configure, get_policy, require
from graphify_worktree.publish import refresh

STATE_EXIT = {"fresh": 0, "stale": 3, "missing": 4}


def emit(payload):
    """The one way a command prints its JSON result (non-ASCII text stays readable)."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def non_negative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def parser():
    parser = argparse.ArgumentParser(
        prog="graphify-worktree",
        description="Local code-only Graphify graphs and isolated generated Obsidian vaults per Git worktree.",
        epilog="Exit codes: 0 success/fresh, 1 operation error, 2 usage error, 3 stale, 4 missing, 5 policy denied, 130 interrupted; recall and nudge always 0. No LLM, daemon or global graph merge.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    policy_parser = commands.add_parser("policy", help="read repository knowledge preference without inspecting artifacts")
    policy_parser.add_argument("path", nargs="?", default=".")
    configure_parser = commands.add_parser("configure", help="save shared repository preference and settings; never build, export, open, or delete artifacts")
    configure_parser.add_argument("mode", nargs="?", metavar="MODE", help="off, graph or obsidian; optional when a setting is given")
    configure_parser.add_argument("path", nargs="?", default=".")
    configure_parser.add_argument("--incremental", choices=("on", "off"), help="incremental rebuilds for every worktree of this repository")
    configure_parser.add_argument("--general-memory", choices=("on", "off"), help="general-scope lessons (General/Memory) in this repository")
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
    export_parser.add_argument("--wiki", action="store_true", help="also write upstream wiki articles to _graphify-worktree/wiki/ in the generated vault")
    for command_parser in (refresh_parser, status_parser, ensure_parser, export_parser):
        command_parser.add_argument("--once", action="store_true", help="permit only this operation without changing repository preference; session off still wins")
    diff_parser = commands.add_parser("diff", help="compare two worktrees' fresh graphs of one repository (nodes by id, edges by endpoints)")
    diff_parser.add_argument("a", help="first worktree")
    diff_parser.add_argument("b", help="second worktree")
    diff_parser.add_argument("--limit", type=non_negative, default=20, help="examples listed per category (default 20)")
    diff_parser.add_argument("--once", action="store_true", help="permit only this comparison; session off still wins")
    memory_schema.add_parser(commands)
    dashboard_parser = commands.add_parser("dashboard", help="obsidian mode: write the Obsidian Bases dashboard of this repository's memory notes; never overwrites a changed copy")
    dashboard_parser.add_argument("path", nargs="?", default=".")
    dashboard_parser.add_argument("--once", action="store_true", help="permit only this operation; session off still wins")
    learning.add_parser(commands)
    hooks.add_parser(commands)
    recall_parser = commands.add_parser("recall", help="session-start hook: print accepted memory titles in obsidian mode; otherwise silent; always exits 0")
    recall_parser.add_argument("path", nargs="?", default=".")
    commands.add_parser("nudge", help="pre-tool hook: read the harness payload on stdin and maybe suggest graph tools (graph consent only); always exits 0")
    return parser


def run_nudge(stdin):
    """A tool call must never fail or be blocked because of the nudge."""
    try:
        text = nudge.respond(json.load(stdin))
    except Exception:
        return 0
    if text:
        print(text)
    return 0


def run_recall(path):
    """A harness session start must never fail or show an error because of recall."""
    try:
        text = recall.session_context(path)
    except Exception:
        return 0
    if text:
        print(text)
    return 0


def configure_arguments(command_parser, arguments):
    """(mode, path, settings) for configure; MODE may be left out when a setting is given."""
    mode, path = arguments.mode, arguments.path
    settings = {name: value == "on" for name, value in (("incremental", arguments.incremental),
                                                         ("general_memory", arguments.general_memory))
                if value is not None}
    if settings and mode is not None and arguments.path == "." and mode not in MODES:
        mode, path = None, mode  # `configure --incremental on PATH`
    elif settings and mode in MODES and arguments.path == "." and Path(mode).is_dir():
        command_parser.error(f"{mode!r} is both a mode and a directory here; write ./{mode} for the directory")
    if mode is None and not settings:
        command_parser.error("configure needs MODE (off, graph or obsidian) or --incremental/--general-memory")
    if mode is not None and mode not in MODES:
        command_parser.error(f"invalid mode {mode!r}: choose off, graph or obsidian")
    return mode, path, settings


def main(argv=None):
    command_parser = parser()
    arguments = command_parser.parse_args(argv)
    if arguments.command == "memory":
        result = handle_memory(arguments.memory_action, arguments.path,
                               memory_schema.payload_from_cli(arguments), once=arguments.once)
        emit(result)
        return 0
    if arguments.command == "diff":
        emit(diff.compare(arguments.a, arguments.b, once=arguments.once, limit=arguments.limit))
        return 0
    if arguments.command == "dashboard":
        emit(dashboard.write(arguments.path, once=arguments.once))
        return 0
    if arguments.command == "learning":
        emit(learning.run(arguments, sys.stdin))
        return 0
    if arguments.command == "hooks":
        result, code = hooks.run(arguments)
        emit(result)
        return code
    if arguments.command == "recall":
        return run_recall(arguments.path)
    if arguments.command == "nudge":
        return run_nudge(sys.stdin)
    if arguments.command == "policy":
        try:
            policy = get_policy(arguments.path)
        except PolicyError as error:
            if not error.not_repository:
                raise
            policy = {"state": "not_repository", "saved_mode": None, "effective_mode": "off",
                      "worktree_root": None, "reason": str(error), "incremental": False, "general_memory": False}
        emit(policy)
        return 0
    if arguments.command == "configure":
        mode, path, settings = configure_arguments(command_parser, arguments)
        emit(configure(path, mode, **settings))
        return 0
    if arguments.command == "ensure":
        emit(ensure(arguments.path, once=arguments.once))
        return 0
    capability = "obsidian" if arguments.command == "export" else "graph"
    root = Path(require(arguments.path, capability=capability, once=arguments.once)["worktree_root"])
    with artifacts.locked(root, create=arguments.command == "refresh") as output:
        require(root, capability=capability, once=arguments.once)  # consent may have changed while queued
        with artifacts.workspace(output) as scratch:
            if arguments.command == "refresh":
                status = refresh(root, output, scratch, arguments.scope,
                                 authorize=lambda: require(root, capability="graph", once=arguments.once))
                emit({key: status[key] for key in ("state", "graph_path", "scope")}
                     | {"code_files": status["code_files"], "vault_path": status["vault_path"]})
                return 0
            if arguments.command == "export":
                result = export(root, output, scratch, wiki=arguments.wiki)
                emit(result)
                if result["state"] != "exported":
                    return STATE_EXIT[result["state"]]
                if arguments.open:
                    open_in_obsidian(Path(result["vault_path"]))
                return 0
            status = inspect(root, output, scratch)
            emit(status)
            return STATE_EXIT[status["state"]]


def report(error):
    """Print an expected failure the way its kind is documented; returns the exit code."""
    if isinstance(error, PolicyError):
        policy = error.policy or {"state": "not_repository" if error.not_repository else "unconfigured",
                                  "saved_mode": None, "effective_mode": "off", "worktree_root": None}
        emit({**policy, "reason": str(error)})
    else:
        print(f"graphify-worktree: {error}", file=sys.stderr)
    return errors.exit_code(error)


def entrypoint():
    try:
        # Hook commands run on every session start or tool call and must never
        # fail it, so they skip the pin check and fall silent on their own errors.
        if sys.argv[1:2] not in (["recall"], ["nudge"]):
            upstream.assert_versions()
        code = main()
    except KeyboardInterrupt:
        print("graphify-worktree: interrupted; unpublished graph discarded", file=sys.stderr)
        code = errors.INTERRUPTED
    except (errors.KnowledgeError, OSError) as error:
        code = report(error)
    sys.exit(code)


if __name__ == "__main__":
    entrypoint()
