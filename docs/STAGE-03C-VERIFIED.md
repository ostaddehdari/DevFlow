# Stage 3C live acceptance

Stage 3C is recorded only after `scripts/record_stage3c_verification.py` checks
the root-private proof at `/etc/devflow/stage3c-proof.json`, current OAuth
configuration, active loopback service, public HTTPS metadata, and bearer
challenge. The separate live probe obtains an ephemeral Auth0 machine-to-machine
token and verifies a real MCP session, read-only tools, cross-project denial,
tampered-token denial, immediate principal revocation, and restoration.

The public resource is `https://srun.ir/mcpdevflow`. The issuer is
`https://dev-fqzv1aqwljc7mhwz.us.auth0.com/`. Root-private OAuth settings,
public JWKS copy, and proof stay outside Git. JWKS rotation requires a trusted
refresh of `/etc/devflow/oauth-jwks.json`; stale keys fail closed. This stage
does not implement OAuth user login for a ChatGPT connector, a remote server
agent, write-capable tools, unattended deployment, or target deployment health.

The checkpoint records the parent Git SHA observed before the milestone commit.
After committing the source projection, create a clean runtime checkpoint and
confirm that continuity reports `READY` with `automatic_execution: false`.
