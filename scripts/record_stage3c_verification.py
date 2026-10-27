"""Record Stage 3 only after a fresh, secret-free live OAuth MCP proof."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.continuity import Continuity
from devflow_core.design import validate_design
from devflow_core.network import NetworkPolicy, private_json


ROOT_RESOURCE = 'https://srun.ir/mcpdevflow'
ISSUER = 'https://dev-fqzv1aqwljc7mhwz.us.auth0.com/'
CONFIG = Path('/etc/devflow/network.json')
PROOF = Path('/etc/devflow/stage3c-proof.json')


def check_live(root, contracts, head):
    proof = private_json(PROOF, 262144)
    if (proof.get('ok') is not True or proof.get('schema_version') != '1.0' or
        proof.get('resource') != ROOT_RESOURCE or proof.get('issuer') != ISSUER or
        proof.get('git_head_sha') != head or type(proof.get('verified_at_epoch')) not in (int, float) or
        not 0 <= time.time() - proof['verified_at_epoch'] <= 86400 or
        type(proof.get('tools_verified')) is not int or proof['tools_verified'] < 8 or
        any(proof.get(key) is not True for key in (
            'public_tls_discovery', 'unauthenticated_rejected', 'tampered_token_rejected',
            'authorized_mcp', 'cross_project_denied', 'revocation_rejected',
            'principal_restored'))):
        raise DevFlowError('Fresh complete Stage 3C live proof is required')
    digest = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
    if digest != proof.get('config_sha256'):
        raise DevFlowError('OAuth grants changed since the live revocation proof')
    policy = NetworkPolicy(root, contracts, CONFIG)
    config = policy.config()
    policy.keys(config)
    if config['issuer'] != ISSUER or config['resource'] != ROOT_RESOURCE:
        raise DevFlowError('Unexpected OAuth issuer or resource')
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'devflow-mcp.service']).returncode:
        raise DevFlowError('DevFlow MCP service is not active')
    listeners = subprocess.check_output(
        ['ss', '-lntH', '( sport = :8765 )'], text=True)
    if (not listeners.strip() or any(
        row.split()[3] != '127.0.0.1:8765' for row in listeners.splitlines())):
        raise DevFlowError('MCP listener must bind only to 127.0.0.1:8765')
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        metadata = client.get(
            'https://srun.ir/.well-known/oauth-protected-resource/mcpdevflow')
        metadata.raise_for_status()
        if metadata.json().get('resource') != ROOT_RESOURCE:
            raise DevFlowError('Public protected-resource discovery changed')
        denied = client.post(ROOT_RESOURCE, json={})
        if denied.status_code != 401 or 'bearer' not in denied.headers.get('www-authenticate', '').lower():
            raise DevFlowError('Public unauthenticated MCP request did not receive a bearer challenge')
    return proof


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.stage3c-', dir=path.parent)
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


def record(root):
    if os.geteuid() != 0:
        raise DevFlowError('Recording the root-private live proof requires root')
    root = Path(root).resolve()
    folder = root / '.ai-project'
    contracts = Contracts(root / 'contracts')
    state = read_json(folder / 'state.json')
    resume = read_json(folder / 'resume.bundle.json')
    design = read_json(folder / 'project.design.json')
    for document, schema in ((state, 'state.schema.json'),
                              (resume, 'resume.bundle.schema.json')):
        contracts.validate(document, schema)
        if document['project_id'] != 'devflow':
            raise DevFlowError('Expected DevFlow project')
    validate_design(design, contracts, 'devflow')
    checkpoint_id = 'CP-STAGE03C-VERIFIED'
    checkpoint_path = folder / 'checkpoints' / (checkpoint_id + '.json')
    if state.get('latest_checkpoint_id') == checkpoint_id:
        checkpoint = read_json(checkpoint_path)
        contracts.validate(checkpoint, 'checkpoint.schema.json')
        if checkpoint['project_id'] != 'devflow' or checkpoint['checkpoint_id'] != checkpoint_id:
            raise DevFlowError('Stage 3C checkpoint identity mismatch')
        return {'ok': True, 'changed': False, 'checkpoint_id': checkpoint_id}
    if (state['current']['stage_id'] != 'STAGE-03' or
        state['current']['work_id'] != 'WORK-03-03' or
        state['current']['work_status'] != 'COMPLETED' or
        state.get('latest_checkpoint_id') != 'CP-STAGE03B-VERIFIED' or
        state.get('execution') is not None):
        raise DevFlowError('Verified Stage 3B with no execution is required')
    previous = read_json(folder / 'checkpoints/CP-STAGE03B-VERIFIED.json')
    contracts.validate(previous, 'checkpoint.schema.json')
    if previous['project_id'] != 'devflow' or not previous['safe_to_resume']:
        raise DevFlowError('Stage 3B checkpoint identity mismatch')
    runtime = Continuity(root, contracts).status()
    if not runtime['initialized'] or (runtime['lease'] and
        runtime['lease']['execution']['status'] == 'ACTIVE'):
        raise DevFlowError('Release the active continuity lease first')
    if any(item['status'] in {'STARTED', 'UNKNOWN'} for item in runtime['operations']):
        raise DevFlowError('Resolve unknown runtime effects first')
    head = subprocess.check_output(
        ['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'], text=True).strip()
    branch = subprocess.check_output(
        ['git', '-C', str(root), 'branch', '--show-current'], text=True).strip()
    if not branch:
        raise DevFlowError('A named source branch is required')
    proof = check_live(root, contracts, head)
    stage = next(s for s in design['stages'] if s['id'] == 'STAGE-03')
    if (stage['status'] != 'ACTIVE' or
        [work['status'] for work in stage['works']] !=
        ['COMPLETED', 'COMPLETED', 'COMPLETED', 'PENDING']):
        raise DevFlowError('Unexpected Stage 3 design work state')
    now = datetime.now(timezone.utc).isoformat()
    next_action = 'plan_stage4_controlled_execution'
    stage['works'][3]['status'] = 'COMPLETED'
    stage['status'] = 'COMPLETED'
    design['architecture'].update(
        network_authentication=True, remote_agent=False,
        oauth_issuer=ISSUER, public_mcp_resource=ROOT_RESOURCE,
        network_transport='authenticated_https_streamable_http',
        execution_readiness_scope=(
            'Stage 3 read-only public MCP verified with Auth0 M2M. '
            'Stage 4 controlled execution, remote agent, and user OAuth client remain unimplemented.'),
    )
    state['current'].update(
        stage_id='STAGE-03', stage_status='COMPLETED', work_id='WORK-03-04',
        work_status='COMPLETED', operation='stage3c_verification',
        step='authenticated_public_mcp_verified', attempt=1)
    state.update(git={'branch': branch, 'head_sha': head},
                 latest_checkpoint_id=checkpoint_id,
                 next_action=next_action, updated_at=now)
    resume['current'].update(stage_id='STAGE-03', work_id='WORK-03-04', status='COMPLETED')
    resume.update(git=dict(state['git']), generated_at=now,
                  latest_checkpoint_id=checkpoint_id,
                  last_action='stage3c_authenticated_public_mcp_verified',
                  next_action=next_action)
    resume['do_not_repeat'] = list(dict.fromkeys(
        resume['do_not_repeat'] + ['configure_authenticated_https_mcp_transport']))
    checkpoint = {
        'schema_version': '1.0', 'checkpoint_id': checkpoint_id,
        'project_id': 'devflow', 'created_at': now, 'reason': 'stage_completed',
        'safe_to_resume': True,
        'snapshot': {
            'stage_id': 'STAGE-03', 'work_id': 'WORK-03-04',
            'git_head_sha': head, 'server_deployments': {},
            'last_action': resume['last_action'], 'next_action': next_action,
            'public_mcp_resource': ROOT_RESOURCE, 'oauth_issuer': ISSUER,
            'live_probe_at_epoch': proof['verified_at_epoch'],
            'tools_verified': proof['tools_verified'],
            'config_sha256': proof['config_sha256'],
            'note': ('HTTPS discovery, bearer denial, authenticated read-only MCP tools, '
                     'cross-project denial, and immediate principal revocation were observed. '
                     'The client is M2M only; user OAuth and target deployment health are '
                     'unverified. Git SHA is the parent before this milestone commit.'),
        },
        'do_not_repeat': resume['do_not_repeat'],
    }
    validate_design(design, contracts, 'devflow')
    changes = [
        (checkpoint_path, checkpoint, 'checkpoint.schema.json'),
        (folder / 'project.design.json', design, 'project.design.schema.json'),
        (folder / 'resume.bundle.json', resume, 'resume.bundle.schema.json'),
        (folder / 'state.json', state, 'state.schema.json'),
    ]
    for _, value, schema in changes:
        contracts.validate(value, schema)
    for path, value, _ in changes:
        write_json(path, value)
    return {
        'ok': True, 'changed': True, 'stage': 'STAGE-03',
        'checkpoint_id': checkpoint_id, 'tools_verified': proof['tools_verified'],
        'next_action': next_action, 'public_mcp_verified': True,
        'user_oauth_verified': False,
    }


if __name__ == '__main__':
    print(json.dumps(record(Path(__file__).resolve().parents[1]), indent=2))
