# Next

Stage 3B was verified on the control server: 125 tests, real non-root UID 984,
three restricted agent tools, source commit 65230c8 and continuity READY.

Stage 3C source preparation adds an OAuth resource server bound to loopback and
local tests. To complete WORK-03-04, use the selected `https://srun.ir/mcpdevflow` URL and choose
the OAuth issuer, provision its public JWKS and project subject mapping outside
Git, then verify a real end-to-end TLS and OAuth MCP session. A source-only
commit is not a network deployment or completed Stage 3 checkpoint.

After Stage 3, target-side fencing and bounded controlled execution are needed
before unattended deployment or recovery. Browser automation remains excluded.
