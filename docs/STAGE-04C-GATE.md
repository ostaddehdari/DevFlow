# Stage 4C: exact Git push plan and operator approval

The `git.push` plan binds the current clean stage branch and SHA, the exact
origin/refspec and the expected remote branch SHA (null for a new branch).
The postcondition must equal the local source SHA. A mismatch in any field
invalidates the plan. Existing exact-plan, expiring operator approvals can
authorize the plan after a local operator credential is provisioned.

This patch adds validation and approval binding. It does not execute a push.
The future runner must observe the remote SHA before the effect, acquire a
continuity lease, recheck approval and local state immediately before push,
use a non-force explicit refspec, and verify the observed remote SHA afterward.
Unknown outcomes must require reconciliation, not an automatic retry.
The public M2M viewer cannot grant an approval.
