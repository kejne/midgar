import json
import tempfile
import unittest
from pathlib import Path

from commands.midgar.factory import dispatch
from commands.midgar.plan import PlanError, materialize_plan, validate_plan


class PlanValidationTests(unittest.TestCase):
    def test_valid_plan_covers_snapshot_acceptance_and_dependencies(self):
        plan = {
            "revision": 1,
            "run_id": "run-1",
            "snapshot": "specs/project/12/snapshot.json",
            "tasks": [
                {
                    "id": "foundation",
                    "title": "Add foundation",
                    "behavior": "A usable foundation exists",
                    "acceptance": ["AC-1"],
                    "owns": ["AC-1"],
                    "depends_on": [],
                },
                {
                    "id": "integration",
                    "title": "Integrate foundation",
                    "behavior": "The public feature is integrated",
                    "acceptance": ["AC-2"],
                    "owns": ["AC-2"],
                    "depends_on": ["foundation"],
                },
            ],
        }
        self.assertEqual(validate_plan(plan, ["AC-1", "AC-2"]), plan)

    def test_rejects_dependency_cycles_and_ambiguous_shared_ownership(self):
        plan = {
            "revision": 1, "run_id": "run-1", "snapshot": "snapshot.json",
            "tasks": [
                {"id": "one", "title": "One", "behavior": "One", "acceptance": ["AC-1"], "owns": ["AC-1"], "depends_on": ["two"]},
                {"id": "two", "title": "Two", "behavior": "Two", "acceptance": ["AC-1", "AC-2"], "owns": ["AC-1", "AC-2"], "depends_on": ["one"]},
            ],
        }
        with self.assertRaisesRegex(PlanError, "integration owner"):
            validate_plan(plan, ["AC-1", "AC-2"])
        plan["integration_owner"] = {"AC-1": "one"}
        with self.assertRaisesRegex(PlanError, "cycle"):
            validate_plan(plan, ["AC-1", "AC-2"])

    def test_rejects_duplicate_task_ids_and_unknown_prerequisites(self):
        task = {"id": "one", "title": "One", "behavior": "One", "acceptance": ["AC-1"], "owns": ["AC-1"], "depends_on": []}
        with self.assertRaisesRegex(PlanError, "duplicate task identity"):
            validate_plan({"revision": 1, "run_id": "run-1", "snapshot": "snapshot.json", "tasks": [task, dict(task)]}, ["AC-1"])
        task["depends_on"] = ["missing"]
        with self.assertRaisesRegex(PlanError, "unknown prerequisites"):
            validate_plan({"revision": 1, "run_id": "run-1", "snapshot": "snapshot.json", "tasks": [task]}, ["AC-1"])

    def test_rejects_uncovered_acceptance_criterion_before_materialization(self):
        with self.assertRaisesRegex(PlanError, "uncovered"):
            validate_plan(
                {"revision": 1, "run_id": "run-1", "snapshot": "snapshot.json", "tasks": [
                    {"id": "one", "title": "One", "behavior": "One", "acceptance": ["AC-1"], "owns": ["AC-1"], "depends_on": []}
                ]},
                ["AC-1", "AC-2"],
            )


class MaterializationTests(unittest.TestCase):
    def test_resumes_after_remote_task_creation_and_populates_owned_convoy(self):
        plan = {
            "revision": 1,
            "run_id": "pending",
            "snapshot": "",
            "tasks": [
                {"id": "first", "title": "First", "behavior": "First behavior", "acceptance": ["AC-1"], "owns": ["AC-1"], "depends_on": []},
                {"id": "second", "title": "Second", "behavior": "Second behavior", "acceptance": ["AC-2"], "owns": ["AC-2"], "depends_on": ["first"]},
            ],
        }

        class Commands:
            def __init__(self):
                self.tasks = {}
                self.convoys = {}
                self.members = set()
                self.edges = set()
                self.interrupt_once = True
                self.interrupt_dependency_once = True
                self.interrupt_convoy_once = True
                self.interrupt_member_once = True
                self.creates = 0
                self.calls = []

            def __call__(self, argv):
                self.calls.append(argv)
                if argv[:3] != ["gc", "--rig", "project"]:
                    raise AssertionError(f"native command missing selected rig: {argv}")
                group, args = argv[3], argv[4:]
                if group == "bd" and args[:2] == ["list", "--all"] and "--external-ref" in args:
                    ref = args[args.index("--external-ref") + 1]
                    bead = self.tasks.get(ref)
                    return json.dumps({"issues": [bead] if bead else []})
                if group == "bd" and args[0] == "create":
                    ref = args[args.index("--external-ref") + 1]
                    metadata = json.loads(args[args.index("--metadata") + 1])
                    self.creates += 1
                    bead = {"id": f"bd-{self.creates}", "external_ref": ref, "metadata": metadata}
                    self.tasks[ref] = bead
                    if self.interrupt_once:
                        self.interrupt_once = False
                        raise PlanError("simulated lost create response")
                    return json.dumps(bead)
                if group == "bd" and args[0] == "show":
                    return json.dumps(next(task for task in self.tasks.values() if task["id"] == args[1]))
                if group == "bd" and args[:2] == ["dep", "list"]:
                    return json.dumps([{"issue_id": left, "depends_on_id": right, "type": "blocks"} for left, right in self.edges if left == args[2]])
                if group == "bd" and args[:2] == ["dep", "add"]:
                    self.edges.add(tuple(args[2:4]))
                    if self.interrupt_dependency_once:
                        self.interrupt_dependency_once = False
                        raise PlanError("simulated lost dependency response")
                    return ""
                if group == "bd" and args[:4] == ["list", "--all", "--type", "convoy"]:
                    return json.dumps({"issues": list(self.convoys.values())})
                if group == "convoy" and args[:2] == ["create", self.convoy_title]:
                    convoy = {"id": "cv-1", "convoy_id": "cv-1", "title": args[1], "owned": True}
                    self.convoys[convoy["title"]] = convoy
                    if self.interrupt_convoy_once:
                        self.interrupt_convoy_once = False
                        raise PlanError("simulated lost convoy response")
                    return json.dumps({"convoy_id": "cv-1", "title": args[1]})
                if group == "convoy" and args[0] == "status":
                    convoy = next(item for item in self.convoys.values() if item["id"] == args[1])
                    children = [{"id": bead_id} for convoy_id, bead_id in self.members if convoy_id == args[1]]
                    return json.dumps({"convoy": convoy, "children": children})
                if group == "convoy" and args[0] == "add":
                    self.members.add((args[1], args[2]))
                    if self.interrupt_member_once:
                        self.interrupt_member_once = False
                        raise PlanError("simulated lost member response")
                    return "{}"
                raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temp:
            issue = {"number": 12, "title": "Plan", "body": "Req", "url": "https://github.com/owner/project/issues/12", "labels": [{"name": "ready-for-agent"}], "comments": []}
            def dispatch_command(argv):
                if argv[:3] == ["gh", "issue", "view"]:
                    return json.dumps(issue)
                if argv[:3] == ["gc", "rig", "list"]:
                    return json.dumps({"rigs": [{"name": "project", "path": "/rig/project", "hq": False}]})
                if argv[:2] == ["git", "-C"]:
                    return "git@github.com:owner/project.git\n"
                raise AssertionError(argv)
            run = dispatch("owner/project#12", "project", temp, dispatch_command)
            plan["run_id"] = run["run_id"]
            plan["snapshot"] = run["snapshot"]
            commands = Commands()
            commands.convoy_title = f"Midgar {run['run_id']} {run['issue']} implementation tasks"
            with self.assertRaisesRegex(PlanError, "lost create"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            in_progress = next(json.loads(path.read_text()) for path in (Path(temp) / ".runs").glob("*.json") if json.loads(path.read_text()).get("run_id") == run["run_id"])
            self.assertFalse(in_progress["progress"]["plan_ready"])
            with self.assertRaisesRegex(PlanError, "lost dependency"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            with self.assertRaisesRegex(PlanError, "lost convoy"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            with self.assertRaisesRegex(PlanError, "lost member"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            result = materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            self.assertTrue(result["ready"])
            run_record = next(json.loads(path.read_text()) for path in (Path(temp) / ".runs").glob("*.json") if json.loads(path.read_text()).get("run_id") == run["run_id"])
            self.assertTrue(run_record["progress"]["plan_ready"])
            self.assertEqual(run_record["progress"]["convoy_owner"], f"midgar:{run['run_id']}")
            self.assertEqual(commands.creates, 2)
            self.assertEqual(len(commands.tasks), 2)
            self.assertEqual(commands.members, {("cv-1", "bd-1"), ("cv-1", "bd-2")})
            self.assertEqual(commands.edges, {("bd-2", "bd-1")})
            self.assertTrue(Path(result["plan"]).is_file())
            resumed = materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            self.assertEqual(resumed["tasks"], result["tasks"])
            self.assertEqual(commands.creates, 2)
            self.assertTrue(all(argv[:3] == ["gc", "--rig", "project"] for argv in commands.calls))
            task_ref = f"midgar:{run['run_id']}:task:first"
            task_metadata = commands.tasks[task_ref]["metadata"]
            commands.tasks[task_ref]["metadata"] = {"midgar": {"run_id": "another-run"}}
            with self.assertRaisesRegex(PlanError, "does not match run/task identity"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            commands.tasks[task_ref]["metadata"] = task_metadata
            commands.convoys[commands.convoy_title]["owned"] = False
            with self.assertRaisesRegex(PlanError, "ownership"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            commands.convoys[commands.convoy_title]["owned"] = True
            commands.members.add(("cv-1", "source-issue-bead"))
            with self.assertRaisesRegex(PlanError, "non-plan or control work"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            commands.members.remove(("cv-1", "source-issue-bead"))
            expanded = json.loads(json.dumps(plan))
            expanded["revision"] = 2
            expanded["tasks"].append({"id": "third", "title": "Third", "behavior": "Third", "acceptance": ["AC-3"], "owns": ["AC-3"], "depends_on": []})
            with self.assertRaisesRegex(PlanError, "acceptance IDs differ"):
                materialize_plan("owner/project#12", expanded, ["AC-1", "AC-2", "AC-3"], temp, commands)
            revised = json.loads(json.dumps(plan))
            revised["revision"] = 2
            revised["tasks"][0]["behavior"] = "Silently changed task scope"
            with self.assertRaisesRegex(PlanError, "explicit reconciliation"):
                materialize_plan("owner/project#12", revised, ["AC-1", "AC-2"], temp, commands)
            calls_before_replacement = len(commands.calls)
            dispatch("owner/project#12", "project", temp, dispatch_command, replace=True)
            with self.assertRaisesRegex(PlanError, "different or replaced factory run"):
                materialize_plan("owner/project#12", plan, ["AC-1", "AC-2"], temp, commands)
            self.assertEqual(len(commands.calls), calls_before_replacement)


if __name__ == "__main__":
    unittest.main()
