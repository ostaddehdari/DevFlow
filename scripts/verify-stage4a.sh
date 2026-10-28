#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
cmp contracts/operation.plan.schema.json plugin/assets/contracts/operation.plan.schema.json
bash scripts/devflow validate
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from devflow_core.contracts import Contracts
from devflow_core.operation_plans import PlanPolicy
root = Path.cwd()
policy = PlanPolicy(root, Contracts(root / 'contracts'))
assert policy.project_id == 'devflow'
print('PASS: Stage 4A source verified; no operation was executed')
PY
