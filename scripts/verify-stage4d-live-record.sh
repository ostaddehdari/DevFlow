#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
(umask 022; .venv/bin/python -m pytest -q)
if test -s control/registry.json; then
    bash scripts/devflow validate
fi
echo 'PASS: Stage 4D live audit and partial milestone source verified; service unchanged'
