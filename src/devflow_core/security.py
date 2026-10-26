"""Local identities and an encrypted reference vault. No HTTP/OAuth provider."""

import base64
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from .contracts import Contracts, DevFlowError, read_json


ROLE_ACTIONS = {
    'viewer': frozenset({'registry.read', 'project.read', 'continuity.read', 'security.read'}),
    'agent': frozenset({'project.read', 'continuity.read', 'continuity.write'}),
    'operator': frozenset({'registry.read', 'project.read', 'continuity.read', 'security.read',
                           'continuity.write', 'vault.read', 'vault.write'}),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', value):
        raise DevFlowError(f'Invalid {label}')
    return value


def _reference(value):
    if not isinstance(value, str) or not value.startswith('vault://') or len(value) > 512:
        raise DevFlowError('Expected a canonical vault:// reference')
    parts = value[8:].split('/')
    if len(parts) < 2 or any(p in {'.', '..'} for p in parts):
        raise DevFlowError('Invalid vault reference')
    for part in parts:
        _name(part, 'vault reference component')
    return value


def read_private_file(path, maximum=1048576):
    """Never print file contents. Protect against a final-component symlink."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DevFlowError('Private file must be a regular owner-only file owned by this user')
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise DevFlowError('Private input is too large')
    return value


def write_private_file(path, value):
    path = Path(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class SecurityStore:
    def __init__(self, root: Path, contracts: Contracts, clock=None):
        self.root = root.resolve()
        self.contracts = contracts
        self.clock = clock or time.time
        settings_path = self.root / '.ai-project/project.settings.json'
        if not settings_path.resolve().is_relative_to(self.root):
            raise DevFlowError('Settings escape project root')
        settings = read_json(settings_path)
        contracts.validate(settings, 'project.settings.schema.json')
        self.project_id = settings['project']['id']
        self.path = self.root / '.devflow/security.sqlite3'
        self._paths()

    def _now(self):
        now = self.clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise DevFlowError('Invalid security clock')
        return now

    def _paths(self):
        if self.path.parent.is_symlink() or self.path.is_symlink() or not self.path.resolve().is_relative_to(self.root):
            raise DevFlowError('Security storage must be inside its project and cannot be a symlink')
        for path in (self.path.parent, self.path):
            if path.exists():
                info = path.stat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise DevFlowError('Security storage must be owner-only and owned by this user')

    @contextmanager
    def _connection(self, write=False, initialize=False):
        self._paths()
        if not self.path.is_file():
            raise DevFlowError('Security store is not initialized')
        connection = None
        try:
            connection = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if write else '?mode=ro'),
                                         uri=True, isolation_level=None, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA trusted_schema=OFF')
            if write:
                connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not initialize:
                meta = self._meta(connection)
                if meta.get('schema_version') != '1' or meta.get('root') != str(self.root) or meta.get('project_id') != self.project_id:
                    raise DevFlowError('Security storage identity/version mismatch')
                now = self._now()
                if now < float(meta['last_clock']):
                    raise DevFlowError('Clock moved backwards; security inspection required')
                if write:
                    connection.execute("UPDATE metadata SET value=? WHERE key='last_clock'", (str(now),))
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise DevFlowError('Security database unavailable or invalid; data was not reset') from exc
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def _meta(self, connection):
        return dict(connection.execute('SELECT key,value FROM metadata'))

    def _key_directory(self, directory, create=False):
        directory = Path(directory)
        if not directory.is_absolute() or directory.is_symlink() or directory.resolve().is_relative_to(self.root):
            raise DevFlowError('Master-key directory must be absolute, outside the project and not a symlink')
        if create:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DevFlowError('Master-key directory must be owner-only and owned by this user')
        return directory

    def _new_key(self, directory):
        key_id = 'K-' + uuid.uuid4().hex
        key_path = directory / (key_id + '.key')
        write_private_file(key_path, Fernet.generate_key())
        return key_id, key_path

    def _fernet(self, connection, key_id=None):
        meta = self._meta(connection)
        key_id = key_id or meta['active_key_id']
        if not re.fullmatch(r'K-[0-9a-f]{32}', key_id):
            raise DevFlowError('Invalid vault key identity')
        directory = self._key_directory(meta['key_directory'])
        try:
            return Fernet(read_private_file(directory / (key_id + '.key'), 128))
        except (ValueError, OSError) as exc:
            raise DevFlowError('Vault master key is unavailable or invalid') from exc

    def _verify_key(self, connection):
        try:
            verifier = self._fernet(connection).decrypt(self._meta(connection)['key_check'].encode()).decode()
            if verifier != 'devflow-vault:' + self.project_id:
                raise DevFlowError('Vault key verification failed')
        except (InvalidToken, ValueError) as exc:
            raise DevFlowError('Vault key verification failed') from exc

    def initialize(self, key_directory):
        self._paths()
        directory = self._key_directory(key_directory, create=True)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            if self.path.stat().st_size:
                with self._connection(write=True) as connection:
                    if self._meta(connection)['key_directory'] != str(directory):
                        raise DevFlowError('Existing vault is bound to another master-key directory')
                    self._verify_key(connection)
                return {'ok': True, 'changed': False, 'project_id': self.project_id}
        key_path = None
        try:
            with self._connection(write=True, initialize=True) as connection:
                for statement in (
                    'CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS principals (principal_id TEXT PRIMARY KEY,role TEXT NOT NULL,projects TEXT NOT NULL,vault_refs TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0)',
                    'CREATE TABLE IF NOT EXISTS credentials (credential_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked REAL)',
                    'CREATE TABLE IF NOT EXISTS vault_entries (reference TEXT PRIMARY KEY,project_id TEXT NOT NULL,ciphertext TEXT NOT NULL,key_id TEXT NOT NULL,updated REAL NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,detail TEXT NOT NULL,occurred REAL NOT NULL)',
                ):
                    connection.execute(statement)
                existing = self._meta(connection)
                if existing:
                    if (existing.get('root') != str(self.root) or existing.get('project_id') != self.project_id or
                        existing.get('key_directory') != str(directory) or existing.get('schema_version') != '1'):
                        raise DevFlowError('Security storage initialization conflict')
                    self._verify_key(connection)
                    return {'ok': True, 'changed': False, 'project_id': self.project_id}
                key_id, key_path = self._new_key(directory)
                cipher = Fernet(read_private_file(key_path, 128))
                connection.executemany('INSERT INTO metadata VALUES (?,?)', [
                    ('schema_version', '1'), ('root', str(self.root)), ('project_id', self.project_id),
                    ('audience', 'devflow-local:' + uuid.uuid4().hex), ('last_clock', str(self._now())),
                    ('key_directory', str(directory)), ('active_key_id', key_id),
                    ('key_check', cipher.encrypt(('devflow-vault:' + self.project_id).encode()).decode()),
                ])
                self._audit(connection, 'security.initialized', {'key_id': key_id})
        except BaseException:
            # Never delete a key after an uncertain commit outcome. Orphan key files
            # are safer than removing a key that a committed database now needs.
            raise
        return {'ok': True, 'changed': True, 'project_id': self.project_id, 'vault_backend': 'local_fernet'}

    def _audit(self, connection, kind, detail):
        # Only caller-selected public identifiers reach this table; never secret values or token hashes.
        connection.execute('INSERT INTO audit(kind,detail,occurred) VALUES (?,?,?)', (kind, _json(detail), self._now()))

    def status(self):
        if not self.path.exists():
            return {'ok': True, 'initialized': False, 'network_authentication': False}
        with self._connection() as connection:
            return {'ok': True, 'initialized': True, 'vault_backend': 'local_fernet',
                    'principals': connection.execute('SELECT count(*) FROM principals WHERE disabled=0').fetchone()[0],
                    'active_credentials': connection.execute('SELECT count(*) FROM credentials WHERE revoked IS NULL AND expires>?', (self._now(),)).fetchone()[0],
                    'secret_references': connection.execute('SELECT count(*) FROM vault_entries').fetchone()[0],
                    'network_authentication': False, 'remote_agent': False}

    def create_principal(self, principal_id, role, projects, vault_refs=()):
        _name(principal_id, 'principal ID')
        if (not isinstance(role, str) or role not in ROLE_ACTIONS or not isinstance(projects, (list, tuple)) or
            not projects or any(not isinstance(p, str) for p in projects) or len(set(projects)) != len(projects)):
            raise DevFlowError('Invalid role or project grants')
        for project in projects:
            _name(project, 'project ID')
        for ref in vault_refs:
            _reference(ref)
        if role != 'operator' and vault_refs:
            raise DevFlowError('Only an operator may receive vault grants')
        with self._connection(write=True) as connection:
            if connection.execute('SELECT 1 FROM principals WHERE principal_id=?', (principal_id,)).fetchone():
                raise DevFlowError('Principal already exists; existing grants were preserved')
            connection.execute('INSERT INTO principals(principal_id,role,projects,vault_refs) VALUES (?,?,?,?)',
                               (principal_id, role, _json(sorted(projects)), _json(sorted(set(vault_refs)))))
            self._audit(connection, 'principal.created', {'principal_id': principal_id, 'role': role, 'projects': sorted(projects)})
        return {'ok': True, 'principal_id': principal_id, 'role': role, 'projects': sorted(projects)}

    def issue(self, principal_id, seconds=3600):
        if type(seconds) is not int or not 60 <= seconds <= 86400:
            raise DevFlowError('Credential lifetime must be 60–86400 seconds')
        credential_path = None
        try:
            with self._connection(write=True) as connection:
                row = connection.execute('SELECT disabled FROM principals WHERE principal_id=?', (principal_id,)).fetchone()
                if row is None or row[0]:
                    raise DevFlowError('Principal is missing or disabled')
                folder = self.path.parent / 'credentials'
                if folder.is_symlink():
                    raise DevFlowError('Credential directory cannot be a symlink')
                folder.mkdir(mode=0o700, exist_ok=True)
                info = folder.stat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise DevFlowError('Credential directory must be owner-only')
                credential_id = 'C-' + uuid.uuid4().hex
                credential_path = folder / (credential_id + '.token')
                token = 'dfc_' + secrets.token_urlsafe(32)
                write_private_file(credential_path, (token + '\n').encode())
                now = self._now()
                connection.execute('INSERT INTO credentials VALUES (?,?,?,?,?,NULL)',
                    (credential_id, principal_id, hashlib.sha256(token.encode()).hexdigest(), now, now + seconds))
                self._audit(connection, 'credential.issued', {'principal_id': principal_id, 'credential_id': credential_id})
        except BaseException:
            if credential_path is not None:
                credential_path.unlink(missing_ok=True)
            raise
        return {'ok': True, 'principal_id': principal_id, 'credential_id': credential_id,
                'credential_file': str(credential_path), 'expires_at_epoch': now + seconds}

    def _authenticate(self, connection, token, audience=None):
        if not isinstance(token, str) or not re.fullmatch(r'dfc_[a-zA-Z0-9_-]{43}', token):
            raise DevFlowError('Credential invalid, expired or revoked')
        expected_audience = self._meta(connection)['audience']
        if audience is not None and audience != expected_audience:
            raise DevFlowError('Credential audience mismatch')
        row = connection.execute('''SELECT c.credential_id,c.expires,c.revoked,p.* FROM credentials c
            JOIN principals p ON c.principal_id=p.principal_id WHERE c.token_hash=?''',
            (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None or row['disabled'] or row['revoked'] is not None or row['expires'] <= self._now():
            raise DevFlowError('Credential invalid, expired or revoked')
        if row['role'] not in ROLE_ACTIONS:
            raise DevFlowError('Invalid principal policy')
        try:
            projects, refs = json.loads(row['projects']), json.loads(row['vault_refs'])
            if not isinstance(projects, list) or not projects or not isinstance(refs, list):
                raise DevFlowError('Invalid principal grants')
            for project in projects:
                _name(project, 'project grant')
            for reference in refs:
                _reference(reference)
            if row['role'] != 'operator' and refs:
                raise DevFlowError('Invalid role/reference grants')
        except (ValueError, TypeError) as exc:
            raise DevFlowError('Invalid principal policy') from exc
        return {'principal_id': row['principal_id'], 'role': row['role'], 'credential_id': row['credential_id'],
                'projects': projects, 'vault_refs': refs, 'audience': expected_audience}

    def authenticate(self, token, audience=None):
        with self._connection() as connection:
            return self._authenticate(connection, token, audience)

    def _authorize(self, connection, token, action, project_id, reference=None):
        principal = self._authenticate(connection, token)
        if action not in ROLE_ACTIONS[principal['role']] or project_id not in principal['projects']:
            raise DevFlowError('Access denied for this action/project')
        if action.startswith('vault.') and (reference is None or reference not in principal['vault_refs']):
            raise DevFlowError('Access denied for this vault reference')
        return principal

    def authorize(self, token, action, project_id, reference=None):
        with self._connection() as connection:
            return self._authorize(connection, token, action, project_id, reference)

    def revoke(self, credential_id):
        if not isinstance(credential_id, str) or not re.fullmatch(r'C-[0-9a-f]{32}', credential_id):
            raise DevFlowError('Expected a credential ID, never a credential value')
        from .agents import revoke_bindings
        revoke_bindings(self.root, self.contracts, credential_id=credential_id)
        return self._revoke_local(credential_id)

    def _revoke_local(self, credential_id):
        # Enrollment cleanup already holds the agent lock and disables the profile.
        with self._connection(write=True) as connection:
            changed = connection.execute('UPDATE credentials SET revoked=? WHERE credential_id=? AND revoked IS NULL', (self._now(), credential_id)).rowcount
            self._audit(connection, 'credential.revoked', {'credential_id': credential_id})
        return {'ok': True, 'changed': bool(changed)}

    def disable(self, principal_id):
        from .agents import revoke_bindings
        revoke_bindings(self.root, self.contracts, principal_id=principal_id)
        with self._connection(write=True) as connection:
            changed = connection.execute('UPDATE principals SET disabled=1 WHERE principal_id=? AND disabled=0', (principal_id,)).rowcount
            connection.execute('UPDATE credentials SET revoked=? WHERE principal_id=? AND revoked IS NULL', (self._now(), principal_id))
            self._audit(connection, 'principal.disabled', {'principal_id': principal_id})
        return {'ok': True, 'changed': bool(changed)}

    def put(self, reference, project_id, value):
        """Trusted local operator only. No API accepts a token to grant write authority implicitly."""
        _reference(reference)
        _name(project_id, 'project ID')
        if not isinstance(value, bytes) or not 1 <= len(value) <= 1048576:
            raise DevFlowError('Secret must contain 1–1048576 bytes')
        with self._connection(write=True) as connection:
            self._verify_key(connection)
            row = connection.execute('SELECT project_id FROM vault_entries WHERE reference=?', (reference,)).fetchone()
            if row and row[0] != project_id:
                raise DevFlowError('Vault reference is bound to another project')
            payload = {'reference': reference, 'project_id': project_id, 'value': base64.b64encode(value).decode()}
            ciphertext = self._fernet(connection).encrypt(_json(payload).encode()).decode()
            key_id = self._meta(connection)['active_key_id']
            connection.execute('''INSERT INTO vault_entries VALUES (?,?,?,?,?) ON CONFLICT(reference) DO UPDATE SET
                ciphertext=excluded.ciphertext,key_id=excluded.key_id,updated=excluded.updated''',
                (reference, project_id, ciphertext, key_id, self._now()))
            self._audit(connection, 'vault.updated', {'reference': reference, 'project_id': project_id})
        return {'ok': True, 'reference': reference, 'project_id': project_id}

    def _decrypt_entry(self, connection, row):
        try:
            payload = json.loads(self._fernet(connection, row['key_id']).decrypt(row['ciphertext'].encode()))
            if payload['reference'] != row['reference'] or payload['project_id'] != row['project_id']:
                raise DevFlowError('Vault ciphertext context mismatch')
            return base64.b64decode(payload['value'], validate=True)
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            raise DevFlowError('Vault entry cannot be verified/decrypted') from exc

    def resolve(self, reference, project_id, token):
        _reference(reference)
        with self._connection() as connection:
            self._authorize(connection, token, 'vault.read', project_id, reference)
            row = connection.execute('SELECT * FROM vault_entries WHERE reference=? AND project_id=?', (reference, project_id)).fetchone()
            if row is None:
                raise DevFlowError('Vault reference is unavailable')
            return self._decrypt_entry(connection, row)

    def references(self):
        with self._connection() as connection:
            return {'ok': True, 'references': [dict(row) for row in connection.execute('SELECT reference,project_id,key_id FROM vault_entries ORDER BY reference')]}

    def rotate_key(self):
        new_path = None
        try:
            with self._connection(write=True) as connection:
                self._verify_key(connection)
                meta = self._meta(connection)
                directory = self._key_directory(meta['key_directory'])
                new_id, new_path = self._new_key(directory)
                cipher = Fernet(read_private_file(new_path, 128))
                entries = connection.execute('SELECT * FROM vault_entries').fetchall()
                for row in entries:
                    plain = self._decrypt_entry(connection, row)
                    payload = {'reference': row['reference'], 'project_id': row['project_id'], 'value': base64.b64encode(plain).decode()}
                    connection.execute('UPDATE vault_entries SET ciphertext=?,key_id=? WHERE reference=?',
                        (cipher.encrypt(_json(payload).encode()).decode(), new_id, row['reference']))
                connection.execute("UPDATE metadata SET value=? WHERE key='active_key_id'", (new_id,))
                connection.execute("UPDATE metadata SET value=? WHERE key='key_check'", (cipher.encrypt(('devflow-vault:' + self.project_id).encode()).decode(),))
                self._audit(connection, 'vault.rotated', {'active_key_id': new_id, 'references': len(entries)})
        except BaseException:
            # Retain both old and new keys even when commit acknowledgement is lost.
            raise
        return {'ok': True, 'active_key_id': new_id, 'references_rotated': len(entries), 'old_keys_retained': True}


class CredentialGate:
    """Authenticate on every tool invocation, including already-open stdio sessions."""
    def __init__(self, store, credential_file):
        self.store = store
        self.credential_file = Path(credential_file)

    def _token(self):
        try:
            return read_private_file(self.credential_file, 256).decode().strip()
        except (ValueError, OSError) as exc:
            raise DevFlowError('Client credential file is unavailable or invalid') from exc

    def principal(self):
        return self.store.authenticate(self._token())

    def check(self, action, project_id, reference=None):
        return self.store.authorize(self._token(), action, project_id, reference)
