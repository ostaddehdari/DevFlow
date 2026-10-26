import asyncio
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import ROOT
from devflow_core.agent_worker import AgentView
from devflow_core.agents import AgentStore, atomic_json
from devflow_core.contracts import DevFlowError
from devflow_core.registry import ControlRegistry
from devflow_core.security import SecurityStore


def can_switch_uid():
    try:
        mapping = [tuple(map(int, line.split())) for line in Path('/proc/self/uid_map').read_text().splitlines()]
        uid = pwd.getpwnam('nobody').pw_uid
        cap_line = next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
        caps = int(cap_line.split()[1], 16)
        return any(start <= uid < start + length for start, _, length in mapping) and bool(caps & 1) and bool(caps & (1 << 7)) and bool(caps & (1 << 6))
    except (OSError, KeyError, StopIteration):
        return False


CAN_SWITCH_UID = can_switch_uid()


@pytest.fixture
def enrolled(settings_path, contracts, tmp_path, monkeypatch):
    assert os.geteuid() == 0
    root = settings_path.parent.parent
    (root / 'docs').mkdir()
    (root / 'src').mkdir()
    (root / 'docs/public.txt').write_text('public document')
    (root / 'docs/.env').write_text('DO_NOT_RETURN')
    (root / 'docs/private.key').write_text('DO_NOT_RETURN')
    registry = ControlRegistry(root / 'control/registry.json', contracts)
    registry.register(settings_path)
    security = SecurityStore(root, contracts)
    security.initialize(tmp_path / 'keys')
    user = pwd.getpwnam('nobody')
    credentials = tmp_path / 'agent-credentials'
    credentials.mkdir(mode=0o700)
    if CAN_SWITCH_UID:
        os.chown(credentials, user.pw_uid, user.pw_gid)
    else:
        # Only the OS ownership adapter is simulated in this single-UID build
        # environment. Path, token, policy, revocation and persistence logic are real.
        monkeypatch.setattr('devflow_core.agents.credential_directory_for', lambda path, root, uid: Path(path))
        monkeypatch.setattr(os, 'fchown', lambda *arguments: None)
    store = AgentStore(root, contracts)
    store.initialize(tmp_path / 'profiles')
    store.enroll(registry, 'test-agent', 'devflow-control-server', 'devflow', 'nobody', credentials,
                 ['docs', 'src'], ['system.info', 'filesystem.list', 'filesystem.read'])
    return root, registry, security, store, credentials


def operator_view(enrolled, contracts):
    root, _, _, store, _ = enrolled
    record, profile_path = store.record('test-agent')
    # Policy unit tests run as the trusted operator and read its copy. The real
    # worker integration test below runs under the non-root agent UID instead.
    credential = root / '.devflow/credentials' / (record['profile']['credential_id'] + '.token')
    return AgentView(profile_path, credential, contracts, 'test-agent')


def test_enrollment_and_inventory_are_scoped_and_secret_free(enrolled):
    _, _, security, store, _ = enrolled
    inventory = store.inventory(['devflow'])
    assert len(inventory['agents']) == 1
    assert inventory['agents'][0]['run_uid'] != 0
    assert inventory['agents'][0]['remote_verified'] is False
    assert 'credential_sha256' not in json.dumps(inventory)
    assert store.inventory(['foreign'])['agents'] == []
    assert security.status()['active_credentials'] == 1


def test_identical_enrollment_is_idempotent_and_rebinding_is_rejected(enrolled):
    _, registry, security, store, credentials = enrolled
    assert not store.enroll(registry, 'test-agent', 'devflow-control-server', 'devflow', 'nobody', credentials,
        ['src', 'docs'], ['filesystem.read', 'system.info', 'filesystem.list'])['changed']
    with pytest.raises(DevFlowError, match='already bound'):
        store.enroll(registry, 'test-agent', 'devflow-control-server', 'devflow', 'nobody', credentials,
            ['docs'], ['system.info'])
    assert security.status()['active_credentials'] == 1


def test_root_agent_account_is_rejected(enrolled):
    _, registry, _, store, credentials = enrolled
    with pytest.raises(DevFlowError, match='non-root'):
        store.enroll(registry, 'root-agent', 'devflow-control-server', 'devflow', 'root', credentials,
            ['docs'], ['system.info'])


@pytest.mark.parametrize('path', ['../etc/passwd', '/etc/passwd', 'docs/../src/file', 'docs//public.txt',
    'docs/.env', 'docs/private.key', '.devflow/security.sqlite3', 'docs/secrets/anything', 'src/keys/value', 'docs\\public.txt'])
def test_path_escape_and_sensitive_names_are_rejected(enrolled, contracts, path):
    view = operator_view(enrolled, contracts)
    with pytest.raises(DevFlowError):
        view.read_text(path)


def test_listing_is_bounded_and_filters_secret_names(enrolled, contracts):
    view = operator_view(enrolled, contracts)
    entries = view.listing('docs')['entries']
    assert entries == [{'name': 'public.txt', 'type': 'file'}]
    with pytest.raises(DevFlowError):
        view.listing('docs', limit=201)
    with pytest.raises(DevFlowError):
        view.read_text('docs/public.txt', 65537)
    assert view.read_text('docs/public.txt', 6) == {
        'ok': True, 'project_id': 'devflow', 'path': 'docs/public.txt', 'text': 'public', 'truncated': True}


def test_symlinks_hardlinks_and_fifos_are_not_read(enrolled, contracts, tmp_path):
    root, _, _, _, _ = enrolled
    outside = tmp_path / 'outside.txt'
    outside.write_text('DO_NOT_RETURN')
    (root / 'docs/link.txt').symlink_to(outside)
    (root / 'docs/linkdir').symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, root / 'docs/hardlink.txt')
    os.mkfifo(root / 'docs/fifo')
    view = operator_view(enrolled, contracts)
    for name in ('link.txt', 'linkdir/outside.txt', 'hardlink.txt', 'fifo'):
        with pytest.raises(DevFlowError):
            view.read_text('docs/' + name)
    assert [entry['name'] for entry in view.listing('docs')['entries']] == ['public.txt']


def test_parent_directory_symlink_swap_is_not_followed(enrolled, contracts, tmp_path):
    root = enrolled[0]
    view = operator_view(enrolled, contracts)
    saved = root / 'saved-docs'
    (root / 'docs').rename(saved)
    (root / 'docs').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DevFlowError, match='symlink'):
        view.read_text('docs/outside.txt')


def test_read_caps_and_identity_are_rechecked_each_call(enrolled, contracts):
    _, _, _, store, _ = enrolled
    view = operator_view(enrolled, contracts)
    assert view.read_text('docs/public.txt')['text'] == 'public document'
    _, profile_path = store.record('test-agent')
    profile = json.loads(profile_path.read_text())
    profile['capabilities'] = ['system.info']
    atomic_json(profile_path, profile, 0o644)
    with pytest.raises(DevFlowError, match='capability'):
        view.read_text('docs/public.txt')
    profile['enabled'] = False
    atomic_json(profile_path, profile, 0o644)
    with pytest.raises(DevFlowError, match='disabled'):
        view.info()


def test_control_credential_revocation_disables_agent_profile(enrolled, contracts):
    _, _, security, store, _ = enrolled
    view = operator_view(enrolled, contracts)
    record, _ = store.record('test-agent')
    security.revoke(record['profile']['credential_id'])
    with pytest.raises(DevFlowError, match='disabled'):
        view.info()
    assert store.inventory()['agents'][0]['status'] == 'REVOKED'
    with pytest.raises(DevFlowError, match='revoked'):
        store.renew('test-agent')


def test_principal_disable_also_revokes_agent(enrolled, contracts):
    _, _, security, store, _ = enrolled
    view = operator_view(enrolled, contracts)
    record, _ = store.record('test-agent')
    security.disable(record['profile']['principal_id'])
    with pytest.raises(DevFlowError):
        view.info()


def test_credential_renewal_does_not_change_grants(enrolled):
    _, _, security, store, _ = enrolled
    previous, path = store.record('test-agent')
    old_profile = previous['profile']
    store.renew('test-agent')
    current, _ = store.record('test-agent')
    assert current['profile']['credential_id'] != old_profile['credential_id']
    for name in ('allowed_paths', 'capabilities', 'project_id', 'server_id', 'run_uid'):
        assert current['profile'][name] == old_profile[name]
    assert json.loads(path.read_text()) == current['profile']
    assert security.status()['active_credentials'] == 1


@pytest.fixture(scope='module')
def public_runtime():
    # /workspace/scratch is private. Do not relax its permissions for a test.
    # Place a separate code/dependency copy in our own public /tmp directory.
    with tempfile.TemporaryDirectory(prefix='devflow-agent-runtime-', dir='/tmp') as name:
        root = Path(name)
        root.chmod(0o755)
        shutil.copytree(ROOT / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'contracts', root / 'contracts')
        deps = Path(sys.prefix) / 'lib/python3.12/site-packages'
        def link_or_copy(source, destination):
            try:
                os.link(source, destination)
            except OSError:
                shutil.copyfile(source, destination)
            return destination
        shutil.copytree(deps, root / 'deps', copy_function=link_or_copy,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'pip', 'pip-*', 'setuptools', 'setuptools-*'))
        yield root


async def actual_agent_session(runtime, profile_path, credential_file, uid, security, credential_id):
    nonce = 'local_probe_123'
    parameters = StdioServerParameters(command=shutil.which('runuser'),
        args=['-u', 'nobody', '--', os.path.realpath(sys.executable), '-s', '-m', 'devflow_core.cli',
            '--contracts', str(runtime / 'contracts'), 'agent-worker', '--agent-id', 'live-agent',
            '--profile', str(profile_path), '--credential-file', str(credential_file)],
        env={'PYTHONPATH': str(runtime / 'src') + ':' + str(runtime / 'deps'),
             'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(credential_file.parent), 'PATH': '/usr/bin:/bin'})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == 'DevFlow Restricted Agent'
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {'agent_info', 'agent_list', 'agent_read'}
            response = await session.call_tool('agent_info', {'nonce': nonce})
            assert not response.isError
            info = json.loads(response.content[0].text)
            assert info['uid'] == uid != 0 and info['nonce'] == nonce and info['no_new_privileges']
            assert not (await session.call_tool('agent_list', {'path': 'docs'})).isError
            read_result = await session.call_tool('agent_read', {'path': 'docs/public.txt'})
            assert not read_result.isError
            assert json.loads(read_result.content[0].text)['text'] == 'live public text'
            for path in ('/etc/shadow', '../master-keys/value', 'docs/.env'):
                assert (await session.call_tool('agent_read', {'path': path})).isError
            assert (await session.call_tool('shell_exec', {'command': 'id'})).isError
            security.revoke(credential_id)
            assert (await session.call_tool('agent_info', {})).isError


@pytest.mark.skipif(not CAN_SWITCH_UID, reason='Single-UID build environment; mandatory real non-root probe runs on the target server')
def test_real_agent_runs_non_root_and_revocation_hits_open_session(public_runtime, contracts):
    with tempfile.TemporaryDirectory(prefix='devflow-agent-fixture-', dir='/tmp') as name:
        base = Path(name)
        base.chmod(0o755)
        root = base / 'project'
        (root / '.ai-project').mkdir(parents=True)
        (root / 'docs').mkdir()
        (root / 'src').mkdir()
        (root / 'docs/public.txt').write_text('live public text')
        settings = json.loads((ROOT / '.ai-project/project.settings.json').read_text())
        settings_path = root / '.ai-project/project.settings.json'
        settings_path.write_text(json.dumps(settings))
        registry = ControlRegistry(root / 'control/registry.json', contracts)
        registry.register(settings_path)
        security = SecurityStore(root, contracts)
        security.initialize(base / 'master-keys')
        user = pwd.getpwnam('nobody')
        credentials = base / 'credentials'
        credentials.mkdir(mode=0o700)
        os.chown(credentials, user.pw_uid, user.pw_gid)
        store = AgentStore(root, contracts)
        store.initialize(base / 'profiles')
        result = store.enroll(registry, 'live-agent', 'devflow-control-server', 'devflow', 'nobody', credentials,
            ['docs', 'src'], ['system.info', 'filesystem.list', 'filesystem.read'])
        record, profile_path = store.record('live-agent')
        asyncio.run(asyncio.wait_for(actual_agent_session(public_runtime, profile_path,
            Path(result['credential_file']), user.pw_uid, security, record['profile']['credential_id']), timeout=45))


def test_worker_refuses_root_process(enrolled):
    _, _, _, store, _ = enrolled
    record, profile_path = store.record('test-agent')
    result = subprocess.run([sys.executable, '-m', 'devflow_core.cli', 'agent-worker', '--agent-id', 'test-agent',
        '--profile', str(profile_path), '--credential-file', record['credential_file']],
        env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and 'root' in result.stderr


def test_real_credential_directory_owner_check(tmp_path):
    from devflow_core.agents import credential_directory_for
    path = tmp_path / 'private'
    path.mkdir(mode=0o700)
    with pytest.raises(DevFlowError, match='owned by its account'):
        credential_directory_for(path, tmp_path / 'project', 65534)


@pytest.mark.parametrize('case', ['root_uid', 'wrong_hash', 'capabilities', 'no_new_privileges', 'expired_proof', 'escape_not_denied'])
def test_milestone_rejects_missing_or_invalid_non_root_proof(enrolled, contracts, case):
    import importlib.util
    import time
    root, _, _, store, _ = enrolled
    # The verifier rejects the proof before changing source documents. This test
    # deliberately does not fabricate a successful OS privilege transition.
    shutil.copytree(ROOT / 'contracts', root / 'contracts')
    shutil.copyfile(ROOT / '.ai-project/project.design.json', root / '.ai-project/project.design.json')
    record, _ = store.record('test-agent')
    profile = record['profile']
    original = json.loads(store.path.read_text())
    original['agents']['devflow-local'] = original['agents'].pop('test-agent')
    original['agents']['devflow-local']['agent_id'] = 'devflow-local'
    profile = dict(profile, agent_id='devflow-local')
    original['agents']['devflow-local']['profile'] = profile
    atomic_json(store.path, original, 0o600)
    atomic_json(Path(original['profile_directory']) / 'devflow-local.json', profile, 0o644)
    proof = {'ok': True, 'agent_id': 'devflow-local', 'server_id': profile['server_id'], 'project_id': 'devflow',
        'uid': profile['run_uid'], 'gid': profile['run_gid'], 'no_new_privileges': True, 'effective_capabilities': 0,
        'transport': 'local_stdio', 'tools_verified': 3, 'escape_denied': True, 'shell_denied': True,
        'verified_at_epoch': time.time(), 'profile_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()}
    if case == 'root_uid': proof['uid'] = 0
    elif case == 'wrong_hash': proof['profile_sha256'] = 'wrong'
    elif case == 'capabilities': proof['effective_capabilities'] = 1
    elif case == 'no_new_privileges': proof['no_new_privileges'] = False
    elif case == 'expired_proof': proof['verified_at_epoch'] = 0
    else: proof['escape_denied'] = False
    atomic_json(root / '.devflow/agent-probe.json', proof, 0o600)
    before = {p: p.read_bytes() for p in (root / '.ai-project').glob('*.json')}
    spec = importlib.util.spec_from_file_location('record_stage3b', ROOT / 'scripts/record_stage3b_verification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(DevFlowError, match='real non-root'):
        module.record(root)
    assert all(p.read_bytes() == data for p, data in before.items())


def test_partial_profile_revocation_cannot_be_undone_by_renewal(enrolled):
    _, _, security, store, _ = enrolled
    record, path = store.record('test-agent')
    atomic_json(path, dict(record['profile'], enabled=False), 0o644)
    before = security.status()['active_credentials']
    with pytest.raises(DevFlowError, match='disabled'):
        store.renew('test-agent')
    assert security.status()['active_credentials'] == before
    assert not json.loads(path.read_text())['enabled']
