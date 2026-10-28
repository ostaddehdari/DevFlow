import copy
import json
import subprocess

import pytest

from devflow_core.contracts import DevFlowError
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
        return {'principal_id': 'human', 'credential_id': 'C-' + 'a' * 32}

    def credential_active(self, credential_id, project_id):
        return self.active and credential_id == 'C-' + 'a' * 32 and project_id == 'devflow'


@pytest.fixture
def push_plan(settings_path, contracts):
    root = settings_path.parent.parent
    settings = json.loads(settings_path.read_text())
    settings['servers']['control']['project_root'] = str(root)
    settings_path.write_text(json.dumps(settings))
    (root / 'README.md').write_text('source\n')
    git(root, 'init', '-b', 'stage/04c-git')
    with (root / '.git' / 'info' / 'exclude').open('a', encoding='utf-8') as exclude:
        exclude.write('\n.devflow/\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-m', 'fixture')
    git(root, 'remote', 'add', 'origin', 'https://github.com/ostaddehdari/DevFlow.git')
    inspected = GitWorkflow(root, contracts).inspect('stage/04c-git')
    plan = {'schema_version': '1.0', 'operation_id': 'push-1',
            'project_id': 'devflow', 'server_id': 'control', 'kind': 'git.push',
            'arguments': {'branch': inspected['branch'], 'remote': inspected['remote'],
                          'refspec': inspected['refspec']},
            'preconditions': {'git_branch': inspected['branch'],
                              'git_head_sha': inspected['head_sha'],
                              'remote_head_sha': None},
            'postconditions': {'remote_head_sha': inspected['head_sha']}}
    return root, plan, PlanPolicy(root, contracts)


def test_exact_plan_is_sensitive_and_approval_is_revocable(push_plan):
    _, plan, policy = push_plan
    checked = policy.validate(plan)
    assert checked['sensitive'] and checked['push_enabled'] is False
    operator = Operator()
    approvals = ApprovalStore(policy, operator, lambda: 1000)
    approvals.initialize()
    with pytest.raises(DevFlowError):
        approvals.verify(plan, 'AP-missing')
    approval = approvals.grant(plan, 'operator', 60)
    assert approvals.verify(plan, approval['approval_id'])['plan_sha256'] == checked['plan_sha256']
    changed = copy.deepcopy(plan)
    changed['preconditions']['remote_head_sha'] = 'b' * 40
    with pytest.raises(DevFlowError, match='another plan'):
        approvals.verify(changed, approval['approval_id'])
    operator.active = False
    with pytest.raises(DevFlowError, match='revoked'):
        approvals.verify(plan, approval['approval_id'])


@pytest.mark.parametrize('field,value', [
    ('refspec', '+HEAD:refs/heads/stage/04c-git'),
    ('refspec', 'HEAD:refs/heads/main'),
    ('branch', 'main'),
    ('remote', 'backup'),
])
def test_destination_changes_are_denied(push_plan, field, value):
    _, plan, policy = push_plan
    plan['arguments'][field] = value
    with pytest.raises(DevFlowError):
        policy.validate(plan)


def test_source_change_dirty_tree_and_wrong_postcondition_denied(push_plan):
    root, plan, policy = push_plan
    plan['postconditions']['remote_head_sha'] = 'b' * 40
    with pytest.raises(DevFlowError):
        policy.validate(plan)
    plan['postconditions']['remote_head_sha'] = plan['preconditions']['git_head_sha']
    (root / 'README.md').write_text('user edit\n')
    with pytest.raises(DevFlowError, match='user changes'):
        policy.validate(plan)
    assert (root / 'README.md').read_text() == 'user edit\n'


def test_extra_git_fields_and_absent_capability_denied(push_plan, settings_path, contracts):
    root, plan, policy = push_plan
    plan['arguments']['path'] = 'docs/x'
    with pytest.raises(DevFlowError):
        policy.validate(plan)
    del plan['arguments']['path']
    settings = json.loads(settings_path.read_text())
    settings['servers']['control']['capabilities'].remove('git')
    settings_path.write_text(json.dumps(settings))
    with pytest.raises(DevFlowError, match='Git capability'):
        PlanPolicy(root, contracts).validate(plan)
