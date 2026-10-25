# Next

1. Apply the canonical-design patch and run `bash scripts/finalize-stage1.sh`.
2. Commit the verified design, status-reader changes and refreshed evidence locally.
3. Start Stage 2: state transitions, execution leases with fencing, checkpoint lifecycle,
   resume bundles and interrupted-execution tests, following `project.design.json`.
4. Resolve identity, actual secret backend and authorized transport before Stage 3
   exposes any network service or enables a remote agent.
5. Complete controlled execution and recovery before enabling watchdog jobs.
6. Connect/install the private plugin and enable reporting only when prerequisites
   and destinations are established.

Already verified by operator output: Stage 0 commit/push and server binding;
Stage 1 server bootstrap, 21 source tests, registry validation and real stdio MCP
handshake, discovery and calls. Operator Stage 1 commit: cfc0d42.
