# Task delivery

The city-owned `factory-task-delivery` formula is the single reusable task graph. A check scope of three total attempts covers implementation, fresh review, serial integration, and check. The same city-imported rig agent (`midgar.factory-worker`) runs each role as a one-shot, fresh Pi process; the target rig imports it through `vendor/midgar`. Rigs do not copy the formula.

## Verified startup boundary

The factory does **not** stamp native worktree ownership metadata onto formula-routed role beads. Gas City's native pool worktree verifier binds `worktree.Spec.BeadID` to the routed bead ID, while the durable #7 workspace is owned by the source task (or run bead). Stamping that source ownership onto reviewer/integrator children would fail native verification.

Instead, the pool starts only the deterministic factory adapter from its ordinary rig cwd. The adapter invokes the installed `gc gc claim` command once, accepts only a normalized `action=work` result, resolves the workflow root's single-member input convoy to the source implementation task, verifies issue/run/task identity and the fixed snapshot, and resolves the recorded #7 binding. The adapter calls `launch(binding, ...)`, which repeats native ownership and Git checks and starts a fresh `pi --print --no-session --approve ...` subprocess with the verified workspace as its actual initial cwd. The model receives the already-claimed role bead and snapshot; its prompt prohibits another claim. It cannot choose or construct a workspace.

`gc prime --strict --hook RIG/midgar.factory-worker` supplies only the static wrapper/role-report contract. The claimed bead description supplies the authoritative role and task instructions. Integrator startup additionally binds the source task's verified task workspace to compare the task HEAD with the review-scoped SHA, then launches Pi in the verified spec integration checkout.

## Result gates

The adapter records `gc.outcome` and the structured report on the exact claimed role bead. It updates the source task's delivery metadata only after validating role-specific evidence. Review requires independent Standards and Spec results, separate evidence, no blocking findings, and a full commit SHA matching the unchanged task HEAD. Integration requires that same task HEAD and review SHA plus a captured pre-integration spec HEAD. The result must be an exact fast-forward of the reviewed commit or a clean no-fast-forward merge with that captured HEAD and reviewed commit as its two parents; the resulting tree must equal Git's conflict-free merge tree. Extra integrator commits, conflict resolutions, and unrelated content fail closed. An already-included reviewed commit is an idempotent no-op. A model's claimed pass cannot establish delivery.

The native formula check script invokes exactly plain `hk check` with cwd set to the verified spec integration worktree. It marks the source task `delivered` only after review, integration, and that check are explicitly evidenced and `hk check` exits successfully. No commit-bound validation receipt is recorded. Failure diagnostics distinguish workspace, prerequisite, review, implementation, and integration failures; rework prompts retain the latest failure reason. Closed beads alone are never accepted as delivery evidence.

## Evidence boundary

Offline tests use temporary Git repositories, controlled native-command responses, and a probe executable in place of Pi to observe the actual process cwd, branch, role, and claimed bead. `gc config show`, `gc prime --strict --hook`, and `gc formula show` are exercised against temporary configuration/formula roots. They do not prove the live controller starts the adapter with the expected session environment or that its installed pool launches the provider as configured. The bounded one-task runtime checkpoint remains #11; parallel DAG behavior remains #12.
