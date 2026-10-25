---
name: project-designer
description: Use when the user provides a proposal, stages, works, requirements, repository details, or server settings and wants a canonical DevFlow project design JSON.
---

# DevFlow Project Designer

Produce a canonical `project.design.json` and compatible project settings from the supplied proposal and work plan.

## Rules
- Treat the proposal as input, not as unquestionable truth.
- Detect missing dependencies, missing infrastructure work, ordering problems, missing tests, missing acceptance criteria, and deployment gaps.
- Preserve explicit user decisions.
- Prefer filling inferable gaps over asking questions.
- Mark truly blocking decisions as `BLOCKED`.
- Include server information when supplied, especially server address, SSH port, username, project root, public URL, health URL, environment, and credential reference.
- Never place passwords, private keys, API tokens, or Telegram session data in generated Git-tracked JSON.
- Output machine-readable JSON conforming to the DevFlow contracts.
- The final validation status must be one of `READY`, `READY_WITH_AUTOFIXES`, or `BLOCKED`.

## Required project execution model
Each executable Work should define:
- dependencies
- acceptance criteria
- tests
- git policy
- deployment policy
- failure policy
- stop conditions

## Continuity
Assume the project must be resumable from external state without relying on previous chat history.
