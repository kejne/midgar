import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("gc"), "native Gas City compiler is required")
class FactoryDeliveryConfigTests(unittest.TestCase):
    def test_native_compiler_produces_one_three_attempt_delivery_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            city = Path(temp)
            (city / "formulas").mkdir()
            shutil.copy(ROOT / "formulas/factory-task-delivery.toml", city / "formulas")
            (city / "city.toml").write_text('[workspace]\nprovider = "pi"\n[providers.pi]\nbase = "builtin:pi"\n')
            result = subprocess.run(
                ["gc", "formula", "show", "factory-task-delivery", "--city", str(city), "--json"],
                check=True, capture_output=True, text=True,
            )
            recipe = json.loads(result.stdout)
            checked = [step for step in recipe["steps"] if step.get("metadata", {}).get("gc.kind") == "ralph"]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["metadata"].get("gc.max_attempts"), "3")
            self.assertEqual(checked[0]["metadata"].get("gc.check_mode"), "exec")
            self.assertEqual(checked[0]["metadata"].get("gc.check_path"), "formulas/factory-task-delivery-check.sh")
            self.assertEqual(checked[0]["metadata"].get("midgar.delivery_role"), "check")
            self.assertTrue((ROOT / "formulas/factory-task-delivery-check.sh").stat().st_mode & 0o111)
            routed = [step for step in recipe["steps"] if step.get("metadata", {}).get("midgar.delivery_role") in {"implementer", "reviewer", "integrator"}]
            self.assertEqual({step["metadata"]["midgar.delivery_role"] for step in routed}, {"implementer", "reviewer", "integrator"})
            self.assertTrue(all(step["metadata"].get("gc.run_target") == "gascity-irc-pack/midgar.factory-worker" for step in routed))
            by_role = {step["metadata"]["midgar.delivery_role"]: step["id"] for step in routed}
            edges = {(edge["step_id"], edge["depends_on_id"]) for edge in recipe["deps"]}
            self.assertIn((by_role["reviewer"], by_role["implementer"] + "-scope-check"), edges)
            self.assertIn((by_role["integrator"], by_role["reviewer"] + "-scope-check"), edges)
            self.assertFalse(any(step.get("metadata", {}).get("gc.max_attempts") not in {None, "3"} for step in recipe["steps"]))

    def test_installed_config_accepts_factory_one_shot_fresh_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            city = Path(temp)
            shutil.copytree(ROOT / "vendor/midgar", city / "vendor/midgar")
            (city / "city.toml").write_text(
                '[workspace]\nprovider = "pi"\n[providers.pi]\nbase = "builtin:pi"\n'
                '[[rigs]]\nname = "gascity-irc-pack"\n'
                '[rigs.imports.midgar]\nsource = "./vendor/midgar"\n'
            )
            result = subprocess.run(["gc", "config", "show", "--city", str(city)], check=True, capture_output=True, text=True)
            config = result.stdout
            self.assertIn('name = "factory-worker"', config)
            self.assertIn('dir = "gascity-irc-pack"', config)
            self.assertIn('start_command = "python3', config)
            self.assertIn('lifecycle = "one_shot"', config)
            self.assertIn('wake_mode = "fresh"', config)
            self.assertIn('prompt_mode = "none"', config)
            self.assertIn('max_active_sessions = 1', config)
            agents = json.loads(subprocess.run(
                ["gc", "agent", "list", "--city", str(city), "--rig", "gascity-irc-pack", "--json"],
                check=True, capture_output=True, text=True,
            ).stdout)
            self.assertIn("gascity-irc-pack/midgar.factory-worker",
                          {agent["qualified_name"] for agent in agents["agents"]})
            prime = subprocess.run(
                ["gc", "--city", str(city), "prime", "--strict", "--hook", "gascity-irc-pack/midgar.factory-worker"],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("one-shot Midgar factory worker", prime.stdout)


if __name__ == "__main__":
    unittest.main()
