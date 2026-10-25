# Stage 3A — Local identity, authorization and encrypted reference vault

This installs the first two Works of Stage 3. Remote-agent enrollment and the
public authenticated MCP transport remain pending. No listener, Nginx rule,
firewall change, OAuth provider or ChatGPT connection is installed here.

## Authority and roles

The control installation stores identities, credential hashes, reference metadata
and an audit trail in `.devflow/security.sqlite3`. It is bound to its installation
root/project and an independent local audience ID. Each principal has explicit
project IDs; wildcards are rejected. Duplicate creation never silently changes
grants. The trusted local operator administers this store through CLI commands.

| Role | Application actions |
| --- | --- |
| viewer | Read scoped registry, project, continuity and control security metadata |
| operator | Viewer actions, local continuity authorization and explicitly granted vault references |
| agent | Read its projects/continuity and receive continuity-write authorization; no inventory, vault or shell authority |

The agent role is an identity/policy definition, not an installed remote agent.
No role grants shell execution or network administration in this stage. Vault
references require exact grants in addition to the project and action. A reference
cannot be rebound to a different project. Actual continuity mutations remain
trusted local CLI-only; no new mutating MCP tools are added.

Credentials use 256 bits of random material. Only hashes are stored in SQLite;
the credential value is written once to a mode-0600 file under ignored
`.devflow/credentials/`. Issue output contains only its public ID, file path and
expiry, never the value. Lifetime is 60–86400 seconds. Revocation and principal
disable are checked on every guarded call, including an open stdio session.
Clock rollback below the last recorded mutation stops authorization.

Local credentials are not OAuth access tokens. The local audience and database
binding prevent accidental reuse across local authorities; this is not an HTTP
OAuth resource-server implementation. A future public MCP endpoint needs proper
OAuth discovery, issuer/resource audience checks, TLS and per-request validation.
These credentials must not be forwarded to an upstream service.

## Scoped stdio

Existing `serve` retains the trusted-local-process mode used by the local owner.
To run a session with application-level restrictions, the owner explicitly uses:

```bash
bash scripts/devflow security principal-create --principal-id chatgpt-reader \
  --role viewer --project devflow
bash scripts/devflow security issue --principal-id chatgpt-reader --seconds 3600
# Use credential_file from the metadata output; do not print its contents.
bash scripts/devflow serve --credential-file /absolute/path/from/issue-output.token
```

A real MCP client launches the last command. Invalid credentials fail before
startup. The session rechecks credentials and project permission for every tool.
Registry lists/counts and server inventories are filtered to authorized projects.
A foreign active project is not substituted with an accessible project: implicit
access to it is denied. Secret values and credential hashes have no MCP tools.
`security_status` adds the seventh read-only tool and returns metadata only.

The OS owner can still choose trusted-local mode or administer the databases.
Application grants do not isolate an untrusted process running as that same OS
user/root. Public service identity and OS isolation belong to the next transport
and agent deployment work. No production client credential is issued by the verifier.

## Encrypted vault and master keys

Vault values use the cryptography library's authenticated Fernet encryption.
The encrypted envelope binds the plaintext to its exact reference and project;
ciphertext swaps across entries fail verification. Values never enter SQLite as
plaintext. Secret lookup is an internal API with project, action and exact-reference
checks; it returns bytes to an authorized consumer, not to a chat tool.

Master keys are mode-0600 files in an owner-only directory outside the repository,
provided explicitly by the operator, for example `/etc/devflow/keys`. Symlink key
files/storage, permissive files/directories and a lost or wrong active key fail
closed. Missing keys are never automatically regenerated for existing data.

Key rotation creates a new external key and re-encrypts all entries in one SQLite
transaction before switching the active key. Old keys remain for backups and
in-flight readers. New keys are retained even after uncertain commit acknowledgement;
an orphan key is preferable to deleting a potentially active committed key.
There is no automatic key deletion, including keys shared with other stores.

Back up the external master-key directory and the security database separately
from Git. Losing the required key loses access to encrypted values. Keys accessible
to the same privileged OS owner do not protect against compromise of that owner.
A live database backup must use SQLite's backup API, or stop writers and take an
offline consistent copy with any rollback journal. Do not put master keys or
credential files in source archives, Git or chat messages.

## Operator commands

```bash
bash scripts/devflow security init --key-dir /etc/devflow/keys
bash scripts/devflow security status
bash scripts/devflow security revoke --credential-id C_PUBLIC_ID
bash scripts/devflow security disable --principal-id chatgpt-reader
bash scripts/devflow security vault-list
bash scripts/devflow security vault-rotate
```

Store an actual secret only when the operator is ready. The interactive command
uses hidden terminal input; controlled stdin is available for multiline/binary data:

```bash
bash scripts/devflow security vault-put --project devflow \
  --reference vault://servers/devflow-control-server/root
```

The source `credential_ref` entries remain declarations until actual values are
stored. Initialization does not import root credentials, Git tokens or Telegram
credentials, and no placeholder value is treated as an available secret. There
is intentionally no CLI command that prints a decrypted value. The audit trail
records selected public identifiers and actions, never token values or secret bodies.

## Verification and source milestone

Run `bash scripts/verify-stage3a.sh /etc/devflow/keys`. Tests cover project/action
denials, exact vault grants, token expiry/revocation, disabled principals, private
file modes, master-key loss, ciphertext tampering/swaps, transactional rotation,
CLI no-echo behavior, and a real scoped MCP session with live revocation. Tests
use temporary identities, keys and values. The verifier initializes only the real
backend and records the source milestone; no real secret or client token is created.

The source milestone marks Stage 3 ACTIVE with WORK-03-01 and WORK-03-02 complete;
WORK-03-03 and WORK-03-04 stay PENDING. Source milestone projection writes are
individual atomic files, separate from transactional runtime checkpoints.
After committing source, create a fresh runtime checkpoint with
`PYTHONPATH=src .venv/bin/python scripts/create-local-checkpoint.py`.
Network authentication, remote-agent readiness and live deployment health remain
false. Configure a public endpoint and OAuth identity flow before that changes.
