"""Local, approved Git stage-branch push with continuity and observed evidence."""

import os
import subprocess
import time

from .contracts import DevFlowError
from .controlled_execution import BoundedProcess, ReadExecutor
from .git_workflow import GitWorkflow


class GitPushExecutor:
    def __init__(self, policy, approvals, continuity, runner=None, workflow=None):
        if policy.root != continuity.root or policy.project_id != continuity.project_id:
            raise DevFlowError('Git operation and continuity project mismatch')
        self.policy = policy
        self.approvals = approvals
        self.continuity = continuity
        self.runner = runner or BoundedProcess(timeout=30, max_output=8192)
        self.workflow = workflow or GitWorkflow(policy.root, policy.contracts)
        if not 0 < self.runner.timeout < 60:
            raise DevFlowError('Git deadline must be below the lease lifetime')

    def _check(self, plan, approval_id):
        checked = self.approvals.verify(plan, approval_id)
        if plan['kind'] != 'git.push' or not checked['sensitive']:
            raise DevFlowError('Only an approved Git push plan may run')
        observed = self.workflow.remote_head(plan['arguments']['branch'])
        if (observed['branch'] != plan['preconditions']['git_branch'] or
            observed['head_sha'] != plan['preconditions']['git_head_sha'] or
            observed['refspec'] != plan['arguments']['refspec'] or
            observed['remote_head_sha'] != plan['preconditions']['remote_head_sha']):
            raise DevFlowError('Git source or remote precondition changed')
        if observed['remote_head_sha'] is not None:
            # A lease can permit rewrites. Enforce ancestry before granting that
            # capability to Git, so the only permitted update is fast-forward.
            result = subprocess.run(
                ['git', '-C', str(self.policy.root), 'merge-base', '--is-ancestor',
                 observed['remote_head_sha'], observed['head_sha']],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode != 0:
                raise DevFlowError('Remote commit is not a known local ancestor')
        return checked, observed

    def _evidence(self, plan, checked, identity, outcome, remote_sha):
        return ReadExecutor(self.policy, self.continuity)._evidence({
            'operation_id': plan['operation_id'],
            'kind': 'git.push',
            'plan_sha256': checked['plan_sha256'],
            'execution_id': identity[1], 'fence': identity[2],
            'source_sha': plan['preconditions']['git_head_sha'],
            'expected_remote_sha': plan['preconditions']['remote_head_sha'],
            'observed_remote_sha': remote_sha,
            'outcome': outcome, 'recorded_at_epoch': time.time()})

    def run(self, plan, approval_id, owner='stage4-git-local'):
        checked, _ = self._check(plan, approval_id)
        lease = self.continuity.acquire(owner, 60)
        identity = owner, lease['execution']['execution_id'], lease['fence']
        key = plan['operation_id']
        try:
            prepared = self.continuity.prepare(*identity, key, {
                'kind': 'git.push', 'plan_sha256': checked['plan_sha256']})
            if not prepared['changed'] and prepared['operation']['status'] == 'SUCCEEDED':
                return {'ok': True, 'changed': False, 'operation': prepared['operation']}
            checked, observed = self._check(plan, approval_id)
            self.continuity.start(*identity, key)
            ref = 'refs/heads/' + observed['branch']
            expected = observed['remote_head_sha'] or ''
            # The explicit old ref is a compare-and-swap guard. The ancestor
            # check above ensures it cannot authorize a history rewrite.
            argv = ['git', '-C', str(self.policy.root),
                    '-c', 'core.hooksPath=/dev/null',
                    'push', '--porcelain', '--no-follow-tags',
                    '--force-with-lease=' + ref + ':' + expected,
                    self.workflow.expected_url, observed['head_sha'] + ':' + ref]
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('GIT_CONFIG_', 'GIT_TRACE'))}
            environment.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false')
            try:
                completed = self.runner.run(argv, str(self.policy.root), env=environment)
                after = self.workflow.remote_head(observed['branch'])
            except (OSError, subprocess.SubprocessError, DevFlowError):
                self._evidence(plan, checked, identity, 'UNKNOWN', None)
                raise DevFlowError('Git push outcome is unknown; reconcile before any retry')
            remote_sha = after['remote_head_sha']
            if completed['reason'] is not None or completed['exit_code'] != 0 or (
                remote_sha != plan['postconditions']['remote_head_sha']):
                self._evidence(plan, checked, identity, 'UNKNOWN', remote_sha)
                raise DevFlowError('Git push outcome is unknown; reconcile before any retry')
            evidence = self._evidence(plan, checked, identity, 'SUCCEEDED', remote_sha)
            finished = self.continuity.finish(*identity, key, 'SUCCEEDED', evidence)
            return {'ok': True, 'changed': True, 'execution_id': identity[1],
                    'fence': identity[2], 'head_sha': remote_sha,
                    'operation': finished['operation']}
        finally:
            # Once STARTED, an unknown effect stays fenced for human review.
            status = self.continuity.status()
            if status.get('lease') and status['lease']['execution']['execution_id'] == identity[1]:
                if not any(item['status'] == 'STARTED' for item in status['operations']):
                    self.continuity.release(*identity)
