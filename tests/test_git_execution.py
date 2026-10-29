import json
import subprocess

import pytest

from devflow_core.continuity import Continuity
from devflow_core.contracts import DevFlowError
from devflow_core.git_execution import GitPushExecutor
from devflow_core.git_workflow import GitWorkflow
from devflow_core.operation_plans import ApprovalStore, PlanPolicy


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Operator:
    active = True

    def authorize(self, token, action, project_id):
        if (token, action, project_id) != ('operator', 'operation.approve', 'devflow'):
            raise DevFlowError('Access denied')
        return {'principal_id': 'human', 'credential_id': 'C-' + 'b' * 32}

    def credential_active(self, credential_id, project_id):
        return self.active and credential_id == 'C-' + 'b' * 32 and project_id == 'devflow'


@pytest.fixture
def setup_push(settings_path, contracts, tmp_path, monkeypatch):
    root = settings_path.parent.parent
    settings = json.loads(settings_path.read_text())
    settings['servers']['control']['project_root'] = str(root)
    settings_path.write_text(json.dumps(settings))
    (root / '.gitignore').write_text('.devflow/\n')
    (root / 'README.md').write_text('source\n')
    git(root, 'init', '-b', 'stage/04c-push')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-m', 'fixture')
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(bare)], check=True,
                   capture_output=True)
    git(root, 'remote', 'add', 'origin', str(bare))
    original = GitWorkflow.__init__

    def local_init(self, project_root, project_contracts):
        original(self, project_root, project_contracts)
        self.expected_url = str(bare)

    monkeypatch.setattr(GitWorkflow, '__init__', local_init)
    policy = PlanPolicy(root, contracts)
    plan = GitWorkflow(root, contracts).prepare_push('stage/04c-push')
    operator = Operator()
    approvals = ApprovalStore(policy, operator)
    approvals.initialize()
    approval = approvals.grant(plan, 'operator', 600)['approval_id']
    continuity = Continuity(root, contracts)
    continuity.initialize()
    return root, bare, plan, operator, approvals, approval, continuity, policy


def test_approved_push_creates_only_stage_branch_with_evidence(setup_push):
    root, bare, plan, _, approvals, approval, continuity, policy = setup_push
    result = GitPushExecutor(policy, approvals, continuity).run(plan, approval)
    assert result['head_sha'] == plan['postconditions']['remote_head_sha']
    assert result['operation']['status'] == 'SUCCEEDED'
    assert git(bare, 'rev-parse', 'refs/heads/stage/04c-push') == result['head_sha']
    uri = result['operation']['evidence']
    evidence = json.loads((root / uri.removeprefix('log://')).read_text())
    assert evidence['outcome'] == 'SUCCEEDED'
    assert evidence['plan_sha256'] == result['operation']['intent']['plan_sha256']
    assert evidence['observed_remote_sha'] == result['head_sha']
    assert continuity.status()['lease']['execution']['status'] == 'DONE'


def test_revoked_approval_denies_before_lease(setup_push):
    _, bare, plan, operator, approvals, approval, continuity, policy = setup_push
    operator.active = False
    with pytest.raises(DevFlowError, match='revoked'):
        GitPushExecutor(policy, approvals, continuity).run(plan, approval)
    assert continuity.status()['lease'] is None
    assert subprocess.run(['git', '-C', str(bare), 'show-ref'],
                          capture_output=True).returncode != 0


def test_remote_creation_race_denies_before_lease(setup_push):
    root, bare, plan, _, approvals, approval, continuity, policy = setup_push
    git(root, 'push', 'origin', plan['arguments']['refspec'])
    with pytest.raises(DevFlowError, match='precondition changed'):
        GitPushExecutor(policy, approvals, continuity).run(plan, approval)
    assert continuity.status()['lease'] is None


def test_existing_branch_only_advances_to_its_descendant(setup_push, contracts):
    root, bare, first, _, approvals, _, continuity, policy = setup_push
    git(root, 'push', 'origin', first['arguments']['refspec'])
    (root / 'README.md').write_text('next\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-m', 'next')
    plan = GitWorkflow(root, contracts).prepare_push('stage/04c-push')
    assert plan['preconditions']['remote_head_sha'] == first['postconditions']['remote_head_sha']
    approval = approvals.grant(plan, 'operator', 600)['approval_id']
    result = GitPushExecutor(policy, approvals, continuity).run(plan, approval)
    assert git(bare, 'rev-parse', 'refs/heads/stage/04c-push') == result['head_sha']


def test_unrelated_remote_commit_cannot_be_rewritten(setup_push, contracts, tmp_path):
    root, bare, first, _, approvals, _, continuity, policy = setup_push
    git(root, 'push', 'origin', first['arguments']['refspec'])
    other = tmp_path / 'other'
    subprocess.run(['git', 'clone', '--branch', 'stage/04c-push',
                    str(bare), str(other)], check=True, capture_output=True)
    (other / 'README.md').write_text('remote-only\n')
    git(other, 'add', '.')
    git(other, '-c', 'user.name=Other', '-c', 'user.email=other@example.invalid',
        'commit', '-m', 'remote-only')
    git(other, 'push', 'origin', 'HEAD:refs/heads/stage/04c-push')
    plan = GitWorkflow(root, contracts).prepare_push('stage/04c-push')
    approval = approvals.grant(plan, 'operator', 600)['approval_id']
    with pytest.raises(DevFlowError, match='known local ancestor'):
        GitPushExecutor(policy, approvals, continuity).run(plan, approval)
    assert continuity.status()['lease'] is None


class TimeoutRunner:
    timeout = 0.1

    def run(self, argv, cwd, env):
        assert argv[-1].endswith(':refs/heads/stage/04c-push')
        assert len(argv[-1].split(':', 1)[0]) == 40
        assert any(arg.startswith('--force-with-lease=refs/heads/stage/04c-push:') for arg in argv)
        return {'reason': 'timeout', 'exit_code': -9, 'output': b''}


def test_unknown_effect_stays_started_and_fenced(setup_push):
    _, _, plan, _, approvals, approval, continuity, policy = setup_push
    executor = GitPushExecutor(policy, approvals, continuity, runner=TimeoutRunner())
    with pytest.raises(DevFlowError, match='unknown'):
        executor.run(plan, approval)
    assert continuity.status()['operations'][0]['status'] == 'STARTED'
    with pytest.raises(DevFlowError):
        executor.run(plan, approval)
