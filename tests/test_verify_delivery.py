from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from config_manager import ConfigManager
from verify_delivery import configs_roundtrip_equivalent


class VerifyDeliveryConfigRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.config = self.manager.default()

    def _roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.json"
            self.manager.save(self.config, path)
            return self.manager.load(path)[0]

    def test_unit_conversion_float_noise_is_equivalent(self) -> None:
        loaded = self._roundtrip()

        # The internal mm values differ at binary floating-point precision,
        # while both produce exactly the same documented external JSON values.
        # Assert that property rather than one platform's exact bit pattern, so
        # editing the default stackup cannot fail this test for an unrelated
        # reason.
        self.assertNotEqual(loaded, self.config)
        original = self.config["stackup"][3]["thickness"]
        roundtripped = loaded["stackup"][3]["thickness"]
        self.assertNotEqual(roundtripped, original)
        self.assertLess(abs(roundtripped - original), 1.0e-12)
        self.assertTrue(configs_roundtrip_equivalent(self.config, loaded))

    def test_meaningful_numeric_change_is_not_equivalent(self) -> None:
        changed = copy.deepcopy(self._roundtrip())
        changed["trace"]["trace_width"] += 0.001

        self.assertFalse(configs_roundtrip_equivalent(self.config, changed))

    def test_non_numeric_change_is_not_equivalent(self) -> None:
        changed = copy.deepcopy(self._roundtrip())
        changed["project"]["project_name"] += "_changed"

        self.assertFalse(configs_roundtrip_equivalent(self.config, changed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
