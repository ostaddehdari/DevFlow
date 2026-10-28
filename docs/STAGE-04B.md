# Stage 4B: bounded local read execution

The `operation-plan run-read` CLI accepts a validated `filesystem.read` plan.
It verifies the project, server capability, target path and clean Git identity,
then acquires a 60 second continuity lease. The plan digest is bound to its
operation key. A previously successful key returns without repeating work.

The runner starts only `devflow_core.operation_worker` in a new process group.
It has a 15 second default deadline and an 8 KiB combined output limit. A
deadline or output limit kills the process group, including its ordinary child
processes. The worker traverses the project using directory descriptors and
`O_NOFOLLOW`, reads at most 1 MiB, and returns only SHA-256 and byte count.
Worker output never contains the file contents. The parent checks the expected
digest, writes root-owned evidence under `.devflow/evidence/`, and records the
result in the continuity ledger. Its `operation.finished` event contains the
same execution ID. Ambiguous, failed or interrupted work requires explicit
continuity reconciliation. No automatic retry or write execution is enabled.

Use `bash scripts/verify-stage4b.sh` to check isolated acceptance tests and
contracts. It does not run a plan against the production project. The public
OAuth MCP remains read-only and does not expose this CLI command. Target-side
fencing, repository push and deployment are later work.
