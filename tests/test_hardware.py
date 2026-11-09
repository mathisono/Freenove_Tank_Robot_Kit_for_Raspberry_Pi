"""Verify the real adapter's independent lease with stub GPIO outputs."""
import sys
import threading
import types
import unittest
from unittest.mock import patch

from rover_web.hardware import FreenoveHardware, validate_hardware_config


class FakeMotor:
    def __init__(self, forward, backward):
        self.forward_pin, self.backward_pin = forward, backward
        self._value = 0.0
        self.fail_write = False
        self.stopped = threading.Event()

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if self.fail_write:
            raise RuntimeError("injected output fault")
        self._value = value
        if value:
            self.stopped.clear()

    def stop(self):
        self._value = 0.0
        self.stopped.set()

    def close(self):
        self.stop()


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.patch = patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Motor=FakeMotor)})
        self.patch.start()
        # This suite exercises the motor thread and explicit servo cancellation;
        # prevent an unrelated servo worker from racing the command-state fixture.
        self.servo_patch = patch.object(FreenoveHardware, "_servo_loop", return_value=None)
        self.servo_patch.start()
        self.hardware = FreenoveHardware({}, clock=lambda: self.now)

    def tearDown(self):
        self.hardware.close()
        self.servo_patch.stop()
        self.patch.stop()

    def test_startup_has_no_servo_pulses_or_motor_movement(self):
        self.assertEqual(self.hardware._pwm, {})
        self.assertEqual(self.hardware._servo_targets, {})
        self.assertEqual((self.hardware._left.value, self.hardware._right.value), (0, 0))
        self.assertEqual((self.hardware._left.forward_pin, self.hardware._left.backward_pin), (24, 23))

    def test_watchdog_stops_without_controller_tick(self):
        self.hardware.drive(0.2, 0.2)
        self.now += 0.31
        self.assertTrue(self.hardware._left.stopped.wait(0.3))
        self.assertTrue(self.hardware._right.stopped.wait(0.3))
        self.assertEqual(self.hardware._left.value, 0)

    def test_sonar_lock_cannot_block_motor_watchdog(self):
        self.hardware._range_lock.acquire()
        try:
            self.hardware.drive(0.2, 0.2)
            self.now += 0.31
            self.assertTrue(self.hardware._left.stopped.wait(0.3))
            self.assertTrue(self.hardware._right.stopped.wait(0.3))
        finally:
            self.hardware._range_lock.release()

    def test_partial_drive_failure_stops_already_written_motor(self):
        self.hardware._right.fail_write = True
        with self.assertRaises(RuntimeError):
            self.hardware.drive(0.2, 0.2)
        self.assertEqual(self.hardware._left.value, 0)
        self.assertIsNotNone(self.hardware.fault)
        self.hardware._right.fail_write = False
        with self.assertRaises(RuntimeError):
            self.hardware.drive(0.2, 0.2)

    def test_stop_manipulator_holds_last_position_not_final_target(self):
        with self.hardware._servo_lock:
            self.hardware._servo_positions = {"gripper": 115.0}
            self.hardware._servo_targets = {"gripper": 145.0, "arm": 135.0}
        self.hardware.hold_manipulator()
        self.assertEqual(self.hardware._servo_targets, {"gripper": 115.0})

    def test_servo_commands_require_commissioning(self):
        with self.assertRaises(RuntimeError):
            self.hardware.grip(True)
        self.assertEqual(self.hardware._pwm, {})

    def test_bad_servo_limits_rejected_before_gpio_use(self):
        with self.assertRaises(ValueError):
            validate_hardware_config({"gripper_closed_angle": 180})
        with self.assertRaises(ValueError):
            validate_hardware_config({"left_forward_gpio": 27})


if __name__ == "__main__":
    unittest.main()
