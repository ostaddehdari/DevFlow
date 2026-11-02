"""Scoped ServerMaster MCP with bounded local observations."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .registry import ControlRegistry


def build_server(registry: ControlRegistry, gate=None, control_root=None, *, transport="stdio", token_verifier=None, auth=None, http_path="/mcp") -> FastMCP:
    mcp = FastMCP("DevFlow ServerMaster", host="127.0.0.1", port=8765,
                  stateless_http=True, max_sessions=128, max_request_body_size=1048576,
                  streamable_http_path=http_path,
                  token_verifier=token_verifier, auth=auth, instructions=(
        "Read-only registry, continuity and bounded local service/resource/Git inspection. "
        "Registry server entries are declarations. No shell, deployment or watchdog is exposed; "
        "vault values and raw logs are never tools."
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
        return {"ok": True, "version": __version__, "transport": transport,
                "read_only": True, "remote_agent": False, "local_restricted_agent": True, "vault": True, "vault_scope": "local_encrypted_reference_backend",
                "identity_mode": "scoped_local_credential" if gate is not None else "trusted_local_process",
                "execution_lease": True, "lease_scope": "cooperative_local_ledger",
                "live_control_diagnostics": True, "watchdog": False}

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

    @mcp.tool(annotations=annotations)
    def agent_inventory() -> dict:
        """List scoped local agent enrollments; never return credentials or claim remote connectivity."""
        from .agents import AgentStore
        projects = None
        if gate is not None:
            principal = gate.principal()
            projects = principal["projects"]
            for project_id in projects:
                gate.check("project.read", project_id)
        root = control_root or registry.path.parent.parent
        return AgentStore(root, registry.contracts).inventory(projects)

    def control_diagnostics(project_id):
        from pathlib import Path
        from .contracts import DevFlowError, read_json
        from .live_diagnostics import LiveDiagnostics
        root = Path(control_root or registry.path.parent.parent).resolve()
        selected = selected_project(project_id)
        if gate is not None:
            gate.check("project.read", selected)
        local = read_json(root / ".ai-project/project.settings.json")["project"]["id"]
        if selected != local:
            raise DevFlowError("The requested project is not hosted on this control server")
        return LiveDiagnostics(root)

    @mcp.tool(annotations=annotations)
    def control_service_live(project_id: str | None = None) -> dict:
        """Observe the local MCP service, pinned release, and public OAuth boundary."""
        return control_diagnostics(project_id).service()

    @mcp.tool(annotations=annotations)
    def control_resources_live(project_id: str | None = None) -> dict:
        """Read bounded memory, project filesystem capacity, and host load."""
        return control_diagnostics(project_id).resources()

    @mcp.tool(annotations=annotations)
    def control_git_live(project_id: str | None = None) -> dict:
        """Read the local control checkout branch, commit, and dirty flag."""
        return control_diagnostics(project_id).git()

    return mcp
