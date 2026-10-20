# Factory workspace and launch contract

`commands.midgar.workspace` provisions a factory-owned workspace and starts fresh processes only after verifying it. It does not run agents, schedule tasks, fetch branches, or change Gas City. Run/task identity and Beads IDs are validated inputs from the factory; agents do not choose a path or branch.

## Provisioning

Construct a `WorkspaceRequest` from the validated run/task and the target rig's configured per-rig worktree root. `role` is `spec`, `implementer`, `reviewer`, `integrator`, or `final-review`. The request derives the worktree as a deterministic direct child of `workspace_root`, the branch, owner (`midgar-factory`), and generation fence from run/role/task identity. It carries the Beads bead/store reference as native ownership fields. `base_ref` and full `base_sha` are recorded exactly; callers cannot supply a workspace path or branch.

```python
from commands.midgar.workspace import WorkspaceRequest, ensure_workspace

request = WorkspaceRequest(
    run_id=run["run_id"], issue=run["issue"], rig=run["rig"], role="implementer",
    repository=rig_repository, workspace_root=configured_worktree_root,
    task_id=task["id"], bead_id=task["bead_id"], store_ref=task["store_ref"],
    base_ref=integration_ref, base_sha=integration_sha,
    prerequisites_delivered=task_delivery_guard_passed,
)
workspace = ensure_workspace(request)
```

The spec workspace caller must fetch the target default branch before choosing its exact base ref/SHA. Task callers must wait for successful prerequisite review, integration, and checks and pass the then-current spec integration head. Non-spec requests require `prerequisites_delivered=True`; it defaults to false. The boolean is a caller-side gate, not a replacement scheduler or independent proof of Beads delivery.

Provisioning invokes installed `gc worktree ensure --json` with the complete expected identity, then `gc worktree verify --json`. Gas City's native worktree manager performs transactional creation/rollback and records ownership provenance in the worktree's private Git metadata. Midgar verifies returned identity/provenance and the actual Git repository root and attached branch before returning. A repeated ensure verifies and reuses the same workspace; a failed ensure is surfaced without an alternate worktree path or manual cleanup attempt.

## Launch

Use `launch(request, argv, ...)` for every fresh implementer, reviewer, integrator, or final-review process. It performs native verification and local Git root/branch checks immediately before `subprocess.run(..., cwd=request.workspace_path)`. The process therefore observes that cwd from its first instruction. Missing/wrong ownership, repository, worktree root, or branch blocks launch; there is no rig-checkout fallback. A process already running is never moved by changing metadata; bind a fresh process with the relevant request.

The native worktree result's provenance, including opaque `repo_identity`, `created_at`, and `attempt_id`, is returned to the caller for durable workspace identity recording alongside run/task references. Do not reconstruct provenance from path names. Native cleanup should later use Gas City's ownership-aware cleanup contract and the recorded `attempt_id`; this module deliberately does not implement cleanup.

## Validation evidence and limits

Deterministic tests use temporary Git repositories and a probe process to observe its actual initial cwd and branch; controlled native-command responses exercise the fields Midgar consumes. The installed CLI contract was checked with `gc version` (1.5.0), `gc worktree ensure/verify --help`, result/failure schemas, and a temporary-repository ensure→verify run. The native CLI requires repo/root/path/branch/base/base-SHA/bead/store-ref/owner/creator/generation/lifecycle and returns provenance. This establishes the consumed provisioning contract, not a live Gas City session or model launch. The wrapper is the launch boundary; no claim is made that city formula/session wiring is implemented here.
