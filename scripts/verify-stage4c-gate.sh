#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
echo 'PASS: Git push plans and exact operator approvals verified; no push executed'
