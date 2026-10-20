# One-task runtime checkpoint (#11)

**Status: blocked before fixture dispatch; no runtime/model smoke was run.** This checkpoint is distinct from the full gascity-irc-pack#6 pilot. Do not interpret the evidence below as delivery, runtime success, or proof of the workspace/claim/session contract.

## Readiness investigation

Starting context: `/tmp/midgar-issue5/smoke-11`, branch `feat/factory-11`, `HEAD=c07a79b` (ancestor verified). Installed Gas City is `gc 1.5.0`.

The supplied root-city readiness probe, `gc prime gascity-irc-pack/midgar.factory-worker --strict --hook --city "$PWD"`, timed out at 30s during merge; see `/tmp/midgar-issue5/merge9-prime-timeout.md`. I did not repeat it. A narrower read-only check, `timeout 8s gc --city /home/kejne/git/personal/github.com/kejne/midgar rig list --json`, also timed out (`124`) with empty stdout/stderr (captured outside the repository as `/tmp/midgar-issue5/smoke11-rig-list.{json,err}`). This prevents safely resolving the pilot rig/runtime contract, so no factory/run/spec fixture was created and no beads were claimed.

The CLI help establishes the installed command path: `gc midgar gc claim` (`gc midgar gc --help` lists `claim`, and `gc midgar gc claim --help` returns command help); `gc gc claim` falls through to top-level help. The 1.5.0 help contract says the command invokes `gc hook --claim --drain-ack --json`, then for `action=work` re-reads and verifies bead ID, status, assignee, and route before returning normalized JSON; drain acknowledges no remaining work. Hook/bead-read failures are retried at most three times. This is help-described behavior, not runtime-exercised behavior. The worker wrapper was corrected to use the installed nested command; a focused offline regression verifies its invocation. Neither help nor the controlled-wrapper test establishes a live claim result.

Temporary-city native compiler/config tests pass under plain `hk check`; these establish only formula/config compatibility. The intentionally clean-check skip was observed before editing. No strict-prime, runtime session startup, native routed-bead ownership, model process, actual initial cwd/branch, review, spec integration, or delivered task was observed in this checkpoint.

## Bounded rerun boundary

After the root-city readiness blocker is understood, rerun only with an explicitly smoke-only task and isolated run/spec storage, isolated task/spec workspace branches owned by the native worktree API, and the city-owned `factory-task-delivery` graph on the pilot rig. Use no gascity-irc-pack#6 acceptance criteria or feature work. Record the exact command/output and actual cwd, branch, native ownership, task/review/integration commit IDs, and plain `hk check` result. Preserve native changed-file skips; task status is delivered only if the graph records successful review, integration, and applicable checks. Keep all fixture state out of the pilot's real #16 dispatch and never publish or close the pilot issue.
