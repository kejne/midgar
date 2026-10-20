import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from commands.midgar.factory import FactoryError, dispatch, record_stage


class FakeCommands:
    def __init__(self, issue, rigs, remote="git@github.com:owner/project.git\n"):
        self.issue = issue
        self.rigs = rigs
        self.remote = remote
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["gh", "issue", "view"]:
            return json.dumps(self.issue)
        if argv[:3] == ["gc", "rig", "list"]:
            return json.dumps({"rigs": self.rigs})
        if argv[:2] == ["git", "-C"]:
            return self.remote
        raise AssertionError(f"unexpected command: {argv}")


def ready_issue(**changes):
    issue = {
        "number": 12,
        "title": "Build the thing",
        "body": "Requirements",
        "url": "https://github.com/owner/project/issues/12",
        "labels": [{"name": "ready-for-agent"}],
        "comments": [{"body": "Clarify the expected behavior"}],
    }
    return issue | changes


def rig():
    return {"name": "project", "path": "/rig/project", "hq": False}


class DispatchTests(unittest.TestCase):
    def test_admits_ready_issue_matching_rig_and_persists_snapshot(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            result = dispatch("https://github.com/owner/project/issues/12", "project", temp, commands)
            self.assertEqual(result["issue"], "owner/project#12")
            self.assertEqual(result["rig"], "project")
            self.assertEqual(result["stage"], "dispatched")
            snapshot = Path(temp) / "project" / "12" / "snapshot.json"
            self.assertTrue(snapshot.exists())
            saved = json.loads(snapshot.read_text())
            self.assertEqual(saved["body"], "Requirements")
            self.assertEqual(saved["comments"], [{"body": "Clarify the expected behavior"}])
            self.assertTrue((Path(temp) / ".runs" / "owner-project-12.json").exists())

    def test_rejects_issue_without_ready_label_before_creating_run(self):
        commands = FakeCommands(ready_issue(labels=[]), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(FactoryError, "ready-for-agent"):
                dispatch("owner/project#12", "project", temp, commands)
            self.assertFalse((Path(temp) / ".runs").exists())

    def test_rejects_issue_owned_by_another_repository(self):
        commands = FakeCommands(ready_issue(), [rig()], remote="git@github.com:owner/other.git\n")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(FactoryError, "does not match"):
                dispatch("owner/project#12", "project", temp, commands)
            self.assertFalse((Path(temp) / ".runs").exists())

    def test_repeated_dispatch_reuses_immutable_snapshot(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            first = dispatch("owner/project#12", "project", temp, commands)
            snapshot = Path(temp) / "project" / "12" / "snapshot.json"
            original = snapshot.read_bytes()
            commands.issue = ready_issue(body="Changed requirements")
            second = dispatch("owner/project#12", "project", temp, commands)
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(snapshot.read_bytes(), original)

    def test_rejects_repeat_dispatch_to_different_rig_unless_replaced(self):
        alternate_rig = {"name": "alternate", "path": "/rig/alternate", "hq": False}
        commands = FakeCommands(ready_issue(), [rig(), alternate_rig])
        with tempfile.TemporaryDirectory() as temp:
            first = dispatch("owner/project#12", "project", temp, commands)
            with self.assertRaisesRegex(FactoryError, "already belongs to rig project"):
                dispatch("owner/project#12", "alternate", temp, commands)
            replacement = dispatch("owner/project#12", "alternate", temp, commands, replace=True)
            self.assertEqual(replacement["rig"], "alternate")
            self.assertNotEqual(first["run_id"], replacement["run_id"])

    def test_reconcile_creates_snapshot_revision_only_when_explicit(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            first = dispatch("owner/project#12", "project", temp, commands)
            original_path = Path(first["snapshot"])
            original_bytes = original_path.read_bytes()
            commands.issue = ready_issue(body="Changed requirements")
            resumed = dispatch("owner/project#12", "project", temp, commands)
            self.assertEqual(resumed["run_id"], first["run_id"])
            self.assertEqual(resumed["snapshot"], first["snapshot"])
            self.assertEqual(original_path.read_bytes(), original_bytes)

            reconciled = dispatch("owner/project#12", "project", temp, commands, reconcile=True)
            revised_path = Path(reconciled["snapshot"])
            self.assertEqual(reconciled["run_id"], first["run_id"])
            self.assertNotEqual(revised_path, original_path)
            self.assertEqual(json.loads(revised_path.read_text())["body"], "Changed requirements")
            self.assertEqual(original_path.read_bytes(), original_bytes)
            active = json.loads((Path(temp) / ".runs" / "owner-project-12.json").read_text())
            self.assertEqual(active["snapshot"], str(revised_path))
            self.assertFalse((Path(temp) / ".runs" / "archive").exists())

    def test_concurrent_dispatch_resolves_one_active_run(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: dispatch("owner/project#12", "project", temp, commands),
                    range(2),
                ))
            self.assertEqual(results[0]["run_id"], results[1]["run_id"])
            self.assertFalse((Path(temp) / ".runs" / "archive").exists())

    def test_record_stage_progress_and_diagnostics_survive_dispatch_and_artifacts_remain(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            run = dispatch("owner/project#12", "project", temp, commands)
            snapshot = Path(run["snapshot"])

            failed = record_stage(
                "owner/project#12", "failed", {"implementation": "blocked"},
                {"message": "test suite failed", "stage": "implementation"}, temp,
            )
            self.assertEqual(failed["stage"], "failed")
            self.assertEqual(failed["progress"]["implementation"], "blocked")
            self.assertEqual(failed["diagnostics"], [{"message": "test suite failed", "stage": "implementation"}])

            paused = record_stage(
                "owner/project#12", "paused", {"review": "waiting", "pr": "#42"},
                {"message": "awaiting reviewer"}, temp,
            )
            run_path = Path(temp) / ".runs" / "owner-project-12.json"
            saved = json.loads(run_path.read_text())
            self.assertEqual(paused["stage"], "paused")
            self.assertEqual(saved["progress"]["implementation"], "blocked")
            self.assertEqual(saved["progress"]["review"], "waiting")
            self.assertEqual(saved["progress"]["pr"], "#42")
            self.assertEqual(len(saved["diagnostics"]), 2)
            self.assertTrue(snapshot.exists())
            self.assertTrue(run_path.exists())
            self.assertEqual(dispatch("owner/project#12", "project", temp, commands)["diagnostics"], saved["diagnostics"])

    def test_explicit_replacement_requires_operator_flag(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            first = dispatch("owner/project#12", "project", temp, commands)
            second = dispatch("owner/project#12", "project", temp, commands, replace=True)
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertTrue((Path(temp) / ".runs" / "archive" / f"{first['run_id']}.json").exists())


if __name__ == "__main__":
    unittest.main()
