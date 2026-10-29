#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
bash scripts/devflow validate
echo 'PASS: Stage 4D release preflight source; no service or release changed'
