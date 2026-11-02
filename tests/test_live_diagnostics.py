"""The public diagnostic inventory has a fixed local scope and no raw logs."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from devflow_core.contracts import DevFlowError
from devflow_core.live_diagnostics import LiveDiagnostics
from devflow_core.server import build_server


class Response:
    def __init__(self, code):
        self.status_code = code
        self.headers = {"www-authenticate": "Bearer"}

    def json(self):
        return {"resource": "https://srun.ir/mcpdevflow"}


class Client:
    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, *_):
        return Response(200)

    def post(self, *_, **__):
        return Response(401)


def test_live_service_and_git_are_bounded_to_control_root(tmp_path, monkeypatch):
    root = tmp_path / "devflow"
    root.mkdir()
    sha = "a" * 40
    release = tmp_path / "devflow-releases" / sha
    release.mkdir(parents=True)
    (release / ".devflow-release").write_text(sha + "\n")
    (release.parent / "current").symlink_to(sha)
    commands = []

    def run(argv, **kwargs):
        commands.append(tuple(argv))
        assert kwargs["cwd"] == root
        assert kwargs["timeout"] == 7
        if argv[0] == "systemctl":
            output = "ActiveState=active\nMainPID=100\n"
        elif argv[1:3] == ("branch", "--show-current"):
            output = "stage/live\n"
        elif argv[1] == "rev-parse":
            output = sha + "\n"
        else:
            output = " M secrets.txt\n"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr("devflow_core.live_diagnostics.subprocess.run", run)
    monkeypatch.setattr("devflow_core.live_diagnostics.httpx.Client", Client)
    inspector = LiveDiagnostics(root)
    assert inspector.service() == {"service": "devflow-mcp.service", "active": True,
                                   "pid": 100, "release_sha": sha,
                                   "public_status": [200, 401], "oauth_boundary_ok": True}
    assert inspector.git() == {"branch": "stage/live", "head_sha": sha, "dirty": True}
    assert not any("secrets.txt" in str(value) for value in (inspector.git(), inspector.service()))
    assert len(commands) == 8


def test_rejects_unexpected_release_pointer(tmp_path, monkeypatch):
    root = tmp_path / "devflow"
    root.mkdir()
    releases = tmp_path / "devflow-releases"
    releases.mkdir()
    (releases / "current").symlink_to("../unexpected")
    monkeypatch.setattr(LiveDiagnostics, "_run", lambda *_: "ActiveState=active\nMainPID=100")
    with pytest.raises(DevFlowError, match="pointer"):
        LiveDiagnostics(root).service()


def test_mcp_service_probe_keeps_event_loop_available(settings_path, registry, monkeypatch):
    """The public probe calls this same MCP server and must not deadlock it."""
    registry.register(settings_path)
    started = threading.Event()
    released = threading.Event()

    def service(_self):
        started.set()
        return {"released": released.wait(timeout=2)}

    monkeypatch.setattr(LiveDiagnostics, "service", service)
    server = build_server(registry, control_root=settings_path.parent.parent)

    async def check():
        task = asyncio.create_task(server.call_tool("control_service_live", {"project_id": "devflow"}))
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), 1.5)
        released.set()
        return await asyncio.wait_for(task, 2)

    try:
        assert json.loads(asyncio.run(check())[0].text)["released"] is True
    finally:
        released.set()
