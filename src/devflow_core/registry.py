"""Schema-validated registry with Linux advisory locking and atomic replacement."""

import copy
import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .contracts import Contracts, DevFlowError, read_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reject_credentials(value) -> None:
    """Reject common inline-secret fields. This is not a general secret scanner."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in {
                "password", "passwd", "token", "accesstoken", "refreshtoken",
                "bottoken", "apikey", "privatekey", "secret", "clientsecret",
            }:
                raise DevFlowError("Inline credential field rejected; use credential_ref")
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)


class ControlRegistry:
    def __init__(self, path: Path, contracts: Contracts):
        self.path = path.absolute()
        self.contracts = contracts

    def validate(self, data: dict) -> None:
        self.contracts.validate(data, "registry.schema.json")
        reject_credentials(data)
        for collection, key in (("projects", "project_id"), ("servers", "server_id")):
            ids = [entry[key] for entry in data[collection]]
            if len(ids) != len(set(ids)):
                raise DevFlowError(f"Duplicate {key} in registry")
        active = data.get("active_project_id")
        if active is not None and active not in {p["project_id"] for p in data["projects"]}:
            raise DevFlowError("Active project does not exist")

    def read(self) -> dict:
        if not self.path.exists():
            raise DevFlowError("Registry is not initialized; run devflow init")
        data = read_json(self.path)
        self.validate(data)
        return data

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(self, data: dict) -> None:
        self.validate(data)
        if self.path.is_symlink():
            raise DevFlowError("Registry must not be a symbolic link")
        fd, name = tempfile.mkstemp(prefix=".registry-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            dir_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def register(self, settings_path: Path, make_active: bool = False) -> dict:
        path = settings_path.resolve(strict=True)
        if path.name != "project.settings.json" or path.parent.name != ".ai-project":
            raise DevFlowError("Settings must be .ai-project/project.settings.json")
        settings = read_json(path)
        self.contracts.validate(settings, "project.settings.schema.json")
        reject_credentials(settings)
        servers = list(settings["servers"].values())
        ids = [s["server_id"] for s in servers]
        if len(ids) != len(set(ids)):
            raise DevFlowError("Duplicate server_id in project settings")
        project_id = settings["project"]["id"]
        repository = settings["repository"]
        entry = {
            "project_id": project_id,
            "repository": f"{repository['owner']}/{repository['name']}",
            "status": settings["project"]["status"],
            "settings_path": str(path),
        }
        with self._locked():
            data = self.read() if self.path.exists() else {
                "schema_version": "1.0", "active_project_id": None,
                "projects": [], "servers": [],
            }
            previous = copy.deepcopy(data)
            by_id = {p["project_id"]: p for p in data["projects"]}
            old = by_id.get(project_id)
            old_path = Path(old["settings_path"]) if old else None
            if old_path is not None and not old_path.is_absolute():
                old_path = self.path.parent.parent / old_path
            if old and (old_path.resolve() != path or
                        old["repository"] != entry["repository"]):
                raise DevFlowError("Project ID already bound to another path/repository")
            if old:
                entry["last_activity_at"] = old.get("last_activity_at")
            by_id[project_id] = entry
            by_server = {s["server_id"]: s for s in data["servers"]}
            for server in servers:
                if server["server_id"] in by_server and by_server[server["server_id"]] != server:
                    raise DevFlowError("Server ID conflict; existing binding was preserved")
                by_server[server["server_id"]] = server
            data["projects"] = sorted(by_id.values(), key=lambda p: p["project_id"])
            data["servers"] = sorted(by_server.values(), key=lambda s: s["server_id"])
            if make_active or data.get("active_project_id") is None:
                data["active_project_id"] = project_id
            changed = data != previous
            if changed:
                entry["last_activity_at"] = utc_now()
                self._write(data)
        return {"ok": True, "changed": changed, "project_id": project_id,
                "projects": len(data["projects"]), "servers": len(data["servers"])}

    def activate(self, project_id: str) -> dict:
        with self._locked():
            data = self.read()
            if project_id not in {p["project_id"] for p in data["projects"]}:
                raise DevFlowError("Unknown project")
            if data.get("active_project_id") != project_id:
                data["active_project_id"] = project_id
                self._write(data)
        return {"ok": True, "active_project_id": project_id}

    def project_status(self, project_id: str | None = None) -> dict:
        data = self.read()
        selected = project_id if project_id is not None else data.get("active_project_id")
        entry = next((p for p in data["projects"] if p["project_id"] == selected), None)
        if entry is None:
            raise DevFlowError("Unknown project")
        directory = Path(entry["settings_path"]).parent
        if directory.name != ".ai-project":
            raise DevFlowError("Invalid registered settings directory")
        root = directory.parent.resolve()
        result = {"project": entry, "documents": {}, "issues": [],
                  "deployment_verified": False}
        try:
            result["observed_git_head"] = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                timeout=5, stderr=subprocess.DEVNULL, text=True,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            result["observed_git_head"] = None
            result["issues"].append("Local Git HEAD could not be observed")
        for filename, schema in (
            ("project.settings.json", "project.settings.schema.json"),
            ("state.json", "state.schema.json"),
            ("resume.bundle.json", "resume.bundle.schema.json"),
        ):
            target = directory / filename
            if not target.resolve().is_relative_to(root):
                raise DevFlowError("Project document escapes its registered root")
            if not target.exists():
                result["issues"].append(f"Missing {filename}")
                continue
            document = read_json(target)
            self.contracts.validate(document, schema)
            reject_credentials(document)
            document_id = (document["project"]["id"] if filename == "project.settings.json"
                           else document["project_id"])
            if document_id != selected:
                raise DevFlowError("Project document identity mismatch")
            # Read only: the registry never advances state or marks a deployment healthy.
            result["documents"][filename] = document
        if not (directory / "project.design.json").exists():
            result["issues"].append("Project design is not present")
        state = result["documents"].get("state.json", {})
        checkpoint_id = state.get("latest_checkpoint_id")
        if checkpoint_id:
            if not re.fullmatch(r"CP-[A-Za-z0-9._-]+", checkpoint_id):
                raise DevFlowError("Invalid checkpoint identifier")
            checkpoint_path = directory / "checkpoints" / f"{checkpoint_id}.json"
            if not checkpoint_path.resolve().is_relative_to(root):
                raise DevFlowError("Checkpoint escapes its registered root")
            if not checkpoint_path.is_file():
                result["issues"].append("Referenced checkpoint is not present")
            else:
                checkpoint = read_json(checkpoint_path)
                self.contracts.validate(checkpoint, "checkpoint.schema.json")
                reject_credentials(checkpoint)
                if checkpoint["project_id"] != selected or checkpoint["checkpoint_id"] != checkpoint_id:
                    raise DevFlowError("Checkpoint identity mismatch")
                result["checkpoint"] = checkpoint
        result["ok"] = not result["issues"]
        return result
