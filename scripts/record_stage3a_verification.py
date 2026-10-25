"""Record only local identity/vault acceptance; keep remote agent and network work pending."""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.continuity import Continuity
from devflow_core.design import validate_design
from devflow_core.security import SecurityStore


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
    checkpoint_id = 'CP-STAGE03A-VERIFIED'
    checkpoint_path = folder / 'checkpoints' / (checkpoint_id + '.json')
    if (state['current']['stage_id'] == 'STAGE-03' and state.get('latest_checkpoint_id') == checkpoint_id):
        cp = read_json(checkpoint_path)
        contracts.validate(cp, 'checkpoint.schema.json')
        if cp['project_id'] != 'devflow' or cp['checkpoint_id'] != checkpoint_id:
            raise DevFlowError('Verification checkpoint identity mismatch')
        return {'ok': True, 'changed': False, 'stage_completed': False}
    if (state['current']['stage_id'] != 'STAGE-02' or state['current']['stage_status'] != 'COMPLETED' or
        state.get('latest_checkpoint_id') != 'CP-STAGE02-VERIFIED' or state.get('execution') is not None):
        raise DevFlowError('Verified Stage 2 with no recorded execution is required')
    previous = read_json(folder / 'checkpoints/CP-STAGE02-VERIFIED.json')
    contracts.validate(previous, 'checkpoint.schema.json')
    if previous['project_id'] != 'devflow' or not previous['safe_to_resume']:
        raise DevFlowError('Stage 2 checkpoint identity mismatch')
    runtime = Continuity(root, contracts).status()
    if not runtime['initialized'] or (runtime['lease'] and runtime['lease']['execution']['status'] == 'ACTIVE'):
        raise DevFlowError('Release the active continuity lease before source reconciliation')
    if any(item['status'] in {'STARTED', 'UNKNOWN'} for item in runtime['operations']):
        raise DevFlowError('Unresolved runtime effects require reconciliation first')
    security = SecurityStore(root, contracts)
    if not security.status()['initialized']:
        raise DevFlowError('Initialize the security backend first')
    # Check actual key availability without exporting the key or any stored secret.
    with security._connection() as connection:
        security._verify_key(connection)
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'], text=True).strip()
    branch = subprocess.check_output(['git', '-C', str(root), 'branch', '--show-current'], text=True).strip()
    if not branch:
        raise DevFlowError('A named source branch is required')
    now = datetime.now(timezone.utc).isoformat()
    stage = next(s for s in design['stages'] if s['id'] == 'STAGE-03')
    stage['status'] = 'ACTIVE'
    for work in stage['works'][:2]:
        work['status'] = 'COMPLETED'
    # Never mark the remote agent or network transport complete from local tests.
    for work in stage['works'][2:]:
        if work['status'] != 'PENDING':
            raise DevFlowError('Later Stage 3 work has changed; inspect rather than overwrite it')
    design['architecture'].update(
        local_identity_backend='project_scoped_revocable_credentials',
        vault_backend='servermaster_local_fernet', master_keys_outside_repository=True,
        network_authentication=False, remote_agent=False,
        execution_readiness_scope='STAGE-03 remote-agent and authenticated-transport configuration; public endpoint and OAuth provider still unresolved.')
    design['architecture']['implemented_mcp_tools'] = list(dict.fromkeys(design['architecture']['implemented_mcp_tools'] + ['security_status']))
    if not any(d.get('id') == 'DEC-05' for d in design.get('decisions', [])):
        design.setdefault('decisions', []).append({'id': 'DEC-05',
            'decision': 'ServerMaster Vault uses authenticated Fernet encryption with master keys outside Git. Local credentials are application-scoped and revocable; they are not OAuth tokens for a public MCP endpoint.'})
    design['open_questions'] = [q for q in design.get('open_questions', []) if q.get('id') != 'Q-02']
    state['current'].update(stage_id='STAGE-03', stage_status='ACTIVE', work_id='WORK-03-02',
        work_status='COMPLETED', operation='stage3a_verification', step='local_identity_and_vault_verified', attempt=1)
    state.update(git={'branch': branch, 'head_sha': head}, latest_checkpoint_id=checkpoint_id,
        next_action='configure_stage3_remote_agent_and_authenticated_transport', updated_at=now)
    resume['current'].update(stage_id='STAGE-03', work_id='WORK-03-02', status='ACTIVE')
    resume.update(git=dict(state['git']), generated_at=now, latest_checkpoint_id=checkpoint_id,
        last_action='stage3a_local_identity_and_vault_verified', next_action=state['next_action'])
    resume['do_not_repeat'] = list(dict.fromkeys(resume['do_not_repeat'] + ['implement_local_identity_and_encrypted_vault']))
    cp = {'schema_version': '1.0', 'checkpoint_id': checkpoint_id, 'project_id': 'devflow',
        'created_at': now, 'reason': 'work_completed', 'safe_to_resume': True,
        'snapshot': {'stage_id': 'STAGE-03', 'work_id': 'WORK-03-02', 'git_head_sha': head,
            'server_deployments': {}, 'last_action': resume['last_action'], 'next_action': state['next_action'],
            'note': 'Local identity, project-scoped stdio checks and encrypted vault verified by operator-run tests. Stage 3 remains ACTIVE; remote agent, OAuth, network endpoint and live deployment health remain unverified. Git HEAD is the observed parent before this milestone commit.'},
        'do_not_repeat': resume['do_not_repeat']}
    validate_design(design, contracts, 'devflow')
    changes = [(checkpoint_path, cp, 'checkpoint.schema.json'),
        (folder / 'project.design.json', design, 'project.design.schema.json'),
        (folder / 'resume.bundle.json', resume, 'resume.bundle.schema.json'),
        (folder / 'state.json', state, 'state.schema.json')]
    for _, document, schema in changes:
        contracts.validate(document, schema)
    for path, document, _ in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.stage3a-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'ok': True, 'changed': True, 'stage': 'STAGE-03', 'stage_completed': False,
        'completed_works': ['WORK-03-01', 'WORK-03-02'], 'pending_works': ['WORK-03-03', 'WORK-03-04'],
        'checkpoint_id': checkpoint_id, 'network_authentication': False}


if __name__ == '__main__':
    print(json.dumps(record(Path(__file__).resolve().parents[1]), indent=2))
