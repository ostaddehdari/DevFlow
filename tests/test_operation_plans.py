import copy
import hashlib
import json

import pytest

from devflow_core.contracts import DevFlowError
from devflow_core.operation_plans import ApprovalStore, PlanPolicy
from devflow_core.security import SecurityStore, read_private_file


class Security:
    def __init__(self):
        self.active = True

    def authorize(self, token, action, project_id):
        if token != 'operator-credential' or action != 'operation.approve' or project_id != 'devflow':
            raise DevFlowError('Access denied')
        return {'principal_id': 'human-operator', 'credential_id': 'C-' + 'a' * 32}

    def credential_active(self, credential_id, project_id):
        return self.active and credential_id == 'C-' + 'a' * 32 and project_id == 'devflow'


@pytest.fixture
def policy(settings_path, contracts):
    data = json.loads(settings_path.read_text())
    data['servers']['control']['project_root'] = str(settings_path.parent.parent)
    settings_path.write_text(json.dumps(data))
    return PlanPolicy(settings_path.parent.parent, contracts)


@pytest.fixture
def plan():
    digest = hashlib.sha256(b'new content').hexdigest()
    return {'schema_version': '1.0', 'operation_id': 'stage4-write-1',
            'project_id': 'devflow', 'server_id': 'control', 'kind': 'filesystem.write',
            'arguments': {'path': 'docs/example.txt', 'content_sha256': digest, 'content_bytes': 11},
            'preconditions': {'git_head_sha': 'a' * 40, 'git_branch': 'stage/04-controlled',
                              'existing_sha256': None},
            'postconditions': {'sha256': digest}}


def test_approved_exact_plan_and_revoke(policy, plan):
    security = Security()
    clock = [1000]
    store = ApprovalStore(policy, security, lambda: clock[0])
    store.initialize()
    approval = store.grant(plan, 'operator-credential', 60)
    assert store.verify(plan, approval['approval_id'])['plan_sha256'] == approval['plan_sha256']
    altered = copy.deepcopy(plan)
    altered['arguments']['path'] = 'docs/other.txt'
    with pytest.raises(DevFlowError, match='another plan'):
        store.verify(altered, approval['approval_id'])
    security.active = False
    with pytest.raises(DevFlowError):
        store.verify(plan, approval['approval_id'])
    security.active = True
    clock[0] += 61
    with pytest.raises(DevFlowError):
        store.verify(plan, approval['approval_id'])
    assert store.revoke(approval['approval_id'])['changed']
    assert not store.revoke(approval['approval_id'])['changed']


def test_no_self_authorization_or_implicit_approval(policy, plan):
    store = ApprovalStore(policy, Security(), lambda: 1000)
    store.initialize()
    with pytest.raises(DevFlowError):
        store.verify(plan, 'AP-unknown')
    with pytest.raises(DevFlowError, match='Access denied'):
        store.grant(plan, 'viewer-credential')


@pytest.mark.parametrize('path', ['/tmp/file', '../outside', 'docs/../../etc',
                                  '.git/config', '.ai-project/state.json', 'docs\\bad',
                                  'docs//bad'])
def test_rejects_outside_and_internal_paths(policy, plan, path):
    plan['arguments']['path'] = path
    with pytest.raises(DevFlowError):
        policy.validate(plan)


def test_rejects_symlink_escape_and_unknown_kind(policy, plan, tmp_path):
    (policy.root / 'docs').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DevFlowError, match='symlink'):
        policy.validate(plan)
    plan['kind'] = 'shell.exec'
    with pytest.raises(DevFlowError):
        policy.validate(plan)


def test_rejects_directory_target(policy, plan):
    (policy.root / 'docs').mkdir()
    plan['arguments']['path'] = 'docs'
    with pytest.raises(DevFlowError, match='regular file'):
        policy.validate(plan)


def test_requires_capability_and_exact_write_digest(policy, plan, settings_path, contracts):
    plan['postconditions']['sha256'] = 'b' * 64
    with pytest.raises(DevFlowError, match='digest'):
        policy.validate(plan)
    plan['postconditions']['sha256'] = plan['arguments']['content_sha256']
    settings = json.loads(settings_path.read_text())
    settings['servers']['control']['capabilities'].remove('filesystem')
    settings_path.write_text(json.dumps(settings))
    with pytest.raises(DevFlowError, match='capability'):
        PlanPolicy(policy.root, contracts).validate(plan)


def test_read_plan_does_not_require_approval(policy, plan):
    plan['kind'] = 'filesystem.read'
    plan['preconditions']['existing_sha256'] = plan['postconditions']['sha256']
    plan['arguments'] = {'path': 'docs/example.txt'}
    store = ApprovalStore(policy, Security(), lambda: 1000)
    assert store.verify(plan, None)['sensitive'] is False
    store.initialize()
    with pytest.raises(DevFlowError, match='do not require'):
        store.grant(plan, 'operator-credential')


def test_real_operator_credential_and_revocation(policy, plan, tmp_path, contracts):
    security = SecurityStore(policy.root, contracts)
    security.initialize(tmp_path / 'keys')
    security.create_principal('stage4-operator', 'operator', ['devflow'])
    issued = security.issue('stage4-operator', 300)
    token = read_private_file(issued['credential_file'], 256).decode().strip()
    store = ApprovalStore(policy, security)
    store.initialize()
    approval = store.grant(plan, token)
    assert store.verify(plan, approval['approval_id'])['sensitive']
    security.disable('stage4-operator')
    with pytest.raises(DevFlowError, match='revoked'):
        store.verify(plan, approval['approval_id'])
