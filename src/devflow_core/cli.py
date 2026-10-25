import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import Contracts, DevFlowError
from .registry import ControlRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="DevFlow Stage 1 control plane")
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
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        registry = ControlRegistry(
            args.registry or root / "control" / "registry.json",
            Contracts(args.contracts or root / "contracts"),
        )
        if args.command in {"init", "register"}:
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
