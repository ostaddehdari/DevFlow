---
name: resume
description: Use when the user says continue, resume, ادامه بده, or asks to continue a DevFlow project after interruption or in a new conversation.
---

# DevFlow Resume

Do not depend on previous conversation history as authoritative project state.

## Resume sequence
1. Resolve the project from explicit input or the DevFlow registry.
2. Load `project.design.json`.
3. Load project settings.
4. Load `state.json`.
5. Load the latest safe checkpoint.
6. Load `resume.bundle.json`.
7. Inspect GitHub current branch and HEAD.
8. Inspect actual server deployment state.
9. Inspect active jobs and execution lease.
10. Read unresolved errors and relevant logs.
11. Detect divergence or incomplete previous operations.
12. Determine the next safe action.
13. Continue execution.
14. Write a new checkpoint after each material action.

## Safety
Before retrying an interrupted action, verify whether it already completed.
Respect `do_not_repeat`.
If a destructive or ambiguous action could cause data loss, require human decision.
