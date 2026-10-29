"""Read-only preflight for migrating the DevFlow MCP service to pinned releases."""

import hashlib
import copy
import re
import subprocess
import uuid
from pathlib import Path

import httpx

from .contracts import DevFlowError
from .git_workflow import GitWorkflow
from .operation_plans import canonical


RESOURCE = 'https://srun.ir/mcpdevflow'
DISCOVERY = 'https://srun.ir/.well-known/oauth-protected-resource/mcpdevflow'
SERVICE = 'devflow-mcp.service'


class LiveProbe:
    """Observe the current service and public authentication boundary."""

    def __init__(self, unit_path=Path('/etc/systemd/system/devflow-mcp.service')):
        self.unit_path = Path(unit_path)

    def inspect(self):
        try:
            active = subprocess.run(
                ['systemctl', 'is-active', '--quiet', SERVICE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            effective = subprocess.run(
                ['systemctl', 'show', SERVICE, '--property=FragmentPath',
                 '--property=DropInPaths'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=5, check=True)
            listeners = subprocess.run(
                ['ss', '-lntH', '( sport = :8765 )'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=5, check=True)
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                metadata = client.get(DISCOVERY)
                denied = client.post(RESOURCE, json={})
            settings = dict(line.split('=', 1) for line in effective.stdout.splitlines()
                            if '=' in line)
            if (active.returncode or settings.get('FragmentPath') != str(self.unit_path) or
                settings.get('DropInPaths') != '' or metadata.status_code != 200 or
                metadata.json().get('resource') != RESOURCE or
                denied.status_code != 401 or
                'bearer' not in denied.headers.get('www-authenticate', '').lower()):
                raise DevFlowError('Live MCP health or authentication boundary changed')
            addresses = [line.split()[3] for line in listeners.stdout.splitlines()]
            if addresses != ['127.0.0.1:8765']:
                raise DevFlowError('MCP must listen on loopback only')
        except DevFlowError:
            raise
        except (OSError, subprocess.SubprocessError, httpx.HTTPError,
                ValueError, IndexError, KeyError) as exc:
            raise DevFlowError('Live MCP preflight failed') from exc
        return {'active': True, 'discovery_status': 200,
                'unauthenticated_status': 401, 'listener': '127.0.0.1:8765'}


class DeploymentWorkflow:
    """Prepare an exact migration plan without changing systemd or the server."""

    def __init__(self, root, contracts, unit_path=None, probe=None, git=None):
        self.root = Path(root).resolve()
        self.contracts = contracts
        self.git = git or GitWorkflow(self.root, contracts)
        self.unit_path = Path(unit_path or '/etc/systemd/system/devflow-mcp.service')
        self.probe = probe or LiveProbe(self.unit_path)
        self.release_root = self.root.parent / (self.root.name + '-releases')

    def _unit_digest(self):
        path = self.unit_path
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
            raise DevFlowError('MCP service unit is missing, linked, or oversized')
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise DevFlowError('MCP service unit cannot be read') from exc
        expected = (
            'WorkingDirectory=' + str(self.root),
            'ExecStart=/usr/bin/bash ' + str(self.root / 'scripts/devflow') +
                ' serve-http --config /etc/devflow/network.json',
            'User=root', 'Group=root', 'NoNewPrivileges=yes',
            'ProtectSystem=strict', 'ProtectHome=yes', 'PrivateTmp=yes',
            'CapabilityBoundingSet=',
        )
        try:
            lines = [line.strip() for line in content.decode('utf-8').splitlines()]
        except UnicodeError as exc:
            raise DevFlowError('MCP service unit is not UTF-8') from exc
        managed_keys = {line.split('=', 1)[0] for line in expected}
        if (any([line for line in lines if line.startswith(key + '=')] !=
                [next(item for item in expected if item.startswith(key + '='))]
                for key in managed_keys) or
            any(line.startswith(('Environment=', 'EnvironmentFile=', 'ExecStartPre=',
                                 'ExecStartPost=')) for line in lines)):
            raise DevFlowError('MCP service unit differs from the inspected legacy layout')
        if (self.unit_path.parent / (SERVICE + '.d')).exists():
            raise DevFlowError('MCP service has an uninspected override')
        return hashlib.sha256(content).hexdigest()

    def inspect(self, branch):
        observed = self.git.remote_head(branch)
        head = observed['head_sha']
        if observed['remote_head_sha'] != head:
            raise DevFlowError('Candidate commit is not published at the exact stage branch')
        if not re.fullmatch(r'[0-9a-f]{40}', head):
            raise DevFlowError('Invalid source commit')
        pointer = self.release_root / 'current'
        if pointer.exists() or pointer.is_symlink():
            raise DevFlowError('Release pointer already exists; inspect existing deployment')
        unit_sha = self._unit_digest()
        health = self.probe.inspect()
        if (health.get('active') is not True or
            health.get('listener') != '127.0.0.1:8765' or
            health.get('discovery_status') != 200 or
            health.get('unauthenticated_status') != 401):
            raise DevFlowError('Live MCP preflight is incomplete')
        return {
            'ok': True, 'project_id': observed['project_id'],
            'branch': observed['branch'], 'candidate_sha': head,
            'remote_url': observed['remote_url'], 'remote_head_sha': head,
            'service': SERVICE, 'unit_sha256': unit_sha,
            'legacy_root': str(self.root),
            'release_root': str(self.release_root),
            'release_path': str(self.release_root / head),
            'release_pointer': str(pointer),
            'runtime_root': str(self.root),
            'health': health,
        }

    def prepare(self, branch):
        inspected = self.inspect(branch)
        plan = {
            'schema_version': '1.0',
            'operation_id': 'service-deploy-' + uuid.uuid4().hex,
            'project_id': inspected['project_id'],
            'kind': 'service.deploy',
            'service': SERVICE,
            'source': {
                'branch': inspected['branch'],
                'git_head_sha': inspected['candidate_sha'],
                'remote_head_sha': inspected['remote_head_sha'],
                'remote_url': inspected['remote_url'],
            },
            'preconditions': {
                'unit_sha256': inspected['unit_sha256'],
                'legacy_root': inspected['legacy_root'],
                'release_pointer_absent': True,
            },
            'release': {
                'path': inspected['release_path'],
                'pointer': inspected['release_pointer'],
                'runtime_root': inspected['runtime_root'],
            },
            'health': {
                'resource': RESOURCE,
                'discovery': DISCOVERY,
                'discovery_status': 200,
                'unauthenticated_status': 401,
                'listener': '127.0.0.1:8765',
            },
            'execution_enabled': False,
        }
        self.contracts.validate(plan, 'deployment.plan.schema.json')
        return plan

    def validate(self, plan):
        self.contracts.validate(plan, 'deployment.plan.schema.json')
        if plan['execution_enabled'] is not False:
            raise DevFlowError('Deployment execution is not implemented')
        current = self.prepare(plan['source']['branch'])
        for key in ('project_id', 'kind', 'service', 'source', 'preconditions',
                    'release', 'health', 'execution_enabled'):
            if plan[key] != current[key]:
                raise DevFlowError('Deployment plan is stale or differs from live inspection')
        return {'ok': True, 'operation_id': plan['operation_id'],
                'candidate_sha': current['source']['git_head_sha'],
                'execution_enabled': False}

    def prepare_execution(self, branch):
        """Produce a separate, sensitive plan that requires local approval."""
        plan = self.prepare(branch)
        if self.release_root.is_symlink() or (self.release_root / plan['source']['git_head_sha']).exists():
            raise DevFlowError('Release destination is already occupied')
        plan['execution_enabled'] = True
        self.contracts.validate(plan, 'deployment.plan.schema.json')
        return plan


class DeploymentPolicy:
    """Adapter for the local exact-plan approval ledger."""

    def __init__(self, workflow):
        self.workflow = workflow
        self.root = workflow.root
        self.contracts = workflow.contracts
        self.project_id = workflow.git.project_id

    def validate(self, plan):
        if not isinstance(plan, dict) or len(canonical(plan)) > 65536:
            raise DevFlowError('Deployment plan must be a bounded JSON object')
        if plan.get('execution_enabled') is not True:
            raise DevFlowError('Only an executable deployment plan may be approved')
        self.contracts.validate(plan, 'deployment.plan.schema.json')
        proposal = copy.deepcopy(plan)
        proposal['execution_enabled'] = False
        self.workflow.validate(proposal)
        if (plan['project_id'] != self.project_id or
            self.workflow.release_root.is_symlink() or
            (self.workflow.release_root / plan['source']['git_head_sha']).exists()):
            raise DevFlowError('Deployment target is occupied or project differs')
        return {'operation_id': plan['operation_id'],
                'plan_sha256': hashlib.sha256(canonical(plan)).hexdigest(),
                'candidate_sha': plan['source']['git_head_sha'], 'sensitive': True,
                'execution_enabled': True}
