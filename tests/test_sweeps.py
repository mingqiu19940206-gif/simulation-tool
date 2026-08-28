from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config_manager import ConfigManager
from sweep_manager import expand_sweep_cases, generate_sweep_cases


class SweepTests(unittest.TestCase):
    def test_enabled_cap_pitch_creates_one_native_project(self) -> None:
        config = ConfigManager(ROOT).default()
        scan = config["sweeps"]["capacitor.cap_pitch"]
        scan.update(enabled=True, start=0.6, stop=0.8, step=0.2)
        cases = expand_sweep_cases(config)
        self.assertEqual([round(case[0]["capacitor"]["cap_pitch"], 9) for case in cases], [0.6, 0.8])
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(config, Path(directory))
            self.assertEqual(len(result.cases), 1)
            self.assertEqual(len(result.variations), 2)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["strategy"], "hybrid")
            self.assertEqual(manifest["project_count"], 1)
            self.assertEqual(manifest["design_count"], 1)
            self.assertEqual(manifest["optimetrics_setup_count"], 1)
            self.assertNotIn("cases", manifest)
            self.assertNotIn("CASE", json.dumps(manifest, ensure_ascii=False).upper())


if __name__ == "__main__":
    unittest.main(verbosity=2)
