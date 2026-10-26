# Next

1. Apply Stage 3B and run `bash scripts/verify-stage3b.sh` as the trusted root operator.
2. Require its actual dedicated-user MCP probe before accepting WORK-03-03; a
   single-UID build environment's skipped subprocess test is not installation proof.
3. Commit verified source/milestones locally and create a clean runtime checkpoint.
4. Implement WORK-03-04: configure the public resource URI, TLS, OAuth discovery,
   issuer/audience checks and request authorization before exposing MCP externally.
5. Local stdio credentials are not public OAuth tokens. Remote-host enrollment and
   authenticated peer transport must not infer trust from a local profile alone.
6. Implement target-side fencing/idempotency and controlled external execution
   before unattended recovery, deployment, watchdogs or reporting.

Operator-verified before this patch: Stage 3A, 95 tests, seven main MCP tools,
clean source commit bfbf520 and runtime resume READY. Stage 3B is pending the
mandatory real control-server agent probe. Publication to GitHub is separate.
