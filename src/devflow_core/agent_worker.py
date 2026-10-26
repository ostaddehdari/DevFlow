"""Unprivileged Linux stdio agent with descriptor-relative, read-only file access."""

import codecs
import ctypes
import hashlib
import hmac
import itertools
import json
import os
import platform
import re
import resource
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .agents import relative_parts
from .contracts import DevFlowError
from .security import read_private_file


def effective_capabilities():
    try:
        line = next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
        return int(line.split()[1], 16)
    except (OSError, StopIteration, ValueError) as exc:
        raise DevFlowError('Cannot inspect effective Linux capabilities') from exc


class AgentView:
    def __init__(self, profile_path, credential_file, contracts, agent_id, clock=None):
        self.profile_path = Path(profile_path)
        self.credential_file = Path(credential_file)
        self.contracts = contracts
        self.agent_id = agent_id
        self.clock = clock or time.time

    def profile(self, capability):
        try:
            parent = self.profile_path.parent.stat()
            if self.profile_path.parent.is_symlink() or parent.st_uid != 0 or parent.st_mode & 0o022:
                raise DevFlowError('Agent profile parent is not controlled by root')
            fd = os.open(self.profile_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise DevFlowError('Agent profile is not controlled by root')
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise DevFlowError('Agent profile exceeds its size limit')
            profile = json.loads(raw)
            self.contracts.validate(profile, 'agent.profile.schema.json')
            if profile['agent_id'] != self.agent_id or not profile['enabled'] or profile['expires_at_epoch'] <= self.clock():
                raise DevFlowError('Agent identity is disabled, expired or inconsistent')
            if os.geteuid() != 0 and (os.geteuid() != profile['run_uid'] or os.getegid() != profile['run_gid']):
                raise DevFlowError('Agent process account no longer matches its binding')
            token = read_private_file(self.credential_file, 256).decode().strip()
            if not re.fullmatch(r'dfc_[A-Za-z0-9_-]{43}', token):
                raise DevFlowError('Invalid agent credential')
            if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), profile['credential_sha256']):
                raise DevFlowError('Invalid agent credential')
            if capability not in profile['capabilities']:
                raise DevFlowError('Agent capability is not granted')
            root = Path(profile['project_root'])
            if root.resolve() != root:
                raise DevFlowError('Agent project root no longer matches its binding')
            for allowed in profile['allowed_paths']:
                relative_parts(allowed)
            return profile
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, DevFlowError):
                raise
            raise DevFlowError('Agent profile or credential unavailable/invalid') from exc

    @contextmanager
    def opened(self, profile, path, directory=False):
        parts = relative_parts(path)
        if not any(parts[:len(relative_parts(allowed))] == relative_parts(allowed) for allowed in profile['allowed_paths']):
            raise DevFlowError('Path is outside the agent allowlist')
        root_fd, current = None, None
        try:
            root_fd = os.open(profile['project_root'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            current = root_fd
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                new_fd = os.open(part, flags, dir_fd=current)
                if current != root_fd:
                    os.close(current)
                current = new_fd
            info = os.fstat(current)
            if directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise DevFlowError('Expected a directory')
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DevFlowError('Only regular non-hardlinked files may be read')
            yield current
        except OSError as exc:
            raise DevFlowError('Agent path is unavailable, forbidden or contains a symlink') from exc
        finally:
            if current is not None and current != root_fd:
                os.close(current)
            if root_fd is not None:
                os.close(root_fd)

    def info(self, nonce='probe'):
        if not isinstance(nonce, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', nonce):
            raise DevFlowError('Invalid probe nonce')
        profile = self.profile('system.info')
        return {'ok': True, 'agent_id': profile['agent_id'], 'server_id': profile['server_id'],
            'project_id': profile['project_id'], 'uid': os.geteuid(), 'gid': os.getegid(),
            'system': platform.system(), 'release': platform.release(), 'machine': platform.machine(),
            'allowed_paths': profile['allowed_paths'], 'capabilities': profile['capabilities'],
            'transport': 'stdio', 'read_only': True, 'nonce': nonce,
            'no_new_privileges': ctypes.CDLL(None).prctl(39, 0, 0, 0, 0) == 1,
            'effective_capabilities': effective_capabilities()}

    def listing(self, path, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset < 5000:
            raise DevFlowError('Invalid listing bounds')
        profile = self.profile('filesystem.list')
        entries = []
        with self.opened(profile, path, directory=True) as fd:
            with os.scandir(fd) as iterator:
                scanned = list(itertools.islice(iterator, 5001))
                for entry in scanned[:5000]:
                    try:
                        relative_parts(entry.name)
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            kind = 'directory'
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            kind = 'file'
                        else:
                            continue
                        entries.append({'name': entry.name, 'type': kind})
                    except (DevFlowError, OSError):
                        continue
        entries.sort(key=lambda item: item['name'])
        output = entries[offset:offset+limit]
        return {'ok': True, 'project_id': profile['project_id'], 'path': path, 'entries': output,
                'truncated': len(scanned) > 5000 or offset + limit < len(entries),
                'next_offset': offset + limit if offset + limit < len(entries) else None}

    def read_text(self, path, max_bytes=32768):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise DevFlowError('Read limit must be 1–65536 bytes')
        profile = self.profile('filesystem.read')
        with self.opened(profile, path) as fd:
            data = os.read(fd, max_bytes + 1)
        truncated = len(data) > max_bytes
        try:
            text = codecs.getincrementaldecoder('utf-8')('strict').decode(data[:max_bytes], final=not truncated)
        except UnicodeDecodeError as exc:
            raise DevFlowError('Only UTF-8 text may be returned') from exc
        return {'ok': True, 'project_id': profile['project_id'], 'path': path, 'text': text, 'truncated': truncated}


def build_agent(view):
    # This check is intentionally in the process entry point; file-policy unit
    # tests can inspect AgentView under the trusted test operator.
    if os.geteuid() == 0 or os.getegid() == 0 or 0 in os.getgroups():
        raise DevFlowError('The agent process must run without root user/group privileges')
    profile = view.profile('system.info')
    if (os.geteuid() != profile['run_uid'] or os.getegid() != profile['run_gid'] or
        not set(os.getgroups()).issubset({profile['run_gid']}) or effective_capabilities() != 0):
        raise DevFlowError('Agent process account does not match enrollment')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise DevFlowError('Cannot enable no-new-privileges for the agent')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    mcp = FastMCP('DevFlow Restricted Agent', instructions='Read-only project paths; no shell, writes, keys, vault or network listener.')
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=annotations)
    def agent_info(nonce: str = 'probe') -> dict:
        return view.info(nonce)

    @mcp.tool(annotations=annotations)
    def agent_list(path: str, limit: int = 100, offset: int = 0) -> dict:
        return view.listing(path, limit, offset)

    @mcp.tool(annotations=annotations)
    def agent_read(path: str, max_bytes: int = 32768) -> dict:
        return view.read_text(path, max_bytes)

    return mcp
