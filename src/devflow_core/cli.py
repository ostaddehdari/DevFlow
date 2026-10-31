import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import Contracts, DevFlowError, read_json
from .registry import ControlRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="DevFlow control plane")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("DEVFLOW_ROOT", ".")))
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--contracts", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "register"):
        p = sub.add_parser(name)
        p.add_argument("--settings", type=Path, default=None)
        p.add_argument("--activate", action="store_true")
    sub.add_parser("validate")
    sub.add_parser("list")
    status = sub.add_parser("status")
    status.add_argument("--project", default=None)
    activate = sub.add_parser("activate")
    activate.add_argument("project_id")
    http = sub.add_parser('serve-http')
    http.add_argument('--config', type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--credential-file", type=Path, default=None)
    git_plan = sub.add_parser('git-plan')
    git_commands = git_plan.add_subparsers(dest='git_command', required=True)
    for name in ('inspect', 'remote', 'prepare'):
        command = git_commands.add_parser(name)
        command.add_argument('--branch', required=True)
    deployment = sub.add_parser('deployment-plan')
    deployment_commands = deployment.add_subparsers(dest='deployment_command', required=True)
    for name in ('inspect', 'prepare', 'validate', 'prepare-execution', 'approve', 'verify', 'run'):
        command = deployment_commands.add_parser(name)
        if name in ('validate', 'approve', 'verify', 'run'):
            command.add_argument('--plan', type=Path, required=True)
        if name in ('inspect', 'prepare', 'prepare-execution'):
            command.add_argument('--branch', required=True)
        if name == 'approve':
            command.add_argument('--credential-file', type=Path, required=True)
            command.add_argument('--seconds', type=int, default=600)
        if name in ('verify', 'run'):
            command.add_argument('--approval-id', required=True)
        if name == 'run':
            command.add_argument('--token-file', type=Path, required=True)
            command.add_argument('--owner', default='stage4-deploy-local')
    transition = sub.add_parser('release-transition')
    transition_commands = transition.add_subparsers(dest='transition_command', required=True)
    for name in ('prepare-upgrade', 'prepare-rollback', 'validate', 'approve', 'verify', 'run'):
        command = transition_commands.add_parser(name)
        if name in ('prepare-upgrade', 'prepare-rollback'):
            command.add_argument('--branch', required=True)
        if name == 'prepare-rollback':
            command.add_argument('--target-sha', required=True)
        if name in ('validate', 'approve', 'verify', 'run'):
            command.add_argument('--plan', type=Path, required=True)
        if name == 'approve':
            command.add_argument('--credential-file', type=Path, required=True)
            command.add_argument('--seconds', type=int, default=600)
        if name in ('verify', 'run'):
            command.add_argument('--approval-id', required=True)
        if name == 'run':
            command.add_argument('--token-file', type=Path, required=True)
            command.add_argument('--owner', default='stage4-transition-local')
    operation = sub.add_parser('operation-plan')
    operation_commands = operation.add_subparsers(dest='operation_command', required=True)
    for name in ('validate', 'approval-init', 'approve', 'verify', 'revoke', 'run-read', 'run-push'):
        command = operation_commands.add_parser(name)
        if name in ('validate', 'approve', 'verify', 'run-read', 'run-push'):
            command.add_argument('--plan', type=Path, required=True)
        if name == 'approve':
            command.add_argument('--credential-file', type=Path, required=True)
            command.add_argument('--seconds', type=int, default=600)
        if name in ('verify', 'revoke', 'run-push'):
            command.add_argument('--approval-id', required=True)
        if name in ('run-read', 'run-push'):
            command.add_argument('--owner', required=True)
    security = sub.add_parser("security")
    security_commands = security.add_subparsers(dest="security_command", required=True)
    for name in ("init", "status", "principal-create", "issue", "revoke", "disable", "vault-put", "vault-list", "vault-rotate"):
        command = security_commands.add_parser(name)
        if name == "init":
            command.add_argument("--key-dir", type=Path, required=True)
        if name in {"principal-create", "issue", "disable"}:
            command.add_argument("--principal-id", required=True)
        if name == "principal-create":
            command.add_argument("--role", choices=["viewer", "operator", "agent"], required=True)
            command.add_argument("--project", action="append", dest="projects", required=True)
            command.add_argument("--vault-ref", action="append", default=[])
        if name == "issue":
            command.add_argument("--seconds", type=int, default=3600)
        if name == "revoke":
            command.add_argument("--credential-id", required=True)
        if name == "vault-put":
            command.add_argument("--reference", required=True)
            command.add_argument("--project", required=True)
            command.add_argument("--from-stdin", action="store_true")
    continuity = sub.add_parser("continuity")
    commands = continuity.add_subparsers(dest="continuity_command", required=True)
    names = ("init", "status", "resume", "lease-acquire", "lease-renew", "lease-release",
             "prepare", "start", "finish", "resolve", "checkpoint", "snapshot")
    for name in names:
        command = commands.add_parser(name)
        command.add_argument("--project", default=None)
        if name in {"lease-acquire", "lease-renew", "lease-release", "prepare", "start", "finish", "resolve", "checkpoint"}:
            command.add_argument("--owner", required=True)
        if name in {"lease-renew", "lease-release", "prepare", "start", "finish", "resolve", "checkpoint"}:
            command.add_argument("--execution-id", required=True)
            command.add_argument("--fence", type=int, required=True)
        if name == "lease-acquire":
            command.add_argument("--seconds", type=int, default=600)
        if name in {"prepare", "start", "finish", "resolve"}:
            command.add_argument("--key", required=True)
        if name == "prepare":
            command.add_argument("--intent", required=True)
        if name in {"finish", "resolve"}:
            command.add_argument("--outcome", required=True,
                                 choices=["SUCCEEDED", "FAILED", "UNKNOWN"] if name == "finish" else ["SUCCEEDED", "FAILED", "NO_EFFECT"])
            command.add_argument("--evidence", required=True)
        if name == "snapshot":
            command.add_argument("--checkpoint-id", default=None)
    worker = sub.add_parser("agent-worker")
    worker.add_argument("--agent-id", required=True)
    worker.add_argument("--profile", type=Path, required=True)
    worker.add_argument("--credential-file", type=Path, required=True)
    agents = sub.add_parser("agent")
    agent_commands = agents.add_subparsers(dest="agent_command", required=True)
    for name in ("init", "inventory", "enroll", "revoke", "renew"):
        command = agent_commands.add_parser(name)
        if name == "init":
            command.add_argument("--profile-dir", type=Path, required=True)
        if name in {"enroll", "revoke", "renew"}:
            command.add_argument("--agent-id", required=True)
        if name == "enroll":
            command.add_argument("--server-id", required=True)
            command.add_argument("--project", required=True)
            command.add_argument("--account", required=True)
            command.add_argument("--credential-dir", type=Path, required=True)
            command.add_argument("--allow-path", action="append", required=True)
            command.add_argument("--capability", action="append", required=True,
                                 choices=["system.info", "filesystem.list", "filesystem.read"])
    args = parser.parse_args()
    if args.command == "agent-worker":
        try:
            from .agent_worker import AgentView, build_agent
            code_root = Path(__file__).resolve().parents[2]
            contracts = Contracts(args.contracts or code_root / "contracts")
            view = AgentView(args.profile, args.credential_file, contracts, args.agent_id)
            build_agent(view).run(transport="stdio")
            return 0
        except (DevFlowError, OSError) as exc:
            message = str(exc) if isinstance(exc, DevFlowError) else "Agent filesystem access failed"
            print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
            return 1
    root = args.root.resolve()
    try:
        registry = ControlRegistry(
            args.registry or root / "control" / "registry.json",
            Contracts(args.contracts or root / "contracts"),
        )
        if args.command == 'serve-http':
            from .network import build_http
            import uvicorn
            mcp = build_http(root, args.config)
            uvicorn.run(mcp.streamable_http_app(), host='127.0.0.1', port=8765,
                        proxy_headers=False, server_header=False, access_log=False)
            return 0
        if args.command == 'deployment-plan':
            from .deployment import DeploymentWorkflow, DeploymentPolicy
            workflow = DeploymentWorkflow(root, registry.contracts)
            if args.deployment_command == 'inspect':
                result = workflow.inspect(args.branch)
            elif args.deployment_command == 'prepare':
                result = workflow.prepare(args.branch)
            elif args.deployment_command == 'prepare-execution':
                result = workflow.prepare_execution(args.branch)
            else:
                if args.plan.stat().st_size > 65536:
                    raise DevFlowError('Deployment plan exceeds 64 KiB')
                plan = read_json(args.plan)
                if args.deployment_command == 'validate':
                    result = workflow.validate(plan) if plan.get('execution_enabled') is False else {
                        'ok': True, **DeploymentPolicy(workflow).validate(plan)}
                else:
                    from .operation_plans import ApprovalStore
                    from .security import SecurityStore, read_private_file
                    policy = DeploymentPolicy(workflow)
                    approvals = ApprovalStore(policy, SecurityStore(root, registry.contracts))
                    if args.deployment_command == 'approve':
                        try:
                            credential = read_private_file(args.credential_file, 256).decode().strip()
                        except UnicodeError as exc:
                            raise DevFlowError('Operator credential is invalid') from exc
                        result = approvals.grant(plan, credential, args.seconds)
                    elif args.deployment_command == 'verify':
                        result = {'ok': True, **approvals.verify(plan, args.approval_id)}
                    else:
                        from .continuity import from_registry
                        from .deployment_execution import ReleaseExecutor
                        result = ReleaseExecutor(workflow, approvals, from_registry(
                            registry, plan['project_id'])).run(
                                plan, args.approval_id, args.token_file, args.owner)
        elif args.command == 'release-transition':
            from .release_transition import TransitionWorkflow, TransitionPolicy, TransitionExecutor
            workflow = TransitionWorkflow(root, registry.contracts)
            name = args.transition_command
            if name == 'prepare-upgrade':
                result = workflow.prepare('service.upgrade', args.branch)
            elif name == 'prepare-rollback':
                result = workflow.prepare('service.rollback', args.branch, args.target_sha)
            else:
                if args.plan.stat().st_size > 65536:
                    raise DevFlowError('Release transition plan exceeds 64 KiB')
                plan = read_json(args.plan)
                policy = TransitionPolicy(workflow)
                if name == 'validate':
                    result = {'ok': True, **policy.validate(plan)}
                else:
                    from .operation_plans import ApprovalStore
                    from .security import SecurityStore, read_private_file
                    approvals = ApprovalStore(policy, SecurityStore(root, registry.contracts))
                    if name == 'approve':
                        try:
                            credential = read_private_file(args.credential_file, 256).decode().strip()
                        except UnicodeError as exc:
                            raise DevFlowError('Operator credential is invalid') from exc
                        result = approvals.grant(plan, credential, args.seconds)
                    elif name == 'verify':
                        result = {'ok': True, **approvals.verify(plan, args.approval_id)}
                    else:
                        from .continuity import from_registry
                        result = TransitionExecutor(workflow, approvals, from_registry(
                            registry, plan['project_id'])).run(
                                plan, args.approval_id, args.token_file, args.owner)
        elif args.command == 'git-plan':
            from .git_workflow import GitWorkflow
            workflow = GitWorkflow(root, registry.contracts)
            if args.git_command == 'inspect':
                result = workflow.inspect(args.branch)
            elif args.git_command == 'remote':
                result = workflow.remote_head(args.branch)
            else:
                result = workflow.prepare_push(args.branch)
        elif args.command == 'operation-plan':
            from .operation_plans import ApprovalStore, PlanPolicy
            from .security import SecurityStore, read_private_file
            policy = PlanPolicy(root, registry.contracts)
            name = args.operation_command
            plan = None
            if name in ('validate', 'approve', 'verify', 'run-read', 'run-push'):
                if args.plan.stat().st_size > 65536:
                    raise DevFlowError('Operation plan exceeds 64 KiB')
                plan = read_json(args.plan)
            if name == 'validate':
                result = {'ok': True, **policy.validate(plan)}
            elif name == 'run-push':
                from .continuity import from_registry
                from .git_execution import GitPushExecutor
                approvals = ApprovalStore(policy, SecurityStore(root, registry.contracts))
                result = GitPushExecutor(policy, approvals, from_registry(registry, plan['project_id'])).run(
                    plan, args.approval_id, args.owner)
            elif name == 'run-read':
                from .continuity import from_registry
                from .controlled_execution import ReadExecutor
                result = ReadExecutor(policy, from_registry(registry, plan['project_id'])).run(plan, args.owner)
            else:
                approvals = ApprovalStore(policy, SecurityStore(root, registry.contracts))
                if name == 'approval-init':
                    result = approvals.initialize()
                elif name == 'approve':
                    try:
                        credential = read_private_file(args.credential_file, 256).decode().strip()
                    except UnicodeError as exc:
                        raise DevFlowError('Operator credential is invalid') from exc
                    result = approvals.grant(plan, credential, args.seconds)
                elif name == 'verify':
                    result = {'ok': True, **approvals.verify(plan, args.approval_id)}
                else:
                    result = approvals.revoke(args.approval_id)
        elif args.command == "agent":
            from .agents import AgentStore
            store = AgentStore(root, registry.contracts)
            if args.agent_command == "init":
                result = store.initialize(args.profile_dir)
            elif args.agent_command == "inventory":
                result = store.inventory()
            elif args.agent_command == "enroll":
                result = store.enroll(registry, args.agent_id, args.server_id, args.project,
                    args.account, args.credential_dir, args.allow_path, args.capability)
            elif args.agent_command == "renew":
                result = store.renew(args.agent_id)
            else:
                result = store.revoke(args.agent_id)
        elif args.command == "security":
            from .security import SecurityStore
            store = SecurityStore(root, registry.contracts)
            name = args.security_command
            known_projects = {p["project_id"] for p in registry.read()["projects"]}
            if name == "init":
                result = store.initialize(args.key_dir)
            elif name == "status":
                result = store.status()
            elif name == "principal-create":
                if not set(args.projects).issubset(known_projects):
                    raise DevFlowError("Unknown project grant")
                result = store.create_principal(args.principal_id, args.role, args.projects, args.vault_ref)
            elif name == "issue":
                result = store.issue(args.principal_id, args.seconds)
            elif name == "revoke":
                result = store.revoke(args.credential_id)
            elif name == "disable":
                result = store.disable(args.principal_id)
            elif name == "vault-list":
                result = store.references()
            elif name == "vault-rotate":
                result = store.rotate_key()
            else:
                if args.project not in known_projects:
                    raise DevFlowError("Unknown vault project binding")
                if args.from_stdin:
                    value = sys.stdin.buffer.read(1048577)
                else:
                    if not sys.stdin.isatty():
                        raise DevFlowError("Use --from-stdin for a controlled secret input stream")
                    import getpass
                    value = getpass.getpass("Secret value (hidden): ").encode()
                result = store.put(args.reference, args.project, value)
        elif args.command == "continuity":
            from .continuity import from_registry
            store = from_registry(registry, args.project)
            name = args.continuity_command
            if name == "init":
                result = store.initialize()
            elif name == "status":
                result = store.status()
            elif name == "resume":
                result = store.resume_plan()
            elif name == "snapshot":
                result = store.snapshot(args.checkpoint_id)
            elif name == "lease-acquire":
                result = store.acquire(args.owner, args.seconds)
            else:
                identity = (args.owner, args.execution_id, args.fence)
                if name == "lease-renew":
                    result = store.renew(*identity)
                elif name == "lease-release":
                    result = store.release(*identity)
                elif name == "prepare":
                    try:
                        intent = json.loads(args.intent)
                    except ValueError as exc:
                        raise DevFlowError("Intent must be valid JSON") from exc
                    result = store.prepare(*identity, args.key, intent)
                elif name == "start":
                    result = store.start(*identity, args.key)
                elif name == "finish":
                    result = store.finish(*identity, args.key, args.outcome, args.evidence)
                elif name == "resolve":
                    result = store.resolve(*identity, args.key, args.outcome, args.evidence)
                else:
                    result = store.checkpoint(*identity)
        elif args.command in {"init", "register"}:
            result = registry.register(
                args.settings or root / ".ai-project" / "project.settings.json", args.activate
            )
        elif args.command == "validate":
            data = registry.read()
            result = {"ok": True, "projects": len(data["projects"]), "servers": len(data["servers"])}
        elif args.command == "list":
            result = registry.read()
        elif args.command == "activate":
            result = registry.activate(args.project_id)
        elif args.command == "status":
            result = registry.project_status(args.project)
        else:
            # Fail before starting MCP if registry initialization has not succeeded.
            registry.read()
            from .server import build_server
            gate = None
            if args.credential_file is not None:
                from .security import CredentialGate, SecurityStore
                gate = CredentialGate(SecurityStore(root, registry.contracts), args.credential_file)
                gate.principal()  # Fail before protocol startup for an invalid credential.
            build_server(registry, gate, root).run(transport="stdio")
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DevFlowError, OSError) as exc:
        # Avoid exception traces or values containing credential data on normal CLI errors.
        message = str(exc) if isinstance(exc, DevFlowError) else "Filesystem operation failed"
        print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
