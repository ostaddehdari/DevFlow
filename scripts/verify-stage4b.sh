#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from devflow_core.contracts import Contracts
from devflow_core.continuity import Continuity
from devflow_core.controlled_execution import ReadExecutor
from devflow_core.operation_plans import PlanPolicy

root = Path.cwd()
contracts = Contracts(root / 'contracts')
policy = PlanPolicy(root, contracts)
store = Continuity(root, contracts)
assert ReadExecutor(policy, store).runner.timeout < 60
print('PASS: Stage 4B isolated execution tests; production actions not run')
PY
