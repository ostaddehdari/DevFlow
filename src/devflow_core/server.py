"""Read-only ServerMaster bootstrap. Only stdio is exposed."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .registry import ControlRegistry


def build_server(registry: ControlRegistry, gate=None, control_root=None) -> FastMCP:
    mcp = FastMCP("DevFlow ServerMaster", instructions=(
        "Read-only registry and local continuity inspection. Recorded server settings are declarations, "
        "not live health checks. No shell, deployment or watchdog is implemented; vault values are never exposed as tools."
    ))
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=False)

    def selected_project(project_id):
        selected = project_id if project_id is not None else registry.read().get("active_project_id")
        if selected is None:
            from .contracts import DevFlowError
            raise DevFlowError("No project selected")
        return selected

    def visible_projects(data):
        if gate is None:
            return data["projects"]
        principal = gate.principal()
        visible = [p for p in data["projects"] if p["project_id"] in principal["projects"]]
        for project in visible:
            gate.check("registry.read", project["project_id"])
        return visible

    def visible_servers(data):
        if gate is None:
            return data["servers"]
        from pathlib import Path
        from .contracts import read_json
        from .registry import reject_credentials
        allowed = set()
        for project in visible_projects(data):
            settings = read_json(Path(project["settings_path"]))
            registry.contracts.validate(settings, "project.settings.schema.json")
            reject_credentials(settings)
            if settings["project"]["id"] != project["project_id"]:
                from .contracts import DevFlowError
                raise DevFlowError("Project settings identity mismatch")
            allowed.update(s["server_id"] for s in settings["servers"].values())
        return [s for s in data["servers"] if s["server_id"] in allowed]

    @mcp.tool(annotations=annotations)
    def servermaster_ping() -> dict:
        """Report bootstrap version and actual supported capabilities."""
        if gate is not None:
            gate.principal()
        return {"ok": True, "version": __version__, "transport": "stdio",
                "read_only": True, "remote_agent": False, "vault": True, "vault_scope": "local_encrypted_reference_backend",
                "identity_mode": "scoped_local_credential" if gate is not None else "trusted_local_process",
                "execution_lease": True, "lease_scope": "cooperative_local_ledger", "watchdog": False}

    @mcp.tool(annotations=annotations)
    def registry_projects() -> dict:
        """List registered projects and the active project ID."""
        data = registry.read()
        projects = visible_projects(data)
        active = data.get("active_project_id")
        return {"active_project_id": active if active in {p["project_id"] for p in projects} else None, "projects": projects}

    @mcp.tool(annotations=annotations)
    def registry_servers() -> dict:
        """List declared server bindings; does not probe their health."""
        return {"servers": visible_servers(registry.read()), "live_verified": False}

    @mcp.tool(annotations=annotations)
    def registry_validate() -> dict:
        """Validate registry schema, unique IDs and active-project integrity."""
        data = registry.read()
        return {"ok": True, "projects": len(visible_projects(data)), "servers": len(visible_servers(data))}

    @mcp.tool(annotations=annotations)
    def project_status(project_id: str | None = None) -> dict:
        """Read registered continuity documents and report missing prerequisites."""
        selected = selected_project(project_id)
        if gate is not None:
            gate.check("project.read", selected)
        return registry.project_status(selected)

    @mcp.tool(annotations=annotations)
    def continuity_status(project_id: str | None = None) -> dict:
        """Inspect local lease, unresolved operations and latest checkpoint without writes."""
        from .continuity import from_registry
        selected = selected_project(project_id)
        if gate is not None:
            gate.check("continuity.read", selected)
        return from_registry(registry, selected).status()

    @mcp.tool(annotations=annotations)
    def security_status() -> dict:
        """Report local security-backend metadata without secret values or credentials."""
        from .security import SecurityStore
        if gate is not None:
            gate.check("security.read", gate.store.project_id)
            return gate.store.status()
        # The control registry's local installation owns the security backend.
        root = control_root or registry.path.parent.parent
        return SecurityStore(root, registry.contracts).status()

    return mcp
