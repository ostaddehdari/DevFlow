import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import Contracts, DevFlowError
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
    sub.add_parser("serve")
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
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        registry = ControlRegistry(
            args.registry or root / "control" / "registry.json",
            Contracts(args.contracts or root / "contracts"),
        )
        if args.command == "continuity":
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
            build_server(registry).run(transport="stdio")
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
