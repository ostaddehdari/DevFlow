import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import ROOT
from devflow_core.continuity import Continuity
from devflow_core.contracts import Contracts, DevFlowError


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def store(settings_path, contracts):
    root = settings_path.parent.parent
    folder = settings_path.parent
    (folder / 'project.design.json').write_bytes((ROOT / '.ai-project/project.design.json').read_bytes())
    for name in ('state.json', 'resume.bundle.json'):
        path = folder / name
        value = json.loads(path.read_text())
        value['current'].update(stage_id='STAGE-01', work_id='WORK-01-01')
        path.write_text(json.dumps(value))
    (root / '.gitignore').write_text('.devflow/\n')
    subprocess.run(['git', 'init', '-b', 'test-stage2', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=DevFlow Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-m', 'fixture'], check=True, capture_output=True)
    return Continuity(root, contracts, Clock())


def identity(lease, owner='owner'):
    return owner, lease['execution']['execution_id'], lease['fence']


def lease(store):
    store.initialize()
    value = store.acquire('owner', 60)
    return identity(value)


def test_read_does_not_initialize_and_init_preserves_identity(store):
    assert store.status()['initialized'] is False
    assert not store.path.exists()
    assert store.initialize()['changed']
    assert not store.initialize()['changed']
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.status()['event_count'] == 1


def test_live_lease_exclusive_and_renewal(store):
    owned = lease(store)
    with pytest.raises(DevFlowError, match='live lease'):
        store.acquire('another')
    store.clock.value += 30
    renewed = store.renew(*owned)
    assert renewed['execution']['heartbeat'] != renewed['execution']['started_at']
    store.clock.value += 31
    assert store.status()['lease']['execution']['status'] == 'ACTIVE'
    assert store.release(*owned)['execution']['status'] == 'DONE'
    newer = store.acquire('other', 60)
    assert newer['fence'] > owned[2]


@pytest.mark.parametrize('mutation', ['renew', 'release', 'prepare', 'start', 'finish', 'resolve', 'checkpoint'])
def test_expired_owner_is_fenced(store, mutation):
    owned = lease(store)
    store.prepare(*owned, 'example', {'action': 'test'})
    store.clock.value += 61
    with pytest.raises(DevFlowError, match='stale|expired'):
        store.renew(*owned)
    replacement = store.acquire('replacement', 60)
    assert replacement['fence'] == owned[2] + 1
    args = {'prepare': ('next', {'action': 'other'}), 'start': ('example',),
            'finish': ('example', 'SUCCEEDED', 'test evidence'),
            'resolve': ('example', 'NO_EFFECT', 'test evidence')}.get(mutation, ())
    with pytest.raises(DevFlowError, match='stale|expired'):
        getattr(store, mutation)(*owned, *args)


@pytest.mark.parametrize('ttl', [0, 59, 86401, True, 60.5])
def test_lease_ttl_contract(store, ttl):
    store.initialize()
    with pytest.raises(DevFlowError):
        store.acquire('owner', ttl)


def test_backward_clock_stops_writes_and_resume(store):
    owned = lease(store)
    store.clock.value -= 1
    with pytest.raises(DevFlowError, match='backwards'):
        store.renew(*owned)
    assert store.resume_plan()['decision'] == 'HUMAN_DECISION'


def test_idempotency_key_binds_intent_and_terminal_result(store):
    owned = lease(store)
    first = store.prepare(*owned, 'task', {'kind': 'example', 'a': 1})
    same = store.prepare(*owned, 'task', {'a': 1, 'kind': 'example'})
    assert first['changed'] and not same['changed']
    with pytest.raises(DevFlowError, match='another intent'):
        store.prepare(*owned, 'task', {'a': 2})
    store.start(*owned, 'task')
    store.finish(*owned, 'task', 'SUCCEEDED', 'log://observed-result')
    assert store.prepare(*owned, 'task', {'a': 1, 'kind': 'example'})['operation']['status'] == 'SUCCEEDED'
    with pytest.raises(DevFlowError, match='retry'):
        store.start(*owned, 'task')
    assert 'task' in store.resume_plan()['do_not_repeat']


def test_unknown_result_blocks_all_new_effects_until_manual_evidence(store):
    owned = lease(store)
    store.prepare(*owned, 'a', {'kind': 'a'})
    store.prepare(*owned, 'b', {'kind': 'b'})
    store.start(*owned, 'a')
    with pytest.raises(DevFlowError, match='started operation'):
        store.release(*owned)
    store.clock.value += 61
    assert store.status()['operations'][0]['status'] == 'UNKNOWN'
    newer = identity(store.acquire('replacement', 60), 'replacement')
    with pytest.raises(DevFlowError, match='unresolved'):
        store.start(*newer, 'b')
    with pytest.raises(DevFlowError, match='Unresolved'):
        store.checkpoint(*newer)
    with pytest.raises(DevFlowError, match='evidence'):
        store.resolve(*newer, 'a', 'NO_EFFECT', '')
    store.resolve(*newer, 'a', 'NO_EFFECT', 'log://verified-no-effect')
    store.start(*newer, 'a')
    assert store.status()['operations'][0]['attempt'] == 2
    store.finish(*newer, 'a', 'SUCCEEDED', 'log://complete')
    store.start(*newer, 'b')


def test_failed_result_is_not_automatically_retryable(store):
    owned = lease(store)
    store.prepare(*owned, 'a', {'kind': 'a'})
    store.start(*owned, 'a')
    store.finish(*owned, 'a', 'FAILED', 'log://failure')
    with pytest.raises(DevFlowError):
        store.start(*owned, 'a')
    assert store.resume_plan()['decision'] == 'HUMAN_DECISION'
    store.resolve(*owned, 'a', 'NO_EFFECT', 'log://operator-verified-no-effect')
    assert store.start(*owned, 'a')['operation']['attempt'] == 2


@pytest.mark.parametrize('intent', [{}, {'token': 'DO_NOT_PRINT'}, {'payload': {'password': 'DO_NOT_PRINT'}}, {'a': float('nan')}, {'data': 'x' * 65537}])
def test_rejects_unsafe_or_invalid_intent(store, intent):
    owned = lease(store)
    with pytest.raises(DevFlowError) as error:
        store.prepare(*owned, 'a', intent)
    assert 'DO_NOT_PRINT' not in str(error.value)
    assert store.status()['operations'] == []


def test_checkpoint_is_transactional_and_resume_checks_git(store):
    owned = lease(store)
    original = {p: p.read_bytes() for p in (store.root / '.ai-project').rglob('*.json')}
    with patch.object(store, '_event', side_effect=RuntimeError('simulated event failure')):
        with pytest.raises(RuntimeError):
            store.checkpoint(*owned)
    assert store.status()['latest_checkpoint_id'] is None
    assert all(p.read_bytes() == data for p, data in original.items())
    created = store.checkpoint(*owned)
    identifier = created['checkpoint_id']
    assert store.snapshot(identifier)['snapshot'] == created['snapshot']
    store.release(*owned)
    assert store.resume_plan()['decision'] == 'READY'
    subprocess.run(['git', '-C', str(store.root), '-c', 'user.name=DevFlow Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-m', 'diverged'], check=True, capture_output=True)
    assert store.resume_plan()['decision'] == 'HUMAN_DECISION'
    assert 'Git differs from checkpoint' in store.resume_plan()['reasons']


def test_dirty_checkout_and_document_divergence_are_reported(store):
    owned = lease(store)
    store.checkpoint(*owned)
    target = store.root / '.ai-project/state.json'
    document = json.loads(target.read_text())
    document['next_action'] = 'changed'
    target.write_text(json.dumps(document))
    with pytest.raises(DevFlowError, match='clean'):
        store.checkpoint(*owned)
    plan = store.resume_plan()
    assert plan['decision'] == 'HUMAN_DECISION'
    assert 'Project documents differ from checkpoint' in plan['reasons']


def test_corrupt_database_and_snapshot_are_not_reset(store):
    owned = lease(store)
    store.checkpoint(*owned)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE checkpoints SET snapshot='{}'")
    with pytest.raises(DevFlowError, match='snapshot'):
        store.status()
    before = store.path.read_bytes()
    with pytest.raises(DevFlowError):
        store.snapshot()
    assert store.path.read_bytes() == before


def test_storage_symlink_is_rejected(store, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    store.path.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DevFlowError, match='symlink'):
        store.initialize()
    assert list(outside.iterdir()) == []


def _acquire_worker(root, contracts_path, gate, results):
    item = Continuity(Path(root), Contracts(Path(contracts_path)))
    gate.wait(timeout=15)
    try:
        item.acquire('pid-' + str(os.getpid()), 60)
        results.put(True)
    except DevFlowError:
        results.put(False)


def test_concurrent_processes_have_one_lease_owner(store):
    store = Continuity(store.root, store.contracts)
    store.initialize()
    ctx = multiprocessing.get_context('spawn')
    gate, results = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_acquire_worker, args=(str(store.root), str(ROOT / 'contracts'), gate, results)) for _ in range(5)]
    for worker in workers:
        worker.start()
    gate.set()
    for worker in workers:
        worker.join(timeout=20)
        if worker.is_alive():
            worker.kill()
            worker.join()
        assert worker.exitcode == 0
    assert sum(results.get(timeout=3) for _ in workers) == 1


@pytest.mark.parametrize('point', ['uncommitted', 'prepared', 'after_effect'])
def test_sigkill_does_not_make_unknown_effect_retryable(store, point):
    owned = lease(store)
    code = r'''
import os,signal,sys
from pathlib import Path
from devflow_core.continuity import Continuity
from devflow_core.contracts import Contracts
root, catalog, owner, execution, fence, point = sys.argv[1:]
store=Continuity(Path(root),Contracts(Path(catalog)),lambda:1000.0)
identity=(owner,execution,int(fence))
if point == 'uncommitted':
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO operations(operation_key,intent_hash,intent,status,updated) VALUES ('never','hash','{}','PREPARED',1000)")
        os.kill(os.getpid(),signal.SIGKILL)
store.prepare(*identity,'effect',{'kind':'test-file'})
if point == 'after_effect':
    store.start(*identity,'effect')
    (Path(root)/'.devflow'/'effect.txt').write_text('one effect')
os.kill(os.getpid(),signal.SIGKILL)
'''
    result = subprocess.run([sys.executable, '-c', code, str(store.root), str(ROOT / 'contracts'), owned[0], owned[1], str(owned[2]), point],
                            env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), capture_output=True, timeout=20)
    assert result.returncode == -signal.SIGKILL, result.stderr
    # An explicit operator mutation recovers any SQLite rollback journal.
    store.clock.value = 1061
    newer = identity(store.acquire('replacement', 60), 'replacement')
    items = store.status()['operations']
    if point == 'uncommitted':
        assert items == []
    elif point == 'prepared':
        assert items[0]['status'] == 'PREPARED'
        assert not (store.path.parent / 'effect.txt').exists()
        store.start(*newer, 'effect')
    else:
        assert items[0]['status'] == 'UNKNOWN'
        with pytest.raises(DevFlowError, match='retry'):
            store.start(*newer, 'effect')
        assert (store.path.parent / 'effect.txt').read_text() == 'one effect'
        store.resolve(*newer, 'effect', 'SUCCEEDED', 'log://operator-observed-test-file')
        with pytest.raises(DevFlowError, match='retry'):
            store.start(*newer, 'effect')


def test_expiration_during_transaction_rolls_back(store):
    owned = lease(store)
    original_event = store._event
    def slow_event(*args, **kwargs):
        original_event(*args, **kwargs)
        store.clock.value = 1061
    with patch.object(store, '_event', side_effect=slow_event):
        with pytest.raises(DevFlowError, match='before transaction commit'):
            store.prepare(*owned, 'a', {'kind': 'test'})
    assert store.status()['operations'] == []


def test_checkpoint_rejects_divergent_document_identities(store):
    owned = lease(store)
    with patch.object(store, '_git', return_value={'branch': 'clean', 'head_sha': 'a'*40, 'dirty': False}):
        path = store.root / '.ai-project/resume.bundle.json'
        value = json.loads(path.read_text())
        value['current']['work_id'] = 'WORK-OTHER'
        path.write_text(json.dumps(value))
        with pytest.raises(DevFlowError, match='identities diverge'):
            store.checkpoint(*owned)


def test_cli_continuity_commands_are_bound_to_registry(store):
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
    registry_path = store.root / 'control/registry.json'
    command = [sys.executable, '-m', 'devflow_core.cli', '--root', str(store.root),
               '--contracts', str(ROOT / 'contracts'), '--registry', str(registry_path)]
    def run(*arguments):
        return subprocess.run(command + list(arguments), env=env, capture_output=True, text=True, timeout=20)
    assert run('init').returncode == 0
    status = run('continuity', 'status')
    assert status.returncode == 0 and not json.loads(status.stdout)['initialized']
    assert run('continuity', 'init').returncode == 0
    acquired = run('continuity', 'lease-acquire', '--owner', 'cli-test', '--seconds', '60')
    value = json.loads(acquired.stdout)
    lease_args = ['--owner', 'cli-test', '--execution-id', value['execution']['execution_id'], '--fence', str(value['fence'])]
    prepared = run('continuity', 'prepare', *lease_args, '--key', 'cli-op', '--intent', '{"kind":"test"}')
    assert prepared.returncode == 0
    bad = run('continuity', 'prepare', *lease_args, '--key', 'bad', '--intent', '{"token":"NEVER_OUTPUT"}')
    assert bad.returncode == 1 and 'NEVER_OUTPUT' not in bad.stderr
    assert run('continuity', 'lease-release', *lease_args).returncode == 0


def test_stage2_milestone_preserves_live_health_and_is_repeatable(store):
    import importlib.util
    import shutil
    shutil.copytree(ROOT / 'contracts', store.root / 'contracts')
    folder = store.root / '.ai-project'
    for name in ('state.json', 'resume.bundle.json'):
        path = folder / name
        value = json.loads(path.read_text())
        value['current'].update(stage_id='STAGE-01', work_id='WORK-01-01')
        if name == 'state.json':
            value['current'].update(stage_status='COMPLETED', work_status='COMPLETED')
        else:
            value['current']['status'] = 'COMPLETED'
        value['latest_checkpoint_id'] = 'CP-STAGE01-VERIFIED'
        path.write_text(json.dumps(value))
    checkpoint = json.loads((ROOT / '.ai-project/checkpoints/CP-STAGE01-SOURCE.json').read_text())
    checkpoint['checkpoint_id'] = 'CP-STAGE01-VERIFIED'
    (folder / 'checkpoints').mkdir()
    (folder / 'checkpoints/CP-STAGE01-VERIFIED.json').write_text(json.dumps(checkpoint))
    before_servers = json.loads((folder / 'state.json').read_text())['servers']
    store.initialize()
    spec = importlib.util.spec_from_file_location('record_stage2', ROOT / 'scripts/record_stage2_verification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.record(store.root)['changed']
    state = json.loads((folder / 'state.json').read_text())
    assert state['current']['stage_id'] == 'STAGE-02'
    assert state['current']['stage_status'] == 'COMPLETED'
    assert state['servers'] == before_servers
    before = {p: p.read_bytes() for p in folder.rglob('*.json')}
    assert module.record(store.root)['changed'] is False
    assert all(p.read_bytes() == contents for p, contents in before.items())
