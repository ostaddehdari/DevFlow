"""Stage 4.1: validate bounded plans and locally granted, exact-plan approvals.

This module never executes a plan. Any future runner must recheck the target,
Git preconditions, lease and approval immediately before an effect.
"""

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .contracts import DevFlowError, read_json


def canonical(plan):
    try:
        return json.dumps(plan, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise DevFlowError('Operation plan must be JSON') from exc


class PlanPolicy:
    def __init__(self, root, contracts):
        self.root = Path(root).resolve()
        self.contracts = contracts
        settings = read_json(self.root / '.ai-project/project.settings.json')
        contracts.validate(settings, 'project.settings.schema.json')
        self.project_id = settings['project']['id']
        self.servers = settings['servers']

    def target(self, path):
        if (not isinstance(path, str) or '\\' in path or '\x00' in path or
            any(ord(c) < 32 for c in path)):
            raise DevFlowError('Invalid operation path')
        relative = Path(path)
        if (relative.is_absolute() or not relative.parts or
            any(part in {'.', '..'} for part in path.split('/')) or
            path.startswith('~') or '//' in path):
            raise DevFlowError('Operation path must be inside the project')
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise DevFlowError('Operation path cannot traverse a symlink')
        if not current.resolve().is_relative_to(self.root) or current == self.root:
            raise DevFlowError('Operation path escapes the project')
        if current.exists() and not stat.S_ISREG(current.stat().st_mode):
            raise DevFlowError('Operation target must be a regular file')
        return current

    def validate(self, plan):
        if not isinstance(plan, dict) or len(canonical(plan)) > 65536:
            raise DevFlowError('Operation plan must be a bounded JSON object')
        self.contracts.validate(plan, 'operation.plan.schema.json')
        if plan['project_id'] != self.project_id:
            raise DevFlowError('Operation project mismatch')
        server = self.servers.get(plan['server_id'])
        if not server or server['project_root'] != str(self.root):
            raise DevFlowError('Operation server is not bound to this project root')
        if 'filesystem' not in server.get('capabilities', []):
            raise DevFlowError('Operation capability is not granted to server')
        kind, args = plan['kind'], plan['arguments']
        path = self.target(args['path'])
        if path.relative_to(self.root).parts[0] in {'.git', '.devflow', '.venv', '.ai-project'}:
            raise DevFlowError('Internal project storage is outside the operation scope')
        before = plan['preconditions']
        after = plan['postconditions']
        if kind == 'filesystem.read':
            if set(args) != {'path'} or before.get('existing_sha256') != after['sha256']:
                raise DevFlowError('Read plan requires matching expected content digest')
        elif kind == 'filesystem.write':
            if (set(args) != {'path', 'content_sha256', 'content_bytes'} or
                args['content_sha256'] != after['sha256'] or
                'existing_sha256' not in before):
                raise DevFlowError('Write plan requires content digest, size and expected prior state')
        else:
            raise DevFlowError('Operation kind is not allowed')
        # Paths are rechecked by the eventual executor; no effect is authorized here.
        return {'operation_id': plan['operation_id'], 'plan_sha256': hashlib.sha256(canonical(plan)).hexdigest(),
                'sensitive': kind == 'filesystem.write', 'path': str(path)}


class ApprovalStore:
    """Owner-only local approval ledger, never exposed as an MCP tool."""
    def __init__(self, policy, security, clock=None):
        self.policy = policy
        self.security = security
        self.clock = clock or time.time
        self.path = policy.root / '.devflow' / 'approvals.sqlite3'

    def _check_path(self):
        directory = self.path.parent
        if directory.is_symlink() or self.path.is_symlink():
            raise DevFlowError('Approval storage cannot be a symlink')
        for path in (directory, self.path):
            if path.exists():
                info = path.stat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise DevFlowError('Approval storage must be owner-only')

    def _connect(self):
        self._check_path()
        if not self.path.is_file():
            raise DevFlowError('Approval storage is not initialized')
        uri = 'file:' + quote(str(self.path), safe='/') + '?mode=rw'
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
        try:
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            return conn
        except BaseException:
            conn.close()
            raise

    def initialize(self):
        self._check_path()
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        conn = self._connect()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, project_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL, approver_id TEXT NOT NULL, credential_id TEXT NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)')
            conn.commit()
        finally:
            conn.close()
        return {'ok': True, 'initialized': True}

    def grant(self, plan, operator_token, seconds=600):
        checked = self.policy.validate(plan)
        if not checked['sensitive']:
            raise DevFlowError('Read-only plans do not require an approval')
        if type(seconds) is not int or not 60 <= seconds <= 600:
            raise DevFlowError('Approval lifetime must be 60–600 seconds')
        principal = self.security.authorize(operator_token, 'operation.approve', plan['project_id'])
        approval_id = 'AP-' + uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,0)',
                         (approval_id, plan['operation_id'], plan['project_id'], checked['plan_sha256'],
                          principal['principal_id'], principal['credential_id'], self.clock() + seconds))
            conn.commit()
        finally:
            conn.close()
        return {'ok': True, 'approval_id': approval_id, 'plan_sha256': checked['plan_sha256']}

    def verify(self, plan, approval_id):
        checked = self.policy.validate(plan)
        if not checked['sensitive']:
            return checked
        conn = self._connect()
        try:
            row = conn.execute('SELECT operation_id,project_id,plan_sha256,credential_id,expires_at,revoked FROM approvals WHERE approval_id=?', (approval_id,)).fetchone()
        finally:
            conn.close()
        if (row is None or row[5] or row[4] <= self.clock() or
            row[0] != plan['operation_id'] or row[1] != plan['project_id'] or
            row[2] != checked['plan_sha256'] or
            not self.security.credential_active(row[3], plan['project_id'])):
            raise DevFlowError('Approval absent, revoked, expired or bound to another plan')
        return checked

    def revoke(self, approval_id):
        conn = self._connect()
        try:
            changed = conn.execute('UPDATE approvals SET revoked=1 WHERE approval_id=? AND revoked=0', (approval_id,)).rowcount
            conn.commit()
        finally:
            conn.close()
        return {'ok': True, 'changed': bool(changed)}
