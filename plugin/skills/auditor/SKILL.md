---
name: auditor
description: Use when the user wants a read-only assessment of a DevFlow project, repository, deployment, server, stage completion, drift, or operational health.
---

# DevFlow Auditor

Operate read-only unless the user separately asks for repairs.

Check:
- repository health
- expected vs actual deployment SHA
- project state consistency
- Stage and Work evidence
- test status
- service health
- Docker/systemd/Nginx state
- pending migrations
- configuration drift
- backups when relevant
- unresolved errors
- continuity state and expired leases
- secret leakage indicators

Return prioritized findings and recommended actions.
