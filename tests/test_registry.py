import copy
import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from devflow_core.contracts import Contracts, DevFlowError
from devflow_core.registry import ControlRegistry
from conftest import ROOT


def test_bootstrap_and_idempotent_registration(registry, settings_path):
    first = registry.register(settings_path)
    content = registry.path.read_bytes()
    second = registry.register(settings_path)
    assert first["changed"] is True and second["changed"] is False
    assert registry.path.read_bytes() == content
    assert registry.read()["active_project_id"] == "devflow"
    assert registry.path.stat().st_mode & 0o777 == 0o600
    assert (settings_path.parent.parent / ".venv").exists() is False


def test_uninitialized_fails(registry):
    with pytest.raises(DevFlowError, match="not initialized"):
        registry.read()


@pytest.mark.parametrize("case", ["port", "credential", "extra", "date"])
def test_invalid_data_preserves_original(registry, settings_path, case):
    registry.register(settings_path)
    original = registry.path.read_bytes()
    value = json.loads(settings_path.read_text())
    if case == "port":
        value["servers"]["control"]["connection"]["port"] = 70000
    elif case == "credential":
        value["servers"]["control"]["connection"]["credential_ref"] = "RAW_SECRET_VALUE"
    elif case == "extra":
        value["servers"]["control"]["connection"]["password"] = "RAW_SECRET_VALUE"
    else:
        data = registry.read()
        data["projects"][0]["last_activity_at"] = "yesterday"
        with pytest.raises(DevFlowError, match="format"):
            registry.validate(data)
        return
    settings_path.write_text(json.dumps(value))
    with pytest.raises(DevFlowError) as error:
        registry.register(settings_path)
    assert "RAW_SECRET_VALUE" not in str(error.value)
    assert registry.path.read_bytes() == original


def test_duplicate_and_unknown_active_rejected(registry, settings_path):
    registry.register(settings_path)
    data = registry.read()
    for collection in ("projects", "servers"):
        bad = copy.deepcopy(data)
        bad[collection].append(bad[collection][0])
        with pytest.raises(DevFlowError, match="Duplicate"):
            registry.validate(bad)
    data["active_project_id"] = "missing"
    with pytest.raises(DevFlowError, match="does not exist"):
        registry.validate(data)


def test_conflicting_server_and_project_binding(registry, settings_path, tmp_path):
    registry.register(settings_path)
    original = registry.path.read_bytes()
    value = json.loads(settings_path.read_text())
    value["servers"]["control"]["address"] = "192.0.2.2"
    settings_path.write_text(json.dumps(value))
    with pytest.raises(DevFlowError, match="Server ID conflict"):
        registry.register(settings_path)
    another = tmp_path / "other" / ".ai-project" / "project.settings.json"
    another.parent.mkdir(parents=True)
    another.write_text(json.dumps(value))
    with pytest.raises(DevFlowError, match="already bound"):
        registry.register(another)
    assert registry.path.read_bytes() == original


def test_activation_unknown_is_non_destructive(registry, settings_path):
    registry.register(settings_path)
    original = registry.path.read_bytes()
    with pytest.raises(DevFlowError, match="Unknown"):
        registry.activate("missing")
    assert registry.path.read_bytes() == original


def test_atomic_write_failure_preserves_file(registry, settings_path):
    registry.register(settings_path)
    original = registry.path.read_bytes()
    with patch("devflow_core.registry.os.replace", side_effect=OSError("simulated")):
        with pytest.raises(OSError):
            with registry._locked():
                registry._write(registry.read())
    assert registry.path.read_bytes() == original
    assert list(registry.path.parent.glob(".registry-*")) == []


def test_corrupt_existing_registry_is_never_reset(registry, settings_path):
    registry.path.parent.mkdir()
    registry.path.write_text("{broken")
    with pytest.raises(DevFlowError, match="Cannot read JSON"):
        registry.register(settings_path)
    assert registry.path.read_text() == "{broken"


def test_rejects_inline_metadata_credentials(registry, settings_path):
    value = json.loads(settings_path.read_text())
    value["servers"]["control"]["metadata"] = {"api_key": "RAW_SECRET_VALUE"}
    settings_path.write_text(json.dumps(value))
    with pytest.raises(DevFlowError, match="Inline credential"):
        registry.register(settings_path)
    assert not registry.path.exists()


def test_duplicate_servers_in_settings(registry, settings_path):
    value = json.loads(settings_path.read_text())
    value["servers"]["copy"] = value["servers"]["control"]
    settings_path.write_text(json.dumps(value))
    with pytest.raises(DevFlowError, match="Duplicate"):
        registry.register(settings_path)


def test_status_reports_missing_prerequisites_without_mutation(registry, settings_path):
    registry.register(settings_path)
    before = {p: p.read_bytes() for p in settings_path.parent.glob("*.json")}
    status = registry.project_status()
    assert status["deployment_verified"] is False
    assert "Project design is not present" in status["issues"]
    assert "Referenced checkpoint is not present" in status["issues"]
    assert {p: p.read_bytes() for p in before} == before


def test_status_blocks_symlink_escape(registry, settings_path, tmp_path):
    registry.register(settings_path)
    external = tmp_path / "outside.json"
    external.write_text('{"secret": "hidden"}')
    state = settings_path.parent / "state.json"
    state.unlink()
    state.symlink_to(external)
    with pytest.raises(DevFlowError, match="escapes"):
        registry.project_status()


def test_status_blocks_wrong_identity(registry, settings_path):
    registry.register(settings_path)
    path = settings_path.parent / "state.json"
    data = json.loads(path.read_text())
    data["project_id"] = "another"
    path.write_text(json.dumps(data))
    with pytest.raises(DevFlowError, match="identity mismatch"):
        registry.project_status()


def _register_process(registry_path, contract_path, settings):
    ControlRegistry(Path(registry_path), Contracts(Path(contract_path))).register(Path(settings))


def test_concurrent_registrations_do_not_lose_projects(registry, settings_path, tmp_path):
    original = json.loads(settings_path.read_text())
    paths = []
    for index in range(6):
        data = copy.deepcopy(original)
        data["project"]["id"] = f"project-{index}"
        data["repository"]["name"] = f"project-{index}"
        path = tmp_path / f"p{index}" / ".ai-project" / "project.settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data))
        paths.append(path)
    context = multiprocessing.get_context("spawn")
    jobs = [context.Process(target=_register_process,
                            args=(str(registry.path), str(ROOT / "contracts"), str(p))) for p in paths]
    for job in jobs:
        job.start()
    for job in jobs:
        job.join(timeout=20)
        if job.is_alive():
            job.kill()
            job.join()
        assert job.exitcode == 0
    data = registry.read()
    assert len(data["projects"]) == 6 and len(data["servers"]) == 1
    assert registry.activate("project-3")["active_project_id"] == "project-3"


def test_all_contracts_and_plugin_mirrors_match(contracts):
    for path in (ROOT / "contracts").glob("*.schema.json"):
        assert (ROOT / "plugin/assets/contracts" / path.name).read_bytes() == path.read_bytes()
    for name, schema in (("project.settings.json", "project.settings.schema.json"),
                         ("state.json", "state.schema.json"),
                         ("resume.bundle.json", "resume.bundle.schema.json")):
        contracts.validate(json.loads((ROOT / ".ai-project" / name).read_text()), schema)


def test_existing_relative_registry_binding_is_adopted(registry, settings_path):
    registry = ControlRegistry(settings_path.parent.parent / "control" / "registry.json", registry.contracts)
    settings = json.loads(settings_path.read_text())
    data = {
        "schema_version": "1.0", "active_project_id": "devflow",
        "projects": [{"project_id": "devflow", "repository": "ostaddehdari/DevFlow",
                      "status": "ACTIVE", "settings_path": ".ai-project/project.settings.json"}],
        "servers": [settings["servers"]["control"]],
    }
    registry.path.parent.mkdir()
    registry.path.write_text(json.dumps(data))
    assert registry.register(settings_path)["changed"]
    assert registry.read()["projects"][0]["settings_path"] == str(settings_path.resolve())
    assert registry.read()["servers"] == data["servers"]
    assert registry.register(settings_path)["changed"] is False
