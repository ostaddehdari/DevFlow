import copy
import importlib.util
import json
import shutil
import subprocess

import pytest

from conftest import ROOT
from devflow_core.contracts import DevFlowError
from devflow_core.design import validate_design


def design():
    return json.loads((ROOT / ".ai-project/project.design.json").read_text())


def test_canonical_design(contracts):
    value = design()
    validate_design(value, contracts, "devflow")
    assert all(s["status"] == "COMPLETED" for s in value["stages"][:2])
    assert value["stages"][2]["status"] in {"PLANNED", "VERIFYING", "COMPLETED"}
    assert all(s["status"] == "PLANNED" for s in value["stages"][3:])


@pytest.mark.parametrize("case", ["identity", "duplicate", "unknown", "cycle", "incomplete", "blocked", "outside"])
def test_design_rejects_inconsistent_plan(contracts, case):
    value = design()
    if case == "identity":
        value["project"]["id"] = "another"
    elif case == "duplicate":
        value["stages"].append(copy.deepcopy(value["stages"][0]))
    elif case == "unknown":
        value["stages"][2]["works"][0]["dependencies"] = ["WORK-MISSING"]
    elif case == "cycle":
        value["stages"][2]["depends_on"].append("STAGE-03")
    elif case == "incomplete":
        value["stages"][0]["works"][0]["status"] = "PENDING"
    elif case == "blocked":
        value["validation"]["design_status"] = "BLOCKED"
    else:
        value["stages"][2]["works"][0]["dependencies"] = ["WORK-06-04"]
    with pytest.raises(DevFlowError):
        validate_design(value, contracts, "devflow")


@pytest.mark.parametrize("case", ["valid", "malformed", "identity", "symlink"])
def test_project_status_validates_design(registry, settings_path, tmp_path, case):
    registry.register(settings_path)
    path = settings_path.parent / "project.design.json"
    value = design()
    if case == "malformed":
        path.write_text("{}")
    elif case == "identity":
        value["project"]["id"] = "another"
        path.write_text(json.dumps(value))
    elif case == "symlink":
        outside = tmp_path / "outside-design.json"
        outside.write_text(json.dumps(value))
        path.symlink_to(outside)
    else:
        path.write_text(json.dumps(value))
        result = registry.project_status()
        assert "Project design is not present" not in result["issues"]
        assert result["documents"]["project.design.json"] == value
        return
    before = registry.path.read_bytes()
    with pytest.raises(DevFlowError):
        registry.project_status()
    assert registry.path.read_bytes() == before


def test_verified_record_updates_checkpoint_and_is_idempotent(settings_path):
    root = settings_path.parent.parent
    shutil.copytree(ROOT / "contracts", root / "contracts")
    shutil.copy(ROOT / ".ai-project/project.design.json", settings_path.parent)
    state_path = settings_path.parent / "state.json"
    state = json.loads(state_path.read_text())
    state["current"].update(stage_id="STAGE-01", stage_status="COMPLETED", work_status="COMPLETED")
    state_path.write_text(json.dumps(state))
    resume_path = settings_path.parent / "resume.bundle.json"
    resume = json.loads(resume_path.read_text())
    resume["current"].update(stage_id="STAGE-01", status="COMPLETED")
    resume_path.write_text(json.dumps(resume))
    subprocess.run(["git", "init", "-b", "stage/01-servermaster-core", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=DevFlow Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    spec = importlib.util.spec_from_file_location("record_stage1", ROOT / "scripts/record_stage1_verification.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.record(root)["changed"] is True
    before = {p: p.read_bytes() for p in settings_path.parent.rglob("*.json")}
    assert module.record(root)["changed"] is False
    assert all(p.read_bytes() == data for p, data in before.items())
    updated = json.loads(state_path.read_text())
    assert updated["latest_checkpoint_id"] == "CP-STAGE01-VERIFIED"
    assert updated["current"]["work_id"] == "WORK-01-01"
    assert updated["servers"] == state["servers"]
