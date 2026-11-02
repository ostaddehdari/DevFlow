"""Bounded, read-only observations of the local DevFlow control host."""

import os
import re
import subprocess
from pathlib import Path

import httpx

from .contracts import DevFlowError
from .deployment import DISCOVERY, RESOURCE, SERVICE


SHA = re.compile(r"[0-9a-f]{40}\Z")


class LiveDiagnostics:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _run(self, *args):
        try:
            return subprocess.run(args, cwd=self.root, capture_output=True, text=True,
                                  check=True, timeout=7).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise DevFlowError("Local diagnostic command failed") from exc

    def service(self):
        lines = self._run("systemctl", "show", SERVICE, "--property=ActiveState",
                          "--property=MainPID").splitlines()
        fields = dict(line.split("=", 1) for line in lines if "=" in line)
        try:
            pid = int(fields["MainPID"])
        except (KeyError, ValueError) as exc:
            raise DevFlowError("Service process status is invalid") from exc
        pointer = self.root.parent / (self.root.name + "-releases") / "current"
        release = os.readlink(pointer) if pointer.is_symlink() else None
        if release is not None and not SHA.fullmatch(release):
            raise DevFlowError("Active release pointer is invalid")
        if release is not None:
            marker = pointer.parent / release / ".devflow-release"
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != release + "\n":
                raise DevFlowError("Active release marker is invalid")
        try:
            with httpx.Client(timeout=7, follow_redirects=False) as client:
                discovery = client.get(DISCOVERY)
                denied = client.post(RESOURCE, json={})
            codes = [discovery.status_code, denied.status_code]
            boundary_ok = (codes == [200, 401] and
                           discovery.json().get("resource") == RESOURCE and
                           "bearer" in denied.headers.get("www-authenticate", "").lower())
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            codes, boundary_ok = [None, None], False
        return {"service": SERVICE, "active": fields.get("ActiveState") == "active",
                "pid": pid if pid > 1 else None, "release_sha": release,
                "public_status": codes, "oauth_boundary_ok": boundary_ok}

    def resources(self):
        try:
            memory = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    memory[key] = int(value.strip().split()[0]) * 1024
            if set(memory) != {"MemTotal", "MemAvailable"}:
                raise ValueError("Incomplete memory data")
            disk = os.statvfs(self.root)
            loads = os.getloadavg()
            return {"memory_total_bytes": memory["MemTotal"],
                    "memory_available_bytes": memory["MemAvailable"],
                    "project_disk_total_bytes": disk.f_frsize * disk.f_blocks,
                    "project_disk_available_bytes": disk.f_frsize * disk.f_bavail,
                    "load_average": [round(value, 2) for value in loads]}
        except (OSError, ValueError, IndexError) as exc:
            raise DevFlowError("Local resource inspection failed") from exc

    def git(self):
        branch = self._run("git", "branch", "--show-current")
        head = self._run("git", "rev-parse", "--verify", "HEAD")
        if not SHA.fullmatch(head):
            raise DevFlowError("Git HEAD is invalid")
        changed = self._run("git", "status", "--porcelain", "--untracked-files=normal")
        return {"branch": branch, "head_sha": head, "dirty": bool(changed)}
