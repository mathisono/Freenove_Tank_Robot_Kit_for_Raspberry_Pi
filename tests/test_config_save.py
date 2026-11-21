"""Standby updates must survive restart without replacing other local settings."""
import json
from pathlib import Path
import tempfile
import unittest

from rover_web.config import load_config, save_standby_angles


class StandbySaveTests(unittest.TestCase):
    def test_updates_only_standby_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rover.json"
            supplied = {"hardware": {"arm_standby_angle": 140, "gripper_standby_angle": 90},
                        "control": {"min_clearance_cm": 6}}
            path.write_text(json.dumps(supplied), encoding="utf-8")
            path.chmod(0o640)
            saved = save_standby_angles(path, 110, 145)
            self.assertEqual(saved["hardware"]["arm_standby_angle"], 145)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"hardware": {"arm_standby_angle": 145, "gripper_standby_angle": 110},
                              "control": {"min_clearance_cm": 6}})
            restarted = load_config(path)
            self.assertEqual(restarted["hardware"]["gripper_standby_angle"], 110)
            self.assertEqual(restarted["hardware"]["arm_standby_angle"], 145)

    def test_invalid_target_leaves_original_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rover.json"
            original = '{"hardware":{"arm_standby_angle":140}}\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside calibrated travel"):
                save_standby_angles(path, 90, 151)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_symlink_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_text("{}", encoding="utf-8")
            alias = Path(directory) / "rover.json"
            alias.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file"):
                save_standby_angles(alias, 90, 140)
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
