#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
echo 'PASS: remote observation and exact Git push plan creation; no push executed'
