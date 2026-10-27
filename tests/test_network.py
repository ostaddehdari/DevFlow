import json
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient

from devflow_core.network import JWTVerifier, NetworkPolicy
from devflow_core.contracts import DevFlowError

ISSUER='https://id.example.invalid/'
RESOURCE='https://control.example.invalid/mcp'

@pytest.fixture
def configured_verifier(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid='one',use='sig',alg='RS256')
    config={'issuer':ISSUER,'resource':RESOURCE,'principals':[
        {'subject':'alice','client_id':'test-client','role':'viewer','projects':['devflow']}]}
    policy=object.__new__(NetworkPolicy)
    monkeypatch.setattr(policy,'config',lambda:config)
    monkeypatch.setattr(policy,'keys',lambda ignored:{'one':public})
    def signed(**changes):
        claims={'iss':ISSUER,'aud':RESOURCE,'exp':int(time.time())+300,'iat':int(time.time()),
                'sub':'alice','client_id':'test-client','scope':'devflow.read'}
        claims.update(changes)
        if claims.get('client_id') is None:
            claims.pop('client_id', None)
        return jwt.encode(claims,private,algorithm='RS256',headers={'kid':'one'})
    return JWTVerifier(policy),signed,config,policy

def test_oauth_verifier_rejects_wrong_audience_scope_subject_client_and_expiry(configured_verifier):
    verifier,signed,config,_=configured_verifier
    check=lambda token: asyncio.run(verifier.verify_token(token))
    assert check(signed()).subject=='alice'
    for changes in ({'aud':'https://other.invalid/mcp'}, {'scope':'other'},
                    {'sub':'mallory'}, {'client_id':'other-client'}, {'exp':int(time.time())-300}):
        assert check(signed(**changes)) is None
    assert check(signed(azp='other-client')) is None
    assert check(signed(client_id=None, azp='test-client')).client_id == 'test-client'
    config['principals'].clear()
    assert check(signed()) is None


def test_bearer_gate_and_protected_resource_metadata(configured_verifier):
    verifier,signed,_,_=configured_verifier
    auth=AuthSettings(issuer_url=AnyHttpUrl(ISSUER),resource_server_url=AnyHttpUrl(RESOURCE),
                      required_scopes=['devflow.read'],validate_token_resource=True)
    app=FastMCP('Test',token_verifier=verifier,auth=auth,stateless_http=True)
    with TestClient(app.streamable_http_app()) as client:
        response=client.post('/mcp',json={})
        assert response.status_code==401
        assert 'resource_metadata=' in response.headers['www-authenticate']
        metadata=client.get('/.well-known/oauth-protected-resource/mcp')
        assert metadata.status_code==200
        assert metadata.json()['resource']==RESOURCE
        response=client.post('/mcp',headers={'Authorization':'Bearer '+signed()},json={})
        assert response.status_code != 401


def test_principal_grants_rechecked_per_call(configured_verifier, monkeypatch):
    _,_,config,policy=configured_verifier
    from mcp.server.auth.provider import AccessToken
    token=AccessToken(token='opaque',client_id='test-client',subject='alice',scopes=['devflow.read'],resource=RESOURCE)
    monkeypatch.setattr('devflow_core.network.get_access_token', lambda:token)
    assert policy.check('project.read','devflow')['role']=='viewer'
    with pytest.raises(DevFlowError):
        policy.check('vault.read','devflow')
    config['principals'].clear()
    with pytest.raises(DevFlowError):
        policy.check('project.read','devflow')


def test_real_server_app_has_authenticated_mcp_routes(tmp_path, configured_verifier, monkeypatch):
    import shutil
    from pathlib import Path
    from devflow_core.contracts import Contracts
    from devflow_core.registry import ControlRegistry
    from devflow_core.network import build_http
    root=tmp_path/'project'
    (root/'.ai-project').mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1]/'.ai-project/project.settings.json',
                    root/'.ai-project/project.settings.json')
    shutil.copytree(Path(__file__).resolve().parents[1]/'contracts',root/'contracts')
    ControlRegistry(root/'control/registry.json',Contracts(root/'contracts')).register(root/'.ai-project/project.settings.json')
    verifier,signed,config,policy=configured_verifier
    resource='https://srun.ir/mcpdevflow'
    config.update(schema_version='1.0',jwks_path='/etc/devflow/test-jwks.json',resource=resource)
    monkeypatch.setattr('devflow_core.network.private_json',lambda path, maximum: config)
    monkeypatch.setattr('devflow_core.network.NetworkPolicy.keys',lambda self, data: policy.keys(data))
    app=build_http(root, '/etc/devflow/network.json')
    with TestClient(app.streamable_http_app()) as client:
        assert client.post('/mcpdevflow',json={}).status_code==401
        assert client.get('/.well-known/oauth-protected-resource/mcpdevflow').json()['resource']==resource
        assert client.post('/mcp',json={}).status_code==404
        response=client.post('/mcpdevflow',headers={'Authorization':'Bearer '+signed(aud=resource)},json={})
        assert response.status_code != 401
