import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from commands.midgar.factory import FactoryError, dispatch


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

    def test_explicit_replacement_requires_operator_flag(self):
        commands = FakeCommands(ready_issue(), [rig()])
        with tempfile.TemporaryDirectory() as temp:
            first = dispatch("owner/project#12", "project", temp, commands)
            second = dispatch("owner/project#12", "project", temp, commands, replace=True)
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertTrue((Path(temp) / ".runs" / "archive" / f"{first['run_id']}.json").exists())


if __name__ == "__main__":
    unittest.main()
