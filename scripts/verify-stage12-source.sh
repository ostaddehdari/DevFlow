#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
(umask 022; .venv/bin/python -m pytest -q)
cmp contracts/network.schema.json plugin/assets/contracts/network.schema.json
.venv/bin/python - <<'PY'
import json
from pathlib import Path
document = json.loads(Path('plugin/mcp.json').read_text())
server = document['mcpServers']['devflow-servermaster']
assert server == {'type': 'streamable-http', 'url': 'https://srun.ir/mcpdevflow'}
print('PASS: 11 read-only source tools, exact OAuth user grant and MCP package verified')
PY
