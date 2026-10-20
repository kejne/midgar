"""Owned workspace provisioning and fail-closed process startup for Midgar."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from commands.midgar.factory import _atomic_json, _github_repository, _issue_lock, _parse_issue


class WorkspaceError(Exception):
    """A workspace cannot be safely provisioned or launched."""


NativeCommand = Callable[[list[str]], str]
WORKSPACE_KINDS = {"spec", "task"}
SESSION_ROLES = {"spec", "implementer", "reviewer", "integrator", "final-review"}
PROVENANCE_FIELDS = {
    "schema_version", "bead_id", "store_ref", "repo_identity", "path", "branch",
    "base_ref", "base_sha", "creator", "owner", "generation", "created_at",
    "lifecycle", "attempt_id",
}


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkspaceError(f"workspace command failed: {' '.join(argv)}") from error


def _safe_token(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise WorkspaceError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class WorkspaceRequest:
    """Low-level native identity; workspace kind is independent of session role."""

    run_id: str
    issue: str
    rig: str
    workspace_kind: str
    session_role: str
    repository: str | Path
    workspace_root: str | Path
    task_id: str
    bead_id: str
    store_ref: str
    base_ref: str
    base_sha: str

    def __post_init__(self) -> None:
        _safe_token(self.run_id, "run identity")
        _safe_token(self.rig, "rig")
        _safe_token(self.task_id, "task identity")
        _safe_token(self.bead_id, "Beads identity")
        if not isinstance(self.store_ref, str) or not self.store_ref.strip():
            raise WorkspaceError("Beads store reference is required")
        if self.workspace_kind not in WORKSPACE_KINDS:
            raise WorkspaceError("workspace kind must be spec or task")
        if self.session_role not in SESSION_ROLES:
            raise WorkspaceError("unsupported session role")
        if self.workspace_kind == "spec" and self.task_id != "spec":
            raise WorkspaceError("spec workspace identity must use task id 'spec'")
        if self.workspace_kind == "task" and self.task_id == "spec":
            raise WorkspaceError("task workspace requires a task identity")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+", self.issue) or self.issue != self.issue.lower():
            raise WorkspaceError("issue identity must be canonical OWNER/REPO#NUMBER")
        if not isinstance(self.base_ref, str) or not self.base_ref.strip() or self.base_ref.startswith("-"):
            raise WorkspaceError("an exact base ref is required")
        if not isinstance(self.base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.base_sha):
            raise WorkspaceError("base SHA must be a full object ID")
        repo = Path(self.repository).resolve()
        root = Path(self.workspace_root).resolve()
        if not repo.is_dir() or repo == root:
            raise WorkspaceError("repository and per-rig workspace root must be valid distinct directories")
        object.__setattr__(self, "repository", repo)
        object.__setattr__(self, "workspace_root", root)

    @property
    def identity(self) -> str:
        return f"{self.run_id}:{self.workspace_kind}:{self.task_id}"

    @property
    def generation(self) -> str:
        return hashlib.sha256(self.identity.encode()).hexdigest()[:32]

    @property
    def owner(self) -> str:
        return "midgar-factory"

    @property
    def creator(self) -> str:
        return "midgar-factory"

    @property
    def workspace_path(self) -> Path:
        key = hashlib.sha256(self.identity.encode()).hexdigest()[:20]
        return Path(self.workspace_root) / f"{self.workspace_kind}-{key}"

    @property
    def branch(self) -> str:
        key = hashlib.sha256(self.identity.encode()).hexdigest()[:12]
        return f"factory/{self.workspace_kind}-{key}"

    @property
    def expected_provenance(self) -> dict:
        return {
            "schema_version": 1, "bead_id": self.bead_id, "store_ref": self.store_ref,
            "path": str(self.workspace_path), "branch": self.branch,
            "base_ref": self.base_ref, "base_sha": self.base_sha,
            "creator": self.creator, "owner": self.owner, "generation": self.generation,
            "lifecycle": "active",
        }


@dataclass(frozen=True)
class WorkspaceBinding:
    """A fresh session role bound to a recorded, verified run workspace."""

    request: WorkspaceRequest
    provenance: dict

    @property
    def path(self) -> Path:
        return self.request.workspace_path

    @property
    def branch(self) -> str:
        return self.request.branch

    @property
    def base_ref(self) -> str:
        return self.request.base_ref

    @property
    def base_sha(self) -> str:
        return self.request.base_sha

    @property
    def session_role(self) -> str:
        return self.request.session_role

    @property
    def native_metadata(self) -> dict[str, str]:
        """Complete pool bead metadata, sourced from the verified request/provenance pair."""
        proof = self.provenance
        request = self.request
        return {
            "work_dir": proof["path"],
            "gc.work_dir": proof["path"],
            "gc.worktree_repo": str(request.repository),
            "gc.worktree_root": str(request.workspace_root),
            "gc.work_branch": proof["branch"],
            "gc.worktree_base_ref": proof["base_ref"],
            "gc.worktree_base_sha": proof["base_sha"],
            "gc.worktree_creator": proof["creator"],
            "gc.worktree_owner": proof["owner"],
            "gc.worktree_generation": proof["generation"],
            "gc.worktree_lifecycle": proof["lifecycle"],
            "gc.root_store_ref": request.store_ref,
        }


def _arguments(request: WorkspaceRequest, action: str) -> list[str]:
    return [
        "gc", "worktree", action,
        "--repo", str(request.repository), "--root", str(request.workspace_root),
        "--path", str(request.workspace_path), "--branch", request.branch,
        "--base", request.base_ref, "--base-sha", request.base_sha,
        "--bead", request.bead_id, "--store-ref", request.store_ref,
        "--owner", request.owner, "--creator", request.creator,
        "--generation", request.generation, "--lifecycle", "active",
        "--city", str(Path(request.workspace_root).parents[2]),
        "--rig", str(request.repository), "--json",
    ]


def _response(command: NativeCommand, argv: list[str]) -> dict:
    try:
        output = command(argv)
        if hasattr(output, "stdout"):
            output = output.stdout
        value = json.loads(output)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise WorkspaceError("invalid response from native worktree command") from error
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise WorkspaceError("native worktree command did not confirm success")
    return value


def _verified(request: WorkspaceRequest, command: NativeCommand) -> dict:
    result = _response(command, _arguments(request, "verify"))
    expected = request.expected_provenance
    provenance = result.get("provenance")
    if not isinstance(provenance, dict) or not PROVENANCE_FIELDS.issubset(provenance):
        raise WorkspaceError("native verification omitted durable ownership provenance")
    if any(provenance.get(key) != value for key, value in expected.items()):
        raise WorkspaceError("workspace ownership or identity does not match request")
    if not provenance.get("repo_identity") or not provenance.get("created_at") or not provenance.get("attempt_id"):
        raise WorkspaceError("native verification returned incomplete ownership evidence")
    if result.get("path") != str(request.workspace_path) or result.get("branch") != request.branch:
        raise WorkspaceError("native verification returned the wrong workspace or branch")
    try:
        actual_root = Path(subprocess.run(
            ["git", "-C", str(request.workspace_path), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()).resolve()
        actual_branch = subprocess.run(
            ["git", "-C", str(request.workspace_path), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        common_dir = subprocess.run(
            ["git", "-C", str(request.workspace_path), "rev-parse", "--git-common-dir"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        repo_common = subprocess.run(
            ["git", "-C", str(request.repository), "rev-parse", "--git-common-dir"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        common_path = (request.workspace_path / common_dir).resolve() if not Path(common_dir).is_absolute() else Path(common_dir).resolve()
        repo_common_path = (Path(request.repository) / repo_common).resolve() if not Path(repo_common).is_absolute() else Path(repo_common).resolve()
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkspaceError("workspace is not a usable Git worktree") from error
    if actual_root != request.workspace_path or actual_branch != request.branch or common_path != repo_common_path:
        raise WorkspaceError("Git repository, worktree root, or attached branch does not match request")
    return {"path": str(actual_root), "branch": actual_branch, "provenance": provenance}


def ensure_workspace(request: WorkspaceRequest, *, native_command: NativeCommand = _run) -> dict:
    """Low-level native ensure; run-aware callers should use provision_workspace_for_run."""
    Path(request.workspace_root).mkdir(parents=True, exist_ok=True)
    _response(native_command, _arguments(request, "ensure"))
    return _verified(request, native_command)


def _request_record(request: WorkspaceRequest) -> dict:
    return {
        "run_id": request.run_id, "issue": request.issue, "rig": request.rig,
        "workspace_kind": request.workspace_kind, "session_role": request.session_role,
        "repository": str(request.repository), "workspace_root": str(request.workspace_root),
        "task_id": request.task_id, "bead_id": request.bead_id, "store_ref": request.store_ref,
        "base_ref": request.base_ref, "base_sha": request.base_sha,
        "branch": request.branch, "path": str(request.workspace_path),
        "generation": request.generation,
    }


def _request_from_record(record: dict, session_role: str) -> WorkspaceRequest:
    try:
        request = WorkspaceRequest(
            run_id=record["run_id"], issue=record["issue"], rig=record["rig"],
            workspace_kind=record["workspace_kind"], session_role=session_role,
            repository=record["repository"], workspace_root=record["workspace_root"],
            task_id=record["task_id"], bead_id=record["bead_id"], store_ref=record["store_ref"],
            base_ref=record["base_ref"], base_sha=record["base_sha"],
        )
    except (KeyError, TypeError) as error:
        raise WorkspaceError("recorded workspace identity is incomplete") from error
    if record.get("path") != str(request.workspace_path) or record.get("branch") != request.branch or record.get("generation") != request.generation:
        raise WorkspaceError("recorded workspace identity does not match its run/task key")
    return request


def _external_text(command: NativeCommand, argv: list[str]) -> str:
    try:
        output = command(argv)
        if hasattr(output, "stdout"):
            output = output.stdout
        return output
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkspaceError(f"workspace command failed: {' '.join(argv)}") from error


def _rig_repository(run: dict, city_root: Path, command: NativeCommand) -> tuple[Path, str]:
    result = _response(command, ["gc", "--city", str(city_root), "rig", "list", "--json"])
    rigs = result.get("rigs")
    rig = next((item for item in rigs if isinstance(item, dict) and item.get("name") == run["rig"]), None) if isinstance(rigs, list) else None
    if rig is None or rig.get("hq"):
        raise WorkspaceError("dispatch run references an unknown project rig")
    path = rig.get("path")
    default_branch = rig.get("default_branch")
    if not isinstance(path, str) or not path or not isinstance(default_branch, str):
        raise WorkspaceError("rig repository path/default branch is unavailable")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", default_branch) or ".." in default_branch or default_branch.endswith(("/", ".")):
        raise WorkspaceError("rig default branch is invalid")
    repository = Path(path).resolve()
    remote = _external_text(command, ["git", "-C", str(repository), "remote", "get-url", "origin"])
    if _github_repository(remote) != tuple(run["repository"].lower().split("/")):
        raise WorkspaceError("dispatch repository does not match the selected rig origin")
    return repository, default_branch


def _check_prerequisites(prerequisite_ids: Sequence[str], results: Mapping[str, dict]) -> None:
    ids = list(prerequisite_ids)
    if len(ids) != len(set(ids)) or any(not isinstance(item, str) or not item for item in ids):
        raise WorkspaceError("prerequisite identities must be unique non-empty task IDs")
    if not isinstance(results, Mapping) or set(results) != set(ids):
        raise WorkspaceError("prerequisite delivery evidence is incomplete")
    for identity in ids:
        result = results[identity]
        if not isinstance(result, dict) or any(result.get(gate) != "passed" for gate in ("review", "integration", "checks")) or result.get("status") != "delivered":
            raise WorkspaceError(f"prerequisite {identity} is not successfully delivered")


def _head(path: Path) -> str:
    try:
        value = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkspaceError("cannot resolve current spec integration head") from error
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise WorkspaceError("spec integration head is not a full object ID")
    return value


def provision_workspace_for_run(
    reference: str,
    session_role: str,
    *,
    specs_root: str | Path = ".gc/specs",
    create: bool = False,
    task_id: str | None = None,
    bead_id: str | None = None,
    store_ref: str | None = None,
    prerequisite_ids: Sequence[str] = (),
    prerequisite_results: Mapping[str, dict] | None = None,
    command: NativeCommand = _run,
) -> WorkspaceBinding:
    """Resolve a dispatched run, provision once, then verify and reuse its owned workspace."""
    if session_role not in SESSION_ROLES:
        raise WorkspaceError("unsupported session role")
    owner, repo_name, number = _parse_issue(reference)
    issue = f"{owner}/{repo_name}#{number}"
    root = Path(specs_root).resolve()
    issue_key, lock_path = _issue_lock(root, owner, repo_name, number)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run_path = root / ".runs" / f"{issue_key}.json"
        if not run_path.is_file():
            raise WorkspaceError(f"no dispatched run for {issue}")
        try:
            run = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkspaceError("dispatched run record is invalid") from error
        if (
            run.get("issue") != issue
            or run.get("repository") != f"{owner}/{repo_name}"
            or not isinstance(run.get("run_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run["run_id"])
            or not isinstance(run.get("rig"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run["rig"])
            or not isinstance(run.get("snapshot"), str)
            or not Path(run["snapshot"]).is_file()
        ):
            raise WorkspaceError("dispatched run identity/snapshot is invalid")
        if run.get("stage") in {"failed", "paused"}:
            raise WorkspaceError(f"run stage {run['stage']} does not permit workspace provisioning")

        if session_role in {"spec", "integrator", "final-review"}:
            kind, identity = "spec", "spec"
        elif session_role in {"implementer", "reviewer"}:
            kind = "task"
            identity = _safe_token(task_id, "task identity") if task_id else ""
            if not identity:
                raise WorkspaceError(f"{session_role} requires a task identity")
        else:
            raise WorkspaceError("unsupported session role")
        key = "spec" if kind == "spec" else f"task:{identity}"
        workspaces = run.setdefault("workspaces", {})
        intents = run.setdefault("workspace_intents", {})
        if not isinstance(workspaces, dict) or not isinstance(intents, dict):
            raise WorkspaceError("run workspace records are malformed")
        recorded = workspaces.get(key)
        intent = intents.get(key)
        if not recorded and not intent and not create:
            raise WorkspaceError("no recorded workspace; refusing to create during resume")
        if not recorded and not intent and session_role not in {"spec", "implementer"}:
            raise WorkspaceError("this session role can only bind an existing workspace")

        city_root = root.parent.parent
        repository, default_branch = _rig_repository(run, city_root, command)
        workspace_root = city_root / ".gc" / "worktrees" / run["rig"]

        if recorded:
            request = _request_from_record(recorded, session_role)
            if request.repository != repository or request.workspace_root != workspace_root.resolve():
                raise WorkspaceError("recorded workspace repository/root no longer matches rig configuration")
            verified = _verified(request, command)
            if verified["provenance"] != recorded.get("provenance"):
                raise WorkspaceError("native provenance differs from centrally recorded ownership evidence")
            return WorkspaceBinding(request=request, provenance=verified["provenance"])

        if intent:
            request = _request_from_record(intent, session_role)
            verified = _verified(request, command)
        else:
            if kind == "spec":
                if not bead_id or not store_ref:
                    raise WorkspaceError("initial spec workspace requires its run Beads identity")
                fetch_ref = f"refs/heads/{default_branch}"
                fetch_refspec = f"{fetch_ref}:refs/remotes/origin/{default_branch}"
                _external_text(command, ["git", "-C", str(repository), "fetch", "--no-tags", "origin", fetch_refspec])
                base_ref = f"refs/remotes/origin/{default_branch}"
                base_sha = _external_text(command, ["git", "-C", str(repository), "rev-parse", base_ref]).strip()
                task_identity = "spec"
                workspace_bead, workspace_store = bead_id, store_ref
            else:
                if not bead_id or not store_ref:
                    raise WorkspaceError("initial task workspace requires its implementation Beads identity")
                _check_prerequisites(prerequisite_ids, prerequisite_results or {})
                spec_record = workspaces.get("spec")
                if not isinstance(spec_record, dict):
                    raise WorkspaceError("task workspace requires a recorded spec integration workspace")
                spec_request = _request_from_record(spec_record, "integrator")
                if spec_request.repository != repository or spec_request.workspace_root != workspace_root.resolve():
                    raise WorkspaceError("spec workspace identity does not match selected rig")
                spec_verified = _verified(spec_request, command)
                if spec_verified["provenance"] != spec_record.get("provenance"):
                    raise WorkspaceError("spec workspace native ownership evidence changed")
                base_sha = _head(spec_request.workspace_path)
                base_ref = base_sha
                task_identity = identity
                workspace_bead, workspace_store = bead_id, store_ref
            request = WorkspaceRequest(
                run_id=run["run_id"], issue=issue, rig=run["rig"], workspace_kind=kind,
                session_role=session_role, repository=repository, workspace_root=workspace_root,
                task_id=task_identity, bead_id=workspace_bead, store_ref=workspace_store,
                base_ref=base_ref, base_sha=base_sha,
            )
            intent = _request_record(request)
            intents[key] = intent
            _atomic_json(run_path, run)
            verified = ensure_workspace(request, native_command=command)

        record = _request_record(request) | {"provenance": verified["provenance"]}
        workspaces[key] = record
        intents.pop(key, None)
        _atomic_json(run_path, run)
        return WorkspaceBinding(request=request, provenance=verified["provenance"])


def launch(
    target: WorkspaceRequest | WorkspaceBinding,
    argv: Sequence[str],
    *,
    native_command: NativeCommand = _run,
    **run_options,
):
    """Verify ownership immediately before starting a fresh process in its actual workspace cwd."""
    if not argv:
        raise WorkspaceError("worker command cannot be empty")
    request = target.request if isinstance(target, WorkspaceBinding) else target
    verified = _verified(request, native_command)
    if isinstance(target, WorkspaceBinding) and verified["provenance"] != target.provenance:
        raise WorkspaceError("workspace provenance changed since this session binding was resolved")
    try:
        return subprocess.run(list(argv), cwd=request.workspace_path, **run_options)
    except OSError as error:
        raise WorkspaceError(f"worker process could not start: {argv[0]}") from error
