"""Mandatory real non-root MCP probe before recording the agent Work complete."""

import asyncio
import hashlib
import json
import os
import pwd
import secrets
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from devflow_core.agents import AgentStore, atomic_json
from devflow_core.contracts import Contracts, DevFlowError


async def probe(root, agent_id='devflow-local'):
    contracts = Contracts(root / 'contracts')
    store = AgentStore(root, contracts)
    record, profile_path = store.record(agent_id)
    expected = record['profile']
    if record['status'] != 'ENROLLED' or not expected['enabled']:
        raise DevFlowError('Agent is revoked or not enrolled')
    user = pwd.getpwnam(record['account'])
    if user.pw_uid == 0 or user.pw_uid != expected['run_uid'] or user.pw_gid != expected['run_gid']:
        raise DevFlowError('Agent OS account no longer matches enrollment')
    runuser = shutil.which('runuser')
    if runuser is None:
        raise DevFlowError('The Linux runuser utility is required for a non-root probe')
    nonce = secrets.token_hex(16)
    parameters = StdioServerParameters(command=runuser,
        args=['-u', record['account'], '--', sys.executable, '-s', '-m', 'devflow_core.cli',
            '--contracts', str(root / 'contracts'), 'agent-worker', '--agent-id', agent_id,
            '--profile', str(profile_path), '--credential-file', record['credential_file']],
        env={'PYTHONPATH': str(root / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
             'HOME': user.pw_dir, 'PATH': '/usr/bin:/bin'})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            initialized = await session.initialize()
            if initialized.serverInfo.name != 'DevFlow Restricted Agent':
                raise DevFlowError('Unexpected agent implementation')
            tools = await session.list_tools()
            if {t.name for t in tools.tools} != {'agent_info', 'agent_list', 'agent_read'}:
                raise DevFlowError('Unexpected agent tool set')
            if not all(t.annotations and t.annotations.readOnlyHint for t in tools.tools):
                raise DevFlowError('Agent tool annotations are not read-only')
            result = await session.call_tool('agent_info', {'nonce': nonce})
            if result.isError:
                raise DevFlowError('Agent identity probe failed')
            info = json.loads(result.content[0].text)
            if (info['uid'] != expected['run_uid'] or info['uid'] == 0 or info['gid'] != expected['run_gid'] or
                info['agent_id'] != agent_id or info['server_id'] != expected['server_id'] or
                info['project_id'] != expected['project_id'] or info['nonce'] != nonce or not info['no_new_privileges'] or
                info['effective_capabilities'] != 0):
                raise DevFlowError('Agent live identity/privilege checks failed')
            path = expected['allowed_paths'][0]
            listed = await session.call_tool('agent_list', {'path': path})
            if listed.isError:
                raise DevFlowError('Agent cannot list its configured project directory')
            files = [entry['name'] for entry in json.loads(listed.content[0].text)['entries'] if entry['type'] == 'file']
            if not files:
                raise DevFlowError('Probe needs a readable text file in the first allowed directory')
            read_result = await session.call_tool('agent_read', {'path': path + '/' + files[0], 'max_bytes': 512})
            if read_result.isError:
                raise DevFlowError('Agent cannot read its allowed text file')
            if not (await session.call_tool('agent_read', {'path': '../.devflow/security.sqlite3'})).isError:
                raise DevFlowError('Agent path escape was not denied')
            if not (await session.call_tool('shell_exec', {'command': 'id'})).isError:
                raise DevFlowError('An unexpected shell tool is available')
    proof = {'ok': True, 'agent_id': agent_id, 'server_id': expected['server_id'],
        'project_id': expected['project_id'], 'uid': info['uid'], 'gid': info['gid'],
        'no_new_privileges': True, 'effective_capabilities': 0, 'transport': 'local_stdio', 'tools_verified': 3,
        'escape_denied': True, 'shell_denied': True, 'verified_at_epoch': time.time(),
        'profile_sha256': hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
        'remote_verified': False}
    atomic_json(root / '.devflow/agent-probe.json', proof, 0o600)
    return proof


if __name__ == '__main__':
    print(json.dumps(asyncio.run(asyncio.wait_for(probe(Path(__file__).resolve().parents[1]), timeout=45)), indent=2))
