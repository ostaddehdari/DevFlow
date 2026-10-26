"""Install the dedicated local agent account/policy; no network service or shell tool."""

import json
import os
import pwd
import stat
import subprocess
from pathlib import Path

from devflow_core.agents import AgentStore
from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.registry import ControlRegistry


def setup(root):
    if os.geteuid() != 0:
        raise DevFlowError('Run the local agent installer as the trusted root operator')
    account, home = 'devflow-agent', Path('/var/lib/devflow-agent')
    try:
        user = pwd.getpwnam(account)
    except KeyError:
        if home.exists() or home.is_symlink():
            raise DevFlowError('Agent home already exists without its account; inspect before changing it')
        subprocess.run(['useradd', '--system', '--user-group', '--home-dir', str(home),
                        '--shell', '/usr/sbin/nologin', account], check=True)
        user = pwd.getpwnam(account)
    if (user.pw_uid == 0 or user.pw_gid == 0 or set(os.getgrouplist(account, user.pw_gid)) != {user.pw_gid} or user.pw_dir != str(home) or
        user.pw_shell not in {'/usr/sbin/nologin', '/sbin/nologin', '/bin/false'}):
        raise DevFlowError('Existing account does not match the dedicated agent account policy')
    if not home.exists():
        home.mkdir(mode=0o700)
        os.chown(home, user.pw_uid, user.pw_gid)
    info = home.stat()
    if home.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
        raise DevFlowError('Existing agent home is not a private directory owned by its account')
    contracts = Contracts(root / 'contracts')
    registry = ControlRegistry(root / 'control/registry.json', contracts)
    settings = read_json(root / '.ai-project/project.settings.json')
    contracts.validate(settings, 'project.settings.schema.json')
    agents = AgentStore(root, contracts)
    agents.initialize(Path('/etc/devflow/agents'))
    result = agents.enroll(registry, 'devflow-local', settings['servers']['control']['server_id'],
        settings['project']['id'], account, home, ['docs', 'src'],
        ['system.info', 'filesystem.list', 'filesystem.read'])
    return {'ok': True, 'agent_id': result['agent_id'], 'changed': result['changed'],
            'account': account, 'uid': user.pw_uid, 'transport': 'local_stdio', 'network_listener': False}


if __name__ == '__main__':
    print(json.dumps(setup(Path(__file__).resolve().parents[1]), indent=2))
