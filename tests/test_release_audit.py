"""Live migration proof is bound to the running release and source projection."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from devflow_core.contracts import Contracts, DevFlowError
from devflow_core.release_audit import (
    MIGRATION_OPERATION, MIGRATION_PLAN_SHA256, MIGRATION_SHA, audit)
from scripts import record_stage4d_migration as recorder


class Row(dict):
    pass


class Database:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_):
        return self

    def fetchone(self):
        return Row(
            intent=json.dumps({"kind": "service.deploy",
                               "plan_sha256": MIGRATION_PLAN_SHA256}),
            status="SUCCEEDED",
            evidence="log://.devflow/evidence/E-" + "a" * 32 + ".json")


class Ledger:
    def __init__(self, *_):
        pass

    def status(self):
        return {
            "initialized": True, "clock_rollback": False, "lease": None,
            "operations": [{"operation_key": MIGRATION_OPERATION,
                            "status": "SUCCEEDED"}]}

    def _connection(self):
        return Database()


class Response:
    def __init__(self, code):
        self.status_code = code
        self.headers = {"www-authenticate": "Bearer"}

    def json(self):
        return {"resource": "https://srun.ir/mcpdevflow"}


class Client:
    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, *_):
        return Response(200)

    def post(self, *_, **__):
        return Response(401)


@pytest.fixture
def live(tmp_path, monkeypatch):
    root = tmp_path / "devflow"
    root.mkdir()
    releases = tmp_path / "devflow-releases"
    target = releases / MIGRATION_SHA
    target.mkdir(parents=True)
    (target / ".devflow-release").write_text(MIGRATION_SHA + "\n")
    (releases / "current").symlink_to(MIGRATION_SHA)
    evidence = root / ".devflow/evidence" / ("E-" + "a" * 32 + ".json")
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({
        "operation_id": MIGRATION_OPERATION, "kind": "service.deploy",
        "plan_sha256": MIGRATION_PLAN_SHA256, "candidate_sha": MIGRATION_SHA,
        "outcome": "SUCCEEDED",
        "details": {"deployed_sha": MIGRATION_SHA, "authenticated_tools": 8,
                    "public_status": [200, 401]}}))
    evidence.chmod(0o600)
    monkeypatch.setattr("devflow_core.release_audit.Continuity", Ledger)
    monkeypatch.setattr("devflow_core.release_audit.httpx.Client", Client)
    def command(*args):
        if args[0] == "systemctl":
            return ("MainPID=123\nActiveState=active\n"
                    "DropInPaths=/etc/systemd/system/devflow-mcp.service.d/"
                    "10-devflow-release.conf")
        return "LISTEN 0 2048 127.0.0.1:8765 0.0.0.0:*"
    monkeypatch.setattr("devflow_core.release_audit._command", command)
    original = Path.read_bytes
    def read_bytes(path):
        if str(path) == "/proc/123/environ":
            return ("PYTHONPATH=" + str(releases / "current/src")).encode() + b"\0"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return root, evidence


def test_audit_binds_success_evidence_and_running_process(live):
    root, _ = live
    result = audit(root, None)
    assert result["deployed_sha"] == MIGRATION_SHA
    assert result["authenticated_tools"] == 8


def test_audit_rejects_incomplete_authenticated_proof(live):
    root, evidence = live
    value = json.loads(evidence.read_text())
    value["details"]["authenticated_tools"] = 0
    evidence.write_text(json.dumps(value))
    with pytest.raises(DevFlowError, match="incomplete"):
        audit(root, None)


def test_record_keeps_stage_four_in_progress(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "devflow"
    root.mkdir()
    shutil.copytree(source / "contracts", root / "contracts")
    shutil.copytree(source / ".ai-project", root / ".ai-project")
    # The recorder fixture is always the source state before this milestone.
    for name in ("state.json", "resume.bundle.json", "project.design.json"):
        original = subprocess.check_output(
            ["git", "-C", str(source), "show",
             MIGRATION_SHA + ":.ai-project/" + name])
        (root / ".ai-project" / name).write_bytes(original)
    monkeypatch.setattr(recorder, "_git",
                        lambda root, *args: {
                            "branch": "stage/04d-release-audit",
                            "rev-parse": "c" * 40,
                            "status": "",
                        }[args[0]])
    monkeypatch.setattr(recorder, "Continuity", Ledger)
    monkeypatch.setattr(recorder, "audit", lambda *_: {
        "ok": True, "deployed_sha": MIGRATION_SHA,
        "operation_id": MIGRATION_OPERATION, "authenticated_tools": 8,
        "evidence_sha256": "f" * 64})
    result = recorder.record(root)
    assert result["stage_completed"] is False
    assert recorder.record(root)["changed"] is False
    state = json.loads((root / ".ai-project/state.json").read_text())
    design = json.loads((root / ".ai-project/project.design.json").read_text())
    assert state["servers"]["control"]["deployed_sha"] == MIGRATION_SHA
    assert state["current"]["work_status"] == "VERIFYING"
    assert [s for s in design["stages"] if s["id"] == "STAGE-04"][0]["works"][-1]["status"] == "VERIFYING"
    Contracts(root / "contracts").validate(
        json.loads((root / ".ai-project/checkpoints/CP-STAGE04D-MIGRATED.json").read_text()),
        "checkpoint.schema.json")
