import asyncio
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import ROOT
from devflow_core.contracts import DevFlowError
from devflow_core.registry import ControlRegistry
from devflow_core.security import CredentialGate, SecurityStore, read_private_file


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def security(settings_path, contracts):
    store = SecurityStore(settings_path.parent.parent, contracts, Clock())
    store.initialize(store.root.parent / 'master-keys')
    return store


def issue(store, principal='reader', role='viewer', projects=None, refs=()):
    store.create_principal(principal, role, projects or ['devflow'], refs)
    metadata = store.issue(principal, 60)
    token = read_private_file(metadata['credential_file'], 256).decode().strip()
    return token, metadata


def key_directory(store):
    with sqlite3.connect(store.path) as connection:
        return Path(connection.execute("SELECT value FROM metadata WHERE key='key_directory'").fetchone()[0])


def test_init_is_repeatable_and_private(security):
    assert security.status()['initialized']
    assert not security.initialize(key_directory(security))['changed']
    assert security.path.stat().st_mode & 0o777 == 0o600
    assert key_directory(security).stat().st_mode & 0o777 == 0o700
    for key in key_directory(security).glob('*.key'):
        assert key.stat().st_mode & 0o777 == 0o600
        assert not key.is_relative_to(security.root)


def test_uninitialized_status_never_creates_storage(settings_path, contracts):
    store = SecurityStore(settings_path.parent.parent, contracts)
    assert not store.status()['initialized']
    assert not store.path.exists()


@pytest.mark.parametrize('case', ['inside', 'world_readable', 'symlink'])
def test_master_key_directory_policy(settings_path, contracts, tmp_path, case):
    store = SecurityStore(settings_path.parent.parent, contracts)
    if case == 'inside':
        directory = store.root / 'master-keys'
    elif case == 'world_readable':
        directory = tmp_path / 'keys'
        directory.mkdir(mode=0o755)
    else:
        actual = tmp_path / 'actual'
        actual.mkdir(mode=0o700)
        directory = tmp_path / 'keys'
        directory.symlink_to(actual, target_is_directory=True)
    with pytest.raises(DevFlowError):
        store.initialize(directory)
    assert not store.path.exists()


def test_credential_has_only_hash_in_database_and_no_value_in_output(security):
    token, metadata = issue(security)
    assert token not in json.dumps(metadata)
    assert token.encode() not in security.path.read_bytes()
    assert Path(metadata['credential_file']).stat().st_mode & 0o777 == 0o600
    assert security.authenticate(token)['principal_id'] == 'reader'
    assert security.authorize(token, 'project.read', 'devflow')['role'] == 'viewer'
    for action, project in [('vault.read', 'devflow'), ('continuity.write', 'devflow'), ('project.read', 'foreign')]:
        with pytest.raises(DevFlowError, match='Access denied'):
            security.authorize(token, action, project)
    with pytest.raises(DevFlowError, match='audience'):
        security.authenticate(token, 'another-server')


def test_expiry_and_backward_clock_deny_credentials(security):
    token, _ = issue(security)
    security.clock.value = 1061
    with pytest.raises(DevFlowError, match='expired'):
        security.authenticate(token)
    security.clock.value = 999
    with pytest.raises(DevFlowError, match='backwards'):
        security.authenticate(token)


def test_revocation_and_principal_disable_are_immediate(security):
    token, metadata = issue(security)
    gate = CredentialGate(security, metadata['credential_file'])
    assert gate.check('project.read', 'devflow')
    assert security.revoke(metadata['credential_id'])['changed']
    with pytest.raises(DevFlowError, match='revoked'):
        gate.check('project.read', 'devflow')
    assert not security.revoke(metadata['credential_id'])['changed']
    second = security.issue('reader')
    second_token = read_private_file(second['credential_file'], 256).decode().strip()
    security.disable('reader')
    with pytest.raises(DevFlowError):
        security.authenticate(second_token)
    with pytest.raises(DevFlowError):
        security.issue('reader')


def test_policy_changes_are_not_silently_applied(security):
    issue(security)
    with pytest.raises(DevFlowError, match='already exists'):
        security.create_principal('reader', 'operator', ['foreign'])
    with pytest.raises(DevFlowError):
        security.create_principal('wildcard', 'viewer', ['*'])
    with pytest.raises(DevFlowError):
        security.create_principal('reader-with-vault', 'viewer', ['devflow'], ['vault://servers/example'])


@pytest.mark.parametrize('case', ['mode', 'symlink'])
def test_credential_file_must_be_private_and_not_symlink(security, case):
    _, metadata = issue(security)
    path = Path(metadata['credential_file'])
    if case == 'mode':
        path.chmod(0o644)
        gate_path = path
    else:
        gate_path = path.with_name('link.token')
        gate_path.symlink_to(path)
    with pytest.raises(DevFlowError):
        CredentialGate(security, gate_path).principal()


@pytest.mark.parametrize('reference', ['env://TOKEN', 'vault://a/../b', 'vault://a//b', 'vault://a/b?x=1', 'vault://a/%2F', 'vault://a/*'])
def test_vault_reference_is_canonical(security, reference):
    with pytest.raises(DevFlowError):
        security.put(reference, 'devflow', b'never-output')


def test_vault_encryption_project_and_exact_reference_grants(security):
    ref = 'vault://servers/example/root'
    token, _ = issue(security, 'operator', 'operator', refs=[ref])
    secret = b'PRIVATE_VALUE_NOT_IN_SQLITE_2026'
    security.put(ref, 'devflow', secret)
    assert secret not in security.path.read_bytes()
    assert secret.decode() not in json.dumps(security.references())
    assert security.resolve(ref, 'devflow', token) == secret
    with pytest.raises(DevFlowError):
        security.resolve('vault://servers/other/root', 'devflow', token)
    with pytest.raises(DevFlowError):
        security.resolve(ref, 'foreign', token)
    with pytest.raises(DevFlowError, match='another project'):
        security.put(ref, 'foreign', b'other')
    with sqlite3.connect(security.path) as connection:
        assert secret.decode() not in ''.join(row[0] for row in connection.execute('SELECT detail FROM audit'))


def test_ciphertext_swaps_and_tampering_are_rejected(security):
    a, b = 'vault://servers/a', 'vault://servers/b'
    token, _ = issue(security, 'operator', 'operator', refs=[a, b])
    security.put(a, 'devflow', b'first')
    security.put(b, 'devflow', b'second')
    with sqlite3.connect(security.path) as connection:
        cipher = connection.execute('SELECT ciphertext FROM vault_entries WHERE reference=?', (a,)).fetchone()[0]
        connection.execute('UPDATE vault_entries SET ciphertext=? WHERE reference=?', (cipher, b))
    with pytest.raises(DevFlowError, match='verified/decrypted'):
        security.resolve(b, 'devflow', token)
    with sqlite3.connect(security.path) as connection:
        connection.execute("UPDATE vault_entries SET ciphertext='tampered' WHERE reference=?", (a,))
    with pytest.raises(DevFlowError, match='verified/decrypted'):
        security.resolve(a, 'devflow', token)


def test_key_rotation_is_transactional_and_retains_recovery_keys(security):
    ref = 'vault://servers/a'
    token, _ = issue(security, 'operator', 'operator', refs=[ref])
    security.put(ref, 'devflow', b'preserve-me')
    original_ids = security.references()
    with patch.object(security, '_audit', side_effect=RuntimeError('simulated')):
        with pytest.raises(RuntimeError):
            security.rotate_key()
    assert security.references() == original_ids
    assert security.resolve(ref, 'devflow', token) == b'preserve-me'
    # A failed/uncertain rotation may leave an orphan key; it must never delete
    # a key that could already be referenced by a committed database.
    assert len(list(key_directory(security).glob('*.key'))) >= 2
    rotated = security.rotate_key()
    assert rotated['references_rotated'] == 1 and rotated['old_keys_retained']
    assert security.references() != original_ids
    assert security.resolve(ref, 'devflow', token) == b'preserve-me'


def test_lost_master_key_is_never_automatically_regenerated(security):
    security.put('vault://servers/a', 'devflow', b'preserve-me')
    for key in key_directory(security).glob('*.key'):
        key.unlink()
    with pytest.raises(DevFlowError, match='key'):
        security.initialize(key_directory(security))
    assert len(security.references()['references']) == 1
    assert not list(key_directory(security).glob('*.key'))


def test_invalid_policy_and_token_values_fail_closed(security):
    token, _ = issue(security)
    with sqlite3.connect(security.path) as connection:
        connection.execute("UPDATE principals SET projects='\"devflow\"'")
    with pytest.raises(DevFlowError, match='policy'):
        security.authorize(token, 'project.read', 'dev')
    with pytest.raises(DevFlowError):
        security.revoke(token)
    with pytest.raises(DevFlowError) as error:
        security.authenticate('DO_NOT_PRINT_THIS')
    assert 'DO_NOT_PRINT_THIS' not in str(error.value)


def test_cli_writes_secret_from_stdin_without_echo(settings_path, contracts):
    root = settings_path.parent.parent
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
    prefix = [sys.executable, '-m', 'devflow_core.cli', '--root', str(root), '--contracts', str(ROOT / 'contracts')]
    def run(*args, input=None):
        return subprocess.run(prefix + list(args), env=env, input=input, capture_output=True, text=True, timeout=20)
    assert run('init').returncode == 0
    assert run('security', 'init', '--key-dir', str(root.parent / 'cli-keys')).returncode == 0
    secret = 'SECRET_INPUT_NOT_IN_OUTPUT'
    result = run('security', 'vault-put', '--project', 'devflow', '--reference', 'vault://servers/cli', '--from-stdin', input=secret)
    assert result.returncode == 0 and secret not in result.stdout + result.stderr
    assert run('security', 'principal-create', '--principal-id', 'reader', '--role', 'viewer', '--project', 'unknown').returncode == 1
    assert run('security', 'principal-create', '--principal-id', 'reader', '--role', 'viewer', '--project', 'devflow').returncode == 0
    issued = run('security', 'issue', '--principal-id', 'reader')
    metadata = json.loads(issued.stdout)
    token = read_private_file(metadata['credential_file'], 256).decode().strip()
    assert token not in issued.stdout + issued.stderr
    assert run('security', 'revoke', '--credential-id', metadata['credential_id']).returncode == 0


async def scoped_mcp_check(root, contracts, registry_path, credential_file, store, credential_id):
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'devflow_core.cli', '--root', str(root), '--contracts', str(ROOT / 'contracts'),
              '--registry', str(registry_path), 'serve', '--credential-file', str(credential_file)], env=env)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert len(listed.tools) == 8
            projects = await session.call_tool('registry_projects', {})
            assert not projects.isError
            body = json.loads(projects.content[0].text)
            assert [p['project_id'] for p in body['projects']] == ['devflow']
            servers = await session.call_tool('registry_servers', {})
            assert not servers.isError
            assert all(s['server_id'] != 'foreign-server' for s in json.loads(servers.content[0].text)['servers'])
            denied = await session.call_tool('project_status', {'project_id': 'foreign'})
            assert denied.isError
            own = await session.call_tool('continuity_status', {'project_id': 'devflow'})
            assert not own.isError
            for name in ('servermaster_ping', 'registry_validate', 'security_status'):
                allowed = await session.call_tool(name, {})
                assert not allowed.isError, name
            allowed = await session.call_tool('project_status', {'project_id': 'devflow'})
            assert not allowed.isError
            implicit = await session.call_tool('project_status', {})
            assert implicit.isError  # The active project is foreign and must not leak.
            store.revoke(credential_id)
            revoked = await session.call_tool('registry_projects', {})
            assert revoked.isError
            ping = await session.call_tool('servermaster_ping', {})
            assert ping.isError


def test_real_mcp_filters_projects_and_checks_revocation_on_open_session(security, settings_path, contracts):
    registry_path = security.root / 'control/registry.json'
    registry = ControlRegistry(registry_path, contracts)
    registry.register(settings_path)
    settings = json.loads(settings_path.read_text())
    settings['project']['id'] = 'foreign'
    settings['repository']['name'] = 'foreign'
    settings['servers']['control']['server_id'] = 'foreign-server'
    foreign = security.root.parent / 'foreign/.ai-project/project.settings.json'
    foreign.parent.mkdir(parents=True)
    foreign.write_text(json.dumps(settings))
    registry.register(foreign)
    registry.activate('foreign')
    security.create_principal('reader', 'viewer', ['devflow'])
    live_store = SecurityStore(security.root, contracts)
    metadata = live_store.issue('reader')
    asyncio.run(asyncio.wait_for(scoped_mcp_check(security.root, contracts, registry_path,
        metadata['credential_file'], live_store, metadata['credential_id']), timeout=45))


def test_agent_identity_is_project_bound_and_has_no_vault_or_shell_authority(security):
    token, _ = issue(security, 'agent-server-one', 'agent')
    assert security.authorize(token, 'continuity.write', 'devflow')['role'] == 'agent'
    for action in ('vault.read', 'vault.write', 'shell.execute', 'registry.read'):
        with pytest.raises(DevFlowError):
            security.authorize(token, action, 'devflow', 'vault://servers/a')
    with pytest.raises(DevFlowError):
        security.authorize(token, 'continuity.write', 'another')


def test_stage3a_milestone_does_not_complete_agent_or_network(security, contracts):
    import importlib.util
    from devflow_core.continuity import Continuity
    root = security.root
    shutil.copytree(ROOT / 'contracts', root / 'contracts')
    folder = root / '.ai-project'
    design = json.loads((ROOT / '.ai-project/project.design.json').read_text())
    for stage in design['stages'][:3]:
        stage['status'] = 'COMPLETED'
        for work in stage['works']:
            work['status'] = 'COMPLETED'
    # Reconstruct the historical Stage 2 fixture from the current project
    # design, which may already contain completed Stage 4 work.
    for stage in design['stages'][3:]:
        stage['status'] = 'PLANNED'
        for work in stage['works']:
            work['status'] = 'PENDING'
    (folder / 'project.design.json').write_text(json.dumps(design))
    for name in ('state.json', 'resume.bundle.json'):
        path = folder / name
        document = json.loads(path.read_text())
        document['current'].update(stage_id='STAGE-02', work_id='WORK-02-05')
        if name == 'state.json':
            document['current'].update(stage_status='COMPLETED', work_status='COMPLETED')
        else:
            document['current']['status'] = 'COMPLETED'
        document['latest_checkpoint_id'] = 'CP-STAGE02-VERIFIED'
        path.write_text(json.dumps(document))
    cp = json.loads((ROOT / '.ai-project/checkpoints/CP-STAGE01-SOURCE.json').read_text())
    cp['checkpoint_id'] = 'CP-STAGE02-VERIFIED'
    (folder / 'checkpoints').mkdir()
    (folder / 'checkpoints/CP-STAGE02-VERIFIED.json').write_text(json.dumps(cp))
    (root / '.gitignore').write_text('.devflow/\n')
    subprocess.run(['git', 'init', '-b', 'stage/03a-identity-vault', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=DevFlow Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-m', 'fixture'], check=True, capture_output=True)
    Continuity(root, contracts).initialize()
    spec = importlib.util.spec_from_file_location('record_stage3a', ROOT / 'scripts/record_stage3a_verification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.record(root)
    assert not result['stage_completed']
    updated = json.loads((folder / 'project.design.json').read_text())['stages'][3]
    assert updated['status'] == 'ACTIVE'
    assert [w['status'] for w in updated['works']] == ['COMPLETED', 'COMPLETED', 'PENDING', 'PENDING']
    before = {p: p.read_bytes() for p in folder.rglob('*.json')}
    assert not module.record(root)['changed']
    assert all(p.read_bytes() == contents for p, contents in before.items())
