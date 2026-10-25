"""Record successful Stage 1 bootstrap after tests; never infer live server health."""

import copy
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from devflow_core.contracts import Contracts, DevFlowError, read_json
from devflow_core.design import validate_design


def record(root: Path) -> dict:
    folder = root / ".ai-project"
    contracts = Contracts(root / "contracts")
    state = read_json(folder / "state.json")
    resume = read_json(folder / "resume.bundle.json")
    for value, schema in ((state, "state.schema.json"), (resume, "resume.bundle.schema.json")):
        contracts.validate(value, schema)
        if value["project_id"] != "devflow":
            raise DevFlowError("Expected DevFlow project")
    if (state["current"]["stage_id"] != "STAGE-01" or
        state["current"]["stage_status"] != "COMPLETED" or
        state["current"]["work_status"] != "COMPLETED" or
        resume["current"]["stage_id"] != "STAGE-01" or
        resume["current"]["status"] != "COMPLETED"):
        raise DevFlowError("Stage 1 bootstrap completion must be recorded first")
    if state.get("execution") is not None:
        raise DevFlowError("Cannot reconcile while an execution is recorded")
    validate_design(read_json(folder / "project.design.json"), contracts, "devflow")
    checkpoint_id = "CP-STAGE01-VERIFIED"
    target = folder / "checkpoints" / (checkpoint_id + ".json")
    if state.get("latest_checkpoint_id") == checkpoint_id and target.exists():
        contracts.validate(read_json(target), "checkpoint.schema.json")
        return {"ok": True, "changed": False, "checkpoint_id": checkpoint_id}
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], text=True).strip()
    branch = subprocess.check_output(["git", "-C", str(root), "branch", "--show-current"], text=True).strip()
    if not branch:
        raise DevFlowError("A named branch is required")
    now = datetime.now(timezone.utc).isoformat()
    state, resume = copy.deepcopy(state), copy.deepcopy(resume)
    checkpoint = {
        "schema_version": "1.0", "checkpoint_id": checkpoint_id, "project_id": "devflow",
        "created_at": now, "reason": "stage_completed", "safe_to_resume": True,
        "snapshot": {"stage_id": "STAGE-01", "work_id": "WORK-01-01", "git_head_sha": head,
            "server_deployments": {}, "last_action": "stage1_bootstrap_and_design_verified",
            "next_action": "implement_stage2_execution_leases",
            "note": "Stage 1 bootstrap confirmed by operator output; this host validates the design and installed source. HEAD is observed before this record commit; no live-health, remote endpoint or deployed SHA is inferred."},
        "do_not_repeat": list(dict.fromkeys(resume["do_not_repeat"] + ["run_stage1_bootstrap_on_control_server"]))}
    state["current"]["work_id"] = "WORK-01-01"
    state["current"]["step"] = "bootstrap_and_design_verified"
    state["git"] = {"branch": branch, "head_sha": head}
    state["latest_checkpoint_id"] = checkpoint_id
    state["next_action"] = "implement_stage2_execution_leases"
    state["updated_at"] = now
    resume["current"]["work_id"] = "WORK-01-01"
    resume["git"] = dict(state["git"])
    resume["latest_checkpoint_id"] = checkpoint_id
    resume["generated_at"] = now
    resume["last_action"] = "stage1_bootstrap_and_design_verified"
    resume["next_action"] = state["next_action"]
    resume["do_not_repeat"] = checkpoint["do_not_repeat"]
    changes = [(target, checkpoint, "checkpoint.schema.json"),
               (folder / "resume.bundle.json", resume, "resume.bundle.schema.json"),
               (folder / "state.json", state, "state.schema.json")]
    for _, value, schema in changes:
        contracts.validate(value, schema)
    # Each file is atomic; a multi-document transactional lifecycle is Stage 2 work.
    # Write snapshot first, resume next, authoritative state last. A retry is safe.
    for path, value, _ in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".verify-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"ok": True, "changed": True, "checkpoint_id": checkpoint_id,
            "observed_git_head": head, "next_action": state["next_action"]}


if __name__ == "__main__":
    print(json.dumps(record(Path(__file__).resolve().parents[1]), indent=2))
