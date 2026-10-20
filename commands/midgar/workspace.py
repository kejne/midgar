"""Owned workspace provisioning and fail-closed process startup for Midgar."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class WorkspaceError(Exception):
    """A workspace cannot be safely provisioned or launched."""


NativeCommand = Callable[[list[str]], str]
ROLES = {"spec", "implementer", "reviewer", "integrator", "final-review"}
PROVENANCE_FIELDS = {
    "schema_version", "bead_id", "store_ref", "repo_identity", "path", "branch",
    "base_ref", "base_sha", "creator", "owner", "generation", "created_at",
    "lifecycle", "attempt_id",
}


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkspaceError(f"native workspace command failed: {' '.join(argv)}") from error


def _safe_token(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise WorkspaceError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class WorkspaceRequest:
    """Validated factory identity; location and branch are derived, never caller-selected."""

    run_id: str
    issue: str
    rig: str
    role: str
    repository: str | Path
    workspace_root: str | Path
    task_id: str
    bead_id: str
    store_ref: str
    base_ref: str
    base_sha: str
    prerequisites_delivered: bool = False

    def __post_init__(self) -> None:
        _safe_token(self.run_id, "run identity")
        _safe_token(self.rig, "rig")
        _safe_token(self.task_id, "task identity")
        _safe_token(self.bead_id, "Beads identity")
        if not isinstance(self.store_ref, str) or not self.store_ref.strip():
            raise WorkspaceError("Beads store reference is required")
        if self.role not in ROLES:
            raise WorkspaceError("unsupported workspace role")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+", self.issue):
            raise WorkspaceError("issue identity must be OWNER/REPO#NUMBER")
        if self.issue != self.issue.lower():
            raise WorkspaceError("issue identity must use canonical lowercase owner/repository")
        if not isinstance(self.prerequisites_delivered, bool):
            raise WorkspaceError("prerequisite delivery gate must be boolean")
        if not self.prerequisites_delivered and self.role != "spec":
            raise WorkspaceError("task workspace requires successfully delivered prerequisites")
        if not isinstance(self.base_ref, str) or not self.base_ref.strip() or self.base_ref.startswith("-"):
            raise WorkspaceError("an exact base ref is required")
        if not isinstance(self.base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.base_sha):
            raise WorkspaceError("base SHA must be a full object ID")
        repo = Path(self.repository).resolve()
        root = Path(self.workspace_root).resolve()
        if not repo.is_dir() or not root.is_absolute() or repo == root:
            raise WorkspaceError("repository and per-rig workspace root must be valid distinct directories")
        object.__setattr__(self, "repository", repo)
        object.__setattr__(self, "workspace_root", root)

    @property
    def identity(self) -> str:
        return f"{self.run_id}:{self.role}:{self.task_id}"

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
        return Path(self.workspace_root) / f"{self.role}-{key}"

    @property
    def branch(self) -> str:
        key = hashlib.sha256(self.identity.encode()).hexdigest()[:12]
        return f"factory/{self.role}-{key}"

    @property
    def expected_provenance(self) -> dict:
        return {
            "schema_version": 1,
            "bead_id": self.bead_id,
            "store_ref": self.store_ref,
            "path": str(self.workspace_path),
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "creator": self.creator,
            "owner": self.owner,
            "generation": self.generation,
            "lifecycle": "active",
        }


def _arguments(request: WorkspaceRequest, action: str) -> list[str]:
    return [
        "gc", "worktree", action,
        "--repo", str(request.repository), "--root", str(request.workspace_root),
        "--path", str(request.workspace_path), "--branch", request.branch,
        "--base", request.base_ref, "--base-sha", request.base_sha,
        "--bead", request.bead_id, "--store-ref", request.store_ref,
        "--owner", request.owner, "--creator", request.creator,
        "--generation", request.generation, "--lifecycle", "active", "--json",
        "--rig", str(request.repository),
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
    """Use Gas City's transactional worktree ensure, then verify its durable evidence."""
    Path(request.workspace_root).mkdir(parents=True, exist_ok=True)
    _response(native_command, _arguments(request, "ensure"))
    return _verified(request, native_command)


def launch(
    request: WorkspaceRequest,
    argv: Sequence[str],
    *,
    native_command: NativeCommand = _run,
    **run_options,
):
    """Verify ownership immediately before starting a fresh process in its actual cwd."""
    if not argv:
        raise WorkspaceError("worker command cannot be empty")
    _verified(request, native_command)
    try:
        return subprocess.run(list(argv), cwd=request.workspace_path, **run_options)
    except OSError as error:
        raise WorkspaceError(f"worker process could not start: {argv[0]}") from error
