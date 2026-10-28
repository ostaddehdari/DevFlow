import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devflow_core.continuity import Continuity
from devflow_core.controlled_execution import BoundedProcess, ReadExecutor
from devflow_core.contracts import DevFlowError
from devflow_core.operation_plans import PlanPolicy
from devflow_core.registry import ControlRegistry
from conftest import ROOT


@pytest.fixture
def setup_read(settings_path, contracts):
    root = settings_path.parent.parent
    settings = json.loads(settings_path.read_text())
    settings['servers']['control']['project_root'] = str(root)
    settings_path.write_text(json.dumps(settings))
    shutil.copytree(ROOT / 'contracts', root / 'contracts')
    (root / 'docs').mkdir()
    content = b'bounded read result\n'
    (root / 'docs' / 'test.txt').write_bytes(content)
    (root / '.gitignore').write_text('.devflow/\n')
    subprocess.run(['git', 'init', '-b', 'stage/04b-read', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=DevFlow Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture'],
                   check=True, capture_output=True)
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    digest = hashlib.sha256(content).hexdigest()
    plan = {'schema_version': '1.0', 'operation_id': 'read-test',
            'project_id': 'devflow', 'server_id': 'control', 'kind': 'filesystem.read',
            'arguments': {'path': 'docs/test.txt'},
            'preconditions': {'git_head_sha': head, 'git_branch': 'stage/04b-read',
                              'existing_sha256': digest},
            'postconditions': {'sha256': digest}}
    continuity = Continuity(root, contracts)
    continuity.initialize()
    return root, plan, ReadExecutor(PlanPolicy(root, contracts), continuity), continuity


def test_read_records_digest_execution_and_event(setup_read):
    root, plan, executor, continuity = setup_read
    result = executor.run(plan)
    assert result['ok'] and result['sha256'] == plan['postconditions']['sha256']
    assert result['bytes'] == len(b'bounded read result\n')
    assert result['operation']['status'] == 'SUCCEEDED'
    assert continuity.status()['lease']['execution']['status'] == 'DONE'
    uri = result['operation']['evidence']
    evidence = json.loads((root / uri.removeprefix('log://')).read_text())
    assert evidence['execution_id'] == result['execution_id']
    assert evidence['plan_sha256'] == result['operation']['intent']['plan_sha256']
    with sqlite3.connect(continuity.path) as conn:
        events = [json.loads(row[0]) for row in conn.execute('SELECT document FROM events')]
    assert any(event['event_type'] == 'operation.finished' and
               event['execution_id'] == result['execution_id'] and
               event['payload']['operation_key'] == plan['operation_id'] for event in events)
    again = executor.run(plan)
    assert again['ok'] and not again['changed']
    assert continuity.status()['operations'][0]['attempt'] == 1


def test_write_and_git_mismatch_fail_before_lease(setup_read):
    _, plan, executor, continuity = setup_read
    plan['kind'] = 'filesystem.write'
    with pytest.raises(DevFlowError, match='digest|Write plan'):
        executor.run(plan)
    assert continuity.status()['lease'] is None
    plan['kind'] = 'filesystem.read'
    plan['preconditions']['git_head_sha'] = 'f' * 40
    with pytest.raises(DevFlowError, match='Git precondition'):
        executor.run(plan)
    assert continuity.status()['lease'] is None


def test_mismatched_content_records_failure_and_blocks_replay(setup_read):
    _, plan, executor, continuity = setup_read
    plan['preconditions']['existing_sha256'] = 'f' * 64
    plan['postconditions']['sha256'] = 'f' * 64
    with pytest.raises(DevFlowError, match='evidence was recorded'):
        executor.run(plan)
    status = continuity.status()
    assert status['operations'][0]['status'] == 'FAILED'
    with sqlite3.connect(continuity.path) as conn:
        evidence = conn.execute('SELECT evidence FROM operations WHERE operation_key=?',
                                (plan['operation_id'],)).fetchone()[0]
    assert evidence.startswith('log://')
    assert json.loads((continuity.root / evidence.removeprefix('log://')).read_text())['failure_reason'] == 'digest_mismatch'
    with pytest.raises(DevFlowError):
        executor.run(plan)
    assert continuity.status()['operations'][0]['attempt'] == 1


def test_timeout_kills_worker_child_processes(tmp_path):
    marker = tmp_path / 'child-survived'
    child = "import time, pathlib; time.sleep(0.8); pathlib.Path(%r).write_text('bad')" % str(marker)
    parent = ("import subprocess,time,sys; subprocess.Popen([sys.executable,'-c',%r]); "
              "time.sleep(10)") % child
    result = BoundedProcess(timeout=0.2).run([sys.executable, '-c', parent], str(tmp_path))
    assert result['reason'] == 'timeout'
    time.sleep(0.9)
    assert not marker.exists()


def test_output_limit_terminates_noisy_process(tmp_path):
    result = BoundedProcess(timeout=2, max_output=128).run(
        [sys.executable, '-c', "import sys,time; print('x'*2000,flush=True); time.sleep(10)"],
        str(tmp_path))
    assert result['reason'] == 'output_limit'
    assert len(result['output']) <= 128


def test_cli_read_uses_project_registry_and_emits_no_file_content(setup_read, contracts):
    root, plan, _, continuity = setup_read
    registry_path = root.parent / 'registry.json'
    registry = ControlRegistry(registry_path, contracts)
    registry.register(root / '.ai-project/project.settings.json', make_active=True)
    plan_file = root.parent / 'read-plan.json'
    plan_file.write_text(json.dumps(plan))
    command = [sys.executable, '-m', 'devflow_core.cli', '--root', str(root),
               '--registry', str(registry_path), 'operation-plan', 'run-read',
               '--plan', str(plan_file), '--owner', 'local-operator']
    result = subprocess.run(command, env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert b'bounded read result' not in result.stdout.encode()
    assert json.loads(result.stdout)['operation']['status'] == 'SUCCEEDED'
    assert continuity.status()['operations'][0]['attempt'] == 1
