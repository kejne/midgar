import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA = (ROOT / "formulas/claim-worker-smoke.formula.toml").read_text()
PACK = (ROOT / "pack.toml").read_text()
VENDOR = ROOT / "vendor/gc-roles"


class VolumeFormulaTest(unittest.TestCase):
    def test_source_payload_drives_both_volume_steps(self):
        self.assertIn('"gc.input_convoy_id" = "{{convoy_id}}"', FORMULA)
        self.assertEqual(FORMULA.count('"gc.input_convoy_id" = "{{convoy_id}}"'), 2)
        self.assertIn("sole source task bead in convoy `{{convoy_id}}`", FORMULA)
        self.assertIn("numeric amount in litres", FORMULA)
        self.assertIn("divide by 1000", FORMULA)
        self.assertIn("Do not substitute a hardcoded amount for the source task payload", FORMULA)
        self.assertIn("For the source payload `1000`", FORMULA)

    def test_second_worker_depends_on_and_reads_first_worker_output(self):
        converter = FORMULA.index('id = "volume-converter"')
        doubler = FORMULA.index('id = "volume-doubler"')
        self.assertLess(converter, doubler)
        self.assertRegex(FORMULA[doubler:], r'depends_on\s*=\s*\["volume-converter"\]')
        self.assertIn("Read the first worker's written `volume-converter.txt` output", FORMULA)
        self.assertIn("exactly `2 m^3`", FORMULA)
        self.assertIn('to `volume-converter.txt`', FORMULA)
        self.assertIn('to `volume-doubler.txt`', FORMULA)

    def test_formula_workers_render_the_vendored_full_upstream_role(self):
        import subprocess

        for agent in ("gascity-irc-pack/volume-converter", "gascity-irc-pack/volume-doubler"):
            rendered = subprocess.run(
                ["gc", "prime", "--strict", "--city", str(ROOT), agent],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertGreater(len(rendered), 3000)
            self.assertIn("# GC Role Worker", rendered)
            self.assertIn("This is your only work-discovery command", rendered)
        import tomllib

        self.assertNotIn("gc-roles", tomllib.loads(PACK).get("imports", {}))
        city = tomllib.loads((ROOT / "city.toml").read_text())
        self.assertEqual(
            city["defaults"]["rig"]["imports"]["gc-roles"]["source"],
            "./vendor/gc-roles",
        )
        self.assertEqual(
            {rig["name"] for rig in city["rigs"]},
            {"gremlins", "gascity-irc-pack", "helm-values-docs"},
        )
        for rig in city["rigs"]:
            self.assertEqual(rig["imports"]["gc-roles"]["source"], "./vendor/gc-roles")
        self.assertIn("520e95cb22992a8a1017f7a60da267f203497c7e", (VENDOR / "README.md").read_text())

    def test_steps_route_to_distinct_workers(self):
        self.assertIn('"gc.run_target" = "gascity-irc-pack/volume-converter"', FORMULA)
        self.assertIn('"gc.run_target" = "gascity-irc-pack/volume-doubler"', FORMULA)
        self.assertIn("target_required = true", FORMULA)
        for agent in ("volume-converter", "volume-doubler"):
            config = (ROOT / f"agents/{agent}/agent.toml").read_text()
            self.assertIn('scope = "rig"', config)
            self.assertIn('dir = "gascity-irc-pack"', config)

    def test_vendor_contains_all_roles_but_no_formulas(self):
        import tomllib

        expected_roles = {
            "design-author",
            "design-implementation-reviewer",
            "design-test-risk-reviewer",
            "feature-refiner",
            "gap-analyst",
            "implementation-reviewer",
            "implementation-worker",
            "issue-triager",
            "publisher",
            "quality-judge",
            "requirements-planner",
            "review-synthesizer",
            "run-operator",
            "task-decomposer",
        }
        actual_roles = {path.parent.name for path in (VENDOR / "agents").glob("*/agent.toml")}
        self.assertEqual(actual_roles, expected_roles)
        for role in expected_roles:
            prompt = (VENDOR / "agents" / role / "prompt.template.md").read_text()
            self.assertIn('{{ template "gc-role-worker" . }}', prompt)
        self.assertFalse(list(VENDOR.rglob("*.formula.toml")))
        vendor_pack = tomllib.loads((VENDOR / "pack.toml").read_text())
        self.assertNotIn("imports", vendor_pack)
        self.assertTrue((VENDOR / "template-fragments/gc-role-worker.template.md").is_file())
        lock = (ROOT / "packs.lock").read_text()
        self.assertNotIn("gascity-packs/tree/main/gascity/roles", lock)

    def test_targeted_smoke_uses_numeric_task_and_checks_visible_outputs(self):
        smoke = (ROOT / "tests/smoke_volume_formula.sh").read_text()
        self.assertIn('--description "1000"', smoke)
        self.assertIn('task_id', smoke)
        self.assertIn("volume-converter.txt", smoke)
        self.assertIn("volume-doubler.txt", smoke)
        self.assertIn('1 m^3', smoke)
        self.assertIn('2 m^3', smoke)
        self.assertIn('sling \\\n  gascity-irc-pack/volume-converter "$task_id" --on claim-worker-smoke --nudge --json', smoke)
        self.assertIn('d["workflow_id"]', smoke)
        self.assertIn('bd show "$root_id"', smoke)
        self.assertIn("isinstance(data,list)", smoke)
        self.assertNotIn('formula cook', smoke)
        self.assertNotIn('--var convoy_id', smoke)
        self.assertNotIn('convoy create', smoke)
        self.assertIn('claim identity, duplicate-claim behavior, or drain', smoke)


if __name__ == "__main__":
    unittest.main()
