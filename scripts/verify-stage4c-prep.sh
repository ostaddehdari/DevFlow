#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
echo 'PASS: Stage 4C isolated Git guard tests; inspect the clean branch after commit'
