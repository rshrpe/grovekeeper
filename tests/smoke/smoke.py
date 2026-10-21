"""Real Git + Graphify + MCP smoke; uses disposable worktrees, no provider APIs.

Failure cases: wrong-worktree routing, stale dirty/deleted code, stale branch HEAD,
failed extraction replacing a good graph, MCP caching old data, colliding exports,
and re-export deleting an authored note. Run after installing graphify-worktree.
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

BIN = Path(os.environ.get('GRAPHIFY_WORKTREE_BIN') or Path(sys.executable).parent)
WRAPPER = BIN / 'graphify-worktree'
MCP = BIN / 'graphify-worktree-mcp'
RESULT = Path(__file__).with_name('smoke-result.json')
CHECKS = []
VAULTS = []


def command(*args, cwd=None, expected=0, env=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


def git(root, *args):
    return command('git', '-C', root, *args)


def status(root, expected=0):
    return json.loads(command(WRAPPER, 'status', root, expected=expected))


def policy(root, **kwargs):
    return json.loads(command(WRAPPER, 'policy', root, **kwargs))


def graph_path(root):
    return root / 'graphify-out/graph.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def labels(root):
    return {n.get('label', '').removesuffix('()') for n in json.loads(graph_path(root).read_text())['nodes']}


def passed(name):
    CHECKS.append(name)
    print('PASS:', name, flush=True)


def isolate_vaults(folder):
    """Point both vault roots into the disposable folder; never the user's vaults."""
    os.environ['WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT'] = str(folder / 'vaults/Graphify')
    os.environ['WORKTREE_KNOWLEDGE_MEMORY_VAULT'] = str(folder / 'vaults/Engineering')


def server(command):
    vaults = {key: os.environ[key] for key in ('WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT', 'WORKTREE_KNOWLEDGE_MEMORY_VAULT')}
    return StdioServerParameters(command=str(command), env={**get_default_environment(), **vaults})


async def exercise(root):
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.email', 'graphify-smoke@localhost')
    git(root, 'config', 'user.name', 'Graphify integration smoke')
    source = root / 'quote.py'
    source.write_text('def primary_quote():\n    return 100\n\ndef run_quote():\n    return primary_quote()\n')
    git(root, 'add', 'quote.py')
    git(root, 'commit', '-m', 'Initial graph smoke')
    # Nested worktree intentionally challenges ancestor scanning exclusions.
    other = root / '.claude/worktrees/alternate'
    git(root, 'worktree', 'add', '-b', 'alternate', other)
    (other / 'quote.py').write_text('def alternate_quote():\n    return 200\n\ndef run_quote():\n    return alternate_quote()\n')
    git(other, 'add', 'quote.py')
    git(other, 'commit', '-m', 'Alternate implementation')
    assert policy(root)['state'] == 'unconfigured'
    assert not (root / 'graphify-out').exists()
    for operation in ('status', 'refresh', 'export'):
        command(WRAPPER, operation, root, expected=5)
    assert not (root / 'graphify-out').exists(), 'Declined use must not create graph files or locks'
    passed('Unconfigured operations fail closed without creating graph artifacts')

    command(WRAPPER, 'configure', 'off', root)
    assert policy(other)['saved_mode'] == 'off'
    command(WRAPPER, 'refresh', root, '--once')
    assert policy(root)['saved_mode'] == 'off', 'One-off build must not enroll repository'
    once_hash = digest(graph_path(root))
    command(WRAPPER, 'status', root, expected=5)
    assert digest(graph_path(root)) == once_hash
    command(WRAPPER, 'configure', 'graph', other)
    assert policy(root)['effective_mode'] == 'graph'
    git(root, 'config', 'extensions.worktreeConfig', 'true')
    git(other, 'config', '--worktree', 'harness.knowledgeMode', 'off')
    assert policy(other)['effective_mode'] == 'graph', 'Consent belongs to common repo config'
    command(WRAPPER, 'export', root, expected=5)
    assert not Path(status(root)['vault_path']).exists()
    env_off = {**os.environ, 'HARNESS_KNOWLEDGE_MODE': 'off'}
    assert policy(root, env=env_off)['effective_mode'] == 'off'
    command(WRAPPER, 'refresh', root, '--once', env=env_off, expected=5)
    assert digest(graph_path(root)) == once_hash
    assert policy(root)['saved_mode'] == 'graph'
    command(WRAPPER, 'configure', 'obsidian', root)
    assert policy(other)['effective_mode'] == 'obsidian'
    passed('Modes share across worktrees; one-off and session opt-out preserve saved consent')
    command(WRAPPER, 'refresh', root)
    command(WRAPPER, 'refresh', other)
    assert 'primary_quote' in labels(root) and 'alternate_quote' not in labels(root)
    assert 'alternate_quote' in labels(other) and 'primary_quote' not in labels(other)
    assert status(root)['state'] == status(other)['state'] == 'fresh'
    passed('Separate graphs; nested worktree excluded from parent scan')
    other_hash = digest(graph_path(other))

    async with stdio_client(server(MCP)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()

            async def node(worktree, name):
                result = await session.call_tool('get_node', {'project_path': str(worktree), 'label': name})
                assert not result.is_error, result
                return '\n'.join(x.text for x in result.content if hasattr(x, 'text'))

            assert 'primary_quote' in await node(root, 'primary_quote')
            assert 'alternate_quote' in await node(other, 'alternate_quote')
            passed('One MCP process routes queries to both explicit worktree roots')

            async def denied(arguments):
                result = await session.call_tool('get_node', arguments)
                assert result.is_error, result
                return result

            await denied({'label': 'primary_quote'})
            await denied({'project_path': '.', 'label': 'primary_quote'})
            await denied({'project_path': str(other.parent), 'label': 'primary_quote'})
            assert not (await session.list_resources()).resources
            command(WRAPPER, 'configure', 'off', root)
            good_hash = digest(graph_path(root))
            await denied({'project_path': str(root), 'label': 'primary_quote'})
            one_off = await session.call_tool('get_node', {
                'project_path': str(root), 'label': 'primary_quote', 'allow_once': True,
            })
            assert not one_off.is_error and 'primary_quote' in str(one_off.content), one_off
            assert policy(root)['saved_mode'] == 'off'
            command(WRAPPER, 'configure', 'graph', root)
            paused = await session.call_tool('knowledge_session', {'mode': 'off'})
            assert not paused.is_error, paused
            await denied({'project_path': str(root), 'label': 'primary_quote', 'allow_once': True})
            assert policy(root)['saved_mode'] == 'graph'
            resumed = await session.call_tool('knowledge_session', {'mode': 'default'})
            assert not resumed.is_error, resumed
            assert 'primary_quote' in await node(root, 'primary_quote')
            assert digest(graph_path(root)) == good_hash
            command(WRAPPER, 'configure', 'obsidian', root)
            passed('Managed MCP gates cached graphs, explicit roots, one-off access and session opt-out')

            source.write_text('def dirty_quote():\n    return 300\n\ndef run_quote():\n    return dirty_quote()\n')
            assert status(root, 3)['state'] == 'stale'
            assert status(other)['state'] == 'fresh'
            command(WRAPPER, 'refresh', root)
            assert 'dirty_quote' in labels(root) and 'primary_quote' not in labels(root)
            assert 'dirty_quote' in await node(root, 'dirty_quote')
            assert digest(graph_path(other)) == other_hash
            passed('Dirty edit refresh removes old symbols; MCP hot-reloads; sibling unchanged')

            good_hash = digest(graph_path(root))
            original = source.read_text()
            source.unlink()
            assert status(root, 3)['state'] == 'stale'
            failure = subprocess.run([str(WRAPPER), 'refresh', str(root)], capture_output=True, text=True, timeout=180)
            assert failure.returncode != 0, 'Empty extraction must not replace a good graph'
            assert digest(graph_path(root)) == good_hash
            source.write_text(original)
            passed('Deletion detected; failed empty extraction preserves last good graph')

            git(root, 'add', 'quote.py')
            git(root, 'commit', '-m', 'Dirty symbol becomes committed')
            assert status(root, 3)['state'] == 'stale'
            command(WRAPPER, 'refresh', root)
            git(root, 'switch', '-c', 'same-files-new-branch')
            assert status(root, 3)['state'] == 'stale'
            command(WRAPPER, 'refresh', root)
            assert status(root)['state'] == 'fresh'
            passed('Commit and same-content branch switch invalidate provenance')

    for tree in (root, other):
        path = Path(status(tree)['vault_path'])
        assert path.is_relative_to(os.environ['WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT']) and not path.exists()
        VAULTS.append(path)
        command(WRAPPER, 'export', tree)
        assert (path / 'graph.canvas').exists()
    assert VAULTS[0] != VAULTS[1]
    first_notes = '\n'.join(p.read_text() for p in VAULTS[0].glob('*.md'))
    second_notes = '\n'.join(p.read_text() for p in VAULTS[1].glob('*.md'))
    assert 'dirty_quote' in first_notes and 'alternate_quote' not in first_notes
    assert 'alternate_quote' in second_notes and 'dirty_quote' not in second_notes
    authored = VAULTS[0] / 'Human note.md'
    authored.write_text('Do not overwrite this authored note.\n')
    command(WRAPPER, 'export', root)
    assert authored.read_text() == 'Do not overwrite this authored note.\n'
    passed('Generated vaults are disjoint; re-export preserves an authored note')

    source.write_text(''.join(f'def bulk_symbol_{i}():\n    return {i}\n\n' for i in range(5001)))
    command(WRAPPER, 'refresh', root)
    command(WRAPPER, 'export', root)
    overview = json.loads((VAULTS[0] / 'graph.canvas').read_text())
    assert len(overview['nodes']) <= 101, 'Large graph must not freeze the Canvas renderer'
    pages = list((VAULTS[0] / '_graphify-worktree').glob('communities-*.canvas'))
    links = []
    for page in pages:
        data = json.loads(page.read_text())
        assert len(data['nodes']) <= 100
        links.extend(n.get('text', '') for n in data['nodes'])
    owned = json.loads((VAULTS[0] / '.graphify_obsidian_manifest.json').read_text())['files']
    for name in owned:
        if name.startswith('_COMMUNITY_'):
            assert f'[[{name}]]' in links, f'Community missing from paged Canvas: {name}'
    assert len(labels(root)) > 5000, 'Overview must not truncate the underlying graph'
    passed('Large Canvas is paged; every community and the full graph remain available')


def main():
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'checks': CHECKS}
    try:
        with tempfile.TemporaryDirectory(prefix='graphify-worktree-smoke-') as folder, \
                tempfile.TemporaryDirectory(prefix='worktree-knowledge-vaults-') as vaults:
            # Outside every disposable repository so vault files never enter a scan.
            isolate_vaults(Path(vaults).resolve())
            asyncio.run(exercise(Path(folder).resolve()))
        report['passed'] = True
    except BaseException as error:
        report.update(passed=False, error=repr(error))
        raise
    finally:
        for vault in VAULTS:
            if vault.exists():
                shutil.rmtree(vault)
            if vault.parent.exists() and not any(vault.parent.iterdir()):
                vault.parent.rmdir()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        RESULT.write_text(json.dumps(report, indent=2) + '\n')
        print('Artifact:', RESULT)


if __name__ == '__main__':
    main()
