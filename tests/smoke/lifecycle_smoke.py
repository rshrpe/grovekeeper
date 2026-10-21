"""Real Git/MCP lifecycle checks using disposable checkouts and hook scripts."""
import hashlib
import json
import subprocess
import sys

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from graphify_worktree import cli
from graphify_worktree.policy import PolicyError
from support import WRAPPER, Smoke, command, git, init_repo, server, wrapper_json

SMOKE = Smoke('lifecycle-smoke')
passed = SMOKE.passed


def graph_path(root):
    return root / 'graphify-out/graph.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status(root, expected=0):
    return wrapper_json('status', root, expected=expected)


def ensure(root):
    return wrapper_json('ensure', root)


def generation(root):
    return json.loads((root / 'graphify-out/.graphify-worktree.json').read_text())['extracted_at']


def write_hook(path, event, log, exit_code=0):
    path.write_text('#!' + sys.executable + '\n'
                    'import json, os, sys\n'
                    f'with open({str(log)!r}, "a") as output:\n'
                    f'    output.write(json.dumps({{"event": {event!r}, "args": sys.argv[1:], "cwd": os.getcwd(), '
                    f'"stdin": sys.stdin.read() if {event!r} == "post-rewrite" else ""}}) + "\\n")\n'
                    f'raise SystemExit({exit_code})\n')
    path.chmod(0o755)


async def exercise(base):
    root = base / 'main'
    root.mkdir()
    init_repo(root, 'lifecycle-smoke')
    source = root / 'quote.py'
    source.write_text('def primary_quote():\n    return 100\n')
    git(root, 'add', 'quote.py')
    git(root, 'commit', '-m', 'Initial lifecycle source')
    command(WRAPPER, 'configure', 'graph', root)

    async with stdio_client(server()) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()

            async def node(tree, label, error=False):
                result = await session.call_tool('get_node', {'project_path': str(tree), 'label': label})
                assert result.is_error == error, result
                return '\n'.join(c.text for c in result.content if hasattr(c, 'text'))

            assert not graph_path(root).exists()
            assert 'primary_quote' in await node(root, 'primary_quote')
            assert status(root)['state'] == 'fresh'
            output_memory = root / 'graphify-out/memory/private.py'
            output_memory.parent.mkdir()
            output_memory.write_text('def must_not_be_indexed():\n    return 999\n')
            first_generation = generation(root)
            reused = ensure(root)
            assert reused['rebuilt'] is False and generation(root) == first_generation
            sibling = base / 'sibling'
            git(root, 'worktree', 'add', '-b', 'sibling', sibling)
            assert not (sibling / 'graphify-out').exists()
            assert 'primary_quote' in await node(sibling, 'primary_quote')
            sibling_hash = digest(graph_path(sibling))
            passed('First MCP query initializes each opted-in worktree; fresh ensure reuses its generation')

            source.write_text('def externally_edited_quote():\n    return 200\n')
            assert status(root, 3)['state'] == 'stale'
            assert 'externally_edited_quote' in await node(root, 'externally_edited_quote')
            assert 'primary_quote' not in {n.get('label', '').removesuffix('()') for n in json.loads(graph_path(root).read_text())['nodes']}
            assert digest(graph_path(sibling)) == sibling_hash
            assert 'must_not_be_indexed' not in {n.get('label', '').removesuffix('()') for n in json.loads(graph_path(root).read_text())['nodes']}
            assert output_memory.read_text() == 'def must_not_be_indexed():\n    return 999\n'
            passed('External uncommitted edits rebuild before an MCP answer without touching sibling graphs')

            source.write_text('def concurrent_quote():\n    return 300\n')
            workers = [subprocess.Popen([str(WRAPPER), 'ensure', str(root)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True) for _ in range(2)]
            outcomes = []
            for worker in workers:
                out, err = worker.communicate(timeout=180)
                assert worker.returncode == 0, (out, err)
                outcomes.append(json.loads(out))
            assert sorted(item['rebuilt'] for item in outcomes) == [False, True]
            assert 'concurrent_quote' in await node(root, 'concurrent_quote')
            passed('Concurrent ensure callers coalesce into one rebuild')

            good = digest(graph_path(root))
            original = source.read_text()
            source.unlink()
            await node(root, 'concurrent_quote', error=True)
            assert digest(graph_path(root)) == good
            source.write_text(original)
            passed('Failed extraction refuses MCP answers while preserving the last good graph')

            core = cli
            try:
                with core.guarded_graph(root):
                    source.write_text('def changed_during_query():\n    return 400\n')
            except core.Error:
                pass
            else:
                raise AssertionError('A source-changing query was allowed to return its result')
            assert digest(graph_path(root)) == good
            assert 'changed_during_query' in await node(root, 'changed_during_query')
            try:
                with core.guarded_graph(root):
                    command(WRAPPER, 'configure', 'off', root)
            except PolicyError:
                pass
            else:
                raise AssertionError('A query survived participation being disabled while it ran')
            command(WRAPPER, 'configure', 'graph', root)
            passed('Post-query source and participation checks reject results invalidated during computation')

            custom = root / '.custom-hooks'
            custom.mkdir()
            (root / '.gitignore').write_text('.custom-hooks/\n')
            log = root / '.git/original-hook.log'
            for event in ('pre-commit', 'post-checkout', 'post-merge', 'post-rewrite', 'post-commit'):
                write_hook(custom / event, event, log)
            original_hooks = {p.name: p.read_bytes() for p in custom.iterdir()}
            git(root, 'config', 'core.hooksPath', '.custom-hooks')
            command(WRAPPER, 'hooks', 'install', root)
            installed_path = git(root, 'config', '--local', '--get', 'core.hooksPath').stdout.strip()
            assert installed_path != '.custom-hooks'
            assert {p.name: p.read_bytes() for p in custom.iterdir()} == original_hooks
            command(WRAPPER, 'hooks', 'install', root)
            before_event = digest(graph_path(root))
            git(root, 'switch', '-c', 'hooked-feature')
            marker = root / 'graphify-out/.graphify-worktree-event.json'
            assert json.loads(marker.read_text())['event'] == 'post-checkout'
            assert digest(graph_path(root)) == before_event, 'Git hook must not rebuild'
            assert status(root, 3)['state'] == 'stale'
            assert 'changed_during_query' in await node(root, 'changed_during_query')
            git(root, 'add', 'quote.py', '.gitignore')
            git(root, 'commit', '-m', 'Record external source change')
            assert json.loads(marker.read_text())['event'] == 'post-commit'
            before_amend = git(root, 'rev-parse', 'HEAD').stdout.strip()
            git(root, 'commit', '--amend', '-m', 'Amended external source change')
            assert json.loads(marker.read_text())['event'] == 'post-rewrite'
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            rewrite = [row for row in entries if row['event'] == 'post-rewrite'][-1]
            assert rewrite['args'] == ['amend'] and before_amend in rewrite['stdin']
            assert 'changed_during_query' in await node(root, 'changed_during_query')
            passed('Checkout/commit/rewrite hooks invalidate lazily and preserve original hook files, arguments and stdin')

            upstream = base / 'upstream'
            git(root, 'worktree', 'add', '-b', 'upstream', upstream)
            assert not (upstream / 'graphify-out').exists(), 'Worktree event must not create a graph'
            (upstream / 'upstream.py').write_text('def upstream_symbol():\n    return 1\n')
            git(upstream, 'add', 'upstream.py')
            git(upstream, 'commit', '-m', 'Upstream source')
            (root / 'local.py').write_text('def local_symbol():\n    return 2\n')
            git(root, 'add', 'local.py')
            git(root, 'commit', '-m', 'Local source')
            git(root, 'rebase', 'upstream')
            assert json.loads(marker.read_text())['event'] == 'post-rewrite'
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            assert any(row['event'] == 'post-rewrite' and row['args'] == ['rebase'] and row['stdin'] for row in entries)
            assert 'upstream_symbol' in await node(root, 'upstream_symbol')
            assert 'local_symbol' in await node(root, 'local_symbol')
            merge_tree = base / 'merge-tree'
            git(root, 'worktree', 'add', '-b', 'merge-source', merge_tree)
            (merge_tree / 'merged.py').write_text('def merged_symbol():\n    return 3\n')
            git(merge_tree, 'add', 'merged.py')
            git(merge_tree, 'commit', '-m', 'Merged source')
            before_merge_graph = digest(graph_path(root))
            git(root, 'merge', '--ff-only', 'merge-source')
            assert json.loads(marker.read_text())['event'] == 'post-merge'
            assert digest(graph_path(root)) == before_merge_graph
            assert 'merged_symbol' in await node(root, 'merged_symbol')
            passed('Actual rebase and merge events invalidate; subsequent MCP queries use the resulting local checkout')

            write_hook(custom / 'pre-commit', 'pre-commit', log, exit_code=7)
            head = git(root, 'rev-parse', 'HEAD').stdout
            failed_commit = git(root, 'commit', '--allow-empty', '-m', 'Must be blocked by original hook', expected=None)
            assert failed_commit.returncode != 0 and git(root, 'rev-parse', 'HEAD').stdout == head
            before_disabled = marker.read_bytes()
            command(WRAPPER, 'configure', 'off', root)
            git(root, 'switch', '-c', 'knowledge-off')
            assert marker.read_bytes() == before_disabled
            await node(root, 'merged_symbol', error=True)
            command(WRAPPER, 'hooks', 'uninstall', root)
            assert git(root, 'config', '--local', '--get', 'core.hooksPath').stdout.strip() == '.custom-hooks'
            assert (custom / 'post-rewrite').read_bytes() == original_hooks['post-rewrite']
            passed('Original hook failures remain effective; disabled knowledge skips events; uninstall restores hooksPath')

            command(WRAPPER, 'configure', 'graph', root)
            command(WRAPPER, 'hooks', 'install', root)
            installed_path = git(root, 'config', '--local', '--get', 'core.hooksPath').stdout.strip()
            git(root, 'config', 'core.hooksPath', '.user-changed-hooks')
            assert command(WRAPPER, 'hooks', 'uninstall', root, expected=None).returncode != 0
            assert git(root, 'config', '--local', '--get', 'core.hooksPath').stdout.strip() == '.user-changed-hooks'
            git(root, 'config', 'core.hooksPath', installed_path)
            command(WRAPPER, 'hooks', 'uninstall', root)
            assert git(root, 'config', '--local', '--get', 'core.hooksPath').stdout.strip() == '.custom-hooks'
            passed('Uninstall refuses to overwrite hooksPath changes made outside the harness')


if __name__ == '__main__':
    SMOKE.run(exercise, 'knowledge-lifecycle-smoke-')
