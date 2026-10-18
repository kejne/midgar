import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA = (ROOT / "formulas/claim-worker-smoke.formula.toml").read_text()
PACK = (ROOT / "pack.toml").read_text()


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

    def test_formula_workers_render_the_pinned_full_upstream_role(self):
        import subprocess

        for agent in ("volume-converter", "volume-doubler"):
            rendered = subprocess.run(
                ["gc", "prime", "--strict", "--city", str(ROOT), agent],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertGreater(len(rendered), 3000)
            self.assertIn("# GC Role Worker", rendered)
            self.assertIn("This is your only work-discovery command", rendered)
        self.assertIn('source = "https://github.com/gastownhall/gascity-packs/tree/main/gascity/roles"', PACK)
        self.assertEqual(PACK.count('source = "https://github.com/gastownhall/gascity-packs/tree/main/gascity/roles"'), 1)
        self.assertEqual(PACK.count('version = "sha:520e95cb22992a8a1017f7a60da267f203497c7e"'), 1)
        self.assertNotIn('[imports.gc]', PACK)

    def test_steps_route_to_distinct_workers(self):
        self.assertIn('"gc.run_target" = "gascity-irc-pack/volume-converter"', FORMULA)
        self.assertIn('"gc.run_target" = "gascity-irc-pack/volume-doubler"', FORMULA)
        self.assertIn("target_required = true", FORMULA)

    def test_targeted_smoke_uses_numeric_task_and_checks_visible_outputs(self):
        smoke = (ROOT / "tests/smoke_volume_formula.sh").read_text()
        self.assertIn('--description "1000"', smoke)
        self.assertIn('task_id', smoke)
        self.assertIn("volume-converter.txt", smoke)
        self.assertIn("volume-doubler.txt", smoke)
        self.assertIn('1 m^3', smoke)
        self.assertIn('2 m^3', smoke)
        self.assertIn('formula cook claim-worker-smoke', smoke)
        self.assertIn('--attach "$task_id"', smoke)


if __name__ == "__main__":
    unittest.main()
