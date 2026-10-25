# Stage 1 — Control registry and ServerMaster MCP bootstrap

## Scope and acceptance

This stage provides the first executable control-plane slice on Ubuntu/Python 3.12.
It uses the unchanged Stage 0 Draft 2020-12 contracts. Acceptance consists of:

1. Schema-validated registration of DevFlow and its declared control server.
2. Idempotent registration, unique identifiers and explicit active-project selection.
3. Concurrent registrations without lost updates; invalid input never replaces good data.
4. Real stdio MCP initialization, discovery and invocation of five read-only tools.
5. Explicit reporting of missing continuity prerequisites and unverified deployment.

Source tests do not certify a production installation. The tracked state remains
`VERIFYING` until the control-server bootstrap output is inspected.

## Architecture and storage

- `contracts/` is the canonical schema source. All references resolve from the local
  schema catalog; the validator does not fetch schema URLs from the network.
- `src/devflow_core/contracts.py` validates schemas and documents, including date-time
  formats through the explicitly installed RFC 3339 validator.
- `src/devflow_core/registry.py` handles registry integrity, registration and document reads.
- `src/devflow_core/server.py` provides the stdio tools through the official MCP Python SDK.
- `src/devflow_core/cli.py` exposes local operator commands.

The default registry is `<root>/control/registry.json`; both reads and writes validate
the full document. Writers serialize using `fcntl.flock` on a stable sibling lock file.
They validate before writing, create a mode-0600 temporary file, fsync it, atomically
replace the registry, and fsync the parent directory. Readers see an entire snapshot.
Advisory locking assumes cooperating local writers on a local Linux filesystem; shared
NFS and multiple control hosts are not supported by this storage implementation.

The registry schema intentionally stays unchanged: no private implementation fields
are added to project or server records. The registered absolute `settings_path` binds
each project's identity to its local root. Server records are declarations copied from
settings, not observed capabilities or a claim that an SSH credential is available.

Registration merges projects and servers under the same lock. Existing relative
settings paths are resolved against the registering project root, verified against
the supplied settings, and stored as absolute paths without creating a second registry. A repeated identical
registration does not rewrite the file. A project ID cannot be rebound to another
path/repository. Different projects may share an exactly identical server record;
conflicting definitions for the same server ID fail. Changing an existing server's
binding requires a later explicit reconciliation workflow; registration will not
silently replace it. Corrupt registries fail closed and must not be deleted to repair them.

## Operator commands

```bash
bash scripts/bootstrap-stage1.sh
bash scripts/devflow validate
bash scripts/devflow list
bash scripts/devflow status --project devflow
bash scripts/devflow register --settings /opt/another/.ai-project/project.settings.json
bash scripts/devflow activate another
```

Overrides precede the subcommand:

```bash
bash scripts/devflow --registry /var/lib/devflow/registry.json init
```

Use the same override for every later read or serve command. Back up the registry
and project documents alongside the repository; ignored runtime data is not on GitHub.

`status` reports recorded state/resume/checkpoint content and a separately observed
local Git HEAD. A recorded SHA is historical, so it need not match the current HEAD.
The source checkpoint records the observed Stage 0 base; a Git commit cannot contain
its own eventual hash. No deployed SHA, server health or remote connectivity is inferred.
The command flags a missing project design/checkpoint, validates document identities,
and rejects continuity-document symlinks escaping the bound project root.
It is an inspection command, not a complete recovery/lease or deployment reconciler.

## MCP startup and verification

For a local client on the control server, use `examples/mcp.local.json`. For another
installation root, change both absolute paths there. A stdio host launches the process
and controls its lifetime; no systemd daemon is needed for this transport.

```bash
.venv/bin/python scripts/smoke_mcp.py --root /opt/devflow
```

The smoke client performs a real initialize handshake, asserts the exact tool set
and read-only annotations, calls all five tools, verifies that an unknown `shell_exec`
tool is rejected, and shuts down the subprocess. Tests use temporary registries.

The tool names are `servermaster_ping`, `registry_projects`, `registry_servers`,
`registry_validate` and `project_status`. No tool accepts an arbitrary file path or
shell command. All operator mutations are CLI-only in this stage. The SDK owns protocol
handling; application stdout is reserved for MCP protocol while serving.

## Boundaries and follow-up

This code is source for an MCP server on the user's machine. It is not an installed
ChatGPT plugin, a remote agent, a network endpoint, a health probe, an encrypted vault,
a Telegram sender, a job runner, an execution lease or a scheduled watchdog.
Capability flags in settings describe desired configuration. The ping tool reports
implemented capabilities separately and explicitly marks these future services false.

Only credential references are accepted by the server/settings schemas. Common inline
credential keys in arbitrary metadata/documents are rejected as an additional guard;
this is not a general-purpose secret scanner. Keep registry files private to the
operator and do not run an untrusted MCP client as root. There is no HTTP authentication
surface in this stdio-only stage. Future remote transport needs its own identity and
authorization design before it can be exposed.

Before enabling autonomous execution, create a project design, add actual execution
leases/checkpoint lifecycle, and reconcile source state against observed server state.
Do not enable the scheduled watchdog or infer that an interrupted command is safe to retry.
