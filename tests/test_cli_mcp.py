import asyncio
import importlib.util
import json
import os
import subprocess
import sys

from conftest import ROOT


def test_cli_init_validate_and_errors(tmp_path, settings_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    command = [sys.executable, "-m", "devflow_core.cli", "--root", str(ROOT),
               "--registry", str(tmp_path / "registry.json")]
    missing = subprocess.run(command + ["validate"], env=env, capture_output=True, text=True)
    assert missing.returncode == 1 and not missing.stdout
    assert json.loads(missing.stderr)["ok"] is False
    registered = subprocess.run(command + ["init", "--settings", str(settings_path)],
                                env=env, capture_output=True, text=True)
    assert registered.returncode == 0 and json.loads(registered.stdout)["changed"]
    valid = subprocess.run(command + ["validate"], env=env, capture_output=True, text=True)
    assert valid.returncode == 0 and json.loads(valid.stdout)["ok"]


def test_real_stdio_mcp(registry, settings_path):
    registry.register(settings_path)
    spec = importlib.util.spec_from_file_location("smoke_mcp", ROOT / "scripts/smoke_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    asyncio.run(asyncio.wait_for(module.check(ROOT, registry.path), timeout=45))
