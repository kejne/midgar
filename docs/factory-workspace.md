# Factory workspace and launch contract

Use `provision_workspace_for_run` as the run-aware public entry point. It resolves the dispatched run from Midgar's central `.gc/specs/.runs` record, revalidates the registered rig and its GitHub origin, and derives the repository, per-rig root, workspace path, branch, owner, and generation. Agents never choose those values. The low-level `WorkspaceRequest`/`ensure_workspace` API exists for narrow native-contract tests and adapters; factory callers should not assemble it from arbitrary repository/base inputs.

## Resolve or provision

```python
from commands.midgar.workspace import provision_workspace_for_run

# Initial integration workspace: fetches the rig's configured default branch once.
spec = provision_workspace_for_run(
    "owner/repo#42", "spec", create=True,
    bead_id=run_bead_id, store_ref=run_bead_store,
)

# An implementation task is based on the current verified spec HEAD, after
# every planned prerequisite has durable delivered/review/integration/check success.
task = provision_workspace_for_run(
    "owner/repo#42", "implementer", create=True, task_id="task-2",
    bead_id=task_bead_id, store_ref=task_bead_store,
    prerequisite_ids=["task-1"],
    prerequisite_results={"task-1": {
        "status": "delivered", "review": "passed",
        "integration": "passed", "checks": "passed",
    }},
)

# Fresh reviewer/integrator/final-review sessions bind existing workspace identity.
review = provision_workspace_for_run("owner/repo#42", "reviewer", task_id="task-2")
final_review = provision_workspace_for_run("owner/repo#42", "final-review")
```

`session_role` is independent of workspace identity. `spec`, `integrator`, and `final-review` bind the one `spec` workspace; `implementer` and `reviewer` bind the specified `task:<task_id>` workspace. Reviewer/integrator/final-review calls never create a role-specific worktree. A `WorkspaceBinding` can be passed to `launch(binding, argv, ...)` to start the fresh process in the verified checkout's actual initial cwd.

Only an explicit `create=True` call may begin initial provisioning, and only `spec` or `implementer` may do so. The default is resume: a missing central record is an error and never invokes `gc worktree ensure`. The per-rig native root is derived as `<city>/.gc/worktrees/<rig>` from the central specs directory and dispatched rig. Creation first persists a deterministic workspace intent under the run lock, then invokes native ensure and verify, then atomically replaces the intent with the complete request identity and native provenance. If interrupted between those steps, resume verifies the intent with native `verify` only; it either records that same native workspace or fails closed. It does not fetch again or call ensure. A recorded workspace is likewise verified and reused, never recreated. The lock is shared with dispatch/stage writes and serializes same-run provisioning.

The spec base uses the default branch from the revalidated `gc rig list --json` record. Initial creation fetches `origin`'s exact configured branch into its remote-tracking ref, resolves the resulting full SHA, and records both ref and SHA. Reuse does not refetch or silently move the spec base.

Task creation requires an existing centrally recorded spec workspace and native ownership verification. `prerequisite_ids` must exactly match the keys in `prerequisite_results`; every result must state `status=delivered`, `review=passed`, `integration=passed`, and `checks=passed`. This is the explicit success-evidence contract for the later plan/task-delivery layer, not a replacement scheduler. The helper then resolves the current `HEAD` of the verified spec integration worktree and uses that full SHA as both the task base ref and base SHA. This captures prerequisite commits that have since been integrated. Empty prerequisite lists are valid for independent tasks.

## Native ownership and process startup

Provisioning uses installed `gc worktree ensure --json` and `gc worktree verify --json`; Gas City owns transactional worktree creation/rollback and private Git-dir provenance. Midgar passes the run/task-derived branch/path, exact base, Beads bead/store reference, owner and generation, then checks the native result against its intent and verifies actual Git repository root and attached branch. The complete native provenance (`repo_identity`, `created_at`, `attempt_id`, and all other returned fields) is persisted in the active run record, alongside the request identity and exact base, for resume and later cleanup. `WorkspaceBinding.native_metadata` exposes the complete routed-work-bead stamp (`work_dir`, `gc.work_dir`, `gc.worktree_repo`, `gc.worktree_root`, `gc.work_branch`, `gc.worktree_base_ref`, `gc.worktree_base_sha`, `gc.worktree_creator`, `gc.worktree_owner`, `gc.worktree_generation`, `gc.worktree_lifecycle`, and `gc.root_store_ref`) for runtimes whose pool verifier binds worktree ownership to that routed bead. The factory delivery path uses the documented deterministic pre-model adapter instead: native ownership is tied to the source task/run bead, so copying it onto distinct review/integration beads would be rejected. The adapter claims the routed bead, resolves the source task, verifies the original #7 binding, then launches a fresh model process with that verified cwd. It does not stamp incompatible ownership metadata or use a rig-checkout fallback. The native `attempt_id` remains in the returned/persisted provenance for cleanup and is not a pool metadata field.

Immediately before process start, `launch` repeats native and local Git verification, checks that a `WorkspaceBinding`'s provenance has not changed, then calls `subprocess.run` with that worktree as `cwd`. Wrong/missing identity or ownership prevents launch; there is no rig-checkout fallback. A running process is never moved by metadata updates: a different role gets a fresh binding and process. This helper does not implement worktree cleanup; later cleanup must use Gas City's native ownership-aware operation and the persisted `attempt_id`.

## Validation evidence and limits

Temporary-repository tests cover fetched initial spec base, no-refetch verify-only resume, persisted provenance, current spec-head task bases behind explicit prerequisite success evidence, failed-gate refusal, missing-record/worktree refusal, concurrent initial resolution, complete routed-bead metadata, and reviewer/integrator/final-review bindings. Probe tests observe actual startup cwd and branch. Controlled `gc` responses test Midgar's consumed contract; they do not reproduce or claim native runtime correctness. The installed CLI contract was checked with `gc version` (1.5.0), help and schemas, and an actual temporary-repository ensure→verify run. Gas City's pool metadata-to-launch-cwd contract was inspected as source design input only; installed 1.5.0 behavior for routed bead metadata and reconciler cwd binding remains a runtime checkpoint. This issue does not wire formulas or run a city. No live city or model is required by `hk check`.
