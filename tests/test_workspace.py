import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from commands.midgar.workspace import WorkspaceError, WorkspaceRequest, ensure_workspace, launch


class WorkspaceLaunchTests(unittest.TestCase):
    def repository(self, root):
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked").write_text("tracked\n")
        subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        return repo

    def request(self, repo, root):
        return WorkspaceRequest(
            run_id="run-123", issue="owner/project#7", rig="project", workspace_kind="task", session_role="implementer",
            repository=repo, workspace_root=root / "worktrees", task_id="task-42",
            bead_id="midgar-abc", store_ref="dolt:midgar-abc", base_ref="HEAD",
            base_sha=subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
        )

    def test_launch_starts_probe_in_verified_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repository(root)
            request = self.request(repo, root)
            workspace = request.workspace_path
            workspace.mkdir(parents=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", request.branch, str(workspace), request.base_sha], check=True)
            provenance = request.expected_provenance | {
                "repo_identity": str(repo.resolve()), "created_at": "2025-01-01T00:00:00Z", "attempt_id": "attempt-1",
            }

            def native(argv):
                self.assertEqual(argv[0:3], ["gc", "worktree", "verify"])
                return json.dumps({"ok": True, "path": str(workspace), "branch": request.branch, "provenance": provenance})

            probe = [os.environ.get("PYTHON", "python3"), "-c", "import os,subprocess; print(os.getcwd()); print(subprocess.check_output(['git','branch','--show-current'], text=True).strip())"]
            result = launch(request, probe, native_command=native, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [str(workspace), request.branch])

    def test_ensure_uses_native_transaction_and_verifies_before_returning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repository(root)
            request = self.request(repo, root)
            workspace = request.workspace_path
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", request.branch, str(workspace), request.base_sha], check=True)
            provenance = request.expected_provenance | {
                "repo_identity": str(repo.resolve()), "created_at": "2025-01-01T00:00:00Z", "attempt_id": "attempt-1",
            }
            calls = []

            def native(argv):
                calls.append(argv)
                action = argv[2]
                return json.dumps({"ok": True, "path": str(workspace), "branch": request.branch, "provenance": provenance})

            result = ensure_workspace(request, native_command=native)
            self.assertEqual(result["path"], str(workspace))
            self.assertEqual([call[2] for call in calls], ["ensure", "verify"])
            self.assertIn("--base-sha", calls[0])
            self.assertIn("--owner", calls[0])

    def test_launch_refuses_a_worktree_on_the_wrong_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repository(root)
            request = self.request(repo, root)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "wrong-branch", str(request.workspace_path), request.base_sha], check=True)
            provenance = request.expected_provenance | {
                "repo_identity": str(repo.resolve()), "created_at": "2025-01-01T00:00:00Z", "attempt_id": "attempt-1",
            }

            def native(argv):
                return json.dumps({"ok": True, "path": str(request.workspace_path), "branch": request.branch, "provenance": provenance})

            with self.assertRaisesRegex(WorkspaceError, "branch"):
                launch(request, ["this-must-not-run"], native_command=native)

    def test_launch_refuses_a_worktree_from_the_wrong_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repository(root)
            other_root = root / "other"
            subprocess.run(["git", "clone", "-q", str(repo), str(other_root)], check=True)
            other_repo = other_root
            request = self.request(repo, root)
            subprocess.run(["git", "-C", str(other_repo), "worktree", "add", "-qb", request.branch, str(request.workspace_path), request.base_sha], check=True)
            provenance = request.expected_provenance | {
                "repo_identity": str(repo.resolve()), "created_at": "2025-01-01T00:00:00Z", "attempt_id": "attempt-1",
            }

            def native(argv):
                return json.dumps({"ok": True, "path": str(request.workspace_path), "branch": request.branch, "provenance": provenance})

            with self.assertRaisesRegex(WorkspaceError, "repository"):
                launch(request, ["this-must-not-run"], native_command=native)

    def test_launch_refuses_wrong_native_ownership_before_process_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repository(root)
            request = self.request(repo, root)

            def native(argv):
                wrong = request.expected_provenance | {"owner": "other"}
                return json.dumps({"ok": True, "path": str(request.workspace_path), "branch": request.branch, "provenance": wrong})

            with self.assertRaisesRegex(WorkspaceError, "ownership"):
                launch(request, ["this-must-not-run"], native_command=native)


if __name__ == "__main__":
    unittest.main()
