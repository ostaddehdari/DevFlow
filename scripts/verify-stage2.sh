#!/usr/bin/env bash
set -euo pipefail
DEVFLOW_STAGE2_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$DEVFLOW_STAGE2_ROOT"
.venv/bin/python -m pytest -q
bash scripts/devflow validate
bash scripts/devflow continuity init
.venv/bin/python scripts/smoke_mcp.py --root "$DEVFLOW_STAGE2_ROOT"
PYTHONPATH=src .venv/bin/python scripts/record_stage2_verification.py
printf '%s\n' 'PASS: Stage 2 local continuity verified. Commit source, then create the runtime checkpoint.'
