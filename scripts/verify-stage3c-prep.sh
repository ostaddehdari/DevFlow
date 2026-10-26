#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
.venv/bin/python scripts/smoke_mcp.py --root "$PWD"
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from devflow_core.contracts import Contracts
from devflow_core.network import JWTVerifier, NetworkPolicy
root=Path.cwd()
Contracts(root/'contracts')
print('PASS: Stage 3C OAuth source preparation; live HTTPS/OAuth verification pending')
PY
