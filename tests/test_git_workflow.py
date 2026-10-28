import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT
from devflow_core.contracts import DevFlowError
from devflow_core.git_workflow import GitWorkflow


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(settings_path, contracts):
    root = settings_path.parent.parent
    (root / 'README.md').write_text('tracked user content\n')
    git(root, 'init', '-b', 'stage/04c-git')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=DevFlow Test', '-c', 'user.email=test@example.invalid',
        'commit', '-m', 'fixture')
    git(root, 'remote', 'add', 'origin', 'https://github.com/ostaddehdari/DevFlow.git')
    return root, GitWorkflow(root, contracts)


def test_exact_stage_branch_and_origin_produce_read_only_inspection(repository):
    root, workflow = repository
    observed = workflow.inspect('stage/04c-git')
    assert observed['head_sha'] == git(root, 'rev-parse', 'HEAD')
    assert observed['refspec'] == 'HEAD:refs/heads/stage/04c-git'
    assert observed['push_enabled'] is False and observed['force_push'] is False
    assert git(root, 'status', '--porcelain') == ''


def test_uncommitted_user_changes_are_preserved(repository):
    root, workflow = repository
    before = (root / 'README.md').read_text() + 'local edit\n'
    (root / 'README.md').write_text(before)
    with pytest.raises(DevFlowError, match='user changes'):
        workflow.inspect('stage/04c-git')
    assert (root / 'README.md').read_text() == before
    assert git(root, 'status', '--porcelain')


@pytest.mark.parametrize('branch', ['main', 'stage/../escape', 'stage//bad',
                                   'stage/bad.', 'stage/-bad', 'stage/abc@{1}'])
def test_branch_scope_rejects_invalid_ref(repository, branch):
    _, workflow = repository
    with pytest.raises(DevFlowError):
        workflow.inspect(branch)


@pytest.mark.parametrize('change', ['wrong_origin', 'alternate_push_url', 'mirror', 'implicit_push'])
def test_rejects_unexpected_push_destination_or_configuration(repository, change):
    root, workflow = repository
    if change == 'wrong_origin':
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/other/other.git')
    elif change == 'alternate_push_url':
        git(root, 'config', '--add', 'remote.origin.pushurl', 'https://github.com/other/other.git')
    elif change == 'mirror':
        git(root, 'config', 'remote.origin.mirror', 'true')
    else:
        git(root, 'config', 'remote.origin.push', 'refs/heads/main:refs/heads/main')
    with pytest.raises(DevFlowError, match='origin|Mirror|refspec'):
        workflow.inspect('stage/04c-git')


def test_local_cli_inspects_without_push(repository):
    root, _ = repository
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
    result = subprocess.run([sys.executable, '-m', 'devflow_core.cli',
                            '--root', str(root), '--contracts', str(ROOT / 'contracts'),
                            'git-plan', 'inspect',
                            '--branch', 'stage/04c-git'], env=env,
                           capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['push_enabled'] is False
