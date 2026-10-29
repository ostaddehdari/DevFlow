# Stage 4D: pinned-release deployment preparation

The running MCP service currently uses /opt/devflow as both its Git source and
Python runtime root. This is the legacy layout. A future release migration must
keep /opt/devflow as the runtime root, with its private registry, continuity
ledger, vault and virtual environment, while loading code from a pinned release
under /opt/devflow-releases. The versioned source directory and the current
pointer must be separate from the mutable Git checkout. The first migration
must preserve the pre-migration unit and its hash for rollback.

`deployment-plan inspect --branch stage/...` observes the exact published Git
commit, legacy systemd unit, loopback listener, public discovery and bearer
denial. `deployment-plan prepare` writes a reviewable JSON proposal to stdout.
`deployment-plan validate --plan FILE` rejects unit drift, a different Git
ref, an existing release pointer or a changed public authentication boundary.
The proposal has `execution_enabled: false`; no systemd command, release
directory, symlink, restart or deployment has been performed by this slice.

Next implementation must bind a sensitive deployment operation to a current
operator approval and continuity lease. It must create a verified immutable
release, switch the service to its code with an atomic pointer, verify the live
OAuth MCP session, and roll back to the preserved unit on failure. It must
record the observed deployed SHA only after health verification, and test a
forced failure and real rollback before declaring WORK-04-04 complete.
