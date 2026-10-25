# Next

1. Install the Stage 2 patch and run `bash scripts/verify-stage2.sh`.
2. Commit the verified source and milestone locally; create a runtime checkpoint
   with `PYTHONPATH=src .venv/bin/python scripts/create-local-checkpoint.py`.
3. Inspect `bash scripts/devflow continuity resume`; READY is expected with a clean
   checkout and no unresolved effects. It is a plan, not automatic execution.
4. Start Stage 3 with identity and authorization design; decide actual secret
   backend, client identity and transport before exposing any network service.
5. Stage 4 must make every external executor/target enforce fencing/idempotency;
   ledger fencing by itself cannot stop an external paused worker's side effects.
6. Implement controlled execution/recovery before enabling watchdogs or reporting.

Verified by operator output before this patch: Stage 1, 34 tests, registry and real
stdio MCP, source commit 2e97e4d. Stage 2 is pending the control-server verifier.
GitHub publication is separate from local source verification and commits.
