"""Off-device tests of actual safety and grab state transitions."""
import math
import unittest

from rover_web.control import RobotController, validate_control_config
from rover_web.hardware import SimHardware
from rover_web.vision import BallObservation


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hardware = SimHardware({"servo_calibrated": True}, clock=self.clock)
        self.control = RobotController({
            "autonomy_calibrated": True,
            "ball_diameter_cm": 6.0,
            "camera_focal_length_norm": 0.6,
            "grasp_stop_cm": 18.0,
            "prepare_s": 0.1,
            "grip_settle_s": 0.1,
            "lift_settle_s": 0.1,
            "release_lower_s": 0.1,
            "release_open_s": 0.1,
        }, self.hardware, clock=self.clock, autostart=False)
        self.control.set_enabled(True)
        self.hardware.events.clear()  # Ignore the two expected boot/enable standby commands.
        self.fresh()

    def tearDown(self):
        self.control.shutdown()

    def fresh(self, seconds=0.05, distance=60.0, valid=True, x=0.5, y=0.6, radius=None, track_id=1,
              heartbeat=True):
        self.clock.advance(seconds)
        if heartbeat:
            self.control.heartbeat()
        if radius is None:
            radius = 3.6 / (2 * ((distance or 60) + 3))
        self.control.update_vision(BallObservation(valid, x, y, radius, 0.9, self.clock(), track_id))
        self.control.update_range(distance, self.clock())
        self.control.tick()

    def reach_approach(self):
        self.assertTrue(self.control.grab()["ok"])
        for _ in range(7):
            self.fresh()
        self.assertEqual(self.control.snapshot()["phase"], "approaching")
        self.assertGreater(self.hardware.left, 0)

    def reach_holding(self):
        self.reach_approach()
        for _ in range(10):
            self.fresh(distance=18)
        self.assertEqual(self.control.snapshot()["phase"], "holding")

    def test_manual_needs_enable(self):
        self.control.set_enabled(False)
        self.assertFalse(self.control.drive(1, 1)["ok"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))

    def test_commissioned_startup_commands_open_gripper_and_raised_arm(self):
        hardware = SimHardware({"servo_calibrated": True}, clock=self.clock)
        controller = RobotController({}, hardware, clock=self.clock, autostart=False)
        try:
            self.assertEqual(hardware.events, [("grip", "standby"), ("arm", "standby")])
            self.assertEqual(controller.snapshot()["standby_state"], "commanded")
            self.assertEqual((hardware.left, hardware.right), (0, 0))
        finally:
            controller.shutdown()

    def test_uncommissioned_startup_keeps_servos_off_and_direct_drive_available(self):
        hardware = SimHardware({}, clock=self.clock)
        controller = RobotController({}, hardware, clock=self.clock, autostart=False)
        try:
            self.assertEqual(hardware.events, [])
            self.assertEqual(controller.snapshot()["standby_state"], "unavailable")
            controller.set_enabled(True)
            controller.update_vision(BallObservation(captured_at=self.clock()))
            controller.update_range(60, self.clock())
            self.assertTrue(controller.drive(1, 1)["ok"])
            self.assertGreater(hardware.left, 0)
        finally:
            controller.shutdown()

    def test_standby_only_raises_claw_without_unlocking_grab_or_release(self):
        hardware = SimHardware({"standby_calibrated": True}, clock=self.clock)
        controller = RobotController({}, hardware, clock=self.clock, autostart=False)
        try:
            self.assertEqual(hardware.events, [("grip", "standby"), ("arm", "standby")])
            self.assertEqual(controller.snapshot()["standby_state"], "commanded")
            self.assertTrue(controller.snapshot()["standby_calibrated"])
            self.assertFalse(controller.snapshot()["manipulator_calibrated"])
            with self.assertRaisesRegex(RuntimeError, "calibration"):
                hardware.grip(True)
            with self.assertRaisesRegex(RuntimeError, "calibration"):
                hardware.arm("pickup")
        finally:
            controller.shutdown()

    def test_physical_drive_waits_for_standby_settle(self):
        self.hardware.simulated = False
        self.control.set_enabled(True)
        self.fresh()
        self.assertEqual(self.control.snapshot()["standby_state"], "moving")
        self.assertTrue(self.control.drive(1, 1)["ok"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))
        self.clock.advance(1.2)
        self.fresh()
        self.assertEqual(self.control.snapshot()["standby_state"], "commanded")
        self.assertTrue(self.control.drive(1, 1)["ok"])
        self.assertGreater(self.hardware.left, 0)

    def test_manual_is_capped_and_needs_no_detected_ball(self):
        self.fresh(valid=False)
        self.assertTrue(self.control.drive(9, -9)["ok"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0.4, -0.4))

    def test_heartbeat_does_not_renew_drive_command(self):
        self.control.drive(1, 1)
        for _ in range(7):
            self.fresh()
        self.assertTrue(self.control.snapshot()["enabled"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))

    def test_dead_owner_disarms_even_with_fresh_sensors(self):
        self.control.drive(1, 1)
        for _ in range(15):
            self.fresh(heartbeat=False)
        self.assertFalse(self.control.snapshot()["enabled"])
        self.assertEqual(self.hardware.left, 0)
        self.control.heartbeat()
        self.assertFalse(self.control.drive(1, 1)["ok"])

    def test_stale_camera_stops_direct_control(self):
        self.control.drive(1, 1)
        self.clock.advance(0.55)
        self.control.heartbeat()
        self.control.update_range(60, self.clock())
        self.assertFalse(self.control.drive(1, 1)["ok"])
        self.assertIn("Camera", self.control.snapshot()["reason"])

    def test_stale_range_does_not_stop_direct_control(self):
        self.clock.advance(0.4)
        self.control.heartbeat()
        self.control.update_vision(BallObservation(captured_at=self.clock()))
        self.assertTrue(self.control.drive(1, 1)["ok"])
        self.assertTrue(self.control.snapshot()["enabled"])
        self.assertGreater(self.hardware.left, 0)

    def test_missing_echo_does_not_stop_direct_control(self):
        self.control.update_range(None)
        self.assertTrue(self.control.drive(1, 1)["ok"])
        self.assertTrue(self.control.snapshot()["enabled"])
        self.assertGreater(self.hardware.left, 0)
        self.assertIsNone(self.control.snapshot()["distance_cm"])

    def test_close_sonar_does_not_stop_direct_forward_or_reverse(self):
        self.fresh(distance=4)
        self.assertTrue(self.control.drive(1, 1)["ok"])
        self.assertTrue(self.control.snapshot()["enabled"])
        self.assertGreater(self.hardware.left, 0)
        self.assertTrue(self.control.drive(-1, -1)["ok"])
        self.assertLess(self.hardware.left, 0)

    def test_missing_echo_still_stops_automatic_grab(self):
        self.reach_approach()
        self.control.update_range(None, self.clock())
        self.control.tick()
        self.assertFalse(self.control.snapshot()["enabled"])
        self.assertIn("ultrasonic echo", self.control.snapshot()["reason"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))

    def test_manual_pose_requires_opt_in_and_preserves_nonopen_grip_for_drive(self):
        self.assertFalse(self.control.set_manual_pose(120, 150)["ok"])
        self.assertEqual(self.hardware.events, [])
        self.hardware.config["manual_servo_enabled"] = True
        self.assertFalse(self.control.set_manual_pose(120, 151)["ok"])
        self.assertEqual(self.hardware.events, [])
        self.assertTrue(self.control.set_manual_pose(120, 150)["ok"])
        self.assertEqual(self.hardware.events, [("pose", 120.0, 150.0)])
        self.assertEqual(self.control.snapshot()["servo_target"], {"gripper": 120.0, "arm": 150.0})
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))
        self.assertTrue(self.control.snapshot()["grip_closed"])
        self.fresh(distance=4)
        self.assertTrue(self.control.drive(-1, -1)["ok"])
        self.assertEqual(self.hardware.events[-1], ("arm", "standby"))

    def test_manual_pose_can_open_gripper_while_drive_disabled(self):
        self.hardware.config["manual_servo_enabled"] = True
        self.control.set_enabled(False)
        self.assertTrue(self.control.set_manual_pose(90, 150)["ok"])
        self.assertFalse(self.control.snapshot()["enabled"])
        self.assertFalse(self.control.snapshot()["grip_closed"])

    def test_nan_drive_stops_both_tracks(self):
        self.control.drive(1, 1)
        self.assertFalse(self.control.drive(math.nan, 1)["ok"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))

    def test_hardware_fault_blocks_reenable(self):
        self.hardware.fault = "injected motor fault"
        self.assertFalse(self.control.set_enabled(True)["ok"])
        self.assertFalse(self.control.snapshot()["enabled"])

    def test_gpio_exception_stops_and_returns_rejection(self):
        def broken(left, right):
            self.hardware.left = left
            raise OSError("injected GPIO fault")
        self.hardware.drive = broken
        self.assertFalse(self.control.drive(1, 1)["ok"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))
        self.assertFalse(self.control.snapshot()["enabled"])

    def test_autonomy_locked_until_calibrated(self):
        self.control.config["autonomy_calibrated"] = False
        self.assertFalse(self.control.grab()["ok"])
        self.assertEqual(self.hardware.events, [])

    def test_missing_geometry_blocks_grab(self):
        self.control.config["camera_focal_length_norm"] = None
        self.assertFalse(self.control.grab()["ok"])

    def test_acquisition_does_not_blindly_search(self):
        self.fresh(valid=False)
        self.control.grab()
        for _ in range(65):
            self.fresh(valid=False)
            self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))
        self.assertEqual(self.control.snapshot()["phase"], "error")
        self.assertIn("No ball", self.control.snapshot()["reason"])

    def test_alignment_steers_toward_right_target(self):
        self.control.grab()
        for _ in range(5):
            self.fresh(x=0.8)
        self.assertGreater(self.hardware.left, 0)
        self.assertLess(self.hardware.right, 0)
        self.assertEqual(self.control.snapshot()["phase"], "aligning")

    def test_same_frame_cannot_satisfy_alignment(self):
        self.control.grab()
        self.fresh(seconds=0.05)
        self.fresh(seconds=0.11)
        for _ in range(20):
            self.control.tick()
        self.assertNotEqual(self.control.snapshot()["phase"], "approaching")
        self.assertEqual(self.hardware.left, 0)

    def test_target_loss_immediately_stops_then_aborts(self):
        self.reach_approach()
        self.fresh(valid=False)
        self.assertEqual(self.hardware.left, 0)
        for _ in range(9):
            self.fresh(valid=False)
        self.assertFalse(self.control.snapshot()["enabled"])
        self.assertEqual(self.control.snapshot()["phase"], "error")

    def test_target_replacement_is_not_silently_followed(self):
        self.reach_approach()
        self.fresh(track_id=2)
        self.assertIn("changed", self.control.snapshot()["reason"])
        self.assertEqual(self.hardware.left, 0)

    def test_wall_echo_disagreement_cancels_approach(self):
        self.reach_approach()
        self.fresh(distance=150, radius=0.09)
        self.assertEqual(self.hardware.left, 0)
        self.assertIn("disagree", self.control.snapshot()["reason"])

    def test_capture_area_mismatch_does_not_close(self):
        self.reach_approach()
        self.fresh(distance=18, y=0.1)
        self.assertNotIn(("grip", False), self.hardware.events)
        self.assertIn("capture area", self.control.snapshot()["reason"])

    def test_grab_closes_then_lifts_and_holds_without_possession_claim(self):
        self.reach_holding()
        self.assertEqual(self.hardware.events, [("grip", True), ("arm", "pickup"),
                                               ("grip", False), ("arm", "carry")])
        self.assertTrue(self.control.snapshot()["grip_closed"])
        self.assertFalse(self.control.snapshot()["possession_confirmed"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))

    def test_manual_drive_raises_arm_without_opening_held_gripper(self):
        self.reach_holding()
        self.hardware.events.clear()
        self.assertTrue(self.control.drive(-1, -1)["ok"])
        self.assertEqual(self.hardware.events, [("arm", "standby")])
        self.assertTrue(self.control.snapshot()["grip_closed"])

    def test_explicit_standby_stops_tracks_and_commands_pose(self):
        self.control.drive(1, 1)
        self.hardware.events.clear()
        self.assertTrue(self.control.standby()["ok"])
        self.assertEqual((self.hardware.left, self.hardware.right), (0, 0))
        self.assertEqual(self.hardware.events, [("grip", "standby"), ("arm", "standby")])

    def test_telemetry_stop_does_not_replace_sensor_fault(self):
        self.clock.advance(1)
        self.control.heartbeat()
        self.assertFalse(self.control.drive(1, 1)["ok"])
        reason = self.control.snapshot()["reason"]
        self.assertIn("Camera frames are stale", reason)
        self.control.stop("Control stopped", preserve_reason=True)
        self.assertEqual(self.control.snapshot()["reason"], reason)
        self.control.stop("Operator stop")
        self.assertEqual(self.control.snapshot()["last_fault"], reason)
        self.control.set_enabled(True)
        self.assertIsNone(self.control.snapshot()["last_fault"])

    def test_stop_and_disconnect_preserve_closed_gripper(self):
        self.reach_holding()
        events = list(self.hardware.events)
        self.control.stop("connection lost")
        self.clock.advance(100)
        self.control.tick()
        self.assertTrue(self.control.snapshot()["grip_closed"])
        self.assertEqual(self.hardware.events, events)
        self.assertEqual(self.control.snapshot()["phase"], "holding")

    def test_manual_override_cancels_approach(self):
        self.reach_approach()
        self.control.drive(-1, -1)
        self.assertEqual(self.control.snapshot()["phase"], "idle")
        self.assertLess(self.hardware.left, 0)
        self.assertNotIn(("grip", False), self.hardware.events)

    def test_new_grab_cannot_open_existing_hold(self):
        self.reach_holding()
        events = list(self.hardware.events)
        self.assertFalse(self.control.grab()["ok"])
        self.assertEqual(self.hardware.events, events)

    def test_explicit_release_works_while_disabled_with_no_camera(self):
        self.reach_holding()
        self.control.set_enabled(False)
        self.clock.advance(100)
        self.assertTrue(self.control.release()["ok"])
        self.assertTrue(self.control.snapshot()["grip_closed"])
        self.clock.advance(0.11)
        self.control.tick()
        self.assertFalse(self.control.snapshot()["grip_closed"])
        self.assertEqual(self.hardware.events[-1], ("grip", True))
        self.clock.advance(0.11)
        self.control.tick()
        self.assertEqual(self.control.snapshot()["phase"], "stopped")
        self.assertFalse(self.control.snapshot()["enabled"])

    def test_stop_during_gripping_does_not_lift_or_open(self):
        self.reach_approach()
        for _ in range(3):
            self.fresh(distance=18)
        self.assertEqual(self.control.snapshot()["phase"], "gripping")
        self.control.stop()
        self.clock.advance(10)
        self.control.tick()
        self.assertNotIn(("arm", "carry"), self.hardware.events)
        self.assertTrue(self.control.snapshot()["grip_closed"])

    def test_stop_calls_servo_hold_to_cancel_remaining_travel(self):
        self.reach_approach()
        calls = []
        self.hardware.hold_manipulator = lambda: calls.append("hold-last-command")
        self.control.stop()
        self.assertEqual(calls, ["hold-last-command"])
        self.assertEqual(self.hardware.left, 0)

    def test_out_of_order_sensor_data_cannot_replace_new_data(self):
        self.control.update_range(80, self.clock() - 0.1)
        self.control.update_vision(BallObservation(True, 0.9, 0.6, 0.05, 0.9, self.clock() - 0.1, 5))
        snapshot = self.control.snapshot()
        self.assertEqual(snapshot["distance_cm"], 60)
        self.assertEqual(snapshot["target"]["x"], 0.5)

    def test_future_sensor_data_cannot_defeat_freshness_check(self):
        self.control.update_range(80, self.clock() + 10)
        self.assertEqual(self.control.snapshot()["distance_cm"], 60)

    def test_autonomous_timeout_is_bounded(self):
        self.control.config["autonomy_timeout_s"] = 0.2
        self.control.grab()
        for _ in range(8):
            self.fresh(x=0.8)
        self.assertIn("timed out", self.control.snapshot()["reason"])
        self.assertEqual(self.hardware.left, 0)

    def test_bad_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_control_config({"grasp_stop_cm": 7})
        with self.assertRaises(ValueError):
            validate_control_config({"manual_timeout_s": 12})


if __name__ == "__main__":
    unittest.main()
