"""Read-only ServerMaster bootstrap. Only stdio is exposed in Stage 1."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .registry import ControlRegistry


def build_server(registry: ControlRegistry) -> FastMCP:
    mcp = FastMCP("DevFlow ServerMaster", instructions=(
        "Read-only Stage 1 registry. Recorded server settings are declarations, "
        "not live health checks. No shell, deployment, vault or watchdog is implemented."
    ))
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=annotations)
    def servermaster_ping() -> dict:
        """Report bootstrap version and actual supported capabilities."""
        return {"ok": True, "version": __version__, "transport": "stdio",
                "read_only": True, "remote_agent": False, "vault": False,
                "execution_lease": False, "watchdog": False}

    @mcp.tool(annotations=annotations)
    def registry_projects() -> dict:
        """List registered projects and the active project ID."""
        data = registry.read()
        return {"active_project_id": data.get("active_project_id"), "projects": data["projects"]}

    @mcp.tool(annotations=annotations)
    def registry_servers() -> dict:
        """List declared server bindings; does not probe their health."""
        return {"servers": registry.read()["servers"], "live_verified": False}

    @mcp.tool(annotations=annotations)
    def registry_validate() -> dict:
        """Validate registry schema, unique IDs and active-project integrity."""
        data = registry.read()
        return {"ok": True, "projects": len(data["projects"]), "servers": len(data["servers"])}

    @mcp.tool(annotations=annotations)
    def project_status(project_id: str | None = None) -> dict:
        """Read registered continuity documents and report missing prerequisites."""
        return registry.project_status(project_id)

    return mcp
