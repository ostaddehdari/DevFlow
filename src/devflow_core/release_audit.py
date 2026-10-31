"""Observe the successful first release migration without changing the service."""

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import httpx

from .contracts import DevFlowError
from .continuity import Continuity
from .deployment import DISCOVERY, RESOURCE, SERVICE


MIGRATION_SHA = "42045b9d87550852a591b54982ff84a520516a6d"
MIGRATION_OPERATION = "service-deploy-ec861ec78e3a4159abb6e2479fa10009"
MIGRATION_PLAN_SHA256 = "2148512afbfd8cdc1e79b2fc2f4d522c1ff34f2b917ad1b84212cd0edbc10f4d"


def _command(*argv):
    try:
        return subprocess.check_output(argv, text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DevFlowError("Cannot inspect live release") from exc


def audit(root: Path, contracts) -> dict:
    """Bind the deployed SHA to process, public health and fenced success evidence."""
    root = root.resolve()
    if os.geteuid() != 0:
        raise DevFlowError("Live migration audit requires the local root operator")
    pointer = root.parent / (root.name + "-releases") / "current"
    if not pointer.is_symlink() or os.readlink(pointer) != MIGRATION_SHA:
        raise DevFlowError("The pinned release pointer differs from the verified migration")
    target = pointer.parent / MIGRATION_SHA
    if target.is_symlink() or (target / ".devflow-release").read_text() != MIGRATION_SHA + "\n":
        raise DevFlowError("The pinned release marker is invalid")
    fields = dict(line.split("=", 1) for line in _command(
        "systemctl", "show", SERVICE, "--property=MainPID",
        "--property=DropInPaths", "--property=ActiveState").splitlines() if "=" in line)
    expected_dropin = "/etc/systemd/system/devflow-mcp.service.d/10-devflow-release.conf"
    if fields.get("ActiveState") != "active" or fields.get("DropInPaths") != expected_dropin:
        raise DevFlowError("The active service is not the pinned release")
    try:
        pid = int(fields["MainPID"])
        if pid <= 1:
            raise ValueError("Invalid service PID")
        environment = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except (KeyError, ValueError, OSError) as exc:
        raise DevFlowError("Cannot observe the running release process") from exc
    if ("PYTHONPATH=" + str(pointer / "src")).encode() not in environment:
        raise DevFlowError("The service process has another source path")
    listeners = _command("ss", "-lntH", "( sport = :8765 )").splitlines()
    if [line.split()[3] for line in listeners] != ["127.0.0.1:8765"]:
        raise DevFlowError("The release listener is not loopback-only")
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            discovery = client.get(DISCOVERY)
            denied = client.post(RESOURCE, json={})
        if (discovery.status_code != 200 or discovery.json().get("resource") != RESOURCE or
            denied.status_code != 401 or
            "bearer" not in denied.headers.get("www-authenticate", "").lower()):
            raise DevFlowError("Public OAuth boundary changed")
    except (httpx.HTTPError, ValueError) as exc:
        raise DevFlowError("Public OAuth boundary unavailable") from exc
    continuity = Continuity(root, contracts)
    runtime = continuity.status()
    if (not runtime.get("initialized") or runtime["clock_rollback"] or
        any(item["status"] in {"STARTED", "UNKNOWN", "FAILED"} for item in runtime["operations"]) or
        not any(item["operation_key"] == MIGRATION_OPERATION and item["status"] == "SUCCEEDED"
                for item in runtime["operations"])):
        raise DevFlowError("Successful fenced migration evidence is not available")
    with continuity._connection() as connection:
        row = connection.execute(
            "SELECT intent,evidence,status FROM operations WHERE operation_key=?",
            (MIGRATION_OPERATION,)).fetchone()
    if not row or row["status"] != "SUCCEEDED":
        raise DevFlowError("Migration operation changed during audit")
    try:
        intent = json.loads(row["intent"])
    except (ValueError, TypeError) as exc:
        raise DevFlowError("Invalid migration intent") from exc
    if intent != {"kind": "service.deploy", "plan_sha256": MIGRATION_PLAN_SHA256}:
        raise DevFlowError("Migration intent differs from the approved plan")
    reference = row["evidence"]
    if not isinstance(reference, str) or not re.fullmatch(
        r"log://\.devflow/evidence/E-[0-9a-f]{32}\.json", reference
    ):
        raise DevFlowError("Migration evidence reference is invalid")
    evidence_path = root / reference.removeprefix("log://")
    directory = evidence_path.parent
    if directory.is_symlink() or evidence_path.is_symlink():
        raise DevFlowError("Migration evidence path cannot be linked")
    info = evidence_path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or
        info.st_size > 65536):
        raise DevFlowError("Migration evidence must be a bounded private file")
    try:
        evidence = json.loads(evidence_path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise DevFlowError("Migration evidence is invalid") from exc
    details = evidence.get("details", {})
    if (evidence.get("operation_id") != MIGRATION_OPERATION or
        evidence.get("kind") != "service.deploy" or
        evidence.get("plan_sha256") != MIGRATION_PLAN_SHA256 or
        evidence.get("candidate_sha") != MIGRATION_SHA or
        evidence.get("outcome") != "SUCCEEDED" or
        not isinstance(details, dict) or details.get("deployed_sha") != MIGRATION_SHA or
        type(details.get("authenticated_tools")) is not int or
        details["authenticated_tools"] < 8 or details.get("public_status") != [200, 401]):
        raise DevFlowError("The recorded release proof is incomplete")
    return {"ok": True, "deployed_sha": MIGRATION_SHA, "operation_id": MIGRATION_OPERATION,
            "authenticated_tools": details["authenticated_tools"],
            "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "public_status": [200, 401]}
