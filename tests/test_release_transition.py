"""Upgrade and rollback bind exact pointers to fenced, approved plans."""

import subprocess

import pytest

from devflow_core.contracts import DevFlowError
from devflow_core.release_transition import (
    TransitionExecutor, TransitionPolicy, TransitionWorkflow)


class Git:
    project_id = "devflow"

    def __init__(self, sha):
        self.sha = sha

    def remote_head(self, branch):
        return {"project_id": "devflow", "branch": branch, "head_sha": self.sha,
                "remote_head_sha": self.sha,
                "remote_url": "https://github.com/ostaddehdari/DevFlow.git"}


class Probe:
    def inspect(self, workflow, sha):
        assert workflow.pointer.readlink().name == sha
        return {"active": True, "discovery_status": 200,
                "unauthenticated_status": 401, "listener": "127.0.0.1:8765"}


class Approvals:
    def __init__(self, policy):
        self.policy = policy

    def verify(self, plan, approval_id):
        if approval_id != "approved":
            raise DevFlowError("Approval missing")
        return self.policy.validate(plan)


class Ledger:
    def __init__(self, root):
        self.root = root
        self.project_id = "devflow"
        self.operations = []
        self.lease = None

    def acquire(self, owner, ttl):
        assert ttl == 600
        self.lease = {"fence": 22, "execution": {"execution_id": "EX-test"}}
        return self.lease

    def prepare(self, *args):
        self.operations.append(("prepare", args[3]))
        return {"changed": True}

    def start(self, *args):
        self.operations.append(("start", args[3]))

    def finish(self, *args):
        self.operations.append(("finish", args[3], args[4]))
        return {"operation": {"status": args[4]}}

    def status(self):
        return {"lease": self.lease, "operations": []}

    def release(self, *args):
        self.operations.append(("release", args[1]))
        self.lease = None


@pytest.fixture
def context(tmp_path, contracts, monkeypatch):
    root = tmp_path / "devflow"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()

    git("init", "-b", "stage/test")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    for name in ("src/devflow_core/cli.py", "src/devflow_core/network.py",
                 "contracts/network.schema.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
    git("add", ".")
    git("commit", "-m", "prior")
    prior = git("rev-parse", "HEAD")
    release_root = tmp_path / "devflow-releases"
    release_root.mkdir(mode=0o700)
    prior_release = release_root / prior
    (prior_release / "src/devflow_core").mkdir(parents=True)
    (prior_release / "src/devflow_core/cli.py").write_text("original\n")
    (prior_release / ".devflow-release").write_text(prior + "\n")
    (release_root / "current").symlink_to(prior)

    (root / "src/devflow_core/cli.py").write_text("updated\n")
    git("add", ".")
    git("commit", "-m", "candidate")
    candidate = git("rev-parse", "HEAD")
    unit = tmp_path / "devflow-mcp.service"
    unit.write_text("[Service]\nExecStart=old\n")
    override = tmp_path / "devflow-mcp.service.d" / "10-devflow-release.conf"
    override.parent.mkdir()
    override.write_text("[Service]\nEnvironment=PYTHONPATH=old\n")
    workflow = TransitionWorkflow(root, contracts, unit_path=unit, git=Git(candidate), probe=Probe())
    (root / ".devflow").mkdir(mode=0o700)
    monkeypatch.setattr("devflow_core.release_transition.os.geteuid", lambda: 0)
    monkeypatch.setattr("devflow_core.release_transition.read_private_file",
                        lambda path, maximum: b"test-token")
    monkeypatch.setattr("devflow_core.release_transition.NetworkPolicy", lambda *args: None)

    class Verifier:
        def __init__(self, _):
            pass

        async def verify_token(self, token):
            return object()

    monkeypatch.setattr("devflow_core.release_transition.JWTVerifier", Verifier)
    return workflow, prior, candidate


def _executor(workflow):
    policy = TransitionPolicy(workflow)
    ledger = Ledger(workflow.root)
    restarts = []
    executor = TransitionExecutor(workflow, Approvals(policy), ledger,
                                  command=lambda *args: restarts.append(args))
    executor._health = lambda plan, token: 8
    return policy, ledger, executor, restarts


def test_upgrade_then_targeted_rollback(context):
    workflow, prior, candidate = context
    policy, ledger, executor, restarts = _executor(workflow)
    upgrade = workflow.prepare("service.upgrade", "stage/test")
    assert upgrade["preconditions"]["active_sha"] == prior
    assert upgrade["release"]["target_sha"] == candidate
    assert policy.validate(upgrade)["sensitive"]
    with pytest.raises(DevFlowError, match="Approval"):
        executor.run(upgrade, "missing", "/tmp/not-used")
    assert workflow.pointer.readlink().name == prior
    result = executor.run(upgrade, "approved", "/tmp/not-used")
    assert result["deployed_sha"] == candidate
    assert workflow.pointer.readlink().name == candidate
    assert (workflow.release_root / candidate / "src/devflow_core/cli.py").read_text() == "updated\n"
    assert ("finish", upgrade["operation_id"], "SUCCEEDED") in ledger.operations
    assert len(restarts) == 1

    rollback = workflow.prepare("service.rollback", "stage/test", prior)
    assert rollback["preconditions"]["active_sha"] == candidate
    policy, ledger, executor, restarts = _executor(workflow)
    result = executor.run(rollback, "approved", "/tmp/not-used")
    assert result["deployed_sha"] == prior
    assert workflow.pointer.readlink().name == prior
    assert ("finish", rollback["operation_id"], "SUCCEEDED") in ledger.operations
    assert len(restarts) == 1


def test_stale_plan_and_tampered_override_are_rejected_before_effect(context):
    workflow, prior, candidate = context
    plan = workflow.prepare("service.upgrade", "stage/test")
    policy, ledger, executor, _ = _executor(workflow)
    plan["release"]["target_sha"] = prior
    with pytest.raises(DevFlowError):
        policy.validate(plan)
    assert ledger.operations == []
    plan = workflow.prepare("service.upgrade", "stage/test")
    workflow.override.write_text("changed\n")
    with pytest.raises(DevFlowError, match="stale"):
        executor.run(plan, "approved", "/tmp/not-used")
    assert workflow.pointer.readlink().name == prior
    assert ledger.operations == []


def test_failed_health_restores_prior_and_fences_failure(context):
    workflow, prior, candidate = context
    plan = workflow.prepare("service.upgrade", "stage/test")
    policy, ledger, executor, restarts = _executor(workflow)

    def health(inner, token):
        if inner["source"]["git_head_sha"] == candidate:
            raise DevFlowError("Injected authenticated probe failure")
        return 8

    executor._health = health
    with pytest.raises(DevFlowError, match="prior release restored"):
        executor.run(plan, "approved", "/tmp/not-used")
    assert workflow.pointer.readlink().name == prior
    assert len(restarts) == 2
    assert ("finish", plan["operation_id"], "FAILED") in ledger.operations


def test_unverified_restoration_leaves_operation_unresolved(context):
    workflow, prior, candidate = context
    plan = workflow.prepare("service.upgrade", "stage/test")
    policy, ledger, executor, _ = _executor(workflow)
    executor._health = lambda plan, token: (_ for _ in ()).throw(DevFlowError("probe down"))
    with pytest.raises(DevFlowError, match="outcome unknown"):
        executor.run(plan, "approved", "/tmp/not-used")
    assert workflow.pointer.readlink().name == prior
    assert not any(item[0] == "finish" for item in ledger.operations)


def test_missing_target_and_existing_upgrade_destination_rejected(context):
    workflow, prior, candidate = context
    with pytest.raises(DevFlowError, match="incomplete"):
        workflow.prepare("service.rollback", "stage/test", "f" * 40)
    (workflow.release_root / candidate).mkdir()
    with pytest.raises(DevFlowError, match="occupied"):
        workflow.prepare("service.upgrade", "stage/test")
