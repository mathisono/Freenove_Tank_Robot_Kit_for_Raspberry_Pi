"""Synthetic image cases for color/shape rejection and target continuity."""
import unittest

import cv2
import numpy as np

from rover_web.vision import HSVBallDetector


class VisionTests(unittest.TestCase):
    def image(self, *balls):
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        for x, y, radius in balls:
            cv2.circle(image, (x, y), radius, (0, 0, 255), -1)
        return image

    def test_red_circle_has_normalized_coordinates(self):
        observation = HSVBallDetector().detect(self.image((80, 120, 20)), 100)
        self.assertTrue(observation.valid)
        self.assertAlmostEqual(observation.x, 0.25, places=2)
        self.assertAlmostEqual(observation.y, 0.5, places=2)
        self.assertAlmostEqual(observation.radius, 20 / 320, delta=0.01)
        self.assertGreater(observation.confidence, 0.8)

    def test_empty_image_is_fresh_but_has_no_ball(self):
        observation = HSVBallDetector().detect(self.image(), 100)
        self.assertFalse(observation.valid)
        self.assertEqual(observation.captured_at, 100)

    def test_long_red_object_is_rejected(self):
        image = self.image()
        cv2.rectangle(image, (20, 100), (280, 115), (0, 0, 255), -1)
        self.assertFalse(HSVBallDetector().detect(image, 100).valid)

    def test_clipped_ball_is_not_used_for_geometry(self):
        self.assertFalse(HSVBallDetector().detect(self.image((0, 120, 30)), 100).valid)

    def test_target_does_not_switch_to_larger_distractor(self):
        detector = HSVBallDetector()
        first = detector.detect(self.image((80, 120, 20)), 100)
        second = detector.detect(self.image((85, 120, 21), (250, 120, 38)), 100.1)
        self.assertEqual(first.track_id, second.track_id)
        self.assertLess(second.x, 0.4)

    def test_lost_target_does_not_immediately_select_another(self):
        detector = HSVBallDetector()
        first = detector.detect(self.image((60, 120, 20)), 100)
        lost = detector.detect(self.image((260, 120, 30)), 100.1)
        reacquired = detector.detect(self.image((260, 120, 30)), 101)
        self.assertFalse(lost.valid)
        self.assertTrue(reacquired.valid)
        self.assertNotEqual(first.track_id, reacquired.track_id)

    def test_short_occlusion_preserves_track_identity(self):
        detector = HSVBallDetector()
        first = detector.detect(self.image((100, 120, 20)), 100)
        detector.detect(self.image(), 100.1)
        final = detector.detect(self.image((105, 120, 20)), 100.2)
        self.assertTrue(final.valid)
        self.assertEqual(first.track_id, final.track_id)


if __name__ == "__main__":
    unittest.main()
