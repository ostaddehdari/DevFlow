#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
echo 'PASS: isolated Git push execution tests; production push not run'
