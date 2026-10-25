#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
    printf '%s\n' 'Usage: bash scripts/verify-stage3a.sh /etc/devflow/keys' >&2
    exit 2
fi
DEVFLOW_STAGE3A_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$DEVFLOW_STAGE3A_ROOT"
.venv/bin/python -m pytest -q
bash scripts/devflow validate
bash scripts/devflow security init --key-dir "$1"
.venv/bin/python scripts/smoke_mcp.py --root "$DEVFLOW_STAGE3A_ROOT"
PYTHONPATH=src .venv/bin/python scripts/record_stage3a_verification.py
printf '%s\n' 'PASS: Stage 3A verified; agent/network work remains pending. Commit source, then create a runtime checkpoint.'
