"""Create a consistent runtime checkpoint after a clean source commit."""

import json
import uuid
from pathlib import Path

from devflow_core.contracts import Contracts
from devflow_core.continuity import from_registry
from devflow_core.registry import ControlRegistry


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    registry = ControlRegistry(root / 'control/registry.json', Contracts(root / 'contracts'))
    store = from_registry(registry, 'devflow')
    owner = 'operator-' + uuid.uuid4().hex[:12]
    acquired = store.acquire(owner)
    identity = (owner, acquired['execution']['execution_id'], acquired['fence'])
    try:
        checkpoint = store.checkpoint(*identity)
    finally:
        store.release(*identity)
    print(json.dumps({'ok': True, 'checkpoint_id': checkpoint['checkpoint_id'],
                      'resume': store.resume_plan()}, indent=2))
