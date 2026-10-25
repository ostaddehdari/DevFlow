#!/usr/bin/env bash
set -euo pipefail
DEVFLOW_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$DEVFLOW_SOURCE_ROOT"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-dev.txt
.venv/bin/python -m pytest -q
bash scripts/devflow init
bash scripts/devflow validate
.venv/bin/python scripts/smoke_mcp.py --root "$DEVFLOW_SOURCE_ROOT"
printf '%s\n' 'PASS: Stage 1 bootstrap verified; stdio MCP is ready for a local client.'
