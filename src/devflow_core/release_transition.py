"""Approved, fenced transitions between immutable MCP releases."""

import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from .contracts import DevFlowError
from .controlled_execution import ReadExecutor
from .deployment import DISCOVERY, RESOURCE, SERVICE
from .deployment_execution import ReleaseExecutor, _command
from .git_workflow import GitWorkflow
from .network import JWTVerifier, NetworkPolicy
from .operation_plans import canonical
from .security import read_private_file


SHA = re.compile(r"[0-9a-f]{40}\Z")
OVERRIDE = "10-devflow-release.conf"


def _digest(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
        raise DevFlowError("Service unit or override is missing, linked, or oversized")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LiveReleaseProbe:
    """Observe process, network boundary and the exact running release."""

    def inspect(self, workflow, sha):
        try:
            result = subprocess.run(
                ["systemctl", "show", SERVICE, "--property=MainPID",
                 "--property=DropInPaths", "--property=ActiveState"],
                capture_output=True, text=True, check=True, timeout=5)
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            pid = int(fields["MainPID"])
            if (pid <= 1 or fields.get("ActiveState") != "active" or
                fields.get("DropInPaths") != str(workflow.override)):
                raise DevFlowError("Active service differs from the pinned release")
            env = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
            if ("PYTHONPATH=" + str(workflow.pointer / "src")).encode() not in env:
                raise DevFlowError("Service process has another source path")
            listeners = subprocess.run(
                ["ss", "-lntH", "( sport = :8765 )"],
                capture_output=True, text=True, check=True, timeout=5)
            if [line.split()[3] for line in listeners.stdout.splitlines()] != ["127.0.0.1:8765"]:
                raise DevFlowError("MCP listener differs from the pinned release")
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                metadata = client.get(DISCOVERY)
                denied = client.post(RESOURCE, json={})
            if (metadata.status_code != 200 or metadata.json().get("resource") != RESOURCE or
                denied.status_code != 401 or
                "bearer" not in denied.headers.get("www-authenticate", "").lower()):
                raise DevFlowError("Public OAuth boundary changed")
        except DevFlowError:
            raise
        except (OSError, subprocess.SubprocessError, httpx.HTTPError, ValueError,
                KeyError, IndexError) as exc:
            raise DevFlowError("Live release preflight failed") from exc
        if not workflow.pointer.is_symlink() or os.readlink(workflow.pointer) != sha:
            raise DevFlowError("Release pointer changed during live preflight")
        return {"active": True, "discovery_status": 200,
                "unauthenticated_status": 401, "listener": "127.0.0.1:8765"}


class TransitionWorkflow:
    def __init__(self, root, contracts, unit_path=None, git=None, probe=None):
        self.root = Path(root).resolve()
        self.contracts = contracts
        self.git = git or GitWorkflow(self.root, contracts)
        self.unit_path = Path(unit_path or "/etc/systemd/system/devflow-mcp.service")
        self.release_root = self.root.parent / (self.root.name + "-releases")
        self.pointer = self.release_root / "current"
        self.override = self.unit_path.parent / (SERVICE + ".d") / OVERRIDE
        self.probe = probe or LiveReleaseProbe()

    def _release(self, sha):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise DevFlowError("Invalid release SHA")
        target = self.release_root / sha
        marker = target / ".devflow-release"
        if (target.is_symlink() or not target.is_dir() or marker.is_symlink() or
            not marker.is_file() or marker.read_text() != sha + "\n" or
            not (target / "src/devflow_core/cli.py").is_file()):
            raise DevFlowError("Pinned release target is incomplete")
        return target

    def _current(self):
        if (self.release_root.is_symlink() or not self.release_root.is_dir() or
            self.release_root.stat().st_uid != os.geteuid() or
            self.release_root.stat().st_mode & 0o077 or
            not self.pointer.is_symlink() or
            self.override.parent.is_symlink()):
            raise DevFlowError("Pinned release layout changed")
        sha = os.readlink(self.pointer)
        self._release(sha)
        return sha

    def _ancestor(self, older, newer):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "merge-base", "--is-ancestor", older, newer],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DevFlowError("Cannot verify release ancestry") from exc
        if result.returncode != 0:
            raise DevFlowError("Release transition is not on the pinned Git ancestry")

    def prepare(self, kind, branch, target_sha=None):
        if kind not in ("service.upgrade", "service.rollback"):
            raise DevFlowError("Unknown release transition")
        observed = self.git.remote_head(branch)
        if observed["head_sha"] != observed["remote_head_sha"]:
            raise DevFlowError("Source branch is not published")
        current = self._current()
        unit_digest = _digest(self.unit_path)
        override_digest = _digest(self.override)
        health = self.probe.inspect(self, current)
        if health != {"active": True, "discovery_status": 200,
                       "unauthenticated_status": 401, "listener": "127.0.0.1:8765"}:
            raise DevFlowError("Active release preflight is incomplete")
        if kind == "service.upgrade":
            target_sha = observed["head_sha"]
            if (current == target_sha or (self.release_root / target_sha).exists() or
                (self.release_root / target_sha).is_symlink()):
                raise DevFlowError("Upgrade release destination is occupied")
            self._ancestor(current, target_sha)
        else:
            if target_sha == current:
                raise DevFlowError("Rollback must select another release")
            self._release(target_sha)
            self._ancestor(target_sha, current)
        plan = {
            "schema_version": "1.0", "operation_id": "service-transition-" + uuid.uuid4().hex,
            "project_id": observed["project_id"], "kind": kind, "service": SERVICE,
            "source": {"branch": branch, "git_head_sha": observed["head_sha"],
                       "remote_head_sha": observed["remote_head_sha"],
                       "remote_url": observed["remote_url"]},
            "preconditions": {"active_sha": current, "unit_sha256": unit_digest,
                              "override_sha256": override_digest},
            "release": {"target_sha": target_sha,
                        "path": str(self.release_root / target_sha),
                        "pointer": str(self.pointer), "runtime_root": str(self.root)},
            "health": {"resource": RESOURCE, "discovery": DISCOVERY,
                       "discovery_status": 200, "unauthenticated_status": 401,
                       "listener": "127.0.0.1:8765"},
            "execution_enabled": True,
        }
        self.contracts.validate(plan, "release.transition.plan.schema.json")
        return plan


class TransitionPolicy:
    def __init__(self, workflow):
        self.workflow = workflow
        self.root = workflow.root
        self.contracts = workflow.contracts
        self.project_id = workflow.git.project_id

    def validate(self, plan):
        if not isinstance(plan, dict) or len(canonical(plan)) > 65536:
            raise DevFlowError("Release transition plan must be bounded JSON")
        self.contracts.validate(plan, "release.transition.plan.schema.json")
        if plan["project_id"] != self.project_id:
            raise DevFlowError("Release transition project differs")
        current = self.workflow.prepare(
            plan["kind"], plan["source"]["branch"],
            plan["release"]["target_sha"] if plan["kind"] == "service.rollback" else None)
        expected = copy.deepcopy(current)
        expected["operation_id"] = plan["operation_id"]
        if plan != expected:
            raise DevFlowError("Release transition plan is stale or differs from live inspection")
        return {"operation_id": plan["operation_id"],
                "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
                "from_sha": plan["preconditions"]["active_sha"],
                "target_sha": plan["release"]["target_sha"],
                "sensitive": True, "execution_enabled": True}


class TransitionExecutor(ReleaseExecutor):
    """Only local root can execute an exact approved release transition."""

    def __init__(self, workflow, approvals, continuity, command=_command, auth_probe=None):
        self.workflow = workflow
        self.policy = TransitionPolicy(workflow)
        self.approvals = approvals
        self.continuity = continuity
        self.command = command
        self.auth_probe = auth_probe or self._authenticated_probe
        if (self.policy.root != continuity.root or
            self.policy.project_id != continuity.project_id or
            approvals.policy.root != self.policy.root):
            raise DevFlowError("Transition, approval and continuity roots differ")

    def _switch(self, expected, target, operation_id):
        if self.workflow._current() != expected:
            raise DevFlowError("Active release changed before switch")
        temporary = self.workflow.release_root / (".current-" + operation_id)
        if temporary.exists() or temporary.is_symlink():
            raise DevFlowError("Temporary release pointer is occupied")
        temporary.symlink_to(target)
        try:
            os.replace(temporary, self.workflow.pointer)
        finally:
            temporary.unlink(missing_ok=True)
        self.command("systemctl", "restart", SERVICE)

    def _evidence_transition(self, plan, checked, identity, outcome, details):
        return ReadExecutor(self.policy, self.continuity)._evidence({
            "operation_id": plan["operation_id"], "kind": plan["kind"],
            "plan_sha256": checked["plan_sha256"], "execution_id": identity[1],
            "fence": identity[2], "from_sha": plan["preconditions"]["active_sha"],
            "target_sha": plan["release"]["target_sha"], "outcome": outcome,
            "details": details, "recorded_at_epoch": time.time()})

    def run(self, plan, approval_id, token_file, owner="stage4-transition-local"):
        if os.geteuid() != 0:
            raise DevFlowError("Release transition requires a local root operator")
        checked = self.approvals.verify(plan, approval_id)
        try:
            token = read_private_file(token_file, 16384).decode().strip()
        except UnicodeError as exc:
            raise DevFlowError("OAuth probe token is invalid") from exc
        network = NetworkPolicy(self.policy.root, self.policy.contracts,
                                "/etc/devflow/network.json")
        if asyncio.run(JWTVerifier(network).verify_token(token)) is None:
            raise DevFlowError("OAuth probe token is invalid or lacks access")
        lease = self.continuity.acquire(owner, 600)
        identity = owner, lease["execution"]["execution_id"], lease["fence"]
        key = plan["operation_id"]
        try:
            prepared = self.continuity.prepare(*identity, key, {
                "kind": plan["kind"], "plan_sha256": checked["plan_sha256"]})
            if not prepared["changed"]:
                if prepared["operation"]["status"] == "SUCCEEDED":
                    return {"ok": True, "changed": False, "operation": prepared["operation"]}
                raise DevFlowError("Transition operation is already prepared or unresolved")
            checked = self.approvals.verify(plan, approval_id)
            self.continuity.start(*identity, key)
            prior = plan["preconditions"]["active_sha"]
            target = plan["release"]["target_sha"]
            try:
                if plan["kind"] == "service.upgrade":
                    self._stage({"source": {"git_head_sha": target}})
                self._switch(prior, target, key)
                count = self._health({"source": {"git_head_sha": target}}, token)
            except BaseException as cause:
                try:
                    active = self.workflow._current()
                    if active == target:
                        self._switch(target, prior, key + "-restore")
                    elif active != prior:
                        raise DevFlowError("Cannot restore unexpected release pointer")
                    self._health({"source": {"git_head_sha": prior}}, token)
                except BaseException:
                    self._evidence_transition(plan, checked, identity, "UNKNOWN",
                                              "Prior release could not be verified after failure")
                    raise DevFlowError("Transition outcome unknown; reconcile before retry") from cause
                evidence = self._evidence_transition(plan, checked, identity, "FAILED",
                                                     "Prior release restored and verified")
                self.continuity.finish(*identity, key, "FAILED", evidence)
                raise DevFlowError("Transition failed; prior release restored and failure fenced") from cause
            evidence = self._evidence_transition(plan, checked, identity, "SUCCEEDED", {
                "deployed_sha": target, "authenticated_tools": count, "public_status": [200, 401]})
            result = self.continuity.finish(*identity, key, "SUCCEEDED", evidence)
            return {"ok": True, "changed": True, "from_sha": prior,
                    "deployed_sha": target, "authenticated_tools": count,
                    "operation": result["operation"]}
        finally:
            status = self.continuity.status()
            if (status.get("lease") and
                status["lease"]["execution"]["execution_id"] == identity[1] and
                not any(item["status"] == "STARTED" for item in status["operations"])):
                self.continuity.release(*identity)
