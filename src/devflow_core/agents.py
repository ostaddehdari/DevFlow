"""Root-administered local agent enrollment. Profiles contain no credential values."""

import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .contracts import DevFlowError, read_json
from .security import SecurityStore, _name, read_private_file

CAPABILITIES = frozenset({'system.info', 'filesystem.list', 'filesystem.read'})
DENIED_SUFFIXES = ('.key', '.pem', '.p12', '.pfx', '.token', '.sqlite', '.sqlite3')
DENIED_PARTS = {'keys', 'credentials', 'vault', 'secrets'}


def relative_parts(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value or '\x00' in value:
        raise DevFlowError('Expected a bounded relative POSIX path')
    parts = value.split('/')
    if any(not p or p.startswith('.') or p.lower() in DENIED_PARTS or p.lower().endswith(DENIED_SUFFIXES) for p in parts):
        raise DevFlowError('Hidden, secret, traversal or empty path components are not permitted')
    return tuple(parts)


def atomic_json(path, value, mode):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if len(encoded.encode()) > 1048576:
        raise DevFlowError('Agent metadata exceeds its 1 MiB limit')
    fd, temporary = tempfile.mkstemp(prefix='.agent-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def controlled_directory(path, create=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise DevFlowError('Agent profile directory must be absolute and contain no symlinks')
    if create:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise DevFlowError('Agent profiles must be in a root-owned non-writable-by-others directory')
    return path


def credential_directory_for(path, root, uid):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path or path.is_relative_to(root)):
        raise DevFlowError('Agent credential directory must be outside the repository without symlinks')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise DevFlowError('Agent credential directory must be private and owned by its account')
    return path


class AgentStore:
    def __init__(self, root, contracts):
        self.root = Path(root).resolve()
        self.contracts = contracts
        self.path = self.root / '.devflow/agents.json'

    @contextmanager
    def _lock(self):
        if os.geteuid() != 0:
            raise DevFlowError('Agent administration requires the trusted root operator')
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise DevFlowError('Agent storage cannot be a symlink')
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def read(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise DevFlowError('Agent storage cannot be a symlink')
        value = json.loads(read_private_file(self.path))
        if value.get('schema_version') != '1.0' or value.get('control_root') != str(self.root) or not isinstance(value.get('agents'), dict):
            raise DevFlowError('Agent registry identity/shape mismatch')
        directory = controlled_directory(value['profile_directory'])
        if directory.is_relative_to(self.root):
            raise DevFlowError('Agent profiles must stay outside the repository')
        for agent_id, record in value['agents'].items():
            if _name(agent_id, 'agent ID') != record.get('agent_id'):
                raise DevFlowError('Agent registry binding mismatch')
            if record.get('status') not in {'ENROLLED', 'REVOKED'}:
                raise DevFlowError('Invalid agent enrollment status')
            self.contracts.validate(record['profile'], 'agent.profile.schema.json')
        return value

    def initialize(self, directory):
        with self._lock():
            directory = controlled_directory(directory, create=True)
            if directory.is_relative_to(self.root):
                raise DevFlowError('Agent profiles must stay outside the repository')
            current = self.read()
            if current:
                if current['profile_directory'] != str(directory):
                    raise DevFlowError('Agent registry already bound to another profile directory')
                return {'ok': True, 'changed': False}
            atomic_json(self.path, {'schema_version': '1.0', 'control_root': str(self.root),
                'profile_directory': str(directory), 'agents': {}}, 0o600)
            return {'ok': True, 'changed': True}

    def inventory(self, projects=None):
        data = self.read()
        output = []
        if data:
            for record in data['agents'].values():
                profile = record['profile']
                if projects is not None and profile['project_id'] not in projects:
                    continue
                output.append({key: profile[key] for key in ('agent_id', 'server_id', 'project_id', 'allowed_paths', 'capabilities', 'run_uid')})
                output[-1].update(status=record['status'], transport='local_stdio', remote_verified=False,
                                  expires_at_epoch=profile['expires_at_epoch'])
        return {'ok': True, 'initialized': data is not None, 'agents': output}

    def record(self, agent_id):
        _name(agent_id, 'agent ID')
        data = self.read()
        if not data or agent_id not in data['agents']:
            raise DevFlowError('Unknown agent')
        return data['agents'][agent_id], Path(data['profile_directory']) / (agent_id + '.json')

    def enroll(self, registry, agent_id, server_id, project_id, account, credential_directory, allowed_paths, capabilities):
        _name(agent_id, 'agent ID')
        _name(server_id, 'server ID')
        if len(agent_id) > 80:
            raise DevFlowError('Agent ID must be at most 80 characters')
        if not allowed_paths or 'system.info' not in capabilities or not set(capabilities).issubset(CAPABILITIES):
            raise DevFlowError('Agent needs explicit paths and supported read-only capabilities')
        for path in allowed_paths:
            relative_parts(path)
        data = registry.read()
        project = next((p for p in data['projects'] if p['project_id'] == project_id), None)
        if project is None:
            raise DevFlowError('Unknown agent project binding')
        settings_path = Path(project['settings_path'])
        if not settings_path.is_absolute() or settings_path.parent.name != '.ai-project':
            raise DevFlowError('Agent project needs an absolute settings binding')
        project_root = settings_path.parent.parent
        if project_root.resolve() != project_root:
            raise DevFlowError('Agent project root cannot contain symlinks')
        settings = read_json(settings_path)
        self.contracts.validate(settings, 'project.settings.schema.json')
        if settings['project']['id'] != project_id or server_id not in {s['server_id'] for s in settings['servers'].values()}:
            raise DevFlowError('Agent server is not bound to this project')
        for path in allowed_paths:
            candidate = project_root / path
            if candidate.resolve() != candidate or not candidate.is_dir():
                raise DevFlowError('Every allowed path must be an existing real directory inside the project')
        try:
            user = pwd.getpwnam(account)
        except KeyError as exc:
            raise DevFlowError('Agent account does not exist') from exc
        if user.pw_uid == 0 or user.pw_gid == 0 or set(os.getgrouplist(account, user.pw_gid)) != {user.pw_gid}:
            raise DevFlowError('Agent must use a non-root account and group without supplementary grants')
        credential_directory = credential_directory_for(credential_directory, self.root, user.pw_uid)
        with self._lock():
            current = self.read()
            if current is None:
                raise DevFlowError('Initialize the agent registry first')
            existing = current['agents'].get(agent_id)
            if existing:
                old = existing['profile']
                if (existing['status'] != 'ENROLLED' or old['server_id'] != server_id or old['project_id'] != project_id or
                    old['project_root'] != str(project_root) or old['run_uid'] != user.pw_uid or
                    old['allowed_paths'] != sorted(set(allowed_paths)) or old['capabilities'] != sorted(set(capabilities))):
                    raise DevFlowError('Agent ID already bound; existing grants were preserved')
                actual = read_json(Path(current['profile_directory']) / (agent_id + '.json'))
                if actual != old or not actual['enabled']:
                    raise DevFlowError('Agent profile needs explicit reconciliation')
                return {'ok': True, 'changed': False, 'agent_id': agent_id}
            security = SecurityStore(self.root, self.contracts)
            principal_id = 'agent-' + agent_id
            security.create_principal(principal_id, 'agent', [project_id])
            issued = security.issue(principal_id, 86400)
            credential = read_private_file(issued['credential_file'], 256)
            destination = credential_directory / (agent_id + '.token')
            if destination.exists() or destination.is_symlink():
                security._revoke_local(issued['credential_id'])
                raise DevFlowError('Agent credential destination already exists; inspect it first')
            try:
                fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    os.fchown(stream.fileno(), user.pw_uid, user.pw_gid)
                    stream.write(credential)
                    stream.flush()
                    os.fsync(stream.fileno())
                profile = {'schema_version': '1.0', 'agent_id': agent_id, 'server_id': server_id,
                    'project_id': project_id, 'project_root': str(project_root), 'principal_id': principal_id,
                    'run_uid': user.pw_uid, 'run_gid': user.pw_gid, 'allowed_paths': sorted(set(allowed_paths)),
                    'capabilities': sorted(set(capabilities)), 'credential_id': issued['credential_id'],
                    'credential_sha256': hashlib.sha256(credential.decode().strip().encode()).hexdigest(),
                    'expires_at_epoch': issued['expires_at_epoch'], 'enabled': False}
                self.contracts.validate(profile, 'agent.profile.schema.json')
                profile_path = Path(current['profile_directory']) / (agent_id + '.json')
                atomic_json(profile_path, profile, 0o644)
                current['agents'][agent_id] = {'agent_id': agent_id, 'status': 'ENROLLED', 'account': account,
                    'credential_file': str(destination), 'profile': dict(profile, enabled=True)}
                atomic_json(self.path, current, 0o600)
                profile['enabled'] = True
                atomic_json(profile_path, profile, 0o644)
            except BaseException:
                # Deny via the profile before attempting database cleanup. Never
                # re-enable a partially revoked/enrolled agent implicitly.
                try:
                    if 'profile' in locals():
                        profile['enabled'] = False
                        atomic_json(Path(current['profile_directory']) / (agent_id + '.json'), profile, 0o644)
                        if agent_id in current['agents']:
                            current['agents'][agent_id]['status'] = 'REVOKED'
                            current['agents'][agent_id]['profile']['enabled'] = False
                            atomic_json(self.path, current, 0o600)
                finally:
                    security._revoke_local(issued['credential_id'])
                raise
        return {'ok': True, 'changed': True, 'agent_id': agent_id, 'account': account,
                'profile_file': str(profile_path), 'credential_file': str(destination), 'remote_verified': False}

    def renew(self, agent_id):
        with self._lock():
            current = self.read()
            if not current or agent_id not in current['agents']:
                raise DevFlowError('Unknown agent')
            record = current['agents'][agent_id]
            if record['status'] != 'ENROLLED':
                raise DevFlowError('A revoked agent cannot be renewed')
            old = dict(record['profile'])
            actual = read_json(Path(current['profile_directory']) / (agent_id + '.json'))
            self.contracts.validate(actual, 'agent.profile.schema.json')
            if actual != old or not actual['enabled']:
                raise DevFlowError('Agent profile changed or was disabled; renewal requires reconciliation')
            security = SecurityStore(self.root, self.contracts)
            issued = security.issue(old['principal_id'], 86400)
            destination = Path(record['credential_file'])
            try:
                credential_directory_for(destination.parent, self.root, old['run_uid'])
            except (DevFlowError, OSError):
                security._revoke_local(issued['credential_id'])
                raise
            fd, temporary = tempfile.mkstemp(prefix='.credential-', dir=destination.parent)
            try:
                value = read_private_file(issued['credential_file'], 256)
                with os.fdopen(fd, 'wb') as stream:
                    os.fchown(stream.fileno(), old['run_uid'], old['run_gid'])
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                profile = dict(old, credential_id=issued['credential_id'],
                    credential_sha256=hashlib.sha256(value.decode().strip().encode()).hexdigest(),
                    expires_at_epoch=issued['expires_at_epoch'])
                atomic_json(Path(current['profile_directory']) / (agent_id + '.json'), profile, 0o644)
                record['profile'] = profile
                atomic_json(self.path, current, 0o600)
                security._revoke_local(old['credential_id'])
            except BaseException:
                try:
                    failed = dict(old, enabled=False)
                    atomic_json(Path(current['profile_directory']) / (agent_id + '.json'), failed, 0o644)
                    record['profile'] = failed
                    record['status'] = 'REVOKED'
                    atomic_json(self.path, current, 0o600)
                finally:
                    security._revoke_local(issued['credential_id'])
                    security._revoke_local(old['credential_id'])
                raise
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return {'ok': True, 'agent_id': agent_id, 'expires_at_epoch': issued['expires_at_epoch']}

    def revoke(self, agent_id):
        record, _ = self.record(agent_id)
        return SecurityStore(self.root, self.contracts).revoke(record['profile']['credential_id'])


def revoke_bindings(root, contracts, credential_id=None, principal_id=None):
    """Fail-closed revocation hook called before the control security DB mutation."""
    store = AgentStore(root, contracts)
    if not store.path.exists():
        return
    with store._lock():
        current = store.read()
        changed = False
        for agent_id, record in current['agents'].items():
            profile = record['profile']
            if ((credential_id is not None and profile['credential_id'] == credential_id) or
                (principal_id is not None and profile['principal_id'] == principal_id)):
                path = Path(current['profile_directory']) / (agent_id + '.json')
                actual = read_json(path)
                store.contracts.validate(actual, 'agent.profile.schema.json')
                if actual['agent_id'] != agent_id:
                    raise DevFlowError('Agent profile identity mismatch during revocation')
                actual['enabled'] = False
                atomic_json(path, actual, 0o644)
                profile['enabled'] = False
                record['status'] = 'REVOKED'
                changed = True
        if changed:
            atomic_json(store.path, current, 0o600)
