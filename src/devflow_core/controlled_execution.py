"""Stage 4.2 local read-only runner with bounded process and continuity evidence."""

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .contracts import DevFlowError


class BoundedProcess:
    def __init__(self, timeout=15, max_output=8192):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise DevFlowError('Process timeout must be 1–30 seconds')
        if type(max_output) is not int or not 128 <= max_output <= 65536:
            raise DevFlowError('Output bound must be 128–65536 bytes')
        self.timeout = timeout
        self.max_output = max_output

    def run(self, argv, cwd, env=None):
        if not isinstance(argv, (list, tuple)) or not argv:
            raise DevFlowError('Invalid internal worker invocation')
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True, stdin=subprocess.DEVNULL, env=env)
        data = bytearray()
        deadline = time.monotonic() + self.timeout
        reason = None
        try:
            with selectors.DefaultSelector() as selector:
                descriptor = process.stdout.fileno()
                os.set_blocking(descriptor, False)
                selector.register(descriptor, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = 'timeout'
                        break
                    for key, _ in selector.select(min(remaining, 0.25)):
                        block = os.read(key.fd, 65536)
                        if not block:
                            selector.unregister(key.fd)
                        elif len(data) + len(block) > self.max_output:
                            reason = 'output_limit'
                            break
                        else:
                            data.extend(block)
                    if reason:
                        break
            if reason is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'timeout'
                else:
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        reason = 'timeout'
            if reason:
                # Child processes share the worker's new session/process group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
            return {'exit_code': process.returncode, 'reason': reason, 'output': bytes(data)}
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            process.stdout.close()


class ReadExecutor:
    def __init__(self, policy, continuity, runner=None):
        if policy.root != continuity.root or policy.project_id != continuity.project_id:
            raise DevFlowError('Operation and continuity project mismatch')
        self.policy = policy
        self.continuity = continuity
        self.runner = runner or BoundedProcess()

    def _git_precondition(self, plan):
        observed = self.continuity._git()
        expected = plan['preconditions']
        if (observed['dirty'] or observed['branch'] != expected['git_branch'] or
            observed['head_sha'] != expected['git_head_sha']):
            raise DevFlowError('Git precondition changed or working tree is dirty')

    def _evidence(self, record):
        directory = self.policy.root / '.devflow' / 'evidence'
        if directory.is_symlink():
            raise DevFlowError('Evidence directory cannot be a symlink')
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DevFlowError('Evidence directory must be owner-only')
        name = 'E-' + uuid.uuid4().hex + '.json'
        fd, temporary = tempfile.mkstemp(prefix='.evidence-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / name)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return 'log://.devflow/evidence/' + name

    def run(self, plan, owner='stage4-local'):
        checked = self.policy.validate(plan)
        if plan['kind'] != 'filesystem.read':
            raise DevFlowError('Only read operations have a Stage 4.2 executor')
        if not 0 < self.runner.timeout <= 30:
            raise DevFlowError('Worker deadline must be below the lease lifetime')
        self._git_precondition(plan)
        lease = self.continuity.acquire(owner, 60)
        identity = owner, lease['execution']['execution_id'], lease['fence']
        key = plan['operation_id']
        try:
            prepared = self.continuity.prepare(*identity, key,
                {'kind': 'filesystem.read', 'plan_sha256': checked['plan_sha256']})
            if not prepared['changed'] and prepared['operation']['status'] == 'SUCCEEDED':
                return {'ok': True, 'changed': False, 'operation': prepared['operation']}
            self._git_precondition(plan)
            self.policy.validate(plan)  # Check path and policy again immediately before start.
            self.continuity.start(*identity, key)
            outcome = 'FAILED'
            result = None
            failure_reason = 'worker_unavailable'
            try:
                argv = [sys.executable, '-m', 'devflow_core.operation_worker',
                        '--root', str(self.policy.root), '--path', plan['arguments']['path']]
                environment = {'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                               'PATH': os.defpath, 'LANG': 'C.UTF-8'}
                completed = self.runner.run(argv, str(self.policy.root), env=environment)
                failure_reason = completed['reason'] or 'worker_rejected'
                if completed['exit_code'] == 0 and completed['reason'] is None:
                    result = json.loads(completed['output'])
                    failure_reason = 'digest_mismatch'
                    if (result['sha256'] == plan['postconditions']['sha256'] and
                        type(result['bytes']) is int and 0 <= result['bytes'] <= 1048576):
                        outcome = 'SUCCEEDED'
                        failure_reason = None
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                pass  # A failed/invalid worker response is recorded without raw output.
            evidence = self._evidence({
                'operation_id': key, 'plan_sha256': checked['plan_sha256'],
                'execution_id': identity[1], 'fence': identity[2], 'outcome': outcome,
                'content_sha256': result['sha256'] if outcome == 'SUCCEEDED' else None,
                'bytes': result['bytes'] if outcome == 'SUCCEEDED' else None,
                'failure_reason': failure_reason,
                'recorded_at_epoch': time.time()})
            finished = self.continuity.finish(*identity, key, outcome, evidence)
            if outcome != 'SUCCEEDED':
                raise DevFlowError('Read result failed verification; evidence was recorded')
            return {'ok': True, 'changed': True, 'execution_id': identity[1],
                    'fence': identity[2], 'operation': finished['operation'],
                    'sha256': result['sha256'], 'bytes': result['bytes']}
        finally:
            # A started but unrecorded effect stays active/unknown for reconciliation.
            status = self.continuity.status()
            if status.get('lease') and status['lease']['execution']['execution_id'] == identity[1]:
                if not any(item['status'] == 'STARTED' for item in status['operations']):
                    self.continuity.release(*identity)
