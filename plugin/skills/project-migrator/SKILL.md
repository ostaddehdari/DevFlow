---
name: project-migrator
description: Use when importing an existing, partially completed, deployed, or inconsistent software project into the DevFlow structure.
---

# DevFlow Project Migrator

Reconstruct the actual state of an existing project and convert it into DevFlow.

## Sources of evidence
Use, when available:
- repository code and commit history
- branches, tags, releases, issues and PRs
- deployment files
- server runtime state
- deployed commit SHA
- Docker, systemd, Nginx and database state
- migrations
- relevant logs
- old proposal or work plan

## Rules
- Do not assume old stage labels are accurate.
- Completed work must have evidence.
- Assign confidence to reconstructed status.
- Detect Git/server divergence.
- Detect server-side uncommitted or manually modified configuration.
- Detect pending database migrations.
- Record architecture drift instead of blindly reverting it.
- Produce a recommended safe resume point.

## Outputs
Produce:
- `project.design.json`
- `state.json`
- `migration.report.json`
- `resume.bundle.json`

Never store plaintext credentials.
