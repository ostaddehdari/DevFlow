---
name: debugger
description: Use when a DevFlow project has an error, failed deployment, unhealthy service, failed test, broken build, or runtime problem that should be diagnosed and repaired.
---

# DevFlow Debugger

Use evidence-driven debugging.

## Loop
1. Identify the failed component and time window.
2. Collect only relevant logs and state.
3. Correlate code, deployment, configuration, runtime, network and database evidence.
4. Identify the most likely root cause.
5. Apply the smallest safe fix.
6. Test.
7. Commit and push when source/config tracked by Git changes.
8. Redeploy if needed.
9. Verify health and acceptance criteria.
10. Record error resolution and checkpoint.

Avoid dumping large raw logs into Git; store concise excerpts and diagnostic summaries.
