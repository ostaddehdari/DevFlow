# Stage 4C: local approved Git push

`operation-plan run-push --plan PRIVATE_FILE --approval-id AP-... --owner OPERATOR`
uses the exact plan created after the final source commit. It is local-only;
the public MCP viewer cannot invoke it. The operator approval ledger must be
initialized and an active operator credential must grant the exact plan.

The runner checks the clean local stage branch, configured origin and source
SHA, reads the remote branch SHA, takes a continuity lease and checks the
approval and Git state again before marking the operation STARTED. For an
existing branch, it requires the remote commit to be a known ancestor of the
local commit. Git's explicit expected-old-ref lease rejects a concurrent
branch change. Thus a rewrite cannot pass the runner's ancestor check. Git
credentials remain in the server's private credential helper, outside Git
and outside the plan. Local Git hooks are disabled for this bounded action.
The push uses the approved immutable source SHA and inspected URL rather than
re-reading a mutable branch or remote name during the effect.

After the push, the runner reads the remote ref and records its SHA with
the execution fence and plan digest. Timeout, rejected push, or an uncertain
network response leaves the operation STARTED for reconciliation, without
an automatic retry. The full test suite uses a disposable bare Git remote;
verification does not push the production repository.
