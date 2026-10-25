#!/usr/bin/env bash
set -euo pipefail
DEVFLOW_VERIFY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$DEVFLOW_VERIFY_ROOT"
.venv/bin/python -m pytest -q
bash scripts/devflow validate
.venv/bin/python scripts/smoke_mcp.py --root "$DEVFLOW_VERIFY_ROOT"
PYTHONPATH=src .venv/bin/python scripts/record_stage1_verification.py
bash scripts/devflow status --project devflow | .venv/bin/python -c '
import json, sys
value = json.load(sys.stdin)
print(json.dumps({key: value[key] for key in ("ok", "issues", "observed_git_head", "deployment_verified")}, indent=2))
sys.exit(0 if value["ok"] else 1)
'
