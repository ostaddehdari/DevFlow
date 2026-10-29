# Stage 4D: approved first release migration

The running production unit still loads code from the mutable /opt/devflow Git
checkout. This change adds a **local root-only** one-time migration runner; it
does not change the service during installation or verification.

After this source branch has been committed, pushed and locally checkpointed,
prepare a **new** plan with `deployment-plan prepare-execution --branch
stage/04d-release-runner`. This executable plan differs from the earlier
read-only proposal: `execution_enabled` is true and its commit, remote ref,
legacy unit hash, service state and release paths are bound to the approval.
Use `deployment-plan validate --plan FILE` and a short-lived
`stage4-operator` credential with `deployment-plan approve --plan FILE
--credential-file FILE --seconds 600`. Approvals remain private, revocable,
and tied to the exact canonical plan digest. Neither approval nor execution
is exposed as a public MCP tool.

The runner takes a short-lived Auth0 **access token** from an owner-only local
file using `--token-file`. It validates the token against the configured JWKS
before any effect. Never place the client secret, token or operator credential
in Git, a command argument, an SSH transcript or the plan. Delete the
ephemeral token and operator credential files after the run.

`deployment-plan run --plan FILE --approval-id ID --token-file FILE`
revalidates the exact approved plan, obtains a fenced continuity lease, builds
a bounded pinned archive from the published local Git commit, and stages it
under /opt/devflow-releases/<sha>. It atomically points
/opt/devflow-releases/current at that release, installs a small systemd
drop-in, reloads and restarts devflow-mcp.service. The service keeps
/opt/devflow as its data/configuration root and uses its existing virtual
environment; Python code comes from the release pointer.

Success requires observing the effective systemd drop-in and process
PYTHONPATH, loopback listener, HTTPS discovery 200, unauthenticated bearer
challenge 401, and a real authenticated MCP initialization and tool calls.
Only then is the deployed SHA recorded in continuity evidence. If any step
fails, the runner removes its override and pointer, restarts the legacy
service, verifies the original unit and health, and records a fenced failure.
If rollback cannot be verified, the operation remains unresolved. A failed
operation requires explicit reconciliation before any other effect.

The release remains on disk for inspection after a failed attempt. This
first-migration runner refuses an existing release pointer, override or
occupied release SHA. Subsequent version upgrades and a separately approved
rollback command are future work. A successful test in a temporary Git
repository cannot itself establish live deployment health.
