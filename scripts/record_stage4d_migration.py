"""Record the observed first release migration as a partial Stage 4 milestone."""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.continuity import Continuity
from devflow_core.design import validate_design
from devflow_core.release_audit import MIGRATION_OPERATION, audit


CHECKPOINT = "CP-STAGE04D-MIGRATED"


def _git(root, *arguments):
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments], text=True,
            stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DevFlowError("Cannot inspect the source commit") from exc


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".stage4d-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record(root):
    root = Path(root).resolve()
    contracts = Contracts(root / "contracts")
    folder = root / ".ai-project"
    documents = {
        name: read_json(folder / name)
        for name in ("state.json", "resume.bundle.json", "project.design.json")
    }
    state, resume, design = (
        documents["state.json"], documents["resume.bundle.json"],
        documents["project.design.json"])
    for name, schema in (("state.json", "state.schema.json"),
                         ("resume.bundle.json", "resume.bundle.schema.json")):
        contracts.validate(documents[name], schema)
        if documents[name]["project_id"] != "devflow":
            raise DevFlowError("Unexpected project identity")
    validate_design(design, contracts, "devflow")
    if state.get("latest_checkpoint_id") == CHECKPOINT:
        checkpoint = read_json(folder / "checkpoints" / (CHECKPOINT + ".json"))
        contracts.validate(checkpoint, "checkpoint.schema.json")
        return {"ok": True, "changed": False, "checkpoint_id": CHECKPOINT}
    if (state["current"]["stage_id"] != "STAGE-03" or
        state["current"]["stage_status"] != "COMPLETED" or
        state["latest_checkpoint_id"] != "CP-STAGE03C-VERIFIED" or
        state["execution"] is not None or
        resume["current"]["stage_id"] != "STAGE-03"):
        raise DevFlowError("A completed Stage 3 projection is required")
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if (not branch.startswith("stage/04d-") or
        _git(root, "status", "--porcelain", "--untracked-files=all")):
        raise DevFlowError("A clean Stage 4D source commit is required")
    runtime = Continuity(root, contracts).status()
    if (not runtime["initialized"] or runtime["clock_rollback"] or
        runtime["lease"] and runtime["lease"]["execution"]["status"] == "ACTIVE" or
        any(item["status"] in {"STARTED", "UNKNOWN", "FAILED"}
            for item in runtime["operations"])):
        raise DevFlowError("Runtime continuity must have no unresolved execution")
    proof = audit(root, contracts)
    if proof["operation_id"] != MIGRATION_OPERATION:
        raise DevFlowError("Migration proof identity mismatch")
    stage = next(item for item in design["stages"] if item["id"] == "STAGE-04")
    if (stage["status"] != "PLANNED" or
        [work["status"] for work in stage["works"]] != ["PENDING"] * 4):
        raise DevFlowError("Stage 4 projection has changed; inspect before recording")
    now = datetime.now(timezone.utc).isoformat()
    next_action = "implement_approved_upgrade_and_targeted_rollback"
    stage["status"] = "ACTIVE"
    for work in stage["works"][:3]:
        work["status"] = "COMPLETED"
    stage["works"][3]["status"] = "VERIFYING"
    design["architecture"]["execution_readiness_scope"] = (
        "The first pinned MCP release migration was observed with authenticated tools. "
        "Approved upgrades and a tested targeted rollback remain pending.")
    state["current"].update(
        stage_id="STAGE-04", stage_status="ACTIVE", work_id="WORK-04-04",
        work_status="VERIFYING", operation="service.deploy",
        step="first_pinned_release_migrated", attempt=1)
    state.update(git={"branch": branch, "head_sha": head},
                 latest_checkpoint_id=CHECKPOINT, next_action=next_action,
                 updated_at=now)
    state["servers"]["control"].update(
        deployed_sha=proof["deployed_sha"], health="HEALTHY", last_verified_at=now)
    resume["current"].update(stage_id="STAGE-04", work_id="WORK-04-04",
                             status="VERIFYING")
    resume.update(git=dict(state["git"]), generated_at=now,
                  latest_checkpoint_id=CHECKPOINT,
                  last_action="first_pinned_release_migration_verified",
                  next_action=next_action)
    resume["servers"]["control"].update(
        deployed_sha=proof["deployed_sha"], health="HEALTHY")
    resume["do_not_repeat"] = list(dict.fromkeys(
        resume["do_not_repeat"] + [MIGRATION_OPERATION]))
    checkpoint = {
        "schema_version": "1.0", "checkpoint_id": CHECKPOINT,
        "project_id": "devflow", "created_at": now,
        "reason": "deployment", "safe_to_resume": True,
        "snapshot": {
            "stage_id": "STAGE-04", "work_id": "WORK-04-04",
            "git_head_sha": head,
            "server_deployments": {
                "control": {"deployed_sha": proof["deployed_sha"],
                            "health": "HEALTHY",
                            "evidence_sha256": proof["evidence_sha256"]}},
            "last_action": resume["last_action"], "next_action": next_action,
            "operation_id": MIGRATION_OPERATION,
            "authenticated_tools": proof["authenticated_tools"],
            "note": "First migration passed; approved upgrade and live rollback remain pending.",
        },
        "do_not_repeat": resume["do_not_repeat"],
    }
    validate_design(design, contracts, "devflow")
    changes = [
        (folder / "project.design.json", design, "project.design.schema.json"),
        (folder / "state.json", state, "state.schema.json"),
        (folder / "resume.bundle.json", resume, "resume.bundle.schema.json"),
        (folder / "checkpoints" / (CHECKPOINT + ".json"),
         checkpoint, "checkpoint.schema.json"),
    ]
    for _, value, schema in changes:
        contracts.validate(value, schema)
    for path, value, _ in changes:
        _write(path, value)
    return {"ok": True, "changed": True, "checkpoint_id": CHECKPOINT,
            "deployed_sha": proof["deployed_sha"], "next_action": next_action,
            "stage_completed": False}


if __name__ == "__main__":
    print(json.dumps(record(Path(__file__).resolve().parents[1]), indent=2))
