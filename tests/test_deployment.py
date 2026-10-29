"""Stage 4D preflight is read-only and refuses drift before any service change."""

from pathlib import Path

import pytest

from devflow_core.contracts import DevFlowError
from devflow_core.deployment import DeploymentWorkflow, LiveProbe


SHA = 'a' * 40
ORIGIN = 'https://github.com/ostaddehdari/DevFlow.git'
BRANCH = 'stage/04d-deployment-prep'


class Git:
    remote_sha = SHA

    def remote_head(self, branch):
        assert branch == BRANCH
        return {'project_id': 'devflow', 'branch': branch, 'head_sha': SHA,
                'remote_head_sha': self.remote_sha, 'remote_url': ORIGIN}


class Probe:
    result = {'active': True, 'discovery_status': 200,
              'unauthenticated_status': 401, 'listener': '127.0.0.1:8765'}

    def inspect(self):
        return self.result


@pytest.fixture
def workflow(tmp_path, contracts):
    root = tmp_path / 'devflow'
    root.mkdir()
    unit = tmp_path / 'devflow-mcp.service'
    unit.write_text(
        '[Service]\n'
        f'WorkingDirectory={root}\n'
        f'ExecStart=/usr/bin/bash {root}/scripts/devflow serve-http '
        '--config /etc/devflow/network.json\n'
        'User=root\nGroup=root\nNoNewPrivileges=yes\n'
        'ProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n'
        'CapabilityBoundingSet=\n')
    return DeploymentWorkflow(root, contracts, unit_path=unit, probe=Probe(), git=Git())


def test_exact_plan_is_read_only_and_detects_service_drift(workflow, contracts):
    before = workflow.unit_path.read_bytes()
    plan = workflow.prepare(BRANCH)
    assert plan['execution_enabled'] is False
    assert plan['release']['runtime_root'] == str(workflow.root)
    assert plan['release']['path'].endswith(SHA)
    assert workflow.validate(plan)['candidate_sha'] == SHA
    assert workflow.unit_path.read_bytes() == before
    assert not workflow.release_root.exists()
    assert not (workflow.root / '.devflow').exists()
    assert (Path(__file__).resolve().parents[1] / 'contracts/deployment.plan.schema.json').read_bytes() == (
        Path(__file__).resolve().parents[1] / 'plugin/assets/contracts/deployment.plan.schema.json'
    ).read_bytes()

    workflow.unit_path.write_bytes(before + b'# changed by another operator\n')
    with pytest.raises(DevFlowError, match='stale'):
        workflow.validate(plan)


@pytest.mark.parametrize('change', ['remote', 'override', 'pointer', 'health', 'unit', 'symlink'])
def test_preflight_refuses_unobserved_state(workflow, change):
    if change == 'remote':
        workflow.git.remote_sha = 'b' * 40
    elif change == 'override':
        (workflow.unit_path.parent / 'devflow-mcp.service.d').mkdir()
    elif change == 'pointer':
        workflow.release_root.mkdir()
        (workflow.release_root / 'current').symlink_to('missing')
    elif change == 'health':
        workflow.probe.result = {**workflow.probe.result, 'unauthenticated_status': 200}
    elif change == 'unit':
        workflow.unit_path.write_text('ExecStart=/bin/false\n')
    else:
        original = workflow.unit_path
        copy = original.with_suffix('.copy')
        original.rename(copy)
        original.symlink_to(copy)
    with pytest.raises(DevFlowError):
        workflow.prepare(BRANCH)


def test_plan_tamper_is_rejected(workflow):
    plan = workflow.prepare(BRANCH)
    plan['release']['path'] = '/tmp/another-release'
    with pytest.raises(DevFlowError, match='stale'):
        workflow.validate(plan)
    plan = workflow.prepare(BRANCH)
    plan['execution_enabled'] = True
    with pytest.raises(DevFlowError):
        workflow.validate(plan)


def test_live_probe_denies_public_read_without_bearer_challenge(monkeypatch):
    import devflow_core.deployment as deployment

    class Process:
        returncode = 0
        stdout = 'LISTEN 0 2048 127.0.0.1:8765 0.0.0.0:*\n'

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {'resource': deployment.RESOURCE}

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, _):
            return Response()

        def post(self, _, json):
            return Response()

    def run(args, **_):
        result = Process()
        if args[:2] == ['systemctl', 'show']:
            result.stdout = (
                'FragmentPath=/etc/systemd/system/devflow-mcp.service\nDropInPaths=\n'
            )
        return result

    monkeypatch.setattr(deployment.subprocess, 'run', run)
    monkeypatch.setattr(deployment.httpx, 'Client', Client)
    with pytest.raises(DevFlowError, match='authentication'):
        LiveProbe().inspect()
