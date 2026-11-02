"""Enrollment is exact, viewer-only, and preserves existing principals."""

import copy
import json
from pathlib import Path

import pytest

from devflow_core.contracts import Contracts, DevFlowError
from scripts import enroll_chatgpt_user as enrollment


def test_enroll_exact_user_preserves_machine_grant(tmp_path, monkeypatch):
    path = tmp_path / "network.json"
    original = {"schema_version": "1.0", "issuer": "https://dev-fqzv1aqwljc7mhwz.us.auth0.com/",
                "resource": "https://srun.ir/mcpdevflow", "jwks_path": "/etc/devflow/oauth-jwks.json",
                "principals": [{"subject": "m2m@clients", "client_id": "machineclient1",
                                "role": "viewer", "projects": ["devflow"]}]}
    path.write_text(json.dumps(original))
    path.chmod(0o600)

    class Policy:
        def __init__(self, *_):
            self.contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts")

        def config(self):
            return json.loads(path.read_text())

    monkeypatch.setattr(enrollment, "NetworkPolicy", Policy)
    monkeypatch.setattr(enrollment, "private_json", lambda *_: copy.deepcopy(original))
    root = Path(__file__).resolve().parents[1]
    subject = "auth0|user-42"
    result = enrollment.enroll(root, path, subject, "chatgptclient1")
    assert result["changed"] is True
    saved = json.loads(path.read_text())
    assert saved["principals"][0] == original["principals"][0]
    assert saved["principals"][1] == {"subject": subject, "client_id": "chatgptclient1",
                                      "role": "viewer", "projects": ["devflow"]}
    assert path.stat().st_mode & 0o777 == 0o600
    assert enrollment.enroll(root, path, subject, "chatgptclient1")["changed"] is False
    with pytest.raises(DevFlowError, match="exact user subject"):
        enrollment.enroll(root, path, "m2m@clients", "chatgptclient1")
