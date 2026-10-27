"""Fail-closed OAuth resource server. TLS termination and provider are external."""
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl

from .contracts import Contracts, DevFlowError
from .security import ROLE_ACTIONS, SecurityStore
from .registry import ControlRegistry

MAX_CONFIG = 262144
MAX_JWKS = 262144


def private_json(path, maximum):
    path = Path(path)
    if not path.is_absolute():
        raise DevFlowError('OAuth configuration paths must be absolute')
    for parent in (path.parent, *path.parent.parents):
        s = parent.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o022:
            raise DevFlowError('OAuth configuration directory ownership invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077 or s.st_size > maximum:
            raise DevFlowError('OAuth configuration must be owner-only and bounded')
        payload = os.read(fd, maximum + 1)
        if len(payload) > maximum:
            raise DevFlowError('OAuth configuration too large')
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise DevFlowError('Duplicate OAuth configuration key')
                result[key] = value
            return result
        result = json.loads(payload, object_pairs_hook=unique_pairs)
        if not isinstance(result, dict):
            raise DevFlowError('OAuth configuration must be a JSON object')
        return result
    except (UnicodeError, ValueError) as exc:
        raise DevFlowError('Invalid OAuth configuration JSON') from exc
    finally:
        os.close(fd)


class NetworkPolicy:
    def __init__(self, root, contracts: Contracts, config_path):
        self.root = Path(root).resolve()
        self.contracts = contracts
        self.path = Path(config_path)
        if not self.path.is_absolute() or self.path.resolve().is_relative_to(self.root):
            raise DevFlowError('OAuth configuration must be outside project')
        self.store = SecurityStore(self.root, contracts)
        self.config()

    def config(self):
        data = private_json(self.path, MAX_CONFIG)
        self.contracts.validate(data, 'network.schema.json')
        issuer, resource = data['issuer'], data['resource']
        if not issuer.startswith('https://') or not resource.startswith('https://') or '?' in issuer+resource or '#' in issuer+resource:
            raise DevFlowError('OAuth issuer/resource require HTTPS without query or fragment')
        resource_parts = urlsplit(resource)
        issuer_parts = urlsplit(issuer)
        if (resource_parts.scheme != 'https' or issuer_parts.scheme != 'https' or
            not resource_parts.hostname or not issuer_parts.hostname or
            resource_parts.username or issuer_parts.username or
            resource_parts.password or issuer_parts.password or
            not resource_parts.path.startswith('/') or resource_parts.path == '/' or
            resource_parts.path.endswith('/') or '//' in resource_parts.path or
            '%' in resource_parts.path or '..' in resource_parts.path.split('/')):
            raise DevFlowError('OAuth resource must be an unambiguous HTTPS path')
        if Path(data['jwks_path']).resolve().is_relative_to(self.root):
            raise DevFlowError('OAuth keys must be outside project')
        projects = {row['project_id'] for row in ControlRegistry(
            self.root/'control/registry.json', self.contracts).read()['projects']}
        identities = set()
        for entry in data['principals']:
            key = (entry['subject'], entry['client_id'])
            if key in identities or not set(entry['projects']).issubset(projects):
                raise DevFlowError('Duplicate OAuth identity or unknown project grant')
            identities.add(key)
        return data

    def keys(self, config):
        payload = private_json(config['jwks_path'], MAX_JWKS)
        if set(payload) != {'keys'} or not isinstance(payload['keys'], list) or not payload['keys']:
            raise DevFlowError('Invalid JWKS structure')
        keys = {}
        for item in payload['keys']:
            if not isinstance(item, dict) or item.get('kty') not in ('RSA','EC') or item.get('use','sig') != 'sig':
                raise DevFlowError('Only asymmetric signing keys are accepted')
            algorithm = item.get('alg')
            if algorithm not in ('RS256','ES256') or (algorithm == 'RS256') != (item['kty'] == 'RSA'):
                raise DevFlowError('JWKS algorithm mismatch')
            kid = item.get('kid')
            if not isinstance(kid,str) or not 1 <= len(kid) <= 128 or kid in keys:
                raise DevFlowError('Duplicate or missing key ID')
            keys[kid] = item
        return keys

    def principal(self):
        token = get_access_token()
        if token is None or not token.subject or not token.client_id:
            raise DevFlowError('OAuth identity missing')
        config = self.config()  # Revocation of grants applies to existing sessions.
        entry = next((p for p in config['principals'] if p['subject'] == token.subject and
                      p['client_id'] == token.client_id), None)
        if entry is None or 'devflow.read' not in token.scopes:
            raise DevFlowError('OAuth principal is not authorized')
        return {'principal_id': token.subject, 'role': entry['role'], 'projects': entry['projects']}

    def check(self, action, project_id):
        principal = self.principal()
        if action not in ROLE_ACTIONS[principal['role']] or project_id not in principal['projects']:
            raise DevFlowError('OAuth access denied for project/action')
        return principal


class JWTVerifier:
    def __init__(self, policy):
        self.policy = policy

    async def verify_token(self, token):
        try:
            if not isinstance(token, str) or len(token) > 16384:
                return None
            config = self.policy.config()
            header = jwt.get_unverified_header(token)
            if set(header).intersection({'jku','x5u','jwk','x5c'}) or header.get('alg') not in ('RS256','ES256'):
                return None
            key_data = self.policy.keys(config).get(header.get('kid'))
            if key_data is None or header['alg'] != key_data['alg']:
                return None
            key = jwt.PyJWK.from_dict(key_data).key
            claims = jwt.decode(token, key, algorithms=[header['alg']], issuer=config['issuer'],
                audience=config['resource'], leeway=30,
                options={'require':['iss','aud','exp','iat','sub','scope']})
            client_id = claims.get('client_id', claims.get('azp'))
            if ('client_id' in claims and 'azp' in claims and claims['client_id'] != claims['azp']):
                return None
            if not isinstance(claims['sub'], str) or not isinstance(client_id, str) or not client_id or not isinstance(claims['scope'], str):
                return None
            scopes = claims['scope'].split()
            if 'devflow.read' not in scopes or not any(p['subject']==claims['sub'] and p['client_id']==client_id for p in config['principals']):
                return None
            return AccessToken(token=token, client_id=client_id, subject=claims['sub'],
                               scopes=scopes, expires_at=int(claims['exp']), resource=config['resource'])
        except (jwt.PyJWTError, DevFlowError, ValueError, KeyError, TypeError, OSError):
            return None


def build_http(root, config_path):
    from .registry import ControlRegistry
    from .server import build_server
    contracts = Contracts(Path(root)/'contracts')
    registry = ControlRegistry(Path(root)/'control/registry.json', contracts)
    registry.read()
    policy = NetworkPolicy(root, contracts, config_path)
    data = policy.config()
    policy.keys(data)
    auth = AuthSettings(issuer_url=AnyHttpUrl(data['issuer']),
                        resource_server_url=AnyHttpUrl(data['resource']),
                        required_scopes=['devflow.read'], validate_token_resource=True)
    return build_server(registry, policy, Path(root), transport='streamable-http',
                        token_verifier=JWTVerifier(policy), auth=auth,
                        http_path=urlsplit(data['resource']).path)
