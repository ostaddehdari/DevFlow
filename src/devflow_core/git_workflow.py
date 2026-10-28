"""Read-only Git source and destination checks for controlled stage branches."""

import re
import subprocess
from pathlib import Path

from .contracts import DevFlowError, read_json


class GitWorkflow:
    def __init__(self, root, contracts):
        self.root = Path(root).resolve()
        settings = read_json(self.root / '.ai-project/project.settings.json')
        contracts.validate(settings, 'project.settings.schema.json')
        repository = settings['repository']
        self.project_id = settings['project']['id']
        if repository['provider'] != 'github' or repository.get('branch_strategy') != 'stage':
            raise DevFlowError('Stage branch workflow requires a GitHub stage repository')
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', repository[key]) or
               '..' in repository[key] for key in ('owner', 'name')):
            raise DevFlowError('Configured GitHub repository identity is invalid')
        self.expected_url = 'https://github.com/{}/{}.git'.format(
            repository['owner'], repository['name'])

    def _git(self, *args):
        try:
            result = subprocess.run(['git', '-C', str(self.root), *args],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DevFlowError('Git inspection failed') from exc
        if len(result.stdout.encode()) > 65536:
            raise DevFlowError('Git inspection exceeds output bound')
        return result.stdout.strip()

    def _config(self, key):
        try:
            result = subprocess.run(['git', '-C', str(self.root), 'config', '--get-all', key],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DevFlowError('Git configuration inspection failed') from exc
        if result.returncode not in (0, 1) or len(result.stdout.encode()) > 4096:
            raise DevFlowError('Git configuration inspection failed')
        return result.stdout.strip().splitlines()

    def inspect(self, branch):
        if (not isinstance(branch, str) or not re.fullmatch(r'stage/[a-z0-9][a-z0-9._/-]{0,79}', branch) or
            '..' in branch or '//' in branch or branch.endswith(('.', '/'))):
            raise DevFlowError('Only a canonical stage branch is allowed')
        if self._git('check-ref-format', '--branch', branch) != branch:
            raise DevFlowError('Stage branch is not a valid Git ref')
        head = self._git('rev-parse', '--verify', 'HEAD')
        current = self._git('branch', '--show-current')
        if current != branch:
            raise DevFlowError('Current branch differs from requested stage branch')
        # Include untracked files: they may be user work that must be preserved.
        if self._git('status', '--porcelain=v1', '--untracked-files=all'):
            raise DevFlowError('Working tree has user changes; no Git action is authorized')
        fetch_urls = self._git('remote', 'get-url', '--all', 'origin').splitlines()
        push_urls = self._git('remote', 'get-url', '--push', '--all', 'origin').splitlines()
        if fetch_urls != [self.expected_url] or push_urls != [self.expected_url]:
            raise DevFlowError('Git origin is outside the configured repository')
        if self._config('remote.origin.mirror') not in ([], ['false']):
            raise DevFlowError('Mirror push is forbidden')
        if self._config('remote.origin.push'):
            raise DevFlowError('Implicit push refspecs are forbidden')
        refspec = 'HEAD:refs/heads/' + branch
        return {'ok': True, 'project_id': self.project_id, 'branch': branch,
                'head_sha': head, 'remote': 'origin', 'remote_url': self.expected_url,
                'refspec': refspec, 'force_push': False, 'push_enabled': False}
