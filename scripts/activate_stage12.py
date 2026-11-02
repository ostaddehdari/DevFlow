"""Start one approved DevFlow release upgrade from an exact published commit.

Read the Auth0 client secret from SSH stdin. The secret never enters argv or disk.
The DevFlow transition runner owns the fenced effect, health checks and restore.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from devflow_core.operation_plans import canonical
from devflow_core.security import read_private_file


ROOT = Path("/opt/devflow")
PLAN = Path("/etc/devflow/stage12-upgrade-plan.json")
BRANCH = "stage/05-06-connect-live"
PRIOR_SHA = "42045b9d87550852a591b54982ff84a520516a6d"
ISSUER = "https://dev-fqzv1aqwljc7mhwz.us.auth0.com/"
CLIENT_ID = "hfOqg60yFiUrVTVc04u9C5L485Ztkqnb"
RESOURCE = "https://srun.ir/mcpdevflow"
WORKER_SCRIPT = Path(__file__).resolve()


def command(*arguments, timeout=120):
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(message[:1500] or "System command failed")
    return result.stdout.strip()


def devflow(*arguments, timeout=120):
    return json.loads(command("bash", "scripts/devflow", *arguments, timeout=timeout))


def preflight(expected_head, expected_digest=None):
    if os.geteuid() != 0:
        raise RuntimeError("A local root operator is required")
    if command("git", "branch", "--show-current") != BRANCH:
        raise RuntimeError("Stage branch changed")
    if command("git", "rev-parse", "HEAD") != expected_head:
        raise RuntimeError("Published source commit changed")
    if command("git", "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Working tree has changes")
    pointer = Path("/opt/devflow-releases/current")
    if not pointer.is_symlink() or os.readlink(pointer) != PRIOR_SHA:
        raise RuntimeError("Active release differs from the reviewed upgrade")
    if not PLAN.exists():
        if expected_digest is not None:
            raise RuntimeError("The approved plan disappeared")
        prepared = devflow("release-transition", "prepare-upgrade", "--branch", BRANCH)
        descriptor, temporary = tempfile.mkstemp(prefix=".stage12-plan-", dir=PLAN.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(prepared, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, PLAN)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    plan = json.loads(PLAN.read_text())
    digest = hashlib.sha256(canonical(plan)).hexdigest()
    if (plan.get("kind") != "service.upgrade" or
        plan.get("source", {}).get("git_head_sha") != expected_head or
        plan.get("source", {}).get("remote_head_sha") != expected_head or
        plan.get("preconditions", {}).get("active_sha") != PRIOR_SHA or
        plan.get("release", {}).get("target_sha") != expected_head or
        (expected_digest is not None and digest != expected_digest)):
        raise RuntimeError("Prepared plan differs from the reviewed plan")
    checked = devflow("release-transition", "validate", "--plan", str(PLAN))
    if (not checked.get("ok") or checked.get("plan_sha256") != digest or
        checked.get("from_sha") != PRIOR_SHA or
        checked.get("target_sha") != expected_head):
        raise RuntimeError("Live upgrade preflight did not match the reviewed plan")
    resume = devflow("continuity", "resume")
    if resume.get("decision") != "READY":
        raise RuntimeError("Continuity is not ready for the transition")
    print("PASS: exact upgrade plan, active release and continuity", flush=True)
    return digest


def mint_token():
    secret = sys.stdin.buffer.readline(2048).rstrip(b"\r\n")
    if not secret or len(secret) > 1024:
        raise RuntimeError("Auth0 Client Secret is missing or too long")
    try:
        response = httpx.post(ISSUER + "oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": secret.decode("utf-8"),
            "audience": RESOURCE,
            "scope": "devflow.read",
        }, timeout=20)
    finally:
        del secret
    if response.status_code != 200:
        raise RuntimeError("Auth0 token request failed: HTTP " + str(response.status_code))
    document = response.json()
    token = document.get("access_token")
    if (not isinstance(token, str) or not 10 < len(token) <= 16384 or
        document.get("token_type", "").lower() != "bearer"):
        raise RuntimeError("Auth0 did not return a valid bearer token")
    descriptor, name = tempfile.mkstemp(prefix=".stage12-upgrade-probe-",
                                        dir="/etc/devflow", text=True)
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def probe_new_tools(token_path, expected_head):
    token = read_private_file(token_path, 16384).decode().strip()
    try:
        async with streamablehttp_client(
            RESOURCE, headers={"Authorization": "Bearer " + token},
            timeout=20, sse_read_timeout=20
        ) as (reader, writer, _):
            async with ClientSession(reader, writer,
                                     read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                names = {item.name for item in (await session.list_tools()).tools}
                required = {"control_service_live", "control_resources_live", "control_git_live"}
                if not required.issubset(names) or len(names) < 11:
                    raise RuntimeError("Live MCP inventory is incomplete")
                results = {}
                for name in sorted(required):
                    response = await session.call_tool(name, {"project_id": "devflow"})
                    if response.isError:
                        raise RuntimeError("Live MCP diagnostic tool failed: " + name)
                    results[name] = json.loads(response.content[0].text)
                service = results["control_service_live"]
                resources = results["control_resources_live"]
                git = results["control_git_live"]
                if (not service["active"] or not service["oauth_boundary_ok"] or
                    service["release_sha"] != expected_head or
                    resources["memory_total_bytes"] <= 0 or
                    resources["project_disk_total_bytes"] <= 0 or
                    git["head_sha"] != expected_head or git["dirty"]):
                    raise RuntimeError("Live MCP observations differ from the published release")
                return {"ok": True, "authenticated_tools": len(names),
                        "active_release": service["release_sha"],
                        "public_status": service["public_status"],
                        "git_branch": git["branch"], "resources_observed": True}
    except Exception as exc:
        raise RuntimeError("Post-upgrade live diagnostic MCP probe failed") from exc


def execute(token_path, expected_head, digest):
    credential_path = None
    approval_id = None
    try:
        issued = devflow("security", "issue", "--principal-id",
                         "stage4-operator", "--seconds", "600")
        credential_path = Path(issued["credential_file"])
        devflow("operation-plan", "approval-init")
        approved = devflow("release-transition", "approve", "--plan", str(PLAN),
                           "--credential-file", str(credential_path),
                           "--seconds", "600")
        approval_id = approved["approval_id"]
        verified = devflow("release-transition", "verify", "--plan", str(PLAN),
                           "--approval-id", approval_id)
        if verified.get("plan_sha256") != digest:
            raise RuntimeError("Approval is not bound to the exact plan")
        print("Executing one approved upgrade with live checks and prior-release restore.",
              flush=True)
        outcome = devflow("release-transition", "run", "--plan", str(PLAN),
                          "--approval-id", approval_id, "--token-file",
                          str(token_path), "--owner", "stage12-upgrade-local",
                          timeout=400)
        if not outcome.get("ok") or outcome.get("deployed_sha") != expected_head:
            raise RuntimeError("Upgrade did not prove the target release")
        print(json.dumps(outcome, ensure_ascii=False, indent=2), flush=True)
        if os.readlink("/opt/devflow-releases/current") != expected_head:
            raise RuntimeError("Release pointer changed after success")
        if command("systemctl", "is-active", "devflow-mcp.service") != "active":
            raise RuntimeError("MCP service is not active")
        print("LIVE_DIAGNOSTICS: " + json.dumps(
            asyncio.run(asyncio.wait_for(probe_new_tools(token_path, expected_head), 90)),
            indent=2), flush=True)
        print("PASS: approved upgrade active at " + expected_head, flush=True)
        print(json.dumps(devflow("continuity", "resume"), indent=2), flush=True)
    finally:
        if approval_id:
            try:
                subprocess.run(["bash", "scripts/devflow", "operation-plan", "revoke",
                                "--approval-id", approval_id], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        token_path.unlink(missing_ok=True)
        if credential_path is not None:
            credential_path.unlink(missing_ok=True)


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--worker":
        token_path = Path(sys.argv[2])
        expected_head, digest = sys.argv[3:]
        if (token_path.parent != Path("/etc/devflow") or
            not token_path.name.startswith(".stage12-upgrade-probe-")):
            raise RuntimeError("Unexpected probe token path")
        try:
            preflight(expected_head, digest)
            execute(token_path, expected_head, digest)
        finally:
            token_path.unlink(missing_ok=True)
        return
    if len(sys.argv) != 3 or sys.argv[1] != "--expected-head" or not (
        len(sys.argv[2]) == 40 and all(c in "0123456789abcdef" for c in sys.argv[2])
    ):
        raise RuntimeError("The exact published source commit is required")
    expected_head = sys.argv[2]
    digest = preflight(expected_head)
    token_path = mint_token()
    try:
        directory = ROOT / ".devflow/evidence"
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Private evidence directory is unavailable")
        info = directory.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("Evidence directory must be owner-only")
        descriptor, name = tempfile.mkstemp(prefix="stage12-upgrade-",
                                            suffix=".log", dir=directory)
        log_path = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            job = subprocess.Popen(
                [sys.executable, str(WORKER_SCRIPT), "--worker",
                 str(token_path), expected_head, digest], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        print(f"STARTED: PID={job.pid} LOG={log_path} PLAN_SHA256={digest}", flush=True)
    except BaseException:
        token_path.unlink(missing_ok=True)
        if "log_path" in locals():
            log_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        print("Inspect service and continuity before any retry.", file=sys.stderr)
        raise SystemExit(1)
