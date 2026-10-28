# Stage 4A: bounded operation plans

`operation.plan.schema.json` and `PlanPolicy` define the first two typed operations:
`filesystem.read` and `filesystem.write`. A plan binds project and server,
project-relative path, source Git identity, expected prior content, intended
content digest and postcondition. Unknown operations, shell commands, internal
metadata paths, symlinks, and paths outside the project fail closed.

A write needs an exact-plan approval in an owner-only local ledger. Only a local
operator credential with the `operation.approve` action can grant it for at most
ten minutes. The digest covers the entire plan. Expiration, revocation and the
operator credential's current status are checked on every verification. The
public M2M viewer has no approval or write action.

This slice only validates plans and approvals. It neither executes commands
nor writes project files. Stage 4.2 must recheck approval, path, Git state and
continuity fencing immediately before each effect, and use bounded execution
with recorded evidence. Source verification on the target is required before
marking WORK-04-01 complete; Stage 4 remains incomplete.

The local CLI provides `operation-plan validate --plan FILE`,
`operation-plan approval-init`, `operation-plan approve --plan FILE
--credential-file PRIVATE_FILE`, `operation-plan verify --plan FILE
--approval-id ID`, and `operation-plan revoke --approval-id ID`. The operator
credential must remain in an owner-only local file and is never printed.
