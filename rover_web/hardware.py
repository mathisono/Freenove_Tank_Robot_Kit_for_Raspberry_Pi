"""FNK0077 V2 GPIO adapter, independent drive lease, and deterministic simulator.

Pin mapping follows Freenove's Code/Server/motor.py and servo.py. No vendor
Servo object is constructed: its constructor commands arm movement. The
adapter starts with tracks stopped and no servo pulse. Hardware is used only
when FreenoveHardware is explicitly constructed on the Raspberry Pi.
"""
from __future__ import annotations

import math
import threading
import time


HARDWARE_DEFAULTS = {
    "board_version": 2,
    "left_forward_gpio": 24,
    "left_backward_gpio": 23,
    "right_forward_gpio": 5,
    "right_backward_gpio": 6,
    "left_inverted": False,
    "right_inverted": False,
    "motor_lease_s": 0.30,
    "ultrasonic_trigger_gpio": 27,
    "ultrasonic_echo_gpio": 22,
    "ultrasonic_timeout_s": 0.025,
    "ultrasonic_max_cm": 250.0,
    "pwm_chip": 0,
    "gripper_pwm_channel": 0,
    "arm_pwm_channel": 1,
    "servo_calibrated": False,
    "gripper_min_angle": 90.0,
    "gripper_max_angle": 150.0,
    "gripper_open_angle": 90.0,
    "gripper_closed_angle": 145.0,
    "gripper_inverted": False,
    "arm_min_angle": 90.0,
    "arm_max_angle": 150.0,
    "arm_pickup_angle": 95.0,
    "arm_carry_angle": 135.0,
    "arm_inverted": False,
    "servo_slew_deg_s": 60.0,
}


def validate_hardware_config(config: dict | None) -> dict:
    result = {**HARDWARE_DEFAULTS, **(config or {})}
    if result["board_version"] != 2:
        raise ValueError("This web adapter supports the pictured FNK0077 V2 board only")
    for key in ("left_inverted", "right_inverted", "servo_calibrated", "gripper_inverted", "arm_inverted"):
        if not isinstance(result[key], bool):
            raise ValueError(f"hardware.{key} must be true or false")
    gpio_keys = [key for key in result if key.endswith("_gpio")]
    if any(not isinstance(result[key], int) or not 0 <= result[key] <= 27 for key in gpio_keys):
        raise ValueError("GPIO numbers must be BCM integers between 0 and 27")
    if len({result[key] for key in gpio_keys}) != len(gpio_keys):
        raise ValueError("Motor and sonar GPIO assignments must be distinct")
    if not 0.05 <= float(result["motor_lease_s"]) <= 0.5:
        raise ValueError("hardware.motor_lease_s must be between 0.05 and 0.5")
    if not 0.005 <= float(result["ultrasonic_timeout_s"]) <= 0.05:
        raise ValueError("Ultrasonic timeout must be between 5 and 50 ms")
    if not 10 <= float(result["ultrasonic_max_cm"]) <= 400:
        raise ValueError("Ultrasonic maximum must be 10 to 400 cm")
    if not 10 <= float(result["servo_slew_deg_s"]) <= 180:
        raise ValueError("Servo slew rate must be 10 to 180 degrees/second")
    for name, positions in (("gripper", ("open", "closed")), ("arm", ("pickup", "carry"))):
        low, high = float(result[f"{name}_min_angle"]), float(result[f"{name}_max_angle"])
        if not 0 <= low < high <= 180:
            raise ValueError(f"Invalid {name} servo travel limits")
        for position in positions:
            if not low <= float(result[f"{name}_{position}_angle"]) <= high:
                raise ValueError(f"{name}_{position}_angle is outside calibrated travel limits")
    if result["gripper_open_angle"] == result["gripper_closed_angle"]:
        raise ValueError("Gripper open and closed angles must differ")
    if result["gripper_pwm_channel"] == result["arm_pwm_channel"]:
        raise ValueError("Arm and gripper require distinct PWM channels")
    for key in ("pwm_chip", "gripper_pwm_channel", "arm_pwm_channel"):
        if not isinstance(result[key], int) or result[key] < 0:
            raise ValueError(f"hardware.{key} must be a nonnegative integer")
    return result


def _bounded(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Motor commands must be finite")
    return max(-1.0, min(1.0, value))


class FreenoveHardware:
    simulated = False

    def __init__(self, config: dict | None = None, *, clock=time.monotonic):
        self.config = validate_hardware_config(config)
        self.clock = clock
        self._motor_lock = threading.Lock()
        self._servo_lock = threading.Lock()
        self._range_lock = threading.Lock()
        self._closed = threading.Event()
        self._drive_deadline = 0.0
        self._pwm = {}
        self._servo_positions = {}
        self._servo_targets = {}
        self._servo_error = None
        self._motor_error = None
        self._trigger = self._echo = None
        self._left = self._right = None
        from gpiozero import Motor
        try:
            self._left = Motor(self.config["left_forward_gpio"], self.config["left_backward_gpio"])
            self._right = Motor(self.config["right_forward_gpio"], self.config["right_backward_gpio"])
            self.stop()
        except BaseException:
            if self._left is not None:
                self._left.close()
            if self._right is not None:
                self._right.close()
            raise
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="motor-lease", daemon=True)
        self._servos = threading.Thread(target=self._servo_loop, name="servo-slew", daemon=True)
        self._watchdog.start()
        self._servos.start()

    @property
    def manipulator_ready(self) -> bool:
        return self.config["servo_calibrated"] and self._servo_error is None

    @property
    def fault(self):
        return self._motor_error or self._servo_error

    def drive(self, left: float, right: float):
        left, right = _bounded(left), _bounded(right)
        if self._closed.is_set():
            raise RuntimeError("Hardware is closed")
        if self.fault:
            raise RuntimeError(self.fault)
        with self._motor_lock:
            self._drive_deadline = self.clock() + self.config["motor_lease_s"]
            try:
                self._left.value = -left if self.config["left_inverted"] else left
                self._right.value = -right if self.config["right_inverted"] else right
            except Exception as exc:
                self._motor_error = f"Motor output failed: {exc}"
                # The lease is already set, even if only one write succeeded.
                # Try both outputs independently; the watchdog will also retry.
                for motor in (self._left, self._right):
                    try:
                        motor.stop()
                    except Exception:
                        pass
                raise

    def stop(self):
        with self._motor_lock:
            errors = []
            for motor in (self._left, self._right):
                if motor is not None:
                    try:
                        motor.stop()
                    except Exception as exc:
                        errors.append(exc)
            if errors:
                self._drive_deadline = self.clock()
                self._motor_error = f"Motor stop failed: {errors[0]}"
                raise RuntimeError(self._motor_error)
            self._drive_deadline = 0.0

    def _watchdog_loop(self):
        # This never takes a controller, camera, sonar, or servo lock.
        while not self._closed.wait(0.02):
            with self._motor_lock:
                if self._drive_deadline and self.clock() >= self._drive_deadline:
                    errors = []
                    for motor in (self._left, self._right):
                        try:
                            motor.stop()
                        except Exception as exc:
                            errors.append(exc)
                    if errors:
                        self._motor_error = f"Motor watchdog stop failed: {errors[0]}"
                    else:
                        self._drive_deadline = 0.0

    def _servo_target(self, name: str, angle: float):
        if self._closed.is_set() or not self.manipulator_ready:
            raise RuntimeError("Servo commissioning must be completed before moving the gripper or arm")
        low, high = self.config[f"{name}_min_angle"], self.config[f"{name}_max_angle"]
        if not low <= angle <= high:
            raise ValueError("Servo angle is outside configured limits")
        if self.config[f"{name}_inverted"]:
            angle = low + high - angle
        with self._servo_lock:
            if name not in self._pwm:
                from rpi_hardware_pwm import HardwarePWM
                pwm = HardwarePWM(pwm_channel=self.config[f"{name}_pwm_channel"], hz=50,
                                  chip=self.config["pwm_chip"])
                pwm.start(0)
                self._pwm[name] = pwm
            self._servo_targets[name] = float(angle)

    def grip(self, open: bool):
        position = "open" if open else "closed"
        self._servo_target("gripper", self.config[f"gripper_{position}_angle"])

    def arm(self, position: str):
        if position not in ("pickup", "carry"):
            raise ValueError("Arm position must be pickup or carry")
        self._servo_target("arm", self.config[f"arm_{position}_angle"])

    def hold_manipulator(self):
        """Cancel pending servo travel while keeping the last pulses active.

        There is no feedback sensor: these are commanded positions. A target
        which has not emitted its first pulse is cancelled without movement.
        """
        with self._servo_lock:
            self._servo_targets = dict(self._servo_positions)

    def _servo_loop(self):
        interval = 0.02
        while not self._closed.wait(interval):
            try:
                with self._servo_lock:
                    for name, target in self._servo_targets.items():
                        previous = self._servo_positions.get(name)
                        if previous is None:
                            # No position feedback exists; the first explicit action
                            # must establish a commanded angle during commissioning.
                            value = target
                        else:
                            step = self.config["servo_slew_deg_s"] * interval
                            value = previous + max(-step, min(step, target - previous))
                        if value != previous:
                            self._pwm[name].change_duty_cycle(2.5 + value * (10 / 180))
                            self._servo_positions[name] = value
            except Exception as exc:
                self._servo_error = f"Servo output failed: {exc}"
                self.stop()
                return

    def distance_cm(self) -> float | None:
        """One bounded echo measurement, called only by the sonar worker.

        No echo is missing data, never an invented 'clear path' measurement.
        Linux scheduling can affect timing; this is a hobby range interlock.
        """
        with self._range_lock:
            if self._closed.is_set():
                return None
            if self._trigger is None:
                from gpiozero import DigitalInputDevice, DigitalOutputDevice
                self._trigger = DigitalOutputDevice(self.config["ultrasonic_trigger_gpio"], initial_value=False)
                self._echo = DigitalInputDevice(self.config["ultrasonic_echo_gpio"], pull_up=False)
            if self._echo.value:
                return None
            self._trigger.on()
            time.sleep(0.00001)
            self._trigger.off()
            deadline = time.monotonic() + self.config["ultrasonic_timeout_s"]
            while not self._echo.value:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0)
            rising = time.monotonic()
            while self._echo.value:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0)
            cm = (time.monotonic() - rising) * 17150.0
            return round(cm, 1) if 2 <= cm <= self.config["ultrasonic_max_cm"] else None

    def close(self):
        if self._closed.is_set():
            return
        self.stop()
        self._closed.set()
        self._watchdog.join(timeout=0.3)
        self._servos.join(timeout=0.3)
        with self._servo_lock:
            for pwm in self._pwm.values():
                pwm.stop()
        with self._range_lock:
            for device in (self._trigger, self._echo):
                if device is not None:
                    device.close()
        self._left.close()
        self._right.close()


class SimHardware:
    """No physical outputs. Tests/synthetic camera may change ``range_cm``."""

    simulated = True

    def __init__(self, config: dict | None = None, *, clock=time.monotonic):
        self.config = validate_hardware_config(config)
        self.clock = clock
        self.left = self.right = 0.0
        self.range_cm: float | None = 60.0
        self.events = []
        self.gripper_open = None
        self.arm_position = None
        self.closed = False
        self.fault = None

    @property
    def manipulator_ready(self):
        return self.config["servo_calibrated"]

    def drive(self, left, right):
        if self.closed:
            raise RuntimeError("Simulator is closed")
        self.left, self.right = _bounded(left), _bounded(right)

    def stop(self):
        self.left = self.right = 0.0

    def grip(self, open: bool):
        if not self.manipulator_ready:
            raise RuntimeError("Servo calibration required")
        self.gripper_open = bool(open)
        self.events.append(("grip", bool(open)))

    def arm(self, position):
        if not self.manipulator_ready:
            raise RuntimeError("Servo calibration required")
        if position not in ("pickup", "carry"):
            raise ValueError("Unknown arm position")
        self.arm_position = position
        self.events.append(("arm", position))

    def distance_cm(self):
        return self.range_cm

    def hold_manipulator(self):
        # Simulated servo moves are instantaneous; keep their last positions.
        pass

    def close(self):
        self.stop()
        self.closed = True


Hardware = FreenoveHardware
