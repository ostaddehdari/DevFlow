# Binding the selected public endpoint

The public MCP resource is `https://srun.ir/mcpdevflow`. The protected-resource
metadata endpoint is
`https://srun.ir/.well-known/oauth-protected-resource/mcpdevflow`.
The application stays on `127.0.0.1:8765` and uses the same `/mcpdevflow`
path internally. No path rewrite is needed.

The following are **location blocks only** for the existing TLS-enabled
`srun.ir` Nginx server block. Inspect that block and existing exact locations
before adding them. Keep unrelated locations and the current certificate.
Do not activate these blocks until the OAuth issuer, public JWKS and principal
mapping are configured and tested locally.

```nginx
location = /mcpdevflow {
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8765;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 60s;
    proxy_pass http://127.0.0.1:8765;
}
location = /.well-known/oauth-protected-resource/mcpdevflow {
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8765;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8765;
}
```

The root-private `/etc/devflow/network.json` must match
`contracts/network.schema.json`. Example values (placeholders are not usable):

```json
{
  "schema_version": "1.0",
  "issuer": "https://YOUR-TENANT.auth0.com/",
  "resource": "https://srun.ir/mcpdevflow",
  "jwks_path": "/etc/devflow/oauth-jwks.json",
  "principals": [
    {"subject":"YOUR-ISSUER-SUBJECT", "client_id":"YOUR-OAUTH-CLIENT-ID",
     "role":"viewer", "projects":["devflow"]}
  ]
}
```

The configured issuer must provide RS256 or ES256 signed access tokens for
this exact audience and `devflow.read` scope. The verifier accepts a `client_id`
claim or an Auth0-style `azp` client identifier; if both exist, they must
match. Its JWKS is copied into the root-private local file and refreshed when
signing keys rotate. Local credentials from Stage 3A are not accepted as OAuth
bearer tokens. Keep WORK-03-04 pending until live TLS, OAuth discovery, token
issuance, real MCP calls and denial cases are verified.
