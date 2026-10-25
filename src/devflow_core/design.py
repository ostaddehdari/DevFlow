"""Validate design identities and dependency graphs against the Stage 0 contract."""

from .contracts import Contracts, DevFlowError


def validate_design(value: dict, contracts: Contracts, project_id: str) -> None:
    contracts.validate(value, "project.design.schema.json")
    if value["project"]["id"] != project_id:
        raise DevFlowError("Project design identity mismatch")

    def unique(entries, key, label):
        ids = [entry[key] for entry in entries]
        if len(ids) != len(set(ids)):
            raise DevFlowError(f"Duplicate {label}")
        return {entry[key]: entry for entry in entries}

    unique(value["objectives"], "id", "objective ID")
    stages = unique(value["stages"], "id", "stage ID")
    unique(value["stages"], "number", "stage number")
    works = unique([w for s in stages.values() for w in s["works"]], "id", "work ID")
    owners = {w["id"]: s["id"] for s in stages.values() for w in s["works"]}

    def check_graph(nodes, dependency_key, label):
        seen, active = set(), set()

        def visit(key):
            if key in active:
                raise DevFlowError(f"Cycle in {label} dependencies")
            if key in seen:
                return
            active.add(key)
            for dependency in nodes[key].get(dependency_key, []):
                if dependency not in nodes:
                    raise DevFlowError(f"Unknown {label} dependency")
                visit(dependency)
                if nodes[key]["status"] == "COMPLETED" and nodes[dependency]["status"] != "COMPLETED":
                    raise DevFlowError(f"Completed {label} has incomplete dependency")
            active.remove(key)
            seen.add(key)

        for key in nodes:
            visit(key)

    check_graph(stages, "depends_on", "stage")
    check_graph(works, "dependencies", "work")
    for stage in stages.values():
        unique(stage["works"], "number", "work number")
        if stage["status"] == "COMPLETED" and any(w["status"] != "COMPLETED" for w in stage["works"]):
            raise DevFlowError("Completed stage contains incomplete work")
        ancestors, pending = set(), list(stage["depends_on"])
        while pending:
            dependency = pending.pop()
            if dependency not in ancestors:
                ancestors.add(dependency)
                pending.extend(stages[dependency]["depends_on"])
        for work in stage["works"]:
            for dependency in work.get("dependencies", []):
                if owners[dependency] != stage["id"] and owners[dependency] not in ancestors:
                    raise DevFlowError("Work dependency is outside declared prerequisite stages")
    if value["validation"]["ready_for_execution"] and (
        value["validation"]["design_status"] == "BLOCKED" or value["validation"]["blocking_questions"]
    ):
        raise DevFlowError("Blocked design cannot be ready for execution")
