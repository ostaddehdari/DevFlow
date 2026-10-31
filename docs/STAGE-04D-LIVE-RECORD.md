# Stage 4D: record the verified first release migration

The first pinned release at commit `42045b9d87550852a591b54982ff84a520516a6d`
has already passed the live authenticated migration probe. This milestone
audits the active pointer, running process, loopback listener, public OAuth
challenge, and the private fenced success evidence again. It records the
observed deployed SHA in the tracked project state and a source checkpoint.
The audit does not restart or alter the production service.

The source recorder marks Stage 4 ACTIVE, the first three works COMPLETED,
and WORK-04-04 VERIFYING. Approved upgrades and a tested targeted rollback
are still required to complete the work. The runner that performed the
first migration cannot be reused while the release pointer exists.

Run the source verifier with a readable `umask 022`, commit the source, run
`PYTHONPATH=src .venv/bin/python scripts/record_stage4d_migration.py`
on the control server, validate and commit the resulting project documents
and checkpoint, then create a local runtime checkpoint. A new source commit
does not itself deploy a new version. Keep the deployed SHA bound to the
release observed by the audit.
