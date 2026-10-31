#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
(umask 022; .venv/bin/python -m pytest -q)
cmp contracts/release.transition.plan.schema.json \
  plugin/assets/contracts/release.transition.plan.schema.json
if test -s control/registry.json; then
  bash scripts/devflow validate
fi
echo 'PASS: approved upgrade and targeted rollback source verified; no production service changed'
