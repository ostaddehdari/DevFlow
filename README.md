# DevFlow

AI Development, Deployment & Continuity Platform.

## Current status
- Stage 0 core JSON contracts: complete
- Initial DevFlow skills-only plugin source: complete
- Browser automation: intentionally excluded
- Next: Stage 1 control repository + ServerMaster MCP bootstrap

## Required binding before Stage 1
Edit `.ai-project/project.settings.json` and replace the control server `address` with the actual server address.

Secrets must stay outside Git and be referenced with `vault://...`.
