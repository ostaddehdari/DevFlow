"""Single-host continuity ledger. SQLite transactions fence cooperative local callers.

This records intent/result; it never runs external effects and cannot fence an
external service that does not itself check the fencing token/idempotency key.
"""

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .contracts import Contracts, DevFlowError, read_json
from .design import validate_design


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _date(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise DevFlowError(f"Invalid {label}")
    return value


def _safe(value):
    # Same inline-secret guard as registry, without a circular import.
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[^a-z0-9]", "", key.lower()) in {
                "password", "passwd", "token", "accesstoken", "refreshtoken", "bottoken",
                "apikey", "privatekey", "secret", "clientsecret"}:
                raise DevFlowError("Inline credential field rejected")
            _safe(item)
    elif isinstance(value, list):
        for item in value:
            _safe(item)


class Continuity:
    def __init__(self, root: Path, contracts: Contracts, clock=None):
        self.root = root.resolve()
        self.contracts = contracts
        self.clock = clock or time.time
        folder = self.root / ".ai-project"
        settings_path = folder / "project.settings.json"
        if not settings_path.resolve().is_relative_to(self.root):
            raise DevFlowError("Settings escape project root")
        settings = read_json(settings_path)
        contracts.validate(settings, "project.settings.schema.json")
        self.project_id = settings["project"]["id"]
        self.path = self.root / ".devflow" / "continuity.sqlite3"
        self._paths()

    def _paths(self):
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise DevFlowError("Continuity storage cannot be a symlink")
        if not self.path.resolve().is_relative_to(self.root):
            raise DevFlowError("Continuity storage escapes project root")

    @contextmanager
    def _connection(self, write=False, initialize=False):
        self._paths()
        if not initialize and not self.path.is_file():
            raise DevFlowError("Continuity is not initialized; run continuity init")
        connection = None
        try:
            uri = "file:" + quote(str(self.path), safe="/") + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initialize:
                meta = dict(connection.execute("SELECT key,value FROM metadata"))
                if meta.get("schema_version") != "1" or meta.get("project_id") != self.project_id or meta.get("root") != str(self.root):
                    raise DevFlowError("Continuity storage identity/version mismatch")
                if write:
                    now = self._now()
                    if now < float(meta["last_clock"]):
                        raise DevFlowError("Clock moved backwards; inspect clock before resuming")
                    connection.execute("UPDATE metadata SET value=? WHERE key='last_clock'", (str(now),))
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise DevFlowError("Continuity database unavailable or invalid; data was not reset") from exc
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def _now(self):
        now = self.clock()
        if not isinstance(now, (float, int)) or not math.isfinite(now) or now < 0:
            raise DevFlowError("Invalid clock")
        return now

    def initialize(self):
        self._paths()
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            # Existing nonempty storage is validated, never overwritten or reset.
            if self.path.stat().st_size:
                with self._connection(write=True) as connection:
                    self._event_rows(connection)
                return {"ok": True, "changed": False, "project_id": self.project_id}
        with self._connection(write=True, initialize=True) as connection:
            statements = [
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS leases (fence INTEGER PRIMARY KEY, execution_id TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, started REAL NOT NULL, heartbeat REAL NOT NULL, expires REAL NOT NULL, ttl INTEGER NOT NULL, status TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS operations (operation_key TEXT PRIMARY KEY, intent_hash TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, fence INTEGER, evidence TEXT, updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS checkpoints (sequence INTEGER PRIMARY KEY AUTOINCREMENT, checkpoint_id TEXT UNIQUE NOT NULL, snapshot TEXT NOT NULL, created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, document TEXT NOT NULL)",
            ]
            for statement in statements:
                connection.execute(statement)
            existing = dict(connection.execute("SELECT key,value FROM metadata"))
            if existing:
                if existing.get("schema_version") != "1" or existing.get("project_id") != self.project_id or existing.get("root") != str(self.root):
                    raise DevFlowError("Continuity storage identity/version mismatch")
                return {"ok": True, "changed": False, "project_id": self.project_id}
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("schema_version", "1"), ("project_id", self.project_id), ("root", str(self.root)),
                ("fence", "0"), ("last_clock", str(self._now()))])
            self._event(connection, "continuity.initialized", {})
        return {"ok": True, "changed": True, "project_id": self.project_id}

    def _event(self, connection, kind, payload, lease=None):
        event = {"event_id": "EV-" + uuid.uuid4().hex, "event_type": kind,
                 "project_id": self.project_id, "severity": "INFO", "occurred_at": _date(self._now()),
                 "payload": payload}
        if lease is not None:
            event["execution_id"] = lease["execution_id"]
        _safe(event)
        self.contracts.validate(event, "event.schema.json")
        connection.execute("INSERT INTO events(document) VALUES (?)", (_json(event),))

    def _event_rows(self, connection):
        return connection.execute("SELECT count(*) FROM events").fetchone()[0]

    def _latest_lease(self, connection):
        row = connection.execute("SELECT * FROM leases ORDER BY fence DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def _execution(self, lease):
        value = {"execution_id": lease["execution_id"], "owner": lease["owner"],
                 "status": lease["status"], "started_at": _date(lease["started"]),
                 "heartbeat": _date(lease["heartbeat"]), "expires_after_seconds": lease["ttl"],
                 "resume_required": lease["status"] == "INTERRUPTED"}
        self.contracts.validate(value, "execution.schema.json")
        return value

    def _owned(self, connection, owner, execution_id, fence):
        if type(fence) is not int or fence < 1:
            raise DevFlowError("Invalid fencing token")
        lease = self._latest_lease(connection)
        if (not lease or lease["owner"] != owner or lease["execution_id"] != execution_id or
            lease["fence"] != fence or lease["status"] != "ACTIVE" or lease["expires"] <= self._now()):
            raise DevFlowError("Lease is stale, expired or owned by another execution")
        return lease

    @contextmanager
    def _fenced(self, owner, execution_id, fence):
        with self._connection(write=True) as connection:
            lease = self._owned(connection, owner, execution_id, fence)
            yield connection, lease
            current = self._latest_lease(connection)
            if (current["owner"] != owner or current["execution_id"] != execution_id or
                current["fence"] != fence or current["expires"] <= self._now()):
                raise DevFlowError("Lease expired before transaction commit; changes rolled back")

    def acquire(self, owner, ttl=600):
        _identifier(owner, "owner")
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise DevFlowError("Lease seconds must be an integer between 60 and 86400")
        with self._connection(write=True) as connection:
            now = self._now()
            old = self._latest_lease(connection)
            if old and old["status"] == "ACTIVE":
                if old["expires"] > now:
                    raise DevFlowError("A live lease already owns this project")
                connection.execute("UPDATE leases SET status='INTERRUPTED' WHERE fence=?", (old["fence"],))
                connection.execute("UPDATE operations SET status='UNKNOWN',updated=? WHERE status='STARTED' AND fence=?", (now, old["fence"]))
                self._event(connection, "lease.expired", {"fence": old["fence"]}, old)
            fence = int(connection.execute("SELECT value FROM metadata WHERE key='fence'").fetchone()[0]) + 1
            execution_id = "EX-" + uuid.uuid4().hex
            connection.execute("UPDATE metadata SET value=? WHERE key='fence'", (str(fence),))
            connection.execute("INSERT INTO leases VALUES (?,?,?,?,?,?,?,?)", (fence, execution_id, owner, now, now, now + ttl, ttl, "ACTIVE"))
            lease = self._latest_lease(connection)
            self._event(connection, "lease.acquired", {"fence": fence}, lease)
            return {"ok": True, "fence": fence, "execution": self._execution(lease)}

    def renew(self, owner, execution_id, fence):
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            now = self._now()
            connection.execute("UPDATE leases SET heartbeat=?,expires=? WHERE fence=?", (now, now + lease["ttl"], fence))
            self._event(connection, "lease.renewed", {"fence": fence}, lease)
            return {"ok": True, "fence": fence, "execution": self._execution(self._latest_lease(connection))}

    def release(self, owner, execution_id, fence):
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            if connection.execute("SELECT 1 FROM operations WHERE status='STARTED' AND fence=?", (fence,)).fetchone():
                raise DevFlowError("Cannot release with a started operation; record its outcome first")
            connection.execute("UPDATE leases SET status='DONE' WHERE fence=?", (fence,))
            self._event(connection, "lease.released", {"fence": fence}, lease)
            return {"ok": True, "fence": fence, "execution": self._execution(self._latest_lease(connection))}

    def _operation(self, connection, key):
        row = connection.execute("SELECT * FROM operations WHERE operation_key=?", (key,)).fetchone()
        if not row:
            raise DevFlowError("Unknown operation key")
        item = dict(row)
        item["intent"] = json.loads(item["intent"])
        return item

    def prepare(self, owner, execution_id, fence, key, intent):
        _identifier(key, "operation key")
        if not isinstance(intent, dict) or not intent:
            raise DevFlowError("Intent must be a nonempty JSON object")
        _safe(intent)
        try:
            serialized = _json(intent)
        except (ValueError, TypeError) as exc:
            raise DevFlowError("Intent is not valid JSON") from exc
        if len(serialized.encode()) > 65536:
            raise DevFlowError("Intent exceeds 64 KiB")
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            found = connection.execute("SELECT intent_hash FROM operations WHERE operation_key=?", (key,)).fetchone()
            if found:
                if found[0] != digest:
                    raise DevFlowError("Operation key is already bound to another intent")
                return {"ok": True, "changed": False, "operation": self._operation(connection, key)}
            connection.execute("INSERT INTO operations(operation_key,intent_hash,intent,status,updated) VALUES (?,?,?,'PREPARED',?)", (key, digest, serialized, self._now()))
            self._event(connection, "operation.prepared", {"operation_key": key, "intent_hash": digest}, lease)
            return {"ok": True, "changed": True, "operation": self._operation(connection, key)}

    def start(self, owner, execution_id, fence, key):
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            item = self._operation(connection, key)
            if item["status"] != "PREPARED":
                raise DevFlowError("Operation is not prepared; retry is not permitted")
            if connection.execute("SELECT 1 FROM operations WHERE status IN ('STARTED','UNKNOWN','FAILED')").fetchone():
                raise DevFlowError("An unresolved operation blocks new effects")
            connection.execute("UPDATE operations SET status='STARTED',attempt=attempt+1,fence=?,updated=? WHERE operation_key=?", (fence, self._now(), key))
            self._event(connection, "operation.started", {"operation_key": key, "fence": fence}, lease)
            return {"ok": True, "operation": self._operation(connection, key)}

    def finish(self, owner, execution_id, fence, key, outcome, evidence):
        if outcome not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise DevFlowError("Invalid operation outcome")
        self._evidence(evidence)
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            item = self._operation(connection, key)
            if item["status"] != "STARTED" or item["fence"] != fence:
                raise DevFlowError("Only the owning execution may finish a started operation")
            connection.execute("UPDATE operations SET status=?,evidence=?,updated=? WHERE operation_key=?", (outcome, evidence, self._now(), key))
            self._event(connection, "operation.finished", {"operation_key": key, "outcome": outcome}, lease)
            return {"ok": True, "operation": self._operation(connection, key)}

    def _evidence(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise DevFlowError("A nonempty evidence reference of at most 4096 characters is required")

    def resolve(self, owner, execution_id, fence, key, outcome, evidence):
        """Explicit operator assertion backed by an external evidence reference."""
        if outcome not in {"SUCCEEDED", "FAILED", "NO_EFFECT"}:
            raise DevFlowError("Resolution must be SUCCEEDED, FAILED or NO_EFFECT")
        self._evidence(evidence)
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            item = self._operation(connection, key)
            if item["status"] not in {"UNKNOWN", "FAILED"}:
                raise DevFlowError("Only unknown/failed operations may be explicitly reconciled")
            status = "PREPARED" if outcome == "NO_EFFECT" else outcome
            connection.execute("UPDATE operations SET status=?,evidence=?,updated=? WHERE operation_key=?", (status, evidence, self._now(), key))
            self._event(connection, "operation.resolved", {"operation_key": key, "operator_assertion": outcome}, lease)
            return {"ok": True, "operation": self._operation(connection, key)}

    def _git(self):
        try:
            def git(*args):
                return subprocess.check_output(["git", "-C", str(self.root), *args], text=True,
                                               stderr=subprocess.DEVNULL, timeout=5).strip()
            head = git("rev-parse", "--verify", "HEAD")
            branch = git("branch", "--show-current")
            dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
            return {"head_sha": head, "branch": branch, "dirty": dirty}
        except (OSError, subprocess.SubprocessError) as exc:
            raise DevFlowError("Cannot observe project Git state") from exc

    def _documents(self):
        folder = self.root / ".ai-project"
        result, hashes = {}, {}
        for name, schema in (("state.json", "state.schema.json"),
                             ("resume.bundle.json", "resume.bundle.schema.json"),
                             ("project.design.json", "project.design.schema.json"),
                             ("project.settings.json", "project.settings.schema.json")):
            path = folder / name
            if not path.resolve().is_relative_to(self.root):
                raise DevFlowError("Continuity document escapes project root")
            document = read_json(path)
            self.contracts.validate(document, schema)
            _safe(document)
            identity = document["project"]["id"] if name in {"project.design.json", "project.settings.json"} else document["project_id"]
            if identity != self.project_id:
                raise DevFlowError("Continuity document identity mismatch")
            if name == "project.design.json":
                validate_design(document, self.contracts, self.project_id)
            result[name] = document
            hashes[name] = hashlib.sha256(_json(document).encode()).hexdigest()
        current = result["state.json"]["current"]
        stages = result["project.design.json"]["stages"]
        stage = next((s for s in stages if s["id"] == current["stage_id"]), None)
        if stage is None or not any(w["id"] == current["work_id"] for w in stage["works"]):
            raise DevFlowError("Current work is not bound to the project design")
        return result, hashes

    def checkpoint(self, owner, execution_id, fence):
        with self._fenced(owner, execution_id, fence) as (connection, lease):
            if connection.execute("SELECT 1 FROM operations WHERE status IN ('STARTED','UNKNOWN')").fetchone():
                raise DevFlowError("Unresolved operations prevent a safe checkpoint")
            git = self._git()
            if git["dirty"] or not git["branch"]:
                raise DevFlowError("A safe checkpoint requires a clean named Git branch")
            documents, hashes = self._documents()
            if documents["state.json"].get("execution") is not None:
                raise DevFlowError("Tracked state contains another execution; reconcile it first")
            state = copy.deepcopy(documents["state.json"])
            resume = copy.deepcopy(documents["resume.bundle.json"])
            if (state["current"]["stage_id"] != resume["current"]["stage_id"] or
                state["current"]["work_id"] != resume["current"]["work_id"]):
                raise DevFlowError("Tracked state and resume identities diverge")
            checkpoint_id = "CP-" + uuid.uuid4().hex
            now = _date(self._now())
            completed = [row[0] for row in connection.execute("SELECT operation_key FROM operations WHERE status IN ('SUCCEEDED','FAILED') ORDER BY operation_key")]
            checkpoint = {"schema_version": "1.0", "checkpoint_id": checkpoint_id,
                "project_id": self.project_id, "created_at": now, "reason": "manual", "safe_to_resume": True,
                "snapshot": {"stage_id": state["current"]["stage_id"], "work_id": state["current"]["work_id"],
                    "git_head_sha": git["head_sha"], "server_deployments": {},
                    "last_action": "runtime_checkpoint", "next_action": state.get("next_action"),
                    "fence": fence, "source_document_hashes": hashes},
                "do_not_repeat": list(dict.fromkeys(resume["do_not_repeat"] + completed))}
            state.update(git={"branch": git["branch"], "head_sha": git["head_sha"]},
                         execution=self._execution(lease), latest_checkpoint_id=checkpoint_id, updated_at=now)
            resume.update(git=dict(state["git"]), latest_checkpoint_id=checkpoint_id, generated_at=now,
                          do_not_repeat=checkpoint["do_not_repeat"], human_decision_required=False)
            bundle = {"checkpoint": checkpoint, "state": state, "resume_bundle": resume,
                      "git": git, "source_document_hashes": hashes}
            self._validate_snapshot(bundle)
            if self._git() != git:
                raise DevFlowError("Git changed while checkpoint was being captured")
            self._owned(connection, owner, execution_id, fence)
            # The three canonical documents and the checkpoint-created event commit together.
            connection.execute("INSERT INTO checkpoints(checkpoint_id,snapshot,created) VALUES (?,?,?)", (checkpoint_id, _json(bundle), self._now()))
            self._event(connection, "checkpoint.created", {"checkpoint_id": checkpoint_id}, lease)
            return {"ok": True, "checkpoint_id": checkpoint_id, "snapshot": bundle}

    def _validate_snapshot(self, bundle):
        for key, schema in (("checkpoint", "checkpoint.schema.json"), ("state", "state.schema.json"),
                            ("resume_bundle", "resume.bundle.schema.json")):
            self.contracts.validate(bundle[key], schema)
            _safe(bundle[key])
            if bundle[key]["project_id"] != self.project_id:
                raise DevFlowError("Snapshot project identity mismatch")
        cp = bundle["checkpoint"]
        if (cp["checkpoint_id"] != bundle["state"]["latest_checkpoint_id"] or
            cp["checkpoint_id"] != bundle["resume_bundle"]["latest_checkpoint_id"] or
            cp["snapshot"]["git_head_sha"] != bundle["git"]["head_sha"] or not cp["safe_to_resume"]):
            raise DevFlowError("Snapshot identity or Git binding is inconsistent")
        expected_git = {"branch": bundle["git"]["branch"], "head_sha": bundle["git"]["head_sha"]}
        if bundle["state"]["git"] != expected_git or bundle["resume_bundle"]["git"] != expected_git:
            raise DevFlowError("Snapshot document Git bindings diverge")
        for key in ("state", "resume_bundle"):
            for field in ("stage_id", "work_id"):
                if bundle[key]["current"][field] != cp["snapshot"][field]:
                    raise DevFlowError("Snapshot document work identities diverge")

    def _snapshot(self, connection, checkpoint_id=None):
        if checkpoint_id:
            row = connection.execute("SELECT snapshot FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
        else:
            row = connection.execute("SELECT snapshot FROM checkpoints ORDER BY sequence DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            self._validate_snapshot(value)
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise DevFlowError("Invalid checkpoint snapshot; inspection required") from exc

    def snapshot(self, checkpoint_id=None):
        with self._connection() as connection:
            value = self._snapshot(connection, checkpoint_id)
            if value is None:
                raise DevFlowError("Checkpoint not found")
            return {"ok": True, "snapshot": value}

    def _status(self, connection):
        lease = self._latest_lease(connection)
        effective = copy.deepcopy(lease)
        if effective and effective["status"] == "ACTIVE" and effective["expires"] <= self._now():
            effective["status"] = "INTERRUPTED"
        operations = [dict(row) for row in connection.execute("SELECT operation_key,status,attempt,fence FROM operations ORDER BY operation_key")]
        for item in operations:
            if item["status"] == "STARTED" and (not effective or effective["status"] != "ACTIVE" or item["fence"] != effective["fence"]):
                item["status"] = "UNKNOWN"
        cp = self._snapshot(connection)
        last_clock = float(connection.execute("SELECT value FROM metadata WHERE key='last_clock'").fetchone()[0])
        return {"ok": True, "initialized": True, "project_id": self.project_id,
                "lease": {"fence": effective["fence"], "execution": self._execution(effective)} if effective else None,
                "operations": operations, "latest_checkpoint_id": cp["checkpoint"]["checkpoint_id"] if cp else None,
                "clock_rollback": self._now() < last_clock, "event_count": self._event_rows(connection),
                "external_effect_runner": False}

    def status(self):
        if not self.path.exists():
            return {"ok": True, "initialized": False, "project_id": self.project_id, "external_effect_runner": False}
        with self._connection() as connection:
            return self._status(connection)

    def resume_plan(self):
        with self._connection() as connection:
            status = self._status(connection)
            snapshot = self._snapshot(connection)
            reasons = []
            if status["clock_rollback"]:
                reasons.append("Clock moved backwards")
            if any(item["status"] in {"STARTED", "UNKNOWN"} for item in status["operations"]):
                reasons.append("Operation result requires explicit evidence reconciliation")
            if any(item["status"] == "FAILED" for item in status["operations"]):
                reasons.append("A failed operation needs an operator decision")
            git = self._git()
            if git["dirty"]:
                reasons.append("Working tree contains uncommitted changes")
            if snapshot:
                _, hashes = self._documents()
                if git["head_sha"] != snapshot["git"]["head_sha"] or git["branch"] != snapshot["git"]["branch"]:
                    reasons.append("Git differs from checkpoint")
                if hashes != snapshot["source_document_hashes"]:
                    reasons.append("Project documents differ from checkpoint")
            live = bool(status.get("lease") and status["lease"]["execution"]["status"] == "ACTIVE")
            decision = "HUMAN_DECISION" if reasons else ("WAIT_FOR_OWNER" if live else ("READY" if snapshot else "CREATE_CHECKPOINT"))
            return {"ok": True, "decision": decision, "reasons": reasons, "observed_git": git,
                    "checkpoint_id": status["latest_checkpoint_id"], "human_decision_required": bool(reasons),
                    "do_not_repeat": [item["operation_key"] for item in status["operations"] if item["status"] in {"SUCCEEDED", "FAILED"}],
                    "prepared_operations": [item["operation_key"] for item in status["operations"] if item["status"] == "PREPARED"],
                    "automatic_execution": False}


def from_registry(registry, project_id=None):
    data = registry.read()
    selected = project_id if project_id is not None else data.get("active_project_id")
    entry = next((p for p in data["projects"] if p["project_id"] == selected), None)
    if entry is None:
        raise DevFlowError("Unknown project")
    path = Path(entry["settings_path"])
    if not path.is_absolute() or path.name != "project.settings.json" or path.parent.name != ".ai-project":
        raise DevFlowError("Project must have an absolute registered settings binding")
    store = Continuity(path.parent.parent, registry.contracts)
    if store.project_id != selected:
        raise DevFlowError("Registered project identity mismatch")
    return store
