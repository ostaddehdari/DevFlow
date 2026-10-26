# Stage 3B — Restricted local server agent

This implements WORK-03-03 on the control server with a dedicated non-root Linux
account and stdio. WORK-03-04 (authenticated public HTTPS/OAuth transport) remains
pending. It is not a remote-host connectivity verifier, daemon, shell runner,
network listener or external execution engine.

## Process boundary and identity

The installer creates/reuses only the matching dedicated `devflow-agent` account,
with a nologin shell, private `/var/lib/devflow-agent` home and no supplementary
grants. Conflicting existing accounts/homes are rejected without changing them.
The agent entry point refuses root UID/GID, supplementary groups outside its
primary group, nonzero effective Linux capabilities and a UID/GID inconsistent
with its enrollment. It enables Linux no-new-privileges and disables core dumps.

The control plane enrolls an agent against one registered project/server binding.
A root-owned, non-writable-by-others profile in `/etc/devflow/agents` contains the
explicit root, allowed relative directories, read-only capability set, UID/GID,
credential ID/hash and expiry. There is no credential value in the profile.
The service account gets only its own mode-0600 credential copy. It has no need
to read the root-only identity database, continuity database, Vault or master keys.
The full application is not a general OS sandbox: an independently compromised
service account can access whatever the OS grants that account, so the dedicated
account must not receive extra groups or file privileges. The RPC interface itself
only exposes the allowlisted reads below.

Enrollment keeps old bindings intact on duplicate/conflicting requests. It does
not auto-enable revoked or inconsistent profiles. Local identity authority is the
root-controlled profile and OS-created stdio process, not cryptographic remote
attestation. A matching nonce, expected agent/server/project IDs and observed
non-root UID/GID bind the local probe to the launched process. A separate remote
transport must establish its own authenticated peer identity.

## Three agent tools

| Tool | Capability | Behavior |
| --- | --- | --- |
| agent_info | system.info | Bounded identity, OS and privilege metadata |
| agent_list | filesystem.list | List at most 200 entries per page within explicit directories |
| agent_read | filesystem.read | Read UTF-8 text, at most 65536 bytes |

The default agent allows `docs` and `src` in the registered project checkout.
No command execution, file write, arbitrary Python, upload, network operation or
Vault lookup exists. Paths are POSIX-relative; empty/dot/traversal components,
backslashes, hidden files and common credential/key/database suffixes or directory
names are denied. This is a policy against common secret locations, not content
classification or a general secret scanner. Do not place actual secrets in allowed
plain-text documentation/code files.

Every path component is opened relative to a directory descriptor with no-follow
flags. Symlink directories/files, non-regular files and hardlinked regular files
are not read. FIFO opening is nonblocking before its file-type rejection. Reads
are bounded and binary/invalid UTF-8 fails. Listing scans at most 5000 entries,
filters hidden/special/secret entries and reports truncation; it is an inspection
API rather than a full filesystem crawler. No parent-directory permission is
broadened automatically. If the service account cannot import the runtime or read
an allowed public project path, the real probe fails for explicit inspection.

## Revocation, renewal and persistence

The root control registry `.devflow/agents.json` stays outside Git. Agent profiles
reload on every call, including open stdio sessions. `security revoke` and
`security disable` first disable associated profiles and then revoke their control
credentials. Agent-specific revocation uses the same path. An already-running call
may finish; subsequent calls are denied. If profile persistence fails, revocation
reports failure instead of claiming success. Cross-file/database updates are not
a distributed transaction. Partial enrollment/rotation is left fail-closed for
operator inspection; it is not silently reset or rebound.

Agent credentials expire after one day. Renewal is an explicit trusted-operator
command, preserves the existing project/path/capability grants and revokes the
old control credential. Failed renewal disables the profile. No unattended
renewal job is added. A revoked agent cannot be renewed back into service.

```bash
bash scripts/devflow agent inventory
bash scripts/devflow agent renew --agent-id devflow-local
bash scripts/devflow agent revoke --agent-id devflow-local
```

The eighth main ServerMaster tool, `agent_inventory`, returns scoped metadata,
without credential values/hashes and without a claim of remote connectivity.
The three agent tools are in a separate MCP process. The trusted local root
operator can launch it via runuser; a public ChatGPT tool to dispatch arbitrary
agent requests is not introduced in this patch.

## Install and mandatory real verification

After applying the patch, run `bash scripts/verify-stage3b.sh` as the trusted root
operator on Ubuntu. It runs source tests, creates/enrolls the dedicated account,
launches a real MCP worker under that account, verifies all three tools and a
permitted text read, rejects path escape and an unknown shell tool, then checks
the eight-tool control MCP server. A private short-lived proof records only public
identity/privilege metadata and a profile fingerprint. A fresh proof matching the
current profile is required before recording WORK-03-03 complete.

Some build containers map only UID 0 and cannot chown/setuid to another user. In
that case policy/enrollment unit tests simulate only the OS ownership adapter,
and the real non-root subprocess test is explicitly skipped. It must run on a
capable server; independently, the installation verifier always requires its real
service-account probe and cannot mark this Work complete by skipping that probe.
The target server's ordinary source tests also run the actual subprocess test
when UID mapping and capabilities permit it. Tests use temporary accounts'
pre-existing `nobody` identity and temporary fixtures, not the production agent.

The installer creates a local `agent-devflow-local` principal and one-day
credential. It does not import server passwords, SSH keys or Telegram credentials.
Its expected paths are:

- Root-controlled public profile: `/etc/devflow/agents/devflow-local.json`.
- Agent-owned private credential: `/var/lib/devflow-agent/devflow-local.token`.
- Root-only control registry/audit/credential copies: `.devflow/`.

Commit the verified source and milestone, then create a new runtime checkpoint
with `PYTHONPATH=src .venv/bin/python scripts/create-local-checkpoint.py`.
Expected continuation is READY, Stage 3 remains ACTIVE, and network authentication
and remote-host verification remain false. No Nginx, firewall or other application
service is changed by this patch.
