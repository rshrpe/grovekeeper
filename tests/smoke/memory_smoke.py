"""Exercise shared memory through real CLI/MCP using disposable repositories."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from graphify_worktree.memory import handle
from graphify_worktree.policy import PolicyError

BIN = Path(os.environ.get('GRAPHIFY_WORKTREE_BIN') or Path(sys.executable).parent)
WRAPPER = BIN / 'graphify-worktree'
MCP = BIN / 'graphify-worktree-mcp'
VAULT = None  # set by isolate_vaults()
RESULT = Path(__file__).with_name('memory-smoke-result.json')
CREATED = set()
CHECKS = []


def command(*args, payload=None, expected=0, env=None):
    result = subprocess.run([str(a) for a in args], input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


def git(root, *args):
    return command('git', '-C', root, *args).strip()


def memory(action, root, payload=None, expected=0, env=None, **options):
    args = [WRAPPER, 'memory', action, root]
    if payload is not None:
        args += ['--input-file', '-']
    for name, value in options.items():
        args.append('--' + name.replace('_', '-'))
        if not isinstance(value, bool):
            args.append(str(value))
    output = command(*args, payload=payload, expected=expected, env=env)
    return json.loads(output) if expected == 0 else output


def passed(message):
    CHECKS.append(message)
    print('PASS:', message, flush=True)


def isolate_vaults(folder):
    """Point both vault roots into the disposable folder; never the user's vaults."""
    os.environ['WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT'] = str(folder / 'vaults/Graphify')
    global VAULT
    os.environ['WORKTREE_KNOWLEDGE_MEMORY_VAULT'] = str(folder / 'vaults/Engineering')
    VAULT = folder / 'vaults/Engineering'


def server(command):
    vaults = {key: os.environ[key] for key in ('WORKTREE_KNOWLEDGE_GRAPH_VAULT_ROOT', 'WORKTREE_KNOWLEDGE_MEMORY_VAULT')}
    return StdioServerParameters(command=str(command), env={**get_default_environment(), **vaults})


def write_payload(kind, title, content, status='proposed'):
    return {'kind': kind, 'title': title, 'content': content, 'status': status,
            'source': 'Disposable integration-smoke scenario', 'recorded_by': 'manual',
            'confirmed': True, 'confirmation': 'Authorized disposable smoke note, removed after verification.'}


def transition(note_id, **extra):
    return {'id': note_id, 'source': 'Explicit lifecycle smoke approval', 'recorded_by': 'manual',
            'confirmed': True, 'confirmation': 'Authorized lifecycle smoke transition.', **extra}


def saved(root, payload):
    note = memory('save', root, payload)['note']
    path = Path(note['path'])
    assert path.is_relative_to(VAULT / 'Projects') and path.parent.name == 'Memory'
    CREATED.add(path.parent.parent)
    return note


def ids(result):
    return {note['id'] for note in result['notes']}


async def exercise(base):
    root = base / 'main'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.email', 'memory-smoke@localhost')
    git(root, 'config', 'user.name', 'Memory smoke')
    (root / 'quote.py').write_text('def quote_total(amount):\n    return amount * 2\n')
    git(root, 'add', 'quote.py')
    git(root, 'commit', '-m', 'Memory smoke source')
    alternate = base / 'alternate'
    git(root, 'worktree', 'add', '-b', 'alternate', alternate)
    isolated_vault = base / 'isolated-vault'
    for mode in (None, 'graph'):
        if mode:
            command(WRAPPER, 'configure', mode, root)
        try:
            handle('search', root, {'query': 'retry'}, vault=isolated_vault)
        except PolicyError:
            pass
        else:
            raise AssertionError('Memory read without Obsidian consent')
        assert not isolated_vault.exists()
    command(WRAPPER, 'configure', 'obsidian', root)
    assert handle('search', root, {'query': 'retry'}, vault=isolated_vault)['notes'] == []
    assert not isolated_vault.exists(), 'Empty recall must not create a vault or locks'
    refused = write_payload('decision', 'Unapproved', 'Never save this without approval')
    refused['confirmed'] = False
    try:
        handle('save', root, refused, vault=isolated_vault)
    except ValueError:
        pass
    else:
        raise AssertionError('Unconfirmed memory was persisted')
    assert not isolated_vault.exists()
    passed('Consent and explicit confirmation gate storage; empty recall is read-only')

    original = saved(root, write_payload('decision', 'Payment retry ceiling', 'Use three payment retry attempts.'))
    fact = saved(root, write_payload('fact', 'Payment retry accounting', 'Record payment retry counters in integer columns.', 'accepted'))
    lesson = saved(alternate, write_payload('lesson', 'Payment retry timeout lesson', 'Avoid unbounded payment retry loops.'))
    provenance = original['provenance']
    assert provenance['branch'] == 'main' and provenance['commit'] == git(root, 'rev-parse', 'HEAD')
    assert provenance['worktree'] == str(root) and provenance['source'] == 'Disposable integration-smoke scenario'
    assert provenance['recorded_by'] == 'manual' and provenance['created_at']
    assert original['status'] == 'proposed'
    assert Path(original['path']).parent == Path(lesson['path']).parent
    assert ids(memory('search', root, query='payment retry')) == {original['id'], fact['id']}
    assert ids(memory('search', alternate, query='payment retry')) == {lesson['id'], fact['id']}
    assert not memory('get', alternate, id=original['id'])['note']['applicable']
    assert not memory('search', root, query='unrelated zebras')['notes']
    passed('Worktrees share accepted facts while branch proposals and lexical recall stay scoped')

    git(alternate, 'switch', '--detach')
    detached = saved(alternate, write_payload('lesson', 'Detached diagnostic finding', 'Detached diagnostic memory remains local.'))
    assert detached['provenance']['branch'] is None
    assert detached['id'] in ids(memory('search', alternate, query='detached diagnostic'))
    assert detached['id'] not in ids(memory('search', root, query='detached diagnostic'))
    git(alternate, 'switch', 'alternate')
    assert detached['id'] not in ids(memory('search', alternate, query='detached diagnostic'))
    assert not memory('get', alternate, id=detached['id'])['note']['applicable']
    passed('Detached proposals apply only to their originating checkout and commit')

    other = base / 'unrelated'
    other.mkdir()
    git(other, 'init', '-b', 'main')
    command(WRAPPER, 'configure', 'obsidian', other)
    assert not memory('search', other, query='payment retry')['notes']
    memory('get', other, id=original['id'], expected=1)
    memory('get', root, id='../outside', expected=1)
    accepted = memory('accept', root, transition(original['id']))['note']
    assert accepted['status'] == 'accepted' and accepted['content'] == original['content']
    assert accepted['provenance'] == original['provenance']
    assert original['id'] in ids(memory('search', alternate, query='payment retry'))
    passed('Repository isolation and explicit acceptance preserve origin provenance')

    replacement = saved(root, write_payload('decision', 'Revised payment retry ceiling', 'Use five payment retry attempts.', 'accepted'))
    before_bad = Path(original['path']).read_bytes()
    memory('supersede', root, transition(original['id'], replacement_id=original['id']), expected=1)
    memory('supersede', root, transition(original['id'], replacement_id=lesson['id']), expected=1)
    assert Path(original['path']).read_bytes() == before_bad
    retired = memory('supersede', root, transition(original['id'], replacement_id=replacement['id']))['note']
    assert retired['status'] == 'superseded' and not retired['applicable']
    assert retired['content'] == original['content'] and retired['provenance'] == original['provenance']
    assert replacement['id'] in Path(original['path']).read_text()
    current = ids(memory('search', root, query='payment retry'))
    assert original['id'] not in current and replacement['id'] in current
    historical = memory('search', root, query='payment retry', include_history=True)
    assert {original['id'], lesson['id']}.issubset(ids(historical))
    assert all(not n['applicable'] for n in historical['notes'] if n['id'] in {original['id'], lesson['id']})
    memory('supersede', root, transition(original['id'], replacement_id=fact['id']), expected=1)
    passed('Supersession retains original content/history and removes stale knowledge from default recall')

    notes_dir = Path(original['path']).parent
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in notes_dir.glob('*.md')}
    command(WRAPPER, 'configure', 'graph', root)
    memory('search', root, query='payment retry', expected=5)
    memory('save', root, write_payload('fact', 'Blocked', 'Do not store'), expected=5)
    command(WRAPPER, 'configure', 'obsidian', root)
    memory('search', root, query='payment retry', once=True, expected=5,
           env={**os.environ, 'HARNESS_KNOWLEDGE_MODE': 'off'})
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in notes_dir.glob('*.md')}
    passed('Graph-only and session-off deny memory without changing notes')

    async with stdio_client(server(MCP)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            args = {'project_path': str(root), 'query': 'payment retry'}
            result = await session.call_tool('memory_search', args)
            assert not result.is_error, result
            payload = json.loads(result.content[0].text)
            assert replacement['id'] in ids(payload) and original['id'] not in ids(payload)
            got = await session.call_tool('memory_get', {'project_path': str(root), 'id': replacement['id']})
            assert not got.is_error and 'five payment retry attempts' in str(got.content)
            await session.call_tool('knowledge_session', {'mode': 'off'})
            assert (await session.call_tool('memory_search', {**args, 'allow_once': True})).is_error
            write_args = {'project_path': str(root), **write_payload('fact', 'Blocked MCP', 'Do not store'), 'allow_once': True}
            assert (await session.call_tool('memory_save', write_args)).is_error
            await session.call_tool('knowledge_session', {'mode': 'default'})
            assert not (await session.call_tool('memory_search', args)).is_error
            malformed = await session.call_tool('memory_save', {
                'project_path': str(root), **write_payload('fact', 'Bad bool', 'Do not store'), 'confirmed': 'true',
            })
            assert malformed.is_error
    assert not (root / 'graphify-out').exists(), 'Memory must not depend on or build a graph'
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in notes_dir.glob('*.md')}
    passed('Managed MCP recalls without a graph and enforces session and payload controls')


def main():
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'checks': CHECKS}
    try:
        with tempfile.TemporaryDirectory(prefix='project-memory-smoke-') as folder, \
                tempfile.TemporaryDirectory(prefix='worktree-knowledge-vaults-') as vaults:
            # Outside every disposable repository so vault files never enter a scan.
            isolate_vaults(Path(vaults).resolve())
            asyncio.run(exercise(Path(folder).resolve()))
        report['passed'] = True
    except BaseException as error:
        report.update(passed=False, error=repr(error))
        raise
    finally:
        for directory in CREATED:
            assert directory.is_relative_to(VAULT / 'Projects')
            if directory.exists():
                shutil.rmtree(directory)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        RESULT.write_text(json.dumps(report, indent=2) + '\n')
        print('Artifact:', RESULT)


if __name__ == '__main__':
    main()
