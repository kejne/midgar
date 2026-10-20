import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from commands.midgar.delivery import (
    DeliveryError, _task_prompt, _write_result, build_pi_argv, require_delivered,
    resolve_task_context, run_model, validate_role_report, verify_role_commit,
)
from commands.midgar.workspace import WorkspaceBinding, WorkspaceRequest


class DeliveryGateTests(unittest.TestCase):
    def test_closed_but_failed_prerequisite_is_not_delivered(self):
        with self.assertRaisesRegex(DeliveryError, "not delivered"):
            require_delivered({"closed-task": {
                "status": "closed", "outcome": "fail", "review": "passed",
                "integration": "passed", "checks": "passed",
            }})

    def test_delivery_requires_explicit_success_for_every_gate(self):
        self.assertTrue(require_delivered({"task-a": {
            "status": "delivered", "review": "passed", "integration": "passed", "checks": "passed",
        }}))
        with self.assertRaisesRegex(DeliveryError, "checks"):
            require_delivered({"task-a": {
                "status": "delivered", "review": "passed", "integration": "passed",
            }})

    def test_review_axes_are_independent_blocking_gates(self):
        passed = {"standards": "passed", "spec": "passed", "standards_evidence": "mandatory rules followed",
                  "spec_evidence": "all task criteria verified", "blocking_findings": [], "commit": "a" * 40}
        self.assertEqual(validate_role_report("reviewer", passed)["verdict"], "passed")
        self.assertEqual(validate_role_report("reviewer", passed)["commit"], "a" * 40)
        for report in (
            {"standards": "blocked", "spec": "passed", "standards_evidence": "mandatory rule violated", "spec_evidence": "covered",
             "blocking_findings": ["mandatory standard"], "commit": "a" * 40},
            {"standards": "passed", "spec": "blocked", "standards_evidence": "rules followed", "spec_evidence": "missing AC",
             "blocking_findings": ["missing AC"], "commit": "a" * 40},
            {"standards": "passed", "spec": "passed", "standards_evidence": "rules followed", "spec_evidence": "incorrect behavior",
             "blocking_findings": ["incorrect behavior"], "commit": "a" * 40},
        ):
            with self.assertRaises(DeliveryError):
                validate_role_report("reviewer", report)

    def test_advisory_smells_do_not_block_review(self):
        report = {
            "standards": "passed", "spec": "passed", "standards_evidence": "rules followed",
            "spec_evidence": "criteria met", "blocking_findings": [], "commit": "a" * 40,
            "advisory_findings": ["consider renaming helper"],
        }
        self.assertEqual(validate_role_report("reviewer", report)["verdict"], "passed")

    def test_unrecognized_or_incomplete_role_reports_fail_closed(self):
        with self.assertRaisesRegex(DeliveryError, "malformed"):
            validate_role_report("reviewer", {"standards": "passed"})
        with self.assertRaisesRegex(DeliveryError, "malformed"):
            validate_role_report("reviewer", {"standards": "passed", "spec": "passed", "blocking_findings": []})
        with self.assertRaisesRegex(DeliveryError, "unsupported"):
            validate_role_report("manager", {"outcome": "pass"})

    def test_claimed_role_resolves_exactly_one_task_from_native_input_convoy(self):
        claimed = {
            "id": "role-review", "title": "Review", "description": "review task",
            "metadata": {
                "gc.root_bead_id": "factory-root", "midgar.delivery_role": "reviewer",
                "midgar": {"issue": "owner/repo#9", "run_id": "run-9", "task_id": "task-1"},
            },
        }
        class Native:
            def __call__(self, argv):
                if argv[3:6] == ["bd", "show", "factory-root"]:
                    return json.dumps({"id": "factory-root", "metadata": {"gc.input_convoy_id": "unit-cv"}})
                if argv[3:6] == ["convoy", "status", "unit-cv"]:
                    return json.dumps({"convoy": {"id": "unit-cv"}, "children": [{"id": "task-bead"}]})
                if argv[3:6] == ["bd", "show", "task-bead"]:
                    return json.dumps({"id": "task-bead", "title": "Task", "acceptance": "AC-1", "metadata": {
                        "midgar": {"issue": "owner/repo#9", "run_id": "run-9", "task_id": "task-1"}
                    }})
                raise AssertionError(argv)
        context = resolve_task_context(claimed, Native(), rig="repo")
        self.assertEqual((context["role"], context["task_bead_id"], context["task_id"]),
                         ("reviewer", "task-bead", "task-1"))

    def test_claimed_role_rejects_ambiguous_source_convoy(self):
        claimed = {"id": "role", "metadata": {"gc.root_bead_id": "root", "midgar.delivery_role": "implementer"}}
        def native(argv):
            if argv[3:6] == ["bd", "show", "root"]:
                return json.dumps({"id": "root", "metadata": {"gc.input_convoy_id": "cv"}})
            if argv[3:6] == ["convoy", "status", "cv"]:
                return json.dumps({"children": [{"id": "one"}, {"id": "two"}]})
            raise AssertionError(argv)
        with self.assertRaisesRegex(DeliveryError, "exactly one source task"):
            resolve_task_context(claimed, native, rig="repo")

    def test_one_shot_pi_argv_is_fresh_and_never_asks_the_model_to_claim(self):
        argv = build_pi_argv("claimed bead task", "factory system prompt", "implementer")
        self.assertEqual(argv[:3], ["pi", "--print", "--no-session"])
        self.assertIn("--system-prompt", argv)
        self.assertEqual(argv[-2:], ["--", "claimed bead task"])
        self.assertNotIn("gc gc claim", " ".join(argv))

    def test_integrator_cannot_report_success_without_reviewed_commit_reachability(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, task_binding, spec_binding, base, task_head = self._task_and_spec_worktrees(Path(temp))
            # A no-op integrator can return the current spec HEAD, but the reviewed task commit is absent.
            with self.assertRaisesRegex(DeliveryError, "not reachable"):
                verify_role_commit(
                    "integrator", {"commit": base, "reviewed_commit": task_head}, spec_binding,
                    task_binding=task_binding,
                    task_metadata={"midgar.delivery.status": "in_progress",
                                   "midgar.delivery.review": "passed",
                                   "midgar.delivery.review_commit": task_head,
                                   "midgar.delivery.implementation_commit": task_head},
                )

    def test_integrator_accepts_only_reachable_reviewed_task_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, task_binding, spec_binding, _, task_head = self._task_and_spec_worktrees(Path(temp))
            subprocess.run(["git", "-C", str(spec_binding.path), "merge", "--no-edit", "--no-ff", task_binding.branch], check=True)
            spec_head = subprocess.check_output(["git", "-C", str(spec_binding.path), "rev-parse", "HEAD"], text=True).strip()
            verify_role_commit(
                "integrator", {"commit": spec_head, "reviewed_commit": task_head}, spec_binding,
                task_binding=task_binding,
                task_metadata={"midgar.delivery.review": "passed", "midgar.delivery.review_commit": task_head,
                               "midgar.delivery.implementation_commit": task_head},
            )

    def test_integrator_rejects_task_head_changed_since_scoped_review(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, task_binding, spec_binding, base, task_head = self._task_and_spec_worktrees(Path(temp))
            (Path(task_binding.path) / "tracked").write_text("changed again")
            subprocess.run(["git", "-C", str(task_binding.path), "add", "tracked"], check=True)
            subprocess.run(["git", "-C", str(task_binding.path), "commit", "-qm", "unreviewed edit"], check=True)
            task_metadata = {"midgar.delivery.status": "in_progress", "midgar.delivery.review": "passed",
                             "midgar.delivery.review_commit": task_head,
                             "midgar.delivery.implementation_commit": task_head}
            with self.assertRaisesRegex(DeliveryError, "changed after review"):
                verify_role_commit("integrator", {"commit": base, "reviewed_commit": task_head}, spec_binding,
                                   task_binding=task_binding, task_metadata=task_metadata)

    def test_reviewer_report_is_bound_to_unchanged_scoped_head(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, task_binding, _, _, task_head = self._task_and_spec_worktrees(Path(temp))
            with self.assertRaisesRegex(DeliveryError, "reviewed HEAD"):
                verify_role_commit("reviewer", {"commit": task_head}, task_binding,
                                   review_head="0" * 40)

    @staticmethod
    def _task_and_spec_worktrees(root):
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
        (repo / "tracked").write_text("base")
        subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        workroot = root / "worktrees"
        workroot.mkdir()
        task_request = WorkspaceRequest("run", "owner/repo#9", "repo", "task", "reviewer", repo, workroot, "task-1", "task-owner", "rig:repo", base, base)
        spec_request = WorkspaceRequest("run", "owner/repo#9", "repo", "spec", "integrator", repo, workroot, "spec", "spec-owner", "city:city", base, base)
        task_path, spec_path = task_request.workspace_path, spec_request.workspace_path
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", task_request.branch, str(task_path), base], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", spec_request.branch, str(spec_path), base], check=True)
        for path in (task_path, spec_path):
            subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
        (task_path / "tracked").write_text("task change")
        subprocess.run(["git", "-C", str(task_path), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(task_path), "commit", "-qm", "task commit"], check=True)
        task_head = subprocess.check_output(["git", "-C", str(task_path), "rev-parse", "HEAD"], text=True).strip()
        return repo, WorkspaceBinding(task_request, {}), WorkspaceBinding(spec_request, {}), base, task_head

    def test_workspace_failure_is_recorded_on_claim_and_source_task(self):
        calls = []
        context = {"role": "reviewer", "bead_id": "role-review", "task_bead_id": "task-bead", "rig": "repo"}
        _write_result(context, lambda argv: calls.append(argv) or "", {}, passed=False,
                      reason="native workspace verify refused", failure_class="workspace_failure")
        self.assertEqual(calls[0][4], "update")
        self.assertIn("gc.outcome=fail", calls[0])
        self.assertIn("midgar.delivery.failure_class=workspace_failure", calls[0])
        self.assertEqual(calls[1][5], "task-bead")
        self.assertIn("midgar.delivery.last_failure_class=workspace_failure", calls[1])

    def test_role_prompt_contains_task_acceptance_and_prior_rework_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            _, task_binding, _, _, _ = self._task_and_spec_worktrees(Path(temp))
            snapshot = Path(temp) / "snapshot.json"
            snapshot.write_text('{"requirements":["AC-1"]}')
            context = {
                "role": "implementer", "bead_id": "role-impl", "task_bead_id": "task-1",
                "task_bead": {"title": "Add behavior", "description": "Implement it", "acceptance": ["AC-1", "AC-2"],
                              "metadata": {"midgar.delivery.last_failure_reason": "review found missing AC-2"}},
                "role_bead": {"id": "role-impl", "title": "Implement", "description": "Commit this slice"},
            }
            prompt = _task_prompt(context, {"snapshot": str(snapshot)}, task_binding)
            self.assertIn("AC-1", prompt)
            self.assertIn("AC-2", prompt)
            self.assertIn("review found missing AC-2", prompt)

    def test_probe_model_starts_in_the_verified_workspace_with_claim_context(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            root = Path(temp) / "worktrees"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
            (repo / "tracked").write_text("seed")
            subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            request = WorkspaceRequest(
                run_id="run-9", issue="owner/repo#9", rig="repo", workspace_kind="task",
                session_role="implementer", repository=repo, workspace_root=root, task_id="task-1",
                bead_id="source-task", store_ref="rig:repo", base_ref=base, base_sha=base,
            )
            root.mkdir()
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", request.branch, str(request.workspace_path), base], check=True)
            provenance = {
                "schema_version": 1, "bead_id": "source-task", "store_ref": "rig:repo",
                "repo_identity": str(repo.resolve()), "path": str(request.workspace_path), "branch": request.branch,
                "base_ref": base, "base_sha": base, "creator": "midgar-factory", "owner": "midgar-factory",
                "generation": request.generation, "created_at": "2025-01-01T00:00:00Z", "lifecycle": "active",
                "attempt_id": "attempt-1",
            }
            def native(argv):
                return json.dumps({"ok": True, "path": str(request.workspace_path), "branch": request.branch, "provenance": provenance})
            probe = Path(temp) / "probe.py"
            probe.write_text("import json,os,subprocess; print(json.dumps({'cwd':os.getcwd(),'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),'role':os.environ['MIDGAR_FACTORY_ROLE'],'bead':os.environ['MIDGAR_CLAIMED_BEAD_ID']}))")
            result = run_model(
                WorkspaceBinding(request, provenance), "task input", "system", role="implementer",
                claimed_bead_id="review-child", pi_bin=sys.executable, pi_prefix=[str(probe)],
                native_command=native,
            )
            observed = json.loads(result.stdout)
            self.assertEqual(observed, {"cwd": str(request.workspace_path), "branch": request.branch,
                                         "role": "implementer", "bead": "review-child"})


if __name__ == "__main__":
    unittest.main()
