# Canonical design and Stage 1 evidence

`.ai-project/project.design.json` is the baseline derived from the Stage 0
contracts, project settings, NEXT instructions and operator-supplied installation
results. Stages 0 and 1 are recorded as complete; stages 2 through 6 are plans,
not implemented capabilities or authorization to execute/publish their operations.
The next implementation is Stage 2: leases, fencing, checkpoints and safe resume.
Later transport, secret backend and notification destinations remain explicit
questions to resolve before those stages. Browser automation stays excluded.

Install the source patch, then run `bash scripts/finalize-stage1.sh` from the
project root. It runs tests and real MCP checks before recording a new verified
checkpoint, updating state/resume and inspecting project status. The historical
work ID `W01-REGISTRY-MCP` maps to canonical `WORK-01-01` in the design.

Status validates design schema, project identity, unique IDs, dependencies and
cycles, completed-work consistency and registered-root containment. The recorder
writes the checkpoint first, resume next and state last, using atomic individual
file replacement. This is an operator step, not the multi-document transactional
lifecycle or concurrent execution engine planned for Stage 2.

`deployment_verified` stays false: the stdio bootstrap has no live network-health
verifier. Recorded Git HEAD is the observed parent before the verification commit;
no self-referential commit hash or deployed SHA is fabricated. Git publication
remains a separate authorized operation.
