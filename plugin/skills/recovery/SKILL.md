---
name: recovery
description: Use when a DevFlow execution, deployment, job, chat session, server operation, or scheduled run was interrupted and the system must safely recover.
---

# DevFlow Recovery

Recovery is state-driven, not browser-driven.

## Recover from
- expired execution lease
- chat interruption
- scheduled run interruption
- SSH disconnect
- build timeout
- server restart
- job interruption
- deployment failure

## Sequence
1. Read latest safe checkpoint.
2. Inspect actual server state.
3. Inspect running or completed jobs.
4. Inspect GitHub state.
5. Determine whether the interrupted action completed.
6. Choose resume, retry, rollback, skip, or human decision.
7. Update state and checkpoint.

Never use Puppeteer, Playwright, Chrome automation, noVNC, or DOM automation for continuity.
