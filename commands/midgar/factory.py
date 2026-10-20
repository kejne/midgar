"""Deterministic issue dispatch and fixed spec snapshots for Midgar."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


class FactoryError(Exception):
    """Input or external state prevents a safe factory dispatch."""


IssueReference = tuple[str, str, int]
Command = Callable[[list[str]], str]
ISSUE_PATTERN = re.compile(
    r"^(?:https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)|([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+))/?$"
)


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise FactoryError(f"command failed: {' '.join(argv)}") from error


def _parse_issue(reference: str) -> IssueReference:
    match = ISSUE_PATTERN.fullmatch(reference)
    if not match:
        raise FactoryError("issue must be a GitHub URL or OWNER/REPO#NUMBER")
    owner, repo, number, short_owner, short_repo, short_number = match.groups()
    return (owner or short_owner).lower(), (repo or short_repo).lower(), int(number or short_number)


def _json(command: Command, argv: list[str]) -> dict:
    try:
        value = json.loads(command(argv))
    except (ValueError, TypeError) as error:
        raise FactoryError(f"invalid JSON from {' '.join(argv)}") from error
    if not isinstance(value, dict):
        raise FactoryError(f"unexpected response from {' '.join(argv)}")
    return value


def _github_repository(remote: str) -> tuple[str, str]:
    remote = remote.strip()
    match = re.fullmatch(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?", remote)
    if match:
        return match.group(1).lower(), match.group(2).lower()
    parsed = urlparse(remote)
    if parsed.hostname and parsed.hostname.lower() == "github.com":
        parts = parsed.path.strip("/").removesuffix(".git").split("/")
        if len(parts) == 2 and all(parts):
            return parts[0].lower(), parts[1].lower()
    raise FactoryError("selected rig has no valid GitHub origin repository")


def _issue_lock(root: Path, owner: str, repo: str, number: int) -> tuple[str, Path]:
    identity = json.dumps([owner.lower(), repo.lower(), number], separators=(",", ":"))
    issue_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    lock_path = root / ".locks" / f"{issue_key}.lock"
    return issue_key, lock_path


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, value: dict) -> None:
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    _atomic_bytes(path, content.encode("utf-8"))


def record_stage(
    reference: str,
    stage: str,
    progress: dict,
    diagnostics: dict | None = None,
    specs_root: str | Path = ".gc/specs",
) -> dict:
    """Atomically persist factory stage, progress references, and diagnostics."""
    owner, repo, number = _parse_issue(reference)
    if not isinstance(stage, str) or not stage.strip():
        raise FactoryError("stage must be a non-empty string")
    if not isinstance(progress, dict):
        raise FactoryError("progress must be an object")
    if diagnostics is not None and not isinstance(diagnostics, dict):
        raise FactoryError("diagnostics must be an object")

    root = Path(specs_root).resolve()
    issue_key, lock_path = _issue_lock(root, owner, repo, number)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    run_path = root / ".runs" / f"{issue_key}.json"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not run_path.exists():
            raise FactoryError(f"no active run for {owner}/{repo}#{number}")
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["stage"] = stage
        run_progress = run.setdefault("progress", {})
        if not isinstance(run_progress, dict):
            raise FactoryError("active run progress is not an object")
        run_progress.update(progress)
        if diagnostics is not None:
            run_diagnostics = run.setdefault("diagnostics", [])
            if not isinstance(run_diagnostics, list):
                raise FactoryError("active run diagnostics is not a list")
            run_diagnostics.append(diagnostics)
        _atomic_json(run_path, run)
        return run


def _snapshot(issue: dict, owner: str, repo: str, number: int) -> dict:
    return {
        "issue": f"{owner}/{repo}#{number}",
        "url": issue.get("url", f"https://github.com/{owner}/{repo}/issues/{number}"),
        "title": issue.get("title", ""),
        "body": issue.get("body", ""),
        "comments": issue.get("comments", []),
    }


def dispatch(
    reference: str,
    rig_name: str,
    specs_root: str | Path = ".gc/specs",
    command: Command = _run,
    *,
    replace: bool = False,
    reconcile: bool = False,
) -> dict:
    """Validate and resolve an issue run; all run/snapshot files stay local."""
    if replace and reconcile:
        raise FactoryError("--replace and --reconcile cannot be used together")
    owner, repo, number = _parse_issue(reference)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", rig_name):
        raise FactoryError("invalid rig name")

    issue = _json(command, ["gh", "issue", "view", str(number), "--repo", f"{owner}/{repo}", "--comments", "--json", "number,title,body,url,labels,comments"])
    if issue.get("number") != number:
        raise FactoryError("GitHub returned a different issue")
    labels = issue.get("labels")
    if not isinstance(labels, list) or not any(
        (label.get("name") if isinstance(label, dict) else label) == "ready-for-agent"
        for label in labels
    ):
        raise FactoryError("issue must have the ready-for-agent label")

    rig_list = _json(command, ["gc", "rig", "list", "--json"])
    rigs = rig_list.get("rigs")
    rig = next((item for item in rigs if isinstance(item, dict) and item.get("name") == rig_name), None) if isinstance(rigs, list) else None
    if rig is None or rig.get("hq"):
        raise FactoryError(f"unknown project rig: {rig_name}")
    rig_path = rig.get("path")
    if not isinstance(rig_path, str) or not rig_path:
        raise FactoryError(f"rig has no provisioned repository path: {rig_name}")
    remote = command(["git", "-C", rig_path, "remote", "get-url", "origin"])
    if _github_repository(remote) != (owner.lower(), repo.lower()):
        raise FactoryError("issue repository does not match selected rig repository")

    root = Path(specs_root).resolve()
    issue_key, lock_path = _issue_lock(root, owner, repo, number)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        directory = root / rig_name / str(number)
        run_path = root / ".runs" / f"{issue_key}.json"
        previous = None
        if run_path.exists():
            previous = json.loads(run_path.read_text(encoding="utf-8"))
            if previous.get("rig") != rig_name and not replace:
                raise FactoryError(f"issue already belongs to rig {previous.get('rig')}")
            if not replace:
                if not reconcile:
                    return previous
                active_snapshot = Path(previous["snapshot"])
                revision = 2
                while True:
                    snapshot_path = active_snapshot.with_name(f"snapshot.revision-{revision}.json")
                    if not snapshot_path.exists():
                        break
                    revision += 1
                _atomic_json(snapshot_path, _snapshot(issue, owner, repo, number))
                previous["snapshot"] = str(snapshot_path)
                previous.setdefault("progress", {})["snapshot"] = str(snapshot_path)
                _atomic_json(run_path, previous)
                return previous

        directory.mkdir(parents=True, exist_ok=True)
        snapshot_path = directory / "snapshot.json"
        if previous is not None:
            active_snapshot = Path(previous["snapshot"])
            if not active_snapshot.is_file():
                raise FactoryError("active run snapshot is missing")
            if previous.get("rig") == rig_name:
                snapshot_path = active_snapshot
            else:
                active_bytes = active_snapshot.read_bytes()
                if snapshot_path.exists() and snapshot_path.read_bytes() != active_bytes:
                    revision = 2
                    while True:
                        candidate = directory / f"snapshot.revision-{revision}.json"
                        if not candidate.exists():
                            snapshot_path = candidate
                            break
                        if candidate.read_bytes() == active_bytes:
                            snapshot_path = candidate
                            break
                        revision += 1
                if not snapshot_path.exists():
                    _atomic_bytes(snapshot_path, active_bytes)
        elif not snapshot_path.exists():
            _atomic_json(snapshot_path, _snapshot(issue, owner, repo, number))

        run = {
            "run_id": str(uuid.uuid4()),
            "issue": f"{owner}/{repo}#{number}",
            "repository": f"{owner}/{repo}",
            "rig": rig_name,
            "stage": "dispatched",
            "progress": {"snapshot": str(snapshot_path)},
            "snapshot": str(snapshot_path),
        }
        if previous is not None:
            history = root / ".runs" / "archive"
            _atomic_json(history / f"{previous['run_id']}.json", previous)
        _atomic_json(run_path, run)
        return run
