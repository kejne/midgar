import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from commands.midgar.factory import dispatch
from commands.midgar.workspace import WorkspaceError, launch, provision_workspace_for_run


class NativeWorktreeBoundary:
    """Controlled gc boundary; Git fixtures remain real temporary worktrees."""

    def __init__(self):
        self.calls = []
        self.provenance = {}
        self.remote = "git@github.com:owner/project.git\n"

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[-3:] == ["rig", "list", "--json"]:
            return json.dumps({"ok": True, "rigs": [{"name": "project", "path": self.repo, "hq": False, "default_branch": "main"}]})
        if argv[:3] == ["gc", "worktree", "ensure"]:
            args = self.arguments(argv)
            repo, path, branch, base = (args[k] for k in ("--repo", "--path", "--branch", "--base"))
            subprocess.run(["git", "-C", repo, "worktree", "add", "-qb", branch, path, base], check=True)
            evidence = {
                "schema_version": 1, "bead_id": args["--bead"], "store_ref": args["--store-ref"],
                "repo_identity": str(Path(repo).resolve()), "path": path, "branch": branch,
                "base_ref": base, "base_sha": args["--base-sha"], "creator": args["--creator"],
                "owner": args["--owner"], "generation": args["--generation"],
                "created_at": "2025-01-01T00:00:00Z", "lifecycle": args["--lifecycle"],
                "attempt_id": f"attempt-{len(self.provenance) + 1}",
            }
            self.provenance[path] = evidence
            return json.dumps({"ok": True, "path": path, "branch": branch, "provenance": evidence})
        if argv[:3] == ["gc", "worktree", "verify"]:
            args = self.arguments(argv)
            path = args["--path"]
            evidence = self.provenance.get(path)
            if evidence is None:
                return json.dumps({"ok": False, "error": "missing"})
            return json.dumps({"ok": True, "path": path, "branch": evidence["branch"], "provenance": evidence})
        if argv[:2] == ["git", "-C"]:
            if argv[-3:] == ["remote", "get-url", "origin"]:
                return self.remote
            return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
        raise AssertionError(f"unexpected command: {argv}")

    @staticmethod
    def arguments(argv):
        values = {}
        index = 3
        while index < len(argv):
            flag = argv[index]
            if flag == "--json":
                index += 1
                continue
            values[flag] = argv[index + 1]
            index += 2
        return values


class RunWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "project"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "--initial-branch=main", str(self.seed)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.seed), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.seed), "config", "user.name", "Test"], check=True)
        (self.seed / "tracked").write_text("base\n")
        subprocess.run(["git", "-C", str(self.seed), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(self.seed), "commit", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", str(self.seed), "remote", "add", "origin", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.seed), "push", "-qu", "origin", "main"], check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)], check=True)
        self.city = self.root / "city"
        self.specs = self.city / ".gc" / "specs"
        self.specs.mkdir(parents=True)
        self.external = NativeWorktreeBoundary()
        self.external.repo = str(self.repo)
        self.dispatch("owner/project#7")

    def tearDown(self):
        self.temp.cleanup()

    def dispatch(self, reference):
        issue = {"number": 7, "title": "Work", "body": "Spec", "url": "https://github.com/owner/project/issues/7", "labels": [{"name": "ready-for-agent"}], "comments": []}

        def command(argv):
            if argv[:3] == ["gh", "issue", "view"]:
                return json.dumps(issue)
            if argv[-3:] == ["rig", "list", "--json"]:
                return json.dumps({"rigs": [{"name": "project", "path": str(self.repo), "hq": False}]})
            if argv[:2] == ["git", "-C"]:
                if argv[-3:] == ["remote", "get-url", "origin"]:
                    return "git@github.com:owner/project.git\n"
                return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
            raise AssertionError(argv)

        return dispatch(reference, "project", self.specs, command)

    def provision(self, role, **kwargs):
        return provision_workspace_for_run(
            "owner/project#7", role, specs_root=self.specs,
            command=self.external, **kwargs,
        )

    def test_initial_spec_uses_fetched_default_and_resume_is_verify_only(self):
        first = self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec")
        fetches = [call for call in self.external.calls if call[:3] == ["git", "-C", str(self.repo)] and "fetch" in call]
        ensures = [call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(len(ensures), 1)
        self.assertEqual(first.base_ref, "refs/remotes/origin/main")
        self.assertEqual(first.base_sha, subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "refs/remotes/origin/main"], text=True).strip())

        resumed = self.provision("spec")
        self.assertEqual(resumed.path, first.path)
        self.assertEqual(resumed.branch, first.branch)
        self.assertEqual(len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]), 1)
        run_path = next((self.specs / ".runs").glob("*.json"))
        saved = json.loads(run_path.read_text())
        self.assertEqual(saved["workspaces"]["spec"]["provenance"], first.provenance)
        self.assertEqual(saved["workspaces"]["spec"]["base_sha"], first.base_sha)

    def test_concurrent_initial_spec_resolution_fetches_and_ensures_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec"),
                range(2),
            ))
        self.assertEqual(results[0].path, results[1].path)
        self.assertEqual(len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]), 1)
        self.assertEqual(len([call for call in self.external.calls if "fetch" in call]), 1)

    def test_resume_does_not_recreate_a_missing_native_worktree(self):
        spec = self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec")
        ensure_count = len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]])
        shutil.rmtree(spec.path)
        with self.assertRaisesRegex(WorkspaceError, "worktree"):
            self.provision("spec")
        self.assertEqual(len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]), ensure_count)

    def test_rejects_rig_origin_that_no_longer_matches_dispatch(self):
        self.external.remote = "git@github.com:owner/other.git\n"
        with self.assertRaisesRegex(WorkspaceError, "does not match"):
            self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec")
        self.assertFalse(any(call[:3] == ["gc", "worktree", "ensure"] for call in self.external.calls))
        self.assertFalse(any("fetch" in call for call in self.external.calls))

    def test_task_uses_current_spec_head_only_after_successful_prerequisites(self):
        spec = self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec")
        subprocess.run(["git", "-C", str(spec.path), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(spec.path), "config", "user.name", "Test"], check=True)
        (Path(spec.path) / "integrated").write_text("delivered\n")
        subprocess.run(["git", "-C", str(spec.path), "add", "integrated"], check=True)
        subprocess.run(["git", "-C", str(spec.path), "commit", "-qm", "integrated prerequisite"], check=True)
        head = subprocess.check_output(["git", "-C", str(spec.path), "rev-parse", "HEAD"], text=True).strip()
        evidence = {"dep-1": {"status": "delivered", "review": "passed", "integration": "passed", "checks": "passed"}}
        task = self.provision(
            "implementer", create=True, task_id="task-2", bead_id="task-bead", store_ref="dolt:task",
            prerequisite_ids=["dep-1"], prerequisite_results=evidence,
        )
        self.assertEqual(task.base_sha, head)
        self.assertEqual(task.base_ref, head)
        self.assertEqual(subprocess.check_output(["git", "-C", str(task.path), "rev-parse", "HEAD"], text=True).strip(), head)
        saved = json.loads(next((self.specs / ".runs").glob("*.json")).read_text())
        self.assertEqual(saved["workspaces"]["task:task-2"]["base_sha"], head)

    def test_failed_prerequisite_and_missing_resume_record_never_ensure(self):
        spec = self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec")
        run_path = next((self.specs / ".runs").glob("*.json"))
        saved = json.loads(run_path.read_text())
        saved["workspaces"].pop("spec")
        run_path.write_text(json.dumps(saved))
        ensure_count = len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]])
        with self.assertRaisesRegex(WorkspaceError, "recorded workspace"):
            self.provision("spec")
        self.assertEqual(len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]), ensure_count)
        self.assertTrue(Path(spec.path).is_dir())

        with self.assertRaisesRegex(WorkspaceError, "prerequisite"):
            self.provision(
                "implementer", create=True, task_id="task-2", bead_id="task-bead", store_ref="dolt:task",
                prerequisite_ids=["dep-1"], prerequisite_results={"dep-1": {"status": "failed"}},
            )
        with self.assertRaisesRegex(WorkspaceError, "recorded workspace"):
            self.provision("spec")
        self.assertEqual(len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]), ensure_count)

    def test_session_roles_bind_existing_task_or_spec_workspace(self):
        spec = self.provision("spec", create=True, bead_id="spec-bead", store_ref="dolt:spec")
        task = self.provision(
            "implementer", create=True, task_id="task-2", bead_id="task-bead", store_ref="dolt:task",
            prerequisite_ids=[], prerequisite_results={},
        )
        ensure_count = len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]])
        reviewer = self.provision("reviewer", task_id="task-2")
        integrator = self.provision("integrator")
        final_review = self.provision("final-review")
        self.assertEqual(reviewer.path, task.path)
        self.assertEqual(reviewer.branch, task.branch)
        self.assertEqual(integrator.path, spec.path)
        self.assertEqual(final_review.path, spec.path)
        self.assertEqual(reviewer.session_role, "reviewer")
        self.assertEqual(integrator.session_role, "integrator")
        metadata = reviewer.native_metadata
        self.assertEqual(set(metadata), {
            "work_dir", "gc.work_dir", "gc.worktree_repo", "gc.worktree_root", "gc.work_branch",
            "gc.worktree_base_ref", "gc.worktree_base_sha", "gc.worktree_creator", "gc.worktree_owner",
            "gc.worktree_generation", "gc.worktree_lifecycle", "gc.root_store_ref",
        })
        self.assertEqual(metadata["work_dir"], str(task.path))
        self.assertEqual(metadata["gc.worktree_repo"], str(self.repo.resolve()))
        self.assertEqual(metadata["gc.worktree_base_sha"], task.provenance["base_sha"])
        self.assertEqual(metadata["gc.worktree_owner"], "midgar-factory")
        self.assertEqual(metadata["gc.root_store_ref"], "dolt:task")
        probe = launch(
            reviewer,
            [sys.executable, "-c", "import os,subprocess; print(os.getcwd()); print(subprocess.check_output(['git','branch','--show-current'], text=True).strip())"],
            native_command=self.external, text=True, capture_output=True, check=True,
        )
        self.assertEqual(probe.stdout.splitlines(), [str(task.path), task.branch])
        self.assertEqual(len([call for call in self.external.calls if call[:3] == ["gc", "worktree", "ensure"]]), ensure_count)


if __name__ == "__main__":
    unittest.main()
