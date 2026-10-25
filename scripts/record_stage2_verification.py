"""Record Stage 2 source acceptance after operator-run tests, not live deployment health."""

import copy
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.continuity import Continuity
from devflow_core.design import validate_design


def record(root):
    root = Path(root).resolve()
    folder = root / '.ai-project'
    contracts = Contracts(root / 'contracts')
    state = read_json(folder / 'state.json')
    resume = read_json(folder / 'resume.bundle.json')
    design = read_json(folder / 'project.design.json')
    for value, schema in ((state, 'state.schema.json'), (resume, 'resume.bundle.schema.json')):
        contracts.validate(value, schema)
        if value['project_id'] != 'devflow':
            raise DevFlowError('Expected DevFlow project')
    validate_design(design, contracts, 'devflow')
    checkpoint_id = 'CP-STAGE02-VERIFIED'
    checkpoint_path = folder / 'checkpoints' / (checkpoint_id + '.json')
    if state['current']['stage_id'] == 'STAGE-02' and state['current']['stage_status'] == 'COMPLETED':
        cp = read_json(checkpoint_path)
        contracts.validate(cp, 'checkpoint.schema.json')
        if cp['checkpoint_id'] != checkpoint_id or cp['project_id'] != 'devflow':
            raise DevFlowError('Verification checkpoint identity mismatch')
        return {'ok': True, 'changed': False, 'checkpoint_id': checkpoint_id}
    if (state['current']['stage_id'] != 'STAGE-01' or state['current']['stage_status'] != 'COMPLETED' or
        state['current']['work_status'] != 'COMPLETED' or state.get('execution') is not None or
        state.get('latest_checkpoint_id') != 'CP-STAGE01-VERIFIED'):
        raise DevFlowError('Verified Stage 1 and no recorded execution are required')
    previous = read_json(folder / 'checkpoints/CP-STAGE01-VERIFIED.json')
    contracts.validate(previous, 'checkpoint.schema.json')
    if previous['project_id'] != 'devflow' or not previous['safe_to_resume']:
        raise DevFlowError('Stage 1 checkpoint is inconsistent')
    runtime = Continuity(root, contracts).status()
    if not runtime['initialized'] or (runtime['lease'] and runtime['lease']['execution']['status'] == 'ACTIVE'):
        raise DevFlowError('Initialize continuity and release any active lease first')
    if any(item['status'] in {'STARTED', 'UNKNOWN'} for item in runtime['operations']):
        raise DevFlowError('Unresolved runtime operations require reconciliation')
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'], text=True).strip()
    branch = subprocess.check_output(['git', '-C', str(root), 'branch', '--show-current'], text=True).strip()
    if not branch:
        raise DevFlowError('A named source branch is required')
    now = datetime.now(timezone.utc).isoformat()
    stage = next(s for s in design['stages'] if s['id'] == 'STAGE-02')
    stage['status'] = 'COMPLETED'
    for work in stage['works']:
        work['status'] = 'COMPLETED'
    final_work = stage['works'][-1]['id']
    design['architecture']['implemented_mcp_tools'] = list(dict.fromkeys(
        design['architecture']['implemented_mcp_tools'] + ['continuity_status']))
    design['architecture']['execution_readiness_scope'] = (
        'STAGE-03 identity and authorization design; resolve transport and secret backend prerequisites before network exposure.')
    design['architecture']['continuity_storage'] = '.devflow/continuity.sqlite3'
    design['architecture']['external_effect_runner'] = False
    state['current'].update(stage_id='STAGE-02', stage_status='COMPLETED', work_id=final_work,
        work_status='COMPLETED', operation='stage2_verification', step='local_continuity_verified', attempt=1)
    state.update(git={'branch': branch, 'head_sha': head}, latest_checkpoint_id=checkpoint_id,
        next_action='resolve_stage3_identity_and_transport', updated_at=now)
    resume['current'].update(stage_id='STAGE-02', work_id=final_work, status='COMPLETED')
    resume.update(git=dict(state['git']), generated_at=now, latest_checkpoint_id=checkpoint_id,
        last_action='stage2_local_continuity_verified', next_action=state['next_action'])
    resume['do_not_repeat'] = list(dict.fromkeys(resume['do_not_repeat'] + ['install_stage1', 'implement_stage2_execution_leases']))
    cp = {'schema_version': '1.0', 'checkpoint_id': checkpoint_id, 'project_id': 'devflow',
        'created_at': now, 'reason': 'stage_completed', 'safe_to_resume': True,
        'snapshot': {'stage_id': 'STAGE-02', 'work_id': final_work, 'git_head_sha': head,
            'server_deployments': {}, 'last_action': resume['last_action'], 'next_action': state['next_action'],
            'note': 'Operator verification script runs source tests, real MCP smoke and initializes the local ledger. HEAD is the parent observed before this milestone commit. External effects, network service, Vault and live deployment health are not verified.'},
        'do_not_repeat': resume['do_not_repeat']}
    validate_design(design, contracts, 'devflow')
    changes = [(checkpoint_path, cp, 'checkpoint.schema.json'),
        (folder / 'project.design.json', design, 'project.design.schema.json'),
        (folder / 'resume.bundle.json', resume, 'resume.bundle.schema.json'),
        (folder / 'state.json', state, 'state.schema.json')]
    for _, value, schema in changes:
        contracts.validate(value, schema)
    # Source milestone projections only; runtime snapshots use one SQLite transaction.
    for path, value, _ in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.stage2-', dir=path.parent)
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
    return {'ok': True, 'changed': True, 'stage': 'STAGE-02', 'checkpoint_id': checkpoint_id,
        'next_action': state['next_action'], 'live_deployment_verified': False}


if __name__ == '__main__':
    print(json.dumps(record(Path(__file__).resolve().parents[1]), indent=2))
