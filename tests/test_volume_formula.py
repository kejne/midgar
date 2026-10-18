import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA = (ROOT / "formulas/claim-worker-smoke.formula.toml").read_text()
PROMPT = (ROOT / "agents/formula-worker/prompt.template.md").read_text()
PACK = (ROOT / "pack.toml").read_text()


class VolumeFormulaTest(unittest.TestCase):
    def test_source_payload_drives_both_volume_steps(self):
        self.assertIn('"gc.input_convoy" = "{{convoy_id}}"', FORMULA)
        self.assertEqual(FORMULA.count('"gc.input_convoy" = "{{convoy_id}}"'), 2)
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

    def test_formula_worker_renders_the_pinned_full_upstream_role(self):
        self.assertEqual(PROMPT.strip(), '{{ template "gc-role-worker" . }}')
        self.assertIn('source = "https://github.com/gastownhall/gascity-packs/tree/main/gascity/roles"', PACK)
        self.assertEqual(PACK.count('source = "https://github.com/gastownhall/gascity-packs/tree/main/gascity/roles"'), 1)
        self.assertEqual(PACK.count('version = "sha:520e95cb22992a8a1017f7a60da267f203497c7e"'), 2)
        self.assertNotIn("formula-worker", PACK.split('[imports."gc-roles"]', 1)[1])


if __name__ == "__main__":
    unittest.main()
