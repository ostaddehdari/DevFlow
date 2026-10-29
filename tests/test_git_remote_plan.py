import json
import subprocess

import pytest

from devflow_core.contracts import DevFlowError
from devflow_core.git_workflow import GitWorkflow


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def local_remote(settings_path, contracts, tmp_path):
    root = settings_path.parent.parent
    settings = json.loads(settings_path.read_text())
    settings['servers']['control']['project_root'] = str(root)
    settings_path.write_text(json.dumps(settings))
    (root / 'README.md').write_text('source\n')
    git(root, 'init', '-b', 'stage/04c-observe')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=DevFlow Test', '-c', 'user.email=test@example.invalid',
        'commit', '-m', 'fixture')
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(bare)], check=True,
                   capture_output=True)
    git(root, 'remote', 'add', 'origin', str(bare))
    workflow = GitWorkflow(root, contracts)
    workflow.expected_url = str(bare)
    return root, bare, workflow


def test_prepare_exact_new_branch_then_observe_existing_branch(local_remote, contracts):
    root, _, workflow = local_remote
    plan = workflow.prepare_push('stage/04c-observe')
    assert plan['preconditions']['remote_head_sha'] is None
    assert plan['postconditions']['remote_head_sha'] == git(root, 'rev-parse', 'HEAD')
    assert plan['arguments']['refspec'] == 'HEAD:refs/heads/stage/04c-observe'
    assert plan['operation_id'].startswith('git-push-')
    contracts.validate(plan, 'operation.plan.schema.json')
    git(root, 'push', 'origin', plan['arguments']['refspec'])
    observed = workflow.remote_head('stage/04c-observe')
    assert observed['remote_head_sha'] == plan['postconditions']['remote_head_sha']
    assert observed['push_enabled'] is False


def test_dirty_tree_and_unexpected_origin_fail_before_remote_call(local_remote):
    root, bare, workflow = local_remote
    (root / 'README.md').write_text('user edit\n')
    with pytest.raises(DevFlowError, match='user changes'):
        workflow.prepare_push('stage/04c-observe')
    (root / 'README.md').write_text('source\n')
    workflow.expected_url = str(bare) + '-wrong'
    with pytest.raises(DevFlowError, match='origin'):
        workflow.remote_head('stage/04c-observe')


def test_missing_remote_does_not_invent_absence(local_remote):
    root, bare, workflow = local_remote
    git(root, 'remote', 'set-url', 'origin', str(bare / 'missing'))
    workflow.expected_url = str(bare / 'missing')
    with pytest.raises(DevFlowError, match='Remote Git inspection failed'):
        workflow.prepare_push('stage/04c-observe')
