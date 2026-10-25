"""Local-only Draft 2020-12 schema validation; no remote schema fetching."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource


class DevFlowError(ValueError):
    """A user-actionable error with no input values included in its message."""


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DevFlowError(f"Cannot read JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise DevFlowError(f"Expected JSON object: {path.name}")
    return value


def _no_remote(uri: str):
    raise NoSuchResource(ref=uri)


class Contracts:
    def __init__(self, directory: Path):
        self.schemas = {}
        resources = []
        for path in sorted(directory.glob("*.schema.json")):
            schema = read_json(path)
            Draft202012Validator.check_schema(schema)
            self.schemas[path.name] = schema
            resources.append((schema["$id"], Resource.from_contents(schema)))
        if not self.schemas:
            raise DevFlowError("No contracts found; pass --contracts /path/to/contracts")
        self.registry = Registry(retrieve=_no_remote).with_resources(resources)

    def validate(self, value: dict, name: str) -> None:
        if name not in self.schemas:
            raise DevFlowError(f"Unknown contract: {name}")
        validator = Draft202012Validator(
            self.schemas[name], registry=self.registry, format_checker=FormatChecker()
        )
        for error in validator.iter_errors(value):
            location = ".".join(map(str, error.absolute_path)) or "<root>"
            # jsonschema's raw message may contain credentials; only report location/rule.
            raise DevFlowError(f"{name}: {location} violates {error.validator}")
