"""Fail-closed delivery evidence contracts for the task-delivery graph."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from commands.midgar.factory import _issue_lock, _parse_issue
from commands.midgar.workspace import (
    NativeCommand, WorkspaceBinding, WorkspaceError, _run, launch,
    provision_workspace_for_run,
)


class DeliveryError(Exception):
    """A task result is missing, malformed, or failed a required gate."""


_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def require_delivered(prerequisites: Mapping[str, Mapping[str, object]]) -> bool:
    """Require explicit durable delivery, not a terminal/closed native root."""
    if not isinstance(prerequisites, Mapping) or not prerequisites:
        raise DeliveryError("required prerequisite delivery evidence is missing")
    for task_id, result in prerequisites.items():
        if not isinstance(task_id, str) or not task_id or not isinstance(result, Mapping):
            raise DeliveryError("prerequisite delivery evidence is malformed")
        expected = {"status": "delivered", "review": "passed", "integration": "passed", "checks": "passed"}
        if any(result.get(key) != value for key, value in expected.items()):
            raise DeliveryError(f"prerequisite {task_id} is not delivered: explicit review, integration, and checks must pass")
    return True


def validate_role_report(role: str, report: object) -> dict:
    """Validate the machine-readable outcome emitted by one fresh task role."""
    if role not in {"implementer", "reviewer", "integrator"}:
        raise DeliveryError(f"unsupported delivery role: {role}")
    if not isinstance(report, dict):
        raise DeliveryError("malformed role report: expected a JSON object")
    if role == "reviewer":
        standards = report.get("standards")
        spec = report.get("spec")
        blocking = report.get("blocking_findings")
        standards_evidence = report.get("standards_evidence")
        spec_evidence = report.get("spec_evidence")
        if (standards not in {"passed", "blocked"} or spec not in {"passed", "blocked"}
                or not isinstance(standards_evidence, str) or not standards_evidence.strip()
                or not isinstance(spec_evidence, str) or not spec_evidence.strip()
                or not isinstance(blocking, list) or not isinstance(report.get("commit"), str)
                or not _SHA.fullmatch(report["commit"])):
            raise DeliveryError("malformed reviewer report: separate Standards/Spec evidence, commit, and blocking_findings are required")
        if any(not isinstance(item, str) or not item.strip() for item in blocking):
            raise DeliveryError("malformed reviewer report: findings must be non-empty strings")
        if standards != "passed" or spec != "passed" or blocking:
            raise DeliveryError("review blocked: both Standards and Spec must pass with no blocking findings")
        return {"role": role, "verdict": "passed", "standards": standards, "spec": spec,
                "standards_evidence": standards_evidence.strip(), "spec_evidence": spec_evidence.strip(),
                "commit": report["commit"], "blocking_findings": [], "advisory_findings": _advisories(report)}

    if role == "implementer":
        commit = report.get("commit")
        if report.get("outcome") != "passed" or not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise DeliveryError("malformed implementer report: a passing outcome and full commit SHA are required")
        return {"role": role, "verdict": "passed", "commit": commit}

    commit = report.get("commit")
    reviewed_commit = report.get("reviewed_commit")
    if (report.get("integration") != "passed" or not isinstance(commit, str) or not _SHA.fullmatch(commit)
            or not isinstance(reviewed_commit, str) or not _SHA.fullmatch(reviewed_commit)):
        raise DeliveryError("malformed integrator report: passed integration, reviewed commit, and full spec commit SHA are required")
    return {"role": role, "verdict": "passed", "commit": commit, "reviewed_commit": reviewed_commit}


def _advisories(report: dict) -> list[str]:
    values = report.get("advisory_findings", [])
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        raise DeliveryError("malformed reviewer report: advisory_findings must be non-empty strings")
    return values


def build_pi_argv(
    prompt: str,
    system_prompt: str,
    role: str,
    *,
    pi_bin: str = "pi",
    pi_prefix: Sequence[str] = (),
) -> list[str]:
    """Build the factory's explicit fresh, one-shot Pi invocation."""
    if role not in {"implementer", "reviewer", "integrator"}:
        raise DeliveryError(f"unsupported delivery role: {role}")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(system_prompt, str) or not system_prompt.strip():
        raise DeliveryError("factory worker requires non-empty task and system prompts")
    argv = [pi_bin, *pi_prefix, "--print", "--no-session", "--approve", "--system-prompt", system_prompt]
    if role == "reviewer":
        # The full diff is supplied by Midgar, so review needs no mutating tools.
        argv.extend(["--tools", "read,grep,find,ls"])
    argv.extend(["--", prompt])
    return argv


def _object(output: str) -> dict:
    try:
        value = json.loads(output)
    except (ValueError, TypeError) as error:
        raise DeliveryError("invalid JSON from claimed task store") from error
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, dict) and isinstance(value.get("issue"), dict):
        value = value["issue"]
    if not isinstance(value, dict):
        raise DeliveryError("unexpected record from claimed task store")
    return value


def _metadata(bead: dict) -> dict:
    value = bead.get("metadata", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise DeliveryError("claimed bead metadata is malformed") from error
    if not isinstance(value, dict):
        raise DeliveryError("claimed bead metadata is malformed")
    return value


def _midgar(bead: dict) -> dict:
    value = _metadata(bead).get("midgar")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise DeliveryError("Midgar task identity metadata is malformed") from error
    if not isinstance(value, dict):
        raise DeliveryError("bead is missing Midgar task identity metadata")
    return value


def _native(rig: str, group: str, *args: str) -> list[str]:
    return ["gc", "--rig", rig, group, *args]


def resolve_task_context(bead: dict, command: NativeCommand = _run, *, rig: str) -> dict:
    """Resolve a claimed role bead to its single source task via its input convoy."""
    if not isinstance(bead, dict) or not isinstance(bead.get("id"), str) or not bead["id"]:
        raise DeliveryError("claimed bead identity is missing")
    metadata = _metadata(bead)
    midgar_value = metadata.get("midgar", {})
    if isinstance(midgar_value, str):
        try:
            midgar_value = json.loads(midgar_value)
        except ValueError as error:
            raise DeliveryError("Midgar role metadata is malformed") from error
    midgar = midgar_value if isinstance(midgar_value, dict) else {}
    role = metadata.get("midgar.delivery_role") or midgar.get("delivery_role")
    if role not in {"implementer", "reviewer", "integrator", "check"}:
        raise DeliveryError("claimed bead has no supported factory delivery role")
    graph_root_id = metadata.get("gc.root_bead_id")
    input_convoy = metadata.get("gc.input_convoy_id")
    if not isinstance(graph_root_id, str) or not graph_root_id:
        raise DeliveryError("claimed role bead has no native workflow root identity")
    graph_root = _object(command(_native(rig, "bd", "show", graph_root_id, "--json")))
    root_meta = _metadata(graph_root)
    input_convoy = input_convoy or root_meta.get("gc.input_convoy_id")
    if not isinstance(input_convoy, str) or not input_convoy:
        raise DeliveryError("factory task workflow is not bound to an input convoy")
    status = _object(command(_native(rig, "convoy", "status", input_convoy, "--json")))
    children = status.get("children")
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict):
        raise DeliveryError("task-delivery input convoy must contain exactly one source task")
    task_bead_id = children[0].get("id")
    if not isinstance(task_bead_id, str) or not task_bead_id:
        raise DeliveryError("source task bead identity is missing")
    task_bead = _object(command(_native(rig, "bd", "show", task_bead_id, "--json")))
    task = _midgar(task_bead)
    if not midgar:
        midgar = _metadata(graph_root).get("midgar", {})
        if isinstance(midgar, str):
            try:
                midgar = json.loads(midgar)
            except ValueError as error:
                raise DeliveryError("workflow root task metadata is malformed") from error
        if not isinstance(midgar, dict):
            raise DeliveryError("workflow root has no source task identity")
    expected = {"issue": task.get("issue"), "run_id": task.get("run_id"), "task_id": task.get("task_id")}
    if any(not isinstance(value, str) or not value for value in expected.values()):
        raise DeliveryError("source task is missing issue/run/task identity")
    if any(midgar.get(key) != value for key, value in expected.items()):
        raise DeliveryError("claimed role bead does not belong to the source task identity")
    if task.get("task_id") != task_bead.get("task_id") and task_bead.get("task_id"):
        raise DeliveryError("source task identity is inconsistent")
    return {"role": role, "bead_id": bead["id"], "task_bead_id": task_bead_id,
            "issue": expected["issue"], "run_id": expected["run_id"], "task_id": expected["task_id"],
            "rig": rig, "task_bead": task_bead, "role_bead": bead}


def _run_record(context: dict, specs_root: str | Path) -> dict:
    owner, repo, number = _parse_issue(context["issue"])
    root = Path(specs_root).resolve()
    key, _ = _issue_lock(root, owner, repo, number)
    path = root / ".runs" / f"{key}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DeliveryError("active factory run record is unavailable") from error
    if (value.get("run_id") != context["run_id"] or value.get("rig") != context["rig"]
            or value.get("issue") != context["issue"] or value.get("stage") in {"failed", "paused"}):
        raise DeliveryError("claimed task does not match an active factory run")
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, str) or not Path(snapshot).is_file():
        raise DeliveryError("fixed spec snapshot is unavailable")
    return value


def _result_metadata(bead: dict, field: str) -> object:
    value = _metadata(bead).get(field)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _prerequisite_results(context: dict, command: NativeCommand) -> tuple[list[str], dict[str, dict]]:
    rows = json.loads(command(_native(context["rig"], "bd", "dep", "list", context["task_bead_id"], "--json")))
    if not isinstance(rows, list):
        raise DeliveryError("task prerequisites response is malformed")
    dependencies = [row.get("depends_on_id") for row in rows if isinstance(row, dict) and row.get("type") == "blocks"]
    results: dict[str, dict] = {}
    for dependency in dependencies:
        if not isinstance(dependency, str) or not dependency:
            raise DeliveryError("task prerequisite identity is malformed")
        bead = _object(command(_native(context["rig"], "bd", "show", dependency, "--json")))
        meta = _metadata(bead)
        results[dependency] = {
            "status": meta.get("midgar.delivery.status"),
            "review": meta.get("midgar.delivery.review"),
            "integration": meta.get("midgar.delivery.integration"),
            "checks": meta.get("midgar.delivery.checks"),
        }
    return dependencies, results


def _store_ref(context: dict) -> str:
    return f"rig:{context['rig']}"


def _workspace(context: dict, specs_root: str | Path, command: NativeCommand) -> WorkspaceBinding:
    role = context["role"]
    if role == "implementer":
        prerequisites, results = _prerequisite_results(context, command)
        if prerequisites:
            require_delivered(results)
        return provision_workspace_for_run(
            context["issue"], role, specs_root=specs_root, create=True,
            task_id=context["task_id"], bead_id=context["task_bead_id"],
            store_ref=_store_ref(context), prerequisite_ids=prerequisites,
            prerequisite_results=results, command=command,
        )
    if role == "reviewer":
        return provision_workspace_for_run(context["issue"], role, specs_root=specs_root,
                                           task_id=context["task_id"], command=command)
    return provision_workspace_for_run(context["issue"], "integrator", specs_root=specs_root, command=command)


def _acceptance_text(value: object) -> str:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(f"- {item}" for item in value)
    if isinstance(value, str) and value.strip():
        return value
    raise DeliveryError("source task has no usable acceptance criteria")


def _git(path: str | Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise DeliveryError(f"Git verification failed in {path}") from error


def _task_prompt(context: dict, run: dict, binding: WorkspaceBinding) -> str:
    task = context["task_bead"]
    role_bead = context["role_bead"]
    snapshot = Path(run["snapshot"]).read_text(encoding="utf-8")
    task_metadata = _metadata(task)
    previous_failure = task_metadata.get("midgar.delivery.last_failure_reason")
    prompt = [
        "This is the one routed factory role bead you have already claimed. Do not claim, search, dispatch, or delegate work.",
        f"Role bead: {role_bead.get('title', '')} ({context['bead_id']})",
        str(role_bead.get("description", "")),
        f"Source implementation task: {task.get('title', '')} ({context['task_bead_id']})",
        str(task.get("description", "")),
        "Task acceptance criteria:", _acceptance_text(task.get("acceptance")),
        "Fixed requirements snapshot (authoritative; do not refetch or change scope):", snapshot,
        f"Task worktree: {binding.path}; branch: {binding.branch}; base: {binding.base_sha}.",
        "Return exactly one JSON object as the final answer matching the role report contract in your system prompt.",
    ]
    if isinstance(previous_failure, str) and previous_failure:
        prompt.extend(["Prior attempt finding/conflict to address without discarding evidence:", previous_failure])
    if context["role"] == "reviewer":
        task_meta = _metadata(task)
        if task_meta.get("midgar.delivery.implementation") != "passed":
            raise DeliveryError("review blocked: implementation has no committed success evidence")
        base = binding.request.base_sha
        head = _git(binding.path, "rev-parse", "HEAD")
        implementation_commit = task_meta.get("midgar.delivery.implementation_commit")
        if head != implementation_commit:
            raise DeliveryError("review workspace HEAD is not the committed implementation HEAD")
        if _git(binding.path, "status", "--porcelain"):
            raise DeliveryError("review workspace must be clean before independent review")
        context["review_head"] = head
        diff = _git(binding.path, "diff", "--no-ext-diff", f"{base}..{head}")
        prompt.extend(["Review this exact task diff; do not modify files:", diff])
    elif context["role"] == "integrator":
        meta = _metadata(task)
        reviewed = meta.get("midgar.delivery.implementation_commit")
        if not isinstance(reviewed, str) or not _SHA.fullmatch(reviewed):
            raise DeliveryError("integration blocked: no reviewed implementation commit is recorded")
        if _metadata(task).get("midgar.delivery.review") != "passed":
            raise DeliveryError("integration blocked: task review has not passed")
        if meta.get("midgar.delivery.review_commit") != reviewed:
            raise DeliveryError("integration blocked: passing review is not scoped to the current implementation commit")
        prompt.extend([f"Reviewed task commit to integrate: {reviewed}.",
                       f"Spec integration checkout: {binding.path}, branch {binding.branch}.",
                       "Integrate only this reviewed commit. Do not edit source to resolve conflicts; report integration failure for rework."])
    return "\n\n".join(prompt)


def run_model(
    binding: WorkspaceBinding,
    prompt: str,
    system_prompt: str,
    *,
    role: str,
    claimed_bead_id: str,
    pi_bin: str = "pi",
    pi_prefix: Sequence[str] = (),
    native_command: NativeCommand = _run,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Start one fresh model process only after #7 verifies its actual cwd."""
    if not isinstance(claimed_bead_id, str) or not claimed_bead_id:
        raise DeliveryError("a successfully claimed routed bead is required before model start")
    child_env = dict(os.environ if env is None else env)
    child_env.update({
        "MIDGAR_FACTORY_ROLE": role,
        "MIDGAR_CLAIMED_BEAD_ID": claimed_bead_id,
    })
    try:
        result = launch(
            binding,
            build_pi_argv(prompt, system_prompt, role, pi_bin=pi_bin, pi_prefix=pi_prefix),
            native_command=native_command, capture_output=True, text=True, env=child_env,
        )
    except WorkspaceError as error:
        raise DeliveryError(f"workspace binding refused model start: {error}") from error
    return result


def _begin_attempt(context: dict, command: NativeCommand) -> None:
    if context["role"] != "implementer":
        return
    values = [
        "--set-metadata", "midgar.delivery.status=in_progress",
    ]
    for key in ("midgar.delivery.review", "midgar.delivery.integration", "midgar.delivery.checks",
                "midgar.delivery.implementation", "midgar.delivery.implementation_commit",
                "midgar.delivery.integration_commit", "midgar.delivery.integration_reviewed_commit"):
        values.extend(["--unset-metadata", key])
    command(_native(context["rig"], "bd", "update", context["task_bead_id"], *values))


def _write_result(
    context: dict, command: NativeCommand, result: dict, *, passed: bool,
    reason: str = "", failure_class: str = "",
) -> None:
    role = context["role"]
    outcome = "pass" if passed else "fail"
    values = [
        f"gc.outcome={outcome}",
        f"midgar.delivery.role={role}",
        f"midgar.delivery.report={json.dumps(result, sort_keys=True, separators=(',', ':'))}",
    ]
    if reason:
        values.append(f"midgar.delivery.failure_reason={reason[:1000]}")
    if failure_class:
        values.append(f"midgar.delivery.failure_class={failure_class}")
    command(_native(context["rig"], "bd", "update", context["bead_id"], *[
        part for value in values for part in ("--set-metadata", value)
    ]))
    if not passed:
        command(_native(context["rig"], "bd", "update", context["task_bead_id"],
                        "--set-metadata", f"midgar.delivery.last_failure_role={role}",
                        "--set-metadata", f"midgar.delivery.last_failure_class={failure_class or 'delivery_failure'}",
                        "--set-metadata", f"midgar.delivery.last_failure_reason={reason[:1000]}"))
        return
    task_values: list[str] = []
    if role == "implementer":
        task_values = ["midgar.delivery.implementation=passed",
                       f"midgar.delivery.implementation_commit={result['commit']}"]
    elif role == "reviewer":
        task_values = ["midgar.delivery.review=passed",
                       f"midgar.delivery.review_commit={result['commit']}"]
    elif role == "integrator":
        task_values = ["midgar.delivery.integration=passed",
                       f"midgar.delivery.integration_commit={result['commit']}",
                       f"midgar.delivery.integration_reviewed_commit={result['reviewed_commit']}"]
    if task_values:
        command(_native(context["rig"], "bd", "update", context["task_bead_id"], *[
            part for value in task_values for part in ("--set-metadata", value)
        ]))


def _decode_report(output: str) -> dict:
    try:
        value = json.loads(output.strip())
    except (ValueError, AttributeError) as error:
        raise DeliveryError("model returned no valid JSON result report") from error
    if not isinstance(value, dict):
        raise DeliveryError("model result report must be a JSON object")
    return value


def verify_role_commit(
    role: str,
    report: dict,
    binding: WorkspaceBinding,
    *,
    task_binding: WorkspaceBinding | None = None,
    task_metadata: Mapping[str, object] | None = None,
    review_head: str | None = None,
) -> None:
    """Bind review/integration claims to exact Git evidence, not model assertion."""
    if role == "implementer":
        head = _git(binding.path, "rev-parse", "HEAD")
        if report.get("commit") != head or head == binding.base_sha:
            raise DeliveryError("implementer report does not identify a new committed task HEAD")
        if _git(binding.path, "status", "--porcelain"):
            raise DeliveryError("implementation must be committed; uncommitted changes are not deliverable")
        ancestry = subprocess.run(["git", "-C", str(binding.path), "merge-base", "--is-ancestor", binding.base_sha, head],
                                  capture_output=True, text=True)
        if ancestry.returncode != 0:
            raise DeliveryError("implementation commit does not descend from its verified task base")
        return
    if role == "reviewer":
        head = _git(binding.path, "rev-parse", "HEAD")
        if not review_head or report.get("commit") != review_head or head != review_head:
            raise DeliveryError("review report is not scoped to the unchanged reviewed HEAD")
        if _git(binding.path, "status", "--porcelain"):
            raise DeliveryError("reviewer changed the task workspace")
        return
    if role != "integrator" or task_binding is None or not isinstance(task_metadata, Mapping):
        raise DeliveryError("integration evidence is incomplete")
    reviewed = task_metadata.get("midgar.delivery.implementation_commit")
    if (task_metadata.get("midgar.delivery.review") != "passed"
            or task_metadata.get("midgar.delivery.review_commit") != reviewed
            or not isinstance(reviewed, str) or not _SHA.fullmatch(reviewed)):
        raise DeliveryError("integration is not authorized by a fresh review of the implementation commit")
    task_head = _git(task_binding.path, "rev-parse", "HEAD")
    if task_head != reviewed:
        raise DeliveryError("task HEAD changed after review; implementation requires fresh review")
    spec_head = _git(binding.path, "rev-parse", "HEAD")
    if report.get("commit") != spec_head or report.get("reviewed_commit") != reviewed:
        raise DeliveryError("integrator report does not identify the reviewed commit and verified spec HEAD")
    if _git(binding.path, "status", "--porcelain"):
        raise DeliveryError("integrator left uncommitted changes in the spec workspace")
    ancestry = subprocess.run(["git", "-C", str(binding.path), "merge-base", "--is-ancestor", reviewed, spec_head],
                              capture_output=True, text=True)
    if ancestry.returncode != 0:
        raise DeliveryError("reviewed implementation commit is not reachable from the spec integration HEAD")


def run_claimed_role(
    claim_output: str,
    *,
    rig: str,
    specs_root: str | Path,
    system_prompt: str,
    command: NativeCommand = _run,
    pi_bin: str = "pi",
) -> dict:
    """Resolve claimed work, bind its native source-owned workspace, then start fresh Pi."""
    reported: dict = {}
    context: dict | None = None
    try:
        claim = _object(claim_output)
        if claim.get("action") != "work" or not isinstance(claim.get("bead"), dict):
            raise DeliveryError("factory worker requires one successfully claimed work bead")
        claimed = claim["bead"]
        if claimed.get("id") != claim.get("bead_id"):
            raise DeliveryError("claim result bead identity does not match its claimed id")
        context = resolve_task_context(claimed, command, rig=rig)
        run = _run_record(context, specs_root)
        _begin_attempt(context, command)
        binding = _workspace(context, specs_root, command)
        prompt = _task_prompt(context, run, binding)
        review_head = context.get("review_head")
        task_binding = None
        if context["role"] == "reviewer":
            review_head = _git(binding.path, "rev-parse", "HEAD")
            context["review_head"] = review_head
            prompt = _task_prompt(context, run, binding)
        elif context["role"] == "integrator":
            task_binding = provision_workspace_for_run(
                context["issue"], "reviewer", specs_root=specs_root,
                task_id=context["task_id"], command=command,
            )
            reviewed = _metadata(context["task_bead"]).get("midgar.delivery.implementation_commit")
            if _git(task_binding.path, "rev-parse", "HEAD") != reviewed:
                raise DeliveryError("task HEAD changed after review; implementation requires fresh review")
        model = run_model(binding, prompt, system_prompt, role=context["role"],
                          claimed_bead_id=context["bead_id"], pi_bin=pi_bin,
                          native_command=command)
        if model.returncode != 0:
            raise DeliveryError(f"Pi exited with status {model.returncode}: {model.stderr[-1000:]}")
        reported = _decode_report(model.stdout)
        report = validate_role_report(context["role"], reported)
        verify_role_commit(context["role"], report, binding, task_binding=task_binding,
                           task_metadata=_metadata(context["task_bead"]), review_head=review_head)
        _write_result(context, command, report, passed=True)
        return {"ok": True, "bead_id": context["bead_id"], "task_bead_id": context["task_bead_id"], "report": report}
    except (DeliveryError, WorkspaceError, OSError, subprocess.SubprocessError) as error:
        if context is not None:
            reason = str(error)
            if context["role"] == "reviewer" and isinstance(reported, dict) and reported.get("blocking_findings"):
                reason += "; blocking findings: " + json.dumps(reported["blocking_findings"], ensure_ascii=False)
            lower_reason = reason.lower()
            if isinstance(error, WorkspaceError) or "workspace" in lower_reason or "worktree" in lower_reason:
                failure_class = "workspace_failure"
            elif "prerequisite" in lower_reason:
                failure_class = "prerequisite_failure"
            else:
                failure_class = {"reviewer": "review_failure", "integrator": "integration_failure"}.get(
                    context["role"], "implementation_failure")
            try:
                _write_result(context, command, reported, passed=False, reason=reason, failure_class=failure_class)
            except Exception as diagnostic_error:
                raise DeliveryError(f"{reason}; failed to persist failure evidence: {diagnostic_error}") from error
        raise DeliveryError(str(error)) from error


def _run_factory_check_impl(
    check_bead_id: str,
    *,
    rig: str,
    specs_root: str | Path,
    command: NativeCommand = _run,
    check_runner=None,
) -> dict:
    """Run exactly plain `hk check` in the verified spec checkout and record success."""
    if not check_bead_id:
        raise DeliveryError("formula check has no GC_BEAD_ID")
    check_bead = _object(command(_native(rig, "bd", "show", check_bead_id, "--json")))
    context = resolve_task_context(check_bead, command, rig=rig)
    if context["role"] != "check":
        raise DeliveryError("formula check subject is not a factory delivery check")
    run = _run_record(context, specs_root)
    task = context["task_bead"]
    task_meta = _metadata(task)
    implementation = task_meta.get("midgar.delivery.implementation_commit")
    if (task_meta.get("midgar.delivery.review") != "passed"
            or task_meta.get("midgar.delivery.review_commit") != implementation
            or task_meta.get("midgar.delivery.integration") != "passed"
            or task_meta.get("midgar.delivery.integration_reviewed_commit") != implementation):
        raise DeliveryError("task delivery check blocked: fresh review and matching integration evidence must pass")
    if not isinstance(implementation, str) or not _SHA.fullmatch(implementation):
        raise DeliveryError("task delivery check blocked: implementation commit evidence is missing")
    task_binding = provision_workspace_for_run(context["issue"], "reviewer", specs_root=specs_root,
                                               task_id=context["task_id"], command=command)
    binding = provision_workspace_for_run(context["issue"], "integrator", specs_root=specs_root, command=command)
    verify_role_commit(
        "integrator", {"commit": _git(binding.path, "rev-parse", "HEAD"), "reviewed_commit": implementation}, binding,
        task_binding=task_binding, task_metadata=task_meta,
    )
    runner = check_runner or subprocess.run
    try:
        result = runner(["hk", "check"], cwd=binding.path, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise DeliveryError(f"hk check could not start in the verified spec workspace: {error}") from error
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=__import__("sys").stderr)
    if result.returncode != 0:
        raise DeliveryError(f"plain hk check failed with status {result.returncode}")
    command(_native(rig, "bd", "update", check_bead_id, "--set-metadata", "gc.outcome=pass",
                    "--set-metadata", "midgar.delivery.checks=passed"))
    command(_native(rig, "bd", "update", context["task_bead_id"],
                    "--set-metadata", "midgar.delivery.checks=passed",
                    "--set-metadata", "midgar.delivery.status=delivered"))
    return {"ok": True, "task_bead_id": context["task_bead_id"], "checks": "passed", "status": "delivered"}


def run_factory_check(
    check_bead_id: str,
    *,
    rig: str,
    specs_root: str | Path,
    command: NativeCommand = _run,
    check_runner=None,
) -> dict:
    """Persist distinct check/workspace failure evidence and preserve the original error."""
    try:
        return _run_factory_check_impl(check_bead_id, rig=rig, specs_root=specs_root,
                                       command=command, check_runner=check_runner)
    except (DeliveryError, WorkspaceError, OSError, subprocess.SubprocessError) as error:
        reason = str(error)
        lower = reason.lower()
        failure_class = ("workspace_failure" if isinstance(error, WorkspaceError) or "workspace" in lower or "worktree" in lower
                         else "check_failure")
        try:
            command(_native(rig, "bd", "update", check_bead_id,
                            "--set-metadata", "gc.outcome=fail",
                            "--set-metadata", f"midgar.delivery.failure_class={failure_class}",
                            "--set-metadata", f"midgar.delivery.failure_reason={reason[:1000]}"))
            check_bead = _object(command(_native(rig, "bd", "show", check_bead_id, "--json")))
            context = resolve_task_context(check_bead, command, rig=rig)
            command(_native(rig, "bd", "update", context["task_bead_id"],
                            "--set-metadata", "midgar.delivery.checks=failed",
                            "--set-metadata", "midgar.delivery.last_failure_role=check",
                            "--set-metadata", f"midgar.delivery.last_failure_class={failure_class}",
                            "--set-metadata", f"midgar.delivery.last_failure_reason={reason[:1000]}"))
        except Exception as evidence_error:
            raise DeliveryError(f"{reason}; failed to persist check failure evidence: {evidence_error}") from error
        raise DeliveryError(reason) from error
