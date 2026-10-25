import json
import shutil
from pathlib import Path

import pytest

from devflow_core.contracts import Contracts
from devflow_core.registry import ControlRegistry


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts():
    return Contracts(ROOT / "contracts")


@pytest.fixture
def settings_path(tmp_path):
    target = tmp_path / "devflow" / ".ai-project"
    target.mkdir(parents=True)
    for name in ("project.settings.json", "state.json", "resume.bundle.json"):
        shutil.copyfile(ROOT / ".ai-project" / name, target / name)
    return target / "project.settings.json"


@pytest.fixture
def registry(tmp_path, contracts):
    return ControlRegistry(tmp_path / "runtime" / "registry.json", contracts)


def write_settings(path, **changes):
    value = json.loads(path.read_text())
    for key, item in changes.items():
        value[key] = item
    path.write_text(json.dumps(value))
    return value
