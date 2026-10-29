"""A real Git archive and isolated service doubles exercise release and rollback."""

import subprocess
from pathlib import Path

import pytest

from devflow_core.contracts import DevFlowError
from devflow_core.deployment import DeploymentPolicy, DeploymentWorkflow
from devflow_core.deployment_execution import ReleaseExecutor


class Git:
    project_id = 'devflow'

    def __init__(self, sha):
        self.sha = sha

    def remote_head(self, branch):
        return {'project_id': 'devflow', 'branch': branch, 'head_sha': self.sha,
                'remote_head_sha': self.sha,
                'remote_url': 'https://github.com/ostaddehdari/DevFlow.git'}


class Probe:
    def inspect(self):
        return {'active': True, 'discovery_status': 200,
                'unauthenticated_status': 401, 'listener': '127.0.0.1:8765'}


class Approvals:
    def __init__(self, policy):
        self.policy = policy

    def verify(self, plan, approval_id):
        if approval_id != 'approved':
            raise DevFlowError('Approval missing')
        return self.policy.validate(plan)


class Ledger:
    def __init__(self, root):
        self.root = root
        self.project_id = 'devflow'
        self.operations = []
        self.lease = None

    def acquire(self, owner, ttl):
        assert ttl == 600
        self.lease = {'fence': 7, 'execution': {'execution_id': 'EX-test'}}
        return self.lease

    def prepare(self, *args):
        self.operations.append(('prepare', args[3]))
        return {'changed': True}

    def start(self, *args):
        self.operations.append(('start', args[3]))

    def finish(self, *args):
        self.operations.append(('finish', args[3], args[4]))
        return {'operation': {'status': args[4]}}

    def status(self):
        return {'lease': self.lease, 'operations': []}

    def release(self, *args):
        self.operations.append(('release', args[1]))
        self.lease = None


@pytest.fixture
def setup(tmp_path, contracts, monkeypatch):
    root = tmp_path / 'devflow'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    git('init', '-b', 'stage/test')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    for name in ('src/devflow_core/cli.py', 'src/devflow_core/network.py',
                 'contracts/network.schema.json'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('safe source\n')
    git('add', '.')
    git('commit', '-m', 'candidate')
    sha = git('rev-parse', 'HEAD')
    unit = tmp_path / 'devflow-mcp.service'
    unit.write_text('[Service]\n'
                    f'WorkingDirectory={root}\n'
                    f'ExecStart=/usr/bin/bash {root}/scripts/devflow serve-http '
                    '--config /etc/devflow/network.json\n'
                    'User=root\nGroup=root\nNoNewPrivileges=yes\n'
                    'ProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n'
                    'CapabilityBoundingSet=\n')
    workflow = DeploymentWorkflow(root, contracts, unit_path=unit, probe=Probe(), git=Git(sha))
    (root / '.devflow').mkdir(mode=0o700)
    plan = workflow.prepare_execution('stage/test')
    policy = DeploymentPolicy(workflow)
    ledger = Ledger(root)
    executor = ReleaseExecutor(workflow, Approvals(policy), ledger)
    monkeypatch.setattr('devflow_core.deployment_execution.os.geteuid', lambda: 0)
    monkeypatch.setattr('devflow_core.deployment_execution.read_private_file',
                        lambda path, maximum: b'valid-test-token')
    monkeypatch.setattr('devflow_core.deployment_execution.NetworkPolicy', lambda *args: None)
    class Verifier:
        def __init__(self, policy):
            pass
        async def verify_token(self, token):
            return object()
    monkeypatch.setattr('devflow_core.deployment_execution.JWTVerifier', Verifier)
    monkeypatch.setattr(executor, '_health', lambda plan, token: 8)
    return workflow, policy, plan, executor, ledger


def test_executable_plan_requires_live_match_and_exact_approval(setup):
    workflow, policy, plan, executor, ledger = setup
    assert policy.validate(plan)['sensitive']
    assert workflow.validate({**plan, 'execution_enabled': False})['ok']
    with pytest.raises(DevFlowError):
        workflow.validate(plan)
    with pytest.raises(DevFlowError, match='Approval'):
        executor.run(plan, 'missing', '/tmp/does-not-exist')
    assert not workflow.release_root.exists()
    plan['source']['remote_head_sha'] = 'f' * 40
    with pytest.raises(DevFlowError):
        policy.validate(plan)


def test_pinned_archive_and_successful_switch_is_recorded(setup, monkeypatch):
    workflow, policy, plan, executor, ledger = setup
    def switch(plan):
        pointer = workflow.release_root / 'current'
        pointer.symlink_to(plan['source']['git_head_sha'])
    monkeypatch.setattr(executor, '_dropin', switch)
    result = executor.run(plan, 'approved', '/tmp/does-not-exist')
    assert result['deployed_sha'] == plan['source']['git_head_sha']
    assert (workflow.release_root / 'current').is_symlink()
    assert (workflow.release_root / plan['source']['git_head_sha'] /
            'src/devflow_core/cli.py').read_text() == 'safe source\n'
    assert ('finish', plan['operation_id'], 'SUCCEEDED') in ledger.operations
    assert ('release', 'EX-test') in ledger.operations


def test_failed_switch_rolls_back_and_blocks_reuse(setup, monkeypatch):
    workflow, policy, plan, executor, ledger = setup
    restored = []
    def fail(_):
        raise DevFlowError('Injected restart failure')
    monkeypatch.setattr(executor, '_dropin', fail)
    monkeypatch.setattr(executor, '_rollback', lambda _: restored.append(True))
    with pytest.raises(DevFlowError, match='legacy MCP restored'):
        executor.run(plan, 'approved', '/tmp/does-not-exist')
    assert restored == [True]
    assert ('finish', plan['operation_id'], 'FAILED') in ledger.operations


def test_unverified_rollback_stays_started_for_reconciliation(setup, monkeypatch):
    workflow, policy, plan, executor, ledger = setup
    monkeypatch.setattr(executor, '_dropin', lambda _: (_ for _ in ()).throw(
        DevFlowError('Injected failure')))
    monkeypatch.setattr(executor, '_rollback', lambda _: (_ for _ in ()).throw(
        DevFlowError('Injected rollback failure')))
    with pytest.raises(DevFlowError, match='outcome unknown'):
        executor.run(plan, 'approved', '/tmp/does-not-exist')
    assert not any(item[0] == 'finish' for item in ledger.operations)


def test_actual_override_rollback_restores_legacy_layout(setup):
    workflow, policy, plan, executor, ledger = setup
    executor.command = lambda *args: None
    staged = executor._stage(plan)
    assert staged.name == plan['source']['git_head_sha']
    prior = workflow.unit_path.read_bytes()
    executor._dropin(plan)
    assert (workflow.release_root / 'current').is_symlink()
    override = workflow.unit_path.parent / 'devflow-mcp.service.d' / '10-devflow-release.conf'
    assert 'ExecStart=' in override.read_text()
    executor._rollback(plan)
    assert workflow.unit_path.read_bytes() == prior
    assert not override.exists()
    assert not (workflow.release_root / 'current').exists()
    assert not (workflow.release_root / 'current').is_symlink()


def test_failed_live_probe_restores_unit_and_pointer(setup, monkeypatch):
    workflow, policy, plan, executor, ledger = setup
    executor.command = lambda *args: None
    monkeypatch.setattr(executor, '_health', lambda *_: (_ for _ in ()).throw(
        DevFlowError('Injected authenticated MCP failure')))
    prior = workflow.unit_path.read_bytes()
    with pytest.raises(DevFlowError, match='legacy MCP restored'):
        executor.run(plan, 'approved', '/tmp/does-not-exist')
    assert workflow.unit_path.read_bytes() == prior
    assert not (workflow.release_root / 'current').is_symlink()
    assert not (workflow.unit_path.parent / 'devflow-mcp.service.d').exists()
    assert ('finish', plan['operation_id'], 'FAILED') in ledger.operations


def test_archive_rejects_symlinks_without_service_change(setup):
    workflow, policy, plan, executor, ledger = setup
    root = workflow.root
    (root / 'unsafe-link').symlink_to('/etc/passwd')
    subprocess.run(['git', '-C', str(root), 'add', 'unsafe-link'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-m', 'unsafe'], check=True,
                   stdout=subprocess.DEVNULL)
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    plan['source']['git_head_sha'] = sha
    workflow.git.sha = sha
    with pytest.raises(DevFlowError, match='unsafe'):
        executor._stage(plan)
    assert not (workflow.release_root / sha).exists()
