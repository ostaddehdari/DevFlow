# Delivery stages 1 and 2: user connection and live control observations

The public Streamable HTTP endpoint is `https://srun.ir/mcpdevflow`.
`plugin/mcp.json` names that endpoint without storing credentials. The eight
existing registry/continuity tools and three new observations are read-only.
The new tools inspect only the local DevFlow control host, require
`project.read` for the control project, and do not return raw journal entries,
filenames from Git status, vault values, tokens, or arbitrary command output.
Registry server entries remain declarations, not live checks of other hosts.

## One-time Auth0 user connection

The existing resource server already verifies asymmetric Auth0 access tokens
against its private JWKS, exact issuer and resource audience, `devflow.read`
scope, and an exact `(sub, client_id/azp)` project grant. It also rechecks
the private grant on each tool call, so removing the grant revokes an existing
session. This release tests the user-token case and adds a narrow enrollment
command. It does not install or configure an Auth0 application automatically.

1. In the selected Auth0 tenant, create a user-facing OAuth application for
   authorization code with PKCE. Authorize the existing API audience
   `https://srun.ir/mcpdevflow` and scope `devflow.read`. Enable refresh token
   access if supported and needed by the ChatGPT connection; grant only this
   scope. Do not reuse the machine-to-machine Client Secret in a plugin.
2. In ChatGPT's custom MCP connection flow, enter the public endpoint and
   choose OAuth. Copy the callback URL that **that flow displays** into the
   Auth0 application's allowed callback URLs. The callback is installation
   specific; do not guess one. Complete sign-in with the intended user.
3. Obtain the signed-in user's exact Auth0 `sub` and the OAuth application's
   client ID from the tenant dashboard. On the control server, after the new
   source branch has been checked out, run as root:

   ```bash
   cd /opt/devflow
   PYTHONPATH=src .venv/bin/python scripts/enroll_chatgpt_user.py \
     --subject 'AUTH0_USER_SUB' --client-id 'AUTH0_USER_CLIENT_ID'
   ```

   The command appends one viewer grant to `/etc/devflow/network.json` with
   mode 0600, preserving the existing machine grant. It is idempotent for an
   exact grant and rejects a conflicting grant. The subject and client ID
   are identifiers, not passwords. Never paste an access token, Client Secret,
   or private policy file into chat.
4. In the client, refresh/scan the tool inventory and call
   `control_service_live` for project `devflow`; confirm the active release,
   HTTP 200/401 boundary, and a successful authorized call. Call
   `control_resources_live` and `control_git_live`, then remove the viewer
   grant during a controlled test to prove revocation if desired.

Account permissions may limit which tools ChatGPT can expose. The endpoint
remains usable by another OAuth-capable MCP client even if the current ChatGPT
account cannot create a custom connection. Do not expose the endpoint without
OAuth as a workaround.

## Source and live acceptance

Run `bash scripts/verify-stage12-source.sh` on the published checkout. The
source test checks all 11 tool annotations, grant revocation and the user
access-token path. Only a real release upgrade and authenticated MCP calls
prove that the three observations work on the control host. The prior
Stage 4D upgrade and rollback both succeeded; the running release after that
drill is `42045b9d87550852a591b54982ff84a520516a6d`. The new source commit
must be published and upgraded through a fresh exact-plan approval. Do not
repeat either prior transition operation.
