# Stage 3C preparation — authenticated HTTP resource server

`devflow serve-http --config /etc/devflow/network.json` binds only to
127.0.0.1:8765. It accepts signed RS256/ES256 JWT access tokens with exact
issuer, resource audience, expiry, subject, client ID and `devflow.read` scope.
Signing public keys are loaded from a local root-owned JWKS file on every
request. Subjects and client IDs map to project grants in a root-owned JSON
file. Project authorization is checked on every MCP tool call. The SDK exposes
OAuth protected-resource metadata and rejects unauthenticated HTTP requests.
No OAuth grant or token issuance is implemented here.

This is **source preparation, not a verified public deployment**. Do not create
`CP-STAGE03C-VERIFIED` or mark WORK-03-04 complete from these tests. First bind
an actual HTTPS domain and a real OAuth issuer. The issuer must issue access
tokens whose `aud` is the exact `https://DOMAIN/mcp` resource. Install its
signing JWKS outside the repository in a root-private file and update it during
key rotation. The JSON configuration follows `contracts/network.schema.json`.
The only allowed principal roles here are `viewer` and `operator`; every remote
tool remains read-only, including for operators. Use a reverse proxy with a
valid TLS certificate, preserve the `/mcp` and
`/.well-known/oauth-protected-resource/mcp` paths, and proxy internally to
127.0.0.1:8765, setting the upstream Host header to `127.0.0.1:8765`
for the SDK host check. Keep the listener bound to loopback. Do not place client
secrets, private signing keys, or access tokens in Git.

For completion, verify the live certificate, resource discovery, unauthenticated
401 challenge, wrong issuer/audience/expired token rejection, scoped successful
MCP tool call, cross-project denial, and immediate subject revocation. Record
actual evidence and update the Stage 3 milestone only then.
