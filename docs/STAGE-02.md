# Stage 2 — Local execution continuity

This stage implements a cooperative single-host ownership and evidence ledger.
It does not execute shell commands, deployments or external effects, expose a
network service, enable a watchdog or install the ChatGPT plugin.

## Persistent data and transactions

Each registered project has `.devflow/continuity.sqlite3`, outside Git. It stores
lease history and monotonically increasing fencing numbers, operation intents
and results, events, and checkpoint bundles. Each runtime checkpoint contains
schema-valid checkpoint, state and resume documents, committed in one SQLite
transaction with its event. The source `.ai-project` files remain Git-tracked
milestone projections; runtime changes do not rewrite them. Runtime resume
reads the database rather than relying on the source projection alone.

The implementation uses `BEGIN IMMEDIATE`, full synchronization, rollback-journal
mode and private files. A killed writer rolls back its uncommitted transaction.
A hot journal may require the operator's next writable CLI action to recover;
read-only MCP inspection never opens a write connection to perform recovery.
`continuity init` is repeatable and never resets corrupt/nonempty data. Metadata
binds the store to its project ID and installation root. Symlink storage is
rejected. Moving a project or restoring onto another host needs explicit
reconciliation; do not change metadata casually or delete the database to repair it.
Use a local filesystem and cooperating trusted local callers; NFS, replicated
control hosts and hostile same-user/root processes are outside this model.

A live SQLite backup requires SQLite's backup API, or stop all writers and take
an offline copy with any rollback journal. Git alone does not back up runtime data.
Never copy an active database file by itself and assume the result is consistent.

## Lease lifecycle

Acquire grants one owner a lease for 60–86400 seconds with a new execution ID and
fence. Renew extends a still-valid lease. Release marks it DONE. Expired ownership
cannot renew, start/finish an operation or create a checkpoint; the next acquire
marks the old execution INTERRUPTED and unfinished STARTED operations UNKNOWN.
Fences never reuse an old number. All fenced transactions recheck expiration
before commit. Clock rollback below the last persisted mutation stops writes and
makes resume require inspection. Keep the host clock synchronized; reads do not
persist clock observations and this is not a distributed-time protocol. After a
restart, a still-unexpired lease remains unavailable until expiry.

The fence protects this ledger. An external service must enforce that fence or
its own idempotency key to reject a paused old worker's actual side effects.
This stage cannot guarantee exactly-once behavior in an arbitrary external system.

## Intent and outcome lifecycle

| Current state | Allowed next state | Requirement |
| --- | --- | --- |
| Absent | PREPARED | Live lease, new key and JSON intent |
| PREPARED | STARTED | Live lease, no unresolved effect or failure |
| STARTED | SUCCEEDED / FAILED / UNKNOWN | Same live execution, evidence reference |
| Expired STARTED | UNKNOWN | Derived during inspection; persisted on next acquire |
| UNKNOWN / FAILED | SUCCEEDED / FAILED | Explicit operator reconciliation and evidence |
| UNKNOWN / FAILED | PREPARED | Explicit NO_EFFECT assertion with evidence |

An operation key is unique within a project and bound to the canonical intent
hash. Repeating prepare returns the existing record; it does not authorize a
second start. Successful results cannot be reset. A retry after NO_EFFECT keeps
the key and increments its attempt counter. UNKNOWN and failed results stop
new starts until resolved. The operator's evidence reference is an assertion
for the audit trail; the ledger cannot independently verify an external system.
Never place secret data in intents/evidence. Common inline-secret keys are
rejected, but this is not a general-purpose secret scanner.

## Checkpoints and resume

Safe runtime checkpoints require a clean named Git branch, matching project
identities, a design-bound current work and no STARTED/UNKNOWN operations.
Git is checked again during capture and the lease is checked before commit.
Checkpoint IDs are immutable. Source document fingerprints, observed HEAD/branch
and terminal-operation exclusions are recorded with the snapshot. Snapshot
validation also checks cross-document checkpoint, Git and work identities.

`continuity resume` is read-only and returns a plan, never executes it:

- `CREATE_CHECKPOINT`: no runtime snapshot exists.
- `WAIT_FOR_OWNER`: another valid lease is present.
- `HUMAN_DECISION`: ambiguous/failed result, dirty or divergent source, or clock rollback.
- `READY`: a consistent checkpoint exists, no live owner or unresolved effects.

A terminal result recorded after a snapshot is still visible in the current
operation ledger and its do-not-repeat list. FAILED results keep resume blocked
until an operator resolves them. READY means the evidence is suitable for the
next controlled step; it does not prove live server health or authorize any action.
Tracked `git.head_sha` is historical; the runtime snapshot binds the actual clean
HEAD observed at capture. Formatting-only JSON changes may preserve a semantic
fingerprint but a tracked Git change still blocks a clean checkpoint.

## Operator interface

```bash
bash scripts/devflow continuity init
bash scripts/devflow continuity status
bash scripts/devflow continuity lease-acquire --owner operator --seconds 600
```

Use the returned execution ID and fence with every mutation:

```bash
bash scripts/devflow continuity prepare --owner operator --execution-id EX_ID --fence 1 \
  --key example --intent '{"kind":"example-only"}'
bash scripts/devflow continuity start --owner operator --execution-id EX_ID --fence 1 --key example
bash scripts/devflow continuity finish --owner operator --execution-id EX_ID --fence 1 \
  --key example --outcome SUCCEEDED --evidence log://observed-result
bash scripts/devflow continuity lease-release --owner operator --execution-id EX_ID --fence 1
```

`lease-renew`, `resolve`, `checkpoint` and `snapshot` are also available; use
`--help` for arguments. `resolve --outcome NO_EFFECT` is an explicit operator
decision, not an automatic retry mechanism. Add `--project ID` after any continuity
verb to use another registered project. No API accepts arbitrary shell commands.

The new MCP tool `continuity_status` is read-only, project-bound, and does not
initialize a missing database. Six stdio tools are now exposed. Mutations remain
operator CLI-only. `servermaster_ping.execution_lease` describes this local ledger.

## Install and verify

After applying the Stage 2 patch, run `bash scripts/verify-stage2.sh`. It runs all
source tests, including competing processes, expired owners, partial-transaction
rollback, actual SIGKILL around intent/effect boundaries, CLI behavior, checkpoint
consistency and real six-tool MCP initialization/calls. Tests use temporary
projects and databases; they do not create operations in the production ledger.
The script initializes the production ledger and records a Stage 2 source milestone.

Commit the verified source and milestone. Only after a clean commit run:

```bash
PYTHONPATH=src .venv/bin/python scripts/create-local-checkpoint.py
bash scripts/devflow continuity resume
```

This creates one runtime checkpoint under a short lease and releases it. Expected
resume decision is READY. Existing active leases or unresolved production effects
cause a failure requiring inspection; the helper never deletes or resets them.
Network identity, actual Vault backend, authenticated transport, execution runner,
external fencing and automatic job/recovery scheduling remain subsequent work.
