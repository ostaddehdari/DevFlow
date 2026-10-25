---
name: executor
description: Use when a DevFlow project design exists and the user wants the project, stage, or work item implemented, tested, committed, deployed, and verified.
---

# DevFlow Executor

Execute the canonical DevFlow workflow.

## Workflow
For each Work:
1. Load project design and settings.
2. Verify preconditions.
3. Acquire or confirm execution lease.
4. Implement.
5. Test.
6. Commit.
7. Push.
8. Deploy when required.
9. Verify acceptance criteria and health.
10. Write checkpoint.
11. Update state and resume bundle.
12. Continue to the next Work when policy permits.

## Failure loop
On failure:
1. collect relevant logs
2. diagnose root cause
3. fix
4. retest
5. commit and push the fix
6. redeploy
7. verify
8. checkpoint

Stop for critical or destructive operations requiring human approval.

Never treat code generation alone as Work completion.
