# Next

1. Apply Stage 3A and run `bash scripts/verify-stage3a.sh /etc/devflow/keys`.
2. Commit the verified source/milestone locally, then create a clean runtime checkpoint.
3. Continue Stage 3 with scoped remote-agent enrollment and project/path/capability
   restrictions. An agent principal is not an installed or connected remote agent.
4. Establish the public MCP resource URI, TLS and OAuth authorization provider;
   implement discovery, audience validation and per-request permissions before exposure.
5. Keep the local credential format separate from OAuth and upstream service tokens.
6. Implement controlled external execution and target-side fencing/idempotency before
   automatic recovery, watchdog jobs, reporting or unattended deployment.

Verified by operator before this patch: Stage 2, 69 tests, six-tool MCP smoke,
clean source commit a3b027c and runtime resume READY. Stage 3A needs its own
control-server verifier. Master-key/credential backups are separate from Git.
GitHub publication remains separate from local implementation and source commits.
