#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
if test -s control/registry.json; then
  bash scripts/devflow validate
fi
cmp contracts/deployment.plan.schema.json plugin/assets/contracts/deployment.plan.schema.json
echo 'PASS: release archive, approval gate and rollback tests; no production service changed'
