"""Validated execution plans and resumable Beads/convoy materialization."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from commands.midgar.factory import FactoryError, _atomic_json, _issue_lock, _parse_issue


class PlanError(FactoryError):
    """An execution plan is invalid or cannot be safely materialized."""


Command = Callable[[list[str]], str]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise PlanError(f"command failed: {' '.join(argv)}") from error


def _objects(output: str) -> list[dict]:
    try:
        value = json.loads(output)
        values = value if isinstance(value, list) else value.get("issues", value.get("convoys", value.get("dependencies", [value])))
    except (ValueError, AttributeError):
        values = []
        for line in output.splitlines():
            if line.strip():
                try:
                    values.append(json.loads(line))
                except ValueError as error:
                    raise PlanError("invalid JSON response from native CLI") from error
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise PlanError("unexpected JSON response from native CLI")
    return values


def _one_object(output: str, description: str) -> dict:
    values = _objects(output)
    if len(values) != 1:
        raise PlanError(f"expected one {description} from native CLI")
    return values[0]


def _native(rig: str, group: str, argv: list[str]) -> list[str]:
    """Route every native operation through Gas City's explicit rig selector."""
    return ["gc", "--rig", rig, group, *argv]


def _task_record(output: str) -> dict:
    try:
        value = json.loads(output)
    except ValueError as error:
        raise PlanError("invalid task response from Beads") from error
    if isinstance(value, dict) and isinstance(value.get("issue"), dict):
        value = value["issue"]
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise PlanError("unexpected task response from Beads")
    return value


def _metadata(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _verify_task(command: Command, rig: str, bead_id: str, external_ref: str, run: dict, task_id: str) -> None:
    issue = _task_record(command(_native(rig, "bd", ["show", bead_id, "--json"])))
    identity = _metadata(issue.get("metadata")).get("midgar")
    expected = {"issue": run["issue"], "run_id": run["run_id"], "task_id": task_id}
    if issue.get("id") != bead_id or issue.get("external_ref") != external_ref or identity != expected:
        raise PlanError(f"Beads task {bead_id} does not match run/task identity")


def _convoy_record(command: Command, rig: str, convoy_id: str) -> tuple[dict, set[str]]:
    try:
        value = json.loads(command(_native(rig, "convoy", ["status", convoy_id, "--json"])))
    except ValueError as error:
        raise PlanError("invalid convoy status response") from error
    convoy = value.get("convoy") if isinstance(value, dict) else None
    children = value.get("children") if isinstance(value, dict) else None
    if not isinstance(convoy, dict) or not isinstance(children, list):
        raise PlanError("unexpected convoy status response")
    child_ids = {child.get("id") for child in children if isinstance(child, dict)}
    return convoy, child_ids


def _convoy_identity(command: Command, rig: str, convoy_id: str, title: str) -> set[str]:
    convoy, children = _convoy_record(command, rig, convoy_id)
    if convoy.get("id") != convoy_id or convoy.get("title") != title or convoy.get("owned") is not True:
        raise PlanError("convoy does not match recorded run identity and ownership")
    return children


def validate_plan(plan: dict, acceptance_ids: list[str] | set[str]) -> dict:
    """Validate task identities, coverage, ownership, references, and DAG acyclicity."""
    if not isinstance(plan, dict):
        raise PlanError("plan must be an object")
    revision = plan.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise PlanError("revision must be a positive integer")
    run_id = plan.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise PlanError("plan must identify its factory run")
    snapshot = plan.get("snapshot")
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise PlanError("plan must reference its fixed spec snapshot")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise PlanError("plan must contain tasks")
    if not isinstance(acceptance_ids, (list, set, tuple)) or any(not isinstance(item, str) or not item.strip() for item in acceptance_ids):
        raise PlanError("snapshot acceptance criteria must be non-empty identifiers")
    required = set(acceptance_ids)
    if not required or len(required) != len(acceptance_ids):
        raise PlanError("snapshot acceptance criteria must be unique non-empty identifiers")
    by_id: dict[str, dict] = {}
    owners: dict[str, list[str]] = {item: [] for item in required}
    for task in tasks:
        if not isinstance(task, dict):
            raise PlanError("each task must be an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise PlanError("invalid task identity")
        if task_id in by_id:
            raise PlanError(f"duplicate task identity: {task_id}")
        for field in ("title", "behavior"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise PlanError(f"task {task_id} requires {field}")
        acceptance = task.get("acceptance")
        owns = task.get("owns")
        dependencies = task.get("depends_on")
        if not _unique_strings(acceptance) or not _unique_strings(owns) or not isinstance(dependencies, list):
            raise PlanError(f"task {task_id} requires unique acceptance, owns, and dependency lists")
        if not acceptance or not owns:
            raise PlanError(f"task {task_id} must state behavior acceptance and owned criteria")
        if not set(owns) <= required:
            raise PlanError(f"task {task_id} owns unknown acceptance criteria")
        if not set(owns) <= set(acceptance):
            raise PlanError(f"task {task_id} does not verify its owned criteria")
        for dependency in dependencies:
            if not isinstance(dependency, str) or not _ID.fullmatch(dependency):
                raise PlanError(f"task {task_id} has invalid prerequisite reference")
        by_id[task_id] = task
        for criterion in owns:
            owners[criterion].append(task_id)
    uncovered = sorted(criterion for criterion, assigned in owners.items() if not assigned)
    if uncovered:
        raise PlanError(f"uncovered acceptance criteria: {', '.join(uncovered)}")
    shared = {criterion: assigned for criterion, assigned in owners.items() if len(assigned) > 1}
    integration_owner = plan.get("integration_owner", {})
    if not isinstance(integration_owner, dict):
        raise PlanError("integration_owner must map shared criteria to an owning task")
    for criterion, assigned in shared.items():
        if integration_owner.get(criterion) not in assigned:
            raise PlanError(f"shared acceptance {criterion} requires an explicit integration owner")
    if set(integration_owner) - set(shared):
        raise PlanError("integration_owner may only resolve shared acceptance ownership")
    for task_id, task in by_id.items():
        dependencies = task["depends_on"]
        if len(set(dependencies)) != len(dependencies):
            raise PlanError(f"task {task_id} has duplicate prerequisites")
        if task_id in dependencies:
            raise PlanError(f"task {task_id} cannot depend on itself")
        missing = set(dependencies) - by_id.keys()
        if missing:
            raise PlanError(f"task {task_id} references unknown prerequisites: {', '.join(sorted(missing))}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlanError("dependency cycle in execution plan")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)
    return plan


def _unique_strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value) and len(value) == len(set(value))


def _run_id(root: Path, reference: str) -> tuple[dict, str]:
    owner, repo, number = _parse_issue(reference)
    key, _ = _issue_lock(root, owner, repo, number)
    path = root / ".runs" / f"{key}.json"
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanError("no valid dispatched run found for issue") from error
    if run.get("issue") != f"{owner}/{repo}#{number}" or not isinstance(run.get("run_id"), str):
        raise PlanError("active run identity does not match source issue")
    return run, key


def materialize_plan(
    reference: str,
    plan: dict,
    acceptance_ids: list[str] | set[str],
    specs_root: str | Path = ".gc/specs",
    command: Command = _run,
) -> dict:
    """Idempotently create task beads and populate the owned convoy before readiness."""
    validate_plan(plan, acceptance_ids)
    root = Path(specs_root).resolve()
    issue_key, lock_path = _issue_lock(root, *_parse_issue(reference))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run, issue_key = _run_id(root, reference)
        if plan["run_id"] != run["run_id"]:
            raise PlanError("plan belongs to a different or replaced factory run")
        if str(Path(plan["snapshot"]).resolve()) != str(Path(run["snapshot"]).resolve()):
            raise PlanError("plan snapshot does not match the active fixed snapshot")
        rig = run.get("rig")
        if not isinstance(rig, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", rig):
            raise PlanError("active run has no valid selected rig")
        plan_dir = Path(run["snapshot"]).parent / "plans"
        plan_path = plan_dir / f"revision-{plan['revision']}.json"
        newer_revisions = [
            int(match.group(1))
            for path in plan_dir.glob("revision-*.json")
            if (match := re.fullmatch(r"revision-(\d+)\.json", path.name)) and int(match.group(1)) > plan["revision"]
        ]
        if newer_revisions:
            raise PlanError("a newer execution-plan revision already exists")
        if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise PlanError("plan revision already exists with different content")
        manifest_path = root / ".runs" / f"{run['run_id']}.plan-manifest.json"
        fingerprints = {
            task["id"]: hashlib.sha256(json.dumps(
                {key: task[key] for key in ("id", "title", "behavior", "acceptance", "owns", "depends_on")},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            for task in plan["tasks"]
        }
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"tasks": {}, "acceptance_ids": []}
        for task_id, fingerprint in fingerprints.items():
            if task_id in manifest["tasks"] and manifest["tasks"][task_id] != fingerprint:
                raise PlanError(f"task {task_id} changed after materialization; explicit reconciliation required")
        removed_tasks = set(manifest["tasks"]) - set(fingerprints)
        if removed_tasks:
            raise PlanError(f"materialized tasks cannot be removed without explicit reconciliation: {', '.join(sorted(removed_tasks))}")
        if manifest["acceptance_ids"] and set(manifest["acceptance_ids"]) != set(acceptance_ids):
            raise PlanError("acceptance IDs differ from the first accepted plan; reconcile the fixed snapshot explicitly")
        manifest["tasks"].update(fingerprints)
        if not manifest["acceptance_ids"]:
            manifest["acceptance_ids"] = sorted(set(acceptance_ids))
        if not plan_path.exists():
            _atomic_json(plan_path, plan)
        _atomic_json(manifest_path, manifest)
        run_path = root / ".runs" / f"{issue_key}.json"
        active_run = json.loads(run_path.read_text(encoding="utf-8"))
        if active_run.get("run_id") != run["run_id"]:
            raise PlanError("active run changed during plan materialization")
        active_progress = active_run.get("progress", {})
        if not isinstance(active_progress, dict):
            raise PlanError("active run progress is not an object")
        active_progress["plan_ready"] = False
        active_progress["pending_plan_revision"] = plan["revision"]
        active_run["progress"] = active_progress
        _atomic_json(run_path, active_run)
        state_path = root / ".runs" / f"{run['run_id']}.plan-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"tasks": {}, "dependencies": [], "members": []}
        task_ids = {}
        for task in plan["tasks"]:
            task_id = task["id"]
            external_ref = f"midgar:{run['run_id']}:task:{task_id}"
            found = _objects(command(_native(rig, "bd", ["list", "--all", "--external-ref", external_ref, "--json"])))
            if len(found) > 1:
                raise PlanError(f"multiple Beads tasks claim identity {external_ref}")
            if found:
                bead_id = found[0].get("id")
            else:
                metadata = json.dumps({"midgar": {"issue": run["issue"], "run_id": run["run_id"], "task_id": task_id}})
                created = _one_object(command(_native(rig, "bd", [
                    "create", task["title"], "--type", "task", "--description", task["behavior"],
                    "--acceptance", "\n".join(task["acceptance"]), "--external-ref", external_ref,
                    "--metadata", metadata, "--json",
                ])), "Beads task")
                bead_id = created.get("id")
            if not isinstance(bead_id, str) or not bead_id:
                raise PlanError(f"Beads returned no identity for task {task_id}")
            _verify_task(command, rig, bead_id, external_ref, run, task_id)
            task_ids[task_id] = bead_id
            state["tasks"][task_id] = bead_id
            _atomic_json(state_path, state)
        for task in plan["tasks"]:
            for predecessor in task["depends_on"]:
                edge = [task_ids[task["id"]], task_ids[predecessor]]
                if edge not in state["dependencies"]:
                    dependency_rows = _objects(command(_native(rig, "bd", ["dep", "list", edge[0], "--json"])))
                    present = any(
                        row.get("issue_id") == edge[0]
                        and row.get("depends_on_id") == edge[1]
                        and row.get("type") == "blocks"
                        for row in dependency_rows
                    )
                    if not present:
                        command(_native(rig, "bd", ["dep", "add", *edge]))
                        dependency_rows = _objects(command(_native(rig, "bd", ["dep", "list", edge[0], "--json"])))
                        present = any(
                            row.get("issue_id") == edge[0]
                            and row.get("depends_on_id") == edge[1]
                            and row.get("type") == "blocks"
                            for row in dependency_rows
                        )
                    if not present:
                        raise PlanError(f"Beads prerequisite edge {edge[0]} -> {edge[1]} was not confirmed")
                    state["dependencies"].append(edge)
                    _atomic_json(state_path, state)
        convoy_title = f"Midgar {run['run_id']} {run['issue']} implementation tasks"
        run_path = root / ".runs" / f"{issue_key}.json"
        active_run = json.loads(run_path.read_text(encoding="utf-8"))
        if active_run.get("run_id") != run["run_id"]:
            raise PlanError("active run changed during plan materialization")
        active_progress = active_run.get("progress", {})
        if not isinstance(active_progress, dict):
            raise PlanError("active run progress is not an object")
        owner_token = f"midgar:{run['run_id']}"
        recorded_id = active_progress.get("convoy_id")
        state_id = state.get("convoy_id")
        if recorded_id and active_progress.get("convoy_owner") != owner_token:
            raise PlanError("run convoy lacks the expected recorded ownership")
        if state_id and state.get("convoy_owner") != owner_token:
            raise PlanError("materialization ledger convoy ownership does not match this run")
        if recorded_id and state_id and recorded_id != state_id:
            raise PlanError("run and materialization ledger disagree on owned convoy identity")
        convoy_id = recorded_id or state_id
        candidates = [
            item for item in _objects(command(_native(rig, "bd", ["list", "--all", "--type", "convoy", "--title", convoy_title, "--json"])))
            if item.get("title") == convoy_title
        ]
        if len(candidates) > 1:
            raise PlanError("multiple convoys claim this run identity")
        if convoy_id:
            if candidates and candidates[0].get("id") != convoy_id:
                raise PlanError("recorded convoy differs from the unique run-owned convoy")
            children = _convoy_identity(command, rig, convoy_id, convoy_title)
        else:
            if candidates:
                convoy_id = candidates[0].get("id")
                if not isinstance(convoy_id, str) or not convoy_id:
                    raise PlanError("Beads returned no identity for matching convoy")
                children = _convoy_identity(command, rig, convoy_id, convoy_title)
            else:
                created = _one_object(command(_native(rig, "convoy", ["create", convoy_title, "--owned", "--json"])), "owned convoy")
                convoy_id = created.get("convoy_id", created.get("id"))
                if not isinstance(convoy_id, str) or not convoy_id:
                    raise PlanError("Gas City returned no convoy identity")
                children = _convoy_identity(command, rig, convoy_id, convoy_title)
        expected_members = set(task_ids.values())
        if children - expected_members:
            raise PlanError("owned implementation convoy contains non-plan or control work")
        state["convoy_id"] = convoy_id
        state["convoy_owner"] = owner_token
        state["members"] = sorted(children)
        _atomic_json(state_path, state)
        active_progress["convoy_id"] = convoy_id
        active_progress["convoy_owner"] = owner_token
        active_run["progress"] = active_progress
        _atomic_json(run_path, active_run)
        for bead_id in task_ids.values():
            if bead_id not in children:
                command(_native(rig, "convoy", ["add", convoy_id, bead_id, "--json"]))
                children = _convoy_identity(command, rig, convoy_id, convoy_title)
                if bead_id not in children:
                    raise PlanError(f"implementation task {bead_id} was not confirmed in convoy")
                state["members"] = sorted(children)
                _atomic_json(state_path, state)
        if children != expected_members:
            raise PlanError("implementation convoy membership is incomplete")
        progress = {"plan": str(plan_path), "plan_revision": plan["revision"], "convoy_id": convoy_id, "convoy_owner": owner_token, "plan_ready": True}
        progress.update({f"task:{task}": bead for task, bead in task_ids.items()})
        run_path = root / ".runs" / f"{issue_key}.json"
        current_run = json.loads(run_path.read_text(encoding="utf-8"))
        if current_run.get("run_id") != run["run_id"]:
            raise PlanError("active run changed during plan materialization")
        if current_run.get("stage") in {"dispatched", "planning", "planned"}:
            current_run["stage"] = "planned"
        run_progress = current_run.setdefault("progress", {})
        if not isinstance(run_progress, dict):
            raise PlanError("active run progress is not an object")
        run_progress.pop("pending_plan_revision", None)
        run_progress.update(progress)
        _atomic_json(run_path, current_run)
        return {"run_id": run["run_id"], "plan": str(plan_path), "revision": plan["revision"], "convoy_id": convoy_id, "tasks": task_ids, "ready": True}
