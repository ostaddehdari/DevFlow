"""Record the bounded local agent only after a real non-root MCP probe succeeded."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from devflow_core.agents import AgentStore
from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.continuity import Continuity
from devflow_core.design import validate_design
from devflow_core.security import read_private_file


def record(root):
    root = Path(root).resolve()
    folder = root / '.ai-project'
    contracts = Contracts(root / 'contracts')
    state = read_json(folder / 'state.json')
    resume = read_json(folder / 'resume.bundle.json')
    design = read_json(folder / 'project.design.json')
    for document, schema in ((state, 'state.schema.json'), (resume, 'resume.bundle.schema.json')):
        contracts.validate(document, schema)
        if document['project_id'] != 'devflow':
            raise DevFlowError('Expected DevFlow project')
    validate_design(design, contracts, 'devflow')
    checkpoint_id = 'CP-STAGE03B-VERIFIED'
    checkpoint_path = folder / 'checkpoints' / (checkpoint_id + '.json')
    # A fresh probe is mandatory even on a repeat install.
    proof = json.loads(read_private_file(root / '.devflow/agent-probe.json'))
    agent, profile_path = AgentStore(root, contracts).record('devflow-local')
    actual = read_json(profile_path)
    contracts.validate(actual, 'agent.profile.schema.json')
    if actual != agent['profile']:
        raise DevFlowError('Agent profile changed after enrollment/probe')
    expected_hash = hashlib.sha256(json.dumps(agent['profile'], sort_keys=True).encode()).hexdigest()
    if (proof.get('ok') is not True or proof.get('agent_id') != 'devflow-local' or
        type(proof.get('uid')) is not int or proof.get('uid', 0) <= 0 or proof.get('uid') != agent['profile']['run_uid'] or
        proof.get('gid') != agent['profile']['run_gid'] or proof.get('project_id') != 'devflow' or
        proof.get('server_id') != agent['profile']['server_id'] or proof.get('transport') != 'local_stdio' or
        proof.get('profile_sha256') != expected_hash or proof.get('no_new_privileges') is not True or
        proof.get('effective_capabilities') != 0 or proof.get('tools_verified') != 3 or
        proof.get('escape_denied') is not True or proof.get('shell_denied') is not True or
        agent['status'] != 'ENROLLED' or not 0 <= time.time() - proof.get('verified_at_epoch', 0) <= 300):
        raise DevFlowError('A fresh successful real non-root agent probe is required')
    if state.get('latest_checkpoint_id') == checkpoint_id:
        checkpoint = read_json(checkpoint_path)
        contracts.validate(checkpoint, 'checkpoint.schema.json')
        if checkpoint['checkpoint_id'] != checkpoint_id or checkpoint['project_id'] != 'devflow':
            raise DevFlowError('Agent checkpoint identity mismatch')
        return {'ok': True, 'changed': False, 'stage_completed': False}
    if (state['current']['stage_id'] != 'STAGE-03' or state.get('latest_checkpoint_id') != 'CP-STAGE03A-VERIFIED' or
        state.get('execution') is not None):
        raise DevFlowError('Verified Stage 3A without a recorded execution is required')
    previous = read_json(folder / 'checkpoints/CP-STAGE03A-VERIFIED.json')
    contracts.validate(previous, 'checkpoint.schema.json')
    if previous['project_id'] != 'devflow' or not previous['safe_to_resume']:
        raise DevFlowError('Stage 3A checkpoint identity mismatch')
    runtime = Continuity(root, contracts).status()
    if not runtime['initialized'] or (runtime['lease'] and runtime['lease']['execution']['status'] == 'ACTIVE'):
        raise DevFlowError('Release the active continuity lease first')
    if any(item['status'] in {'STARTED', 'UNKNOWN'} for item in runtime['operations']):
        raise DevFlowError('Unresolved runtime effects require reconciliation first')
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'], text=True).strip()
    branch = subprocess.check_output(['git', '-C', str(root), 'branch', '--show-current'], text=True).strip()
    if not branch:
        raise DevFlowError('A named source branch is required')
    now = datetime.now(timezone.utc).isoformat()
    stage = next(stage for stage in design['stages'] if stage['id'] == 'STAGE-03')
    if any(work['status'] != 'COMPLETED' for work in stage['works'][:2]) or stage['works'][3]['status'] != 'PENDING':
        raise DevFlowError('Unexpected Stage 3 work state; inspect rather than overwrite it')
    stage['status'] = 'ACTIVE'
    stage['works'][2]['status'] = 'COMPLETED'
    design['architecture'].update(local_restricted_agent=True, local_agent_uid=proof['uid'],
        local_agent_transport='stdio', local_agent_capabilities=agent['profile']['capabilities'],
        network_authentication=False, remote_agent=False,
        execution_readiness_scope='WORK-03-04 authenticated HTTPS MCP transport; public resource URI and OAuth provider remain unresolved.')
    design['architecture']['implemented_mcp_tools'] = list(dict.fromkeys(design['architecture']['implemented_mcp_tools'] + ['agent_inventory']))
    state['current'].update(stage_id='STAGE-03', stage_status='ACTIVE', work_id='WORK-03-03',
        work_status='COMPLETED', operation='stage3b_verification', step='restricted_local_agent_verified', attempt=1)
    state.update(git={'branch': branch, 'head_sha': head}, latest_checkpoint_id=checkpoint_id,
        next_action='configure_authenticated_https_mcp_transport', updated_at=now)
    resume['current'].update(stage_id='STAGE-03', work_id='WORK-03-03', status='ACTIVE')
    resume.update(git=dict(state['git']), generated_at=now, latest_checkpoint_id=checkpoint_id,
        last_action='restricted_local_agent_verified', next_action=state['next_action'])
    resume['do_not_repeat'] = list(dict.fromkeys(resume['do_not_repeat'] + ['implement_and_enroll_restricted_local_agent']))
    checkpoint = {'schema_version': '1.0', 'checkpoint_id': checkpoint_id, 'project_id': 'devflow',
        'created_at': now, 'reason': 'work_completed', 'safe_to_resume': True,
        'snapshot': {'stage_id': 'STAGE-03', 'work_id': 'WORK-03-03', 'git_head_sha': head,
            'server_deployments': {}, 'last_action': resume['last_action'], 'next_action': state['next_action'],
            'agent_probe_uid': proof['uid'], 'agent_profile_sha256': expected_hash,
            'note': 'Three read-only agent MCP tools verified in a real non-root process with no effective capabilities and no-new-privileges. Network transport, another remote host and live deployment health are not verified. HEAD is observed before this milestone commit.'},
        'do_not_repeat': resume['do_not_repeat']}
    validate_design(design, contracts, 'devflow')
    changes = [(checkpoint_path, checkpoint, 'checkpoint.schema.json'),
        (folder / 'project.design.json', design, 'project.design.schema.json'),
        (folder / 'resume.bundle.json', resume, 'resume.bundle.schema.json'),
        (folder / 'state.json', state, 'state.schema.json')]
    for _, value, schema in changes:
        contracts.validate(value, schema)
    for path, value, _ in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.stage3b-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'ok': True, 'changed': True, 'stage': 'STAGE-03', 'stage_completed': False,
        'completed_work': 'WORK-03-03', 'pending_work': 'WORK-03-04', 'agent_uid': proof['uid'],
        'checkpoint_id': checkpoint_id, 'network_authentication': False}


if __name__ == '__main__':
    print(json.dumps(record(Path(__file__).resolve().parents[1]), indent=2))
