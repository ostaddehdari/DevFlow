"""Approved one-time migration from the legacy MCP unit to a pinned code release."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import timedelta
from pathlib import Path, PurePosixPath

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .contracts import DevFlowError
from .controlled_execution import ReadExecutor
from .deployment import DeploymentPolicy, DeploymentWorkflow, RESOURCE, SERVICE
from .network import JWTVerifier, NetworkPolicy
from .security import read_private_file


def _command(*argv, timeout=20):
    try:
        subprocess.run(argv, timeout=timeout, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DevFlowError('Deployment system command failed') from exc


class ReleaseExecutor:
    """The only effectful path for the first, legacy-to-release migration."""

    def __init__(self, workflow: DeploymentWorkflow, approvals, continuity,
                 command=_command, auth_probe=None):
        self.workflow = workflow
        self.policy = DeploymentPolicy(workflow)
        self.approvals = approvals
        self.continuity = continuity
        self.command = command
        self.auth_probe = auth_probe or self._authenticated_probe
        if (self.policy.root != continuity.root or
            self.policy.project_id != continuity.project_id or
            approvals.policy.root != self.policy.root):
            raise DevFlowError('Deployment, approval and continuity roots differ')

    def _stage(self, plan):
        release_root = self.workflow.release_root
        if release_root.is_symlink():
            raise DevFlowError('Release root cannot be a symlink')
        release_root.mkdir(mode=0o700, exist_ok=True)
        info = release_root.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DevFlowError('Release root must be owner-only')
        candidate = plan['source']['git_head_sha']
        target = release_root / candidate
        if target.exists() or target.is_symlink():
            raise DevFlowError('Release already exists')
        temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=release_root))
        archive = temporary.parent / (temporary.name + '.tar')
        try:
            with archive.open('xb') as output:
                subprocess.run(
                    ['git', '-C', str(self.workflow.root), '-c', 'core.hooksPath=/dev/null',
                     'archive', '--format=tar', candidate], stdout=output,
                    stderr=subprocess.DEVNULL, timeout=40, check=True)
            if archive.stat().st_size > 32 * 1024 * 1024:
                raise DevFlowError('Release archive exceeds 32 MiB')
            count, total = 0, 0
            with tarfile.open(archive, mode='r:') as tar:
                for item in tar:
                    count += 1
                    total += item.size
                    path = PurePosixPath(item.name)
                    if (count > 4096 or total > 64 * 1024 * 1024 or
                        path.is_absolute() or not path.parts or
                        any(part in ('', '.', '..') for part in path.parts) or
                        not (item.isfile() or item.isdir())):
                        raise DevFlowError('Release archive contains an unsafe entry')
                    destination = temporary.joinpath(*path.parts)
                    if item.isdir():
                        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        source = tar.extractfile(item)
                        if source is None:
                            raise DevFlowError('Release archive entry is missing')
                        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT |
                                             os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(descriptor, 'wb') as output:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
            for needed in ('src/devflow_core/cli.py', 'src/devflow_core/network.py',
                           'contracts/network.schema.json'):
                if not (temporary / needed).is_file():
                    raise DevFlowError('Pinned release is incomplete')
            (temporary / '.devflow-release').write_text(candidate + '\n')
            os.replace(temporary, target)
            return target
        except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
            raise DevFlowError('Cannot stage pinned release') from exc
        finally:
            archive.unlink(missing_ok=True)
            if temporary.exists():
                shutil.rmtree(temporary)

    def _dropin(self, plan):
        root = self.workflow.root
        pointer = self.workflow.release_root / 'current'
        target = Path(plan['release']['path'])
        if pointer.exists() or pointer.is_symlink():
            raise DevFlowError('Release pointer already exists')
        directory = self.workflow.unit_path.parent / (SERVICE + '.d')
        if directory.exists() or directory.is_symlink():
            raise DevFlowError('Service override already exists')
        directory.mkdir(mode=0o700)
        try:
            temporary = pointer.parent / ('.current-' + plan['operation_id'])
            temporary.symlink_to(target.name)
            os.replace(temporary, pointer)
            content = ('[Service]\n'
                       'Environment=PYTHONPATH=' + str(pointer / 'src') + '\n'
                       'ExecStart=\n'
                       'ExecStart=' + str(root / '.venv/bin/python') +
                       ' -m devflow_core.cli --root ' + str(root) +
                       ' serve-http --config /etc/devflow/network.json\n')
            fd = os.open(directory / '10-devflow-release.conf',
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            self.command('systemctl', 'daemon-reload')
            self.command('systemctl', 'restart', SERVICE)
        except BaseException:
            # The caller owns rollback even when only the pointer was installed.
            raise

    def _running_release(self, plan):
        pointer = self.workflow.release_root / 'current'
        if not pointer.is_symlink() or os.readlink(pointer) != plan['source']['git_head_sha']:
            raise DevFlowError('Active release pointer does not match candidate')
        if (pointer / '.devflow-release').read_text() != plan['source']['git_head_sha'] + '\n':
            raise DevFlowError('Active release marker does not match candidate')
        try:
            result = subprocess.run(
                ['systemctl', 'show', SERVICE, '--property=MainPID',
                 '--property=DropInPaths', '--property=ActiveState'],
                capture_output=True, text=True, check=True, timeout=5)
            fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            pid = int(fields['MainPID'])
            expected = str(self.workflow.unit_path.parent / (SERVICE + '.d') /
                           '10-devflow-release.conf')
            if (pid <= 1 or fields['ActiveState'] != 'active' or
                fields['DropInPaths'] != expected):
                raise DevFlowError('Effective service differs from pinned release override')
            environment = (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
            if ('PYTHONPATH=' + str(pointer / 'src')).encode() not in environment:
                raise DevFlowError('Service process did not load the pinned release path')
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            raise DevFlowError('Cannot observe active release process') from exc

    async def _authenticated_probe(self, token):
        async with streamablehttp_client(
                RESOURCE, headers={'Authorization': 'Bearer ' + token},
                timeout=20, sse_read_timeout=20) as (reader, writer, _):
            async with ClientSession(reader, writer,
                                     read_timeout_seconds=timedelta(seconds=20)) as session:
                initialized = await session.initialize()
                if initialized.serverInfo.name != 'DevFlow ServerMaster':
                    raise DevFlowError('Authenticated MCP server identity changed')
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                if 'registry_validate' not in names or 'project_status' not in names or len(names) < 8:
                    raise DevFlowError('Authenticated MCP tool inventory is incomplete')
                result = await session.call_tool('registry_validate', {})
                if result.isError or not json.loads(result.content[0].text)['ok']:
                    raise DevFlowError('Authenticated MCP registry call failed')
                result = await session.call_tool('project_status', {'project_id': 'devflow'})
                if result.isError:
                    raise DevFlowError('Authenticated MCP project access failed')
                return len(names)

    def _health(self, plan, token):
        # The legacy LiveProbe rejects drop-ins; these checks apply after switching.
        import httpx
        for attempt in range(20):
            try:
                self._running_release(plan)
                with httpx.Client(timeout=5, follow_redirects=False) as client:
                    metadata = client.get('https://srun.ir/.well-known/oauth-protected-resource/mcpdevflow')
                    denied = client.post(RESOURCE, json={})
                if metadata.status_code == 200 and denied.status_code == 401:
                    break
            except (DevFlowError, httpx.HTTPError, OSError):
                if attempt == 19:
                    raise
            if attempt == 19:
                raise DevFlowError('Release failed to become healthy')
            time.sleep(1)
        if (metadata.status_code != 200 or metadata.json().get('resource') != RESOURCE or
            denied.status_code != 401 or
            'bearer' not in denied.headers.get('www-authenticate', '').lower()):
            raise DevFlowError('Public discovery or unauthorized MCP boundary changed')
        listeners = subprocess.run(['ss', '-lntH', '( sport = :8765 )'],
                                   capture_output=True, text=True, timeout=5, check=True)
        if [line.split()[3] for line in listeners.stdout.splitlines()] != ['127.0.0.1:8765']:
            raise DevFlowError('Released MCP is not loopback-only')
        verified = asyncio.run(asyncio.wait_for(self.auth_probe(token), timeout=75))
        if verified < 8:
            raise DevFlowError('Authenticated MCP tool count is incomplete')
        self._running_release(plan)
        return verified

    def _rollback(self, plan):
        directory = self.workflow.unit_path.parent / (SERVICE + '.d')
        override = directory / '10-devflow-release.conf'
        pointer = self.workflow.release_root / 'current'
        if directory.is_symlink() or (pointer.exists() and not pointer.is_symlink()):
            raise DevFlowError('Cannot safely restore service: deployment paths changed')
        if override.exists():
            override.unlink()
        if directory.exists():
            directory.rmdir()
        if pointer.is_symlink():
            if os.readlink(pointer) != plan['source']['git_head_sha']:
                raise DevFlowError('Cannot safely remove an unexpected release pointer')
            pointer.unlink()
        self.command('systemctl', 'daemon-reload')
        self.command('systemctl', 'restart', SERVICE)
        self.workflow.probe.inspect()
        if hashlib.sha256(self.workflow.unit_path.read_bytes()).hexdigest() != plan['preconditions']['unit_sha256']:
            raise DevFlowError('Legacy service unit changed during rollback')

    def _evidence(self, plan, checked, identity, outcome, details):
        return ReadExecutor(self.policy, self.continuity)._evidence({
            'operation_id': plan['operation_id'], 'kind': 'service.deploy',
            'plan_sha256': checked['plan_sha256'], 'execution_id': identity[1],
            'fence': identity[2], 'candidate_sha': plan['source']['git_head_sha'],
            'outcome': outcome, 'details': details, 'recorded_at_epoch': time.time()})

    def run(self, plan, approval_id, token_file, owner='stage4-deploy-local'):
        if os.geteuid() != 0:
            raise DevFlowError('Service migration requires a local root operator')
        checked = self.approvals.verify(plan, approval_id)
        if not checked['sensitive'] or plan['kind'] != 'service.deploy':
            raise DevFlowError('Only a specifically approved deployment may execute')
        try:
            token = read_private_file(token_file, 16384).decode().strip()
        except UnicodeError as exc:
            raise DevFlowError('OAuth probe token is invalid') from exc
        policy = NetworkPolicy(self.policy.root, self.policy.contracts,
                               '/etc/devflow/network.json')
        if asyncio.run(JWTVerifier(policy).verify_token(token)) is None:
            raise DevFlowError('OAuth probe token is invalid or lacks access')
        lease = self.continuity.acquire(owner, 600)
        identity = owner, lease['execution']['execution_id'], lease['fence']
        key = plan['operation_id']
        started = False
        try:
            prepared = self.continuity.prepare(*identity, key, {
                'kind': 'service.deploy', 'plan_sha256': checked['plan_sha256']})
            if not prepared['changed'] and prepared['operation']['status'] == 'SUCCEEDED':
                return {'ok': True, 'changed': False, 'operation': prepared['operation']}
            checked = self.approvals.verify(plan, approval_id)
            self.continuity.start(*identity, key)
            started = True
            try:
                self._stage(plan)
                self._dropin(plan)
                count = self._health(plan, token)
            except BaseException as cause:
                try:
                    self._rollback(plan)
                except BaseException:
                    self._evidence(plan, checked, identity, 'UNKNOWN',
                                   'Rollback could not be verified; inspect service and release paths')
                    raise DevFlowError('Deployment and rollback outcome unknown; reconcile before retry') from cause
                evidence = self._evidence(plan, checked, identity, 'FAILED',
                                          'Legacy service restored after unsuccessful migration')
                self.continuity.finish(*identity, key, 'FAILED', evidence)
                raise DevFlowError('Deployment failed; legacy MCP restored and failure fenced') from cause
            evidence = self._evidence(plan, checked, identity, 'SUCCEEDED',
                                      {'deployed_sha': plan['source']['git_head_sha'],
                                       'authenticated_tools': count, 'public_status': [200, 401]})
            result = self.continuity.finish(*identity, key, 'SUCCEEDED', evidence)
            return {'ok': True, 'changed': True, 'deployed_sha': plan['source']['git_head_sha'],
                    'authenticated_tools': count, 'operation': result['operation']}
        finally:
            status = self.continuity.status()
            if (status.get('lease') and
                status['lease']['execution']['execution_id'] == identity[1] and
                not any(item['status'] == 'STARTED' for item in status['operations'])):
                self.continuity.release(*identity)
