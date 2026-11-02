"""Add one exact Auth0 user/client grant to the private MCP resource policy."""

import argparse
import copy
import json
import os
import re
import tempfile
from pathlib import Path

from devflow_core.contracts import Contracts, DevFlowError
from devflow_core.network import NetworkPolicy, private_json, MAX_CONFIG


CONFIG = Path("/etc/devflow/network.json")


def enroll(root, path, subject, client_id):
    if os.geteuid() != 0:
        raise DevFlowError("A local root operator is required")
    if (not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\|[A-Za-z0-9._~:@+-]{1,220}", subject) or
        not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", client_id)):
        raise DevFlowError("An exact user subject and OAuth client ID are required")
    root, path = Path(root).resolve(), Path(path)
    policy = NetworkPolicy(root, Contracts(root / "contracts"), path)
    document = policy.config()
    original = copy.deepcopy(document)
    if document["issuer"] != "https://dev-fqzv1aqwljc7mhwz.us.auth0.com/":
        raise DevFlowError("The configured Auth0 issuer differs from the reviewed tenant")
    grant = {"subject": subject, "client_id": client_id,
             "role": "viewer", "projects": ["devflow"]}
    existing = [p for p in document["principals"] if
                p["subject"] == subject and p["client_id"] == client_id]
    if existing:
        if existing != [grant]:
            raise DevFlowError("An existing user grant differs; inspect it before changing")
        return {"ok": True, "changed": False, "subject": subject}
    document["principals"].append(grant)
    policy.contracts.validate(document, "network.schema.json")
    if len(json.dumps(document).encode()) > MAX_CONFIG:
        raise DevFlowError("OAuth policy exceeds the configured limit")
    current = private_json(path, MAX_CONFIG)
    if current != original:
        raise DevFlowError("OAuth policy changed during enrollment")
    descriptor, temporary = tempfile.mkstemp(prefix=".network-user-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    policy.config()
    return {"ok": True, "changed": True, "subject": subject}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args()
    print(json.dumps(enroll(Path(__file__).resolve().parents[1], CONFIG,
                            args.subject, args.client_id)))
