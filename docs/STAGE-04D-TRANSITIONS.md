# Stage 4D: approved release upgrades and targeted rollback

`release-transition` is a local root-only command. Its plan binds the published
source branch, active release SHA, target release SHA, unit and override hashes,
release paths and the public OAuth boundary. Approval is a short-lived exact
plan digest issued by the local operator security store. The public MCP server
does not expose these operations.

An upgrade stages a bounded Git archive from the published source commit and
atomically changes `/opt/devflow-releases/current`. A rollback selects an
already staged release by its full SHA and atomically changes the same pointer.
Both restart `devflow-mcp.service` and require process, listener, discovery,
unauthorized challenge and authenticated MCP tool checks. The service's data
root, virtual environment and systemd override stay in place.

After a verified failure, the runner restores the prior pointer, restarts the
service, and checks its authenticated health. It records a fenced `FAILED`
operation that requires an operator decision before another effect. If the
prior release cannot be verified, it leaves the operation unresolved for
manual reconciliation. A staged candidate is kept for inspection after a
failed upgrade. A plan cannot be reused after the pointer moves.

Prepare and validate an upgrade after committing and publishing the new stage
branch on the control server:

```
bash scripts/devflow release-transition prepare-upgrade --branch stage/04d-transitions > /etc/devflow/upgrade-plan.json
bash scripts/devflow release-transition validate --plan /etc/devflow/upgrade-plan.json
```

For a targeted rollback, use the SHA of an existing release:

```
bash scripts/devflow release-transition prepare-rollback --branch stage/04d-transitions --target-sha FULL_PRIOR_SHA > /etc/devflow/rollback-plan.json
bash scripts/devflow release-transition validate --plan /etc/devflow/rollback-plan.json
```

`approve --plan FILE --credential-file FILE --seconds 600` and `run --plan
FILE --approval-id ID --token-file FILE` use the same private operator
credential and short-lived Auth0 access token rules as the first migration.
Neither the token nor the client secret belongs in a plan, Git, or command
argument. Prepare a new plan after each successful transition; approval is
specific to that plan and the observed live release. The real upgrade and
rollback must be executed and audited separately before WORK-04-04 can be
marked complete. Unit tests and a local dry run do not establish live health.
