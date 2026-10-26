#!/usr/bin/env bash
set -euo pipefail
DEVFLOW_STAGE3B_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$DEVFLOW_STAGE3B_ROOT"
.venv/bin/python -m pytest -q
bash scripts/devflow validate
PYTHONPATH=src .venv/bin/python scripts/setup-local-agent.py
PYTHONPATH=src .venv/bin/python scripts/probe-local-agent.py
.venv/bin/python scripts/smoke_mcp.py --root "$DEVFLOW_STAGE3B_ROOT"
PYTHONPATH=src .venv/bin/python scripts/record_stage3b_verification.py
printf '%s\n' 'PASS: Restricted local agent verified under a non-root account; HTTPS/OAuth transport remains pending.'
