"""Bounded Direct control and a supervised, local colored-ball grab sequence.

The controller's thread is independent of HTTP, video encoding, and detection.
The hardware adapter has an additional motor lease. Neither is a substitute
for the physical S2 load switch or a hardwired emergency stop.
"""
from __future__ import annotations

import math
import threading
import time

from .vision import BallObservation


CONTROL_DEFAULTS = {
    "tick_hz": 30.0,
    "manual_max_speed": 0.40,
    "auto_approach_speed": 0.14,
    "auto_creep_speed": 0.08,
    "auto_turn_speed": 0.14,
    "steering_kp": 0.65,
    "manual_timeout_s": 0.30,
    "heartbeat_timeout_s": 0.65,
    "vision_max_age_s": 0.50,
    "range_max_age_s": 0.35,
    "range_poll_s": 0.08,
    "target_lost_timeout_s": 0.40,
    "acquire_timeout_s": 3.0,
    "autonomy_timeout_s": 25.0,
    "alignment_tolerance": 0.06,
    "alignment_frames": 3,
    "close_range_samples": 3,
    "approach_slowdown_cm": 12.0,
    "min_clearance_cm": 6.0,
    "grasp_stop_cm": None,
    "grasp_range_tolerance_cm": 2.0,
    "grasp_x": 0.5,
    "grasp_y": 0.6,
    "grasp_y_tolerance": 0.25,
    "grasp_min_radius": 0.04,
    "grasp_max_radius": 0.35,
    "target_confidence_min": 0.60,
    "autonomy_calibrated": False,
    "require_geometry_check": True,
    "ball_diameter_cm": None,
    "camera_focal_length_norm": None,
    "camera_to_sonar_cm": 0.0,
    "geometry_tolerance_cm": 10.0,
    "geometry_relative_tolerance": 0.45,
    "prepare_s": 1.2,
    "grip_settle_s": 1.2,
    "lift_settle_s": 1.2,
    "release_lower_s": 1.2,
    "release_open_s": 1.2,
}


def validate_control_config(config: dict | None) -> dict:
    result = {**CONTROL_DEFAULTS, **(config or {})}
    for key in ("autonomy_calibrated", "require_geometry_check"):
        if not isinstance(result[key], bool):
            raise ValueError(f"control.{key} must be true or false")
    positive = ("tick_hz", "steering_kp", "manual_timeout_s", "heartbeat_timeout_s",
                "vision_max_age_s", "range_max_age_s", "range_poll_s", "target_lost_timeout_s",
                "acquire_timeout_s", "autonomy_timeout_s", "approach_slowdown_cm", "min_clearance_cm",
                "grasp_range_tolerance_cm", "geometry_tolerance_cm", "prepare_s", "grip_settle_s",
                "lift_settle_s", "release_lower_s", "release_open_s")
    for key in positive:
        if not math.isfinite(float(result[key])) or float(result[key]) <= 0:
            raise ValueError(f"control.{key} must be positive and finite")
    for key in ("manual_max_speed", "auto_approach_speed", "auto_creep_speed", "auto_turn_speed"):
        if not 0 < float(result[key]) <= 0.6:
            raise ValueError(f"control.{key} must be in (0, 0.6]")
    if result["auto_creep_speed"] > result["auto_approach_speed"]:
        raise ValueError("Creep speed must not exceed approach speed")
    for key in ("alignment_tolerance", "grasp_x", "grasp_y", "grasp_y_tolerance",
                "grasp_min_radius", "grasp_max_radius", "target_confidence_min", "geometry_relative_tolerance"):
        if not 0 <= float(result[key]) <= 1:
            raise ValueError(f"control.{key} must be between 0 and 1")
    if not 0 < result["grasp_min_radius"] < result["grasp_max_radius"] < 0.5:
        raise ValueError("Grasp radius bounds must be ordered within (0, 0.5)")
    if not 10 <= result["tick_hz"] <= 100:
        raise ValueError("Control tick rate must be 10 to 100 Hz")
    if result["manual_timeout_s"] > 0.5 or result["heartbeat_timeout_s"] > 1.5:
        raise ValueError("Control timeout limits are 0.5 s for drive and 1.5 s for heartbeat")
    if result["vision_max_age_s"] > 1 or result["range_max_age_s"] > 1:
        raise ValueError("Sensor maximum age cannot exceed one second")
    for key in ("alignment_frames", "close_range_samples"):
        if not isinstance(result[key], int) or not 1 <= result[key] <= 20:
            raise ValueError(f"control.{key} must be an integer from 1 to 20")
    for key in ("ball_diameter_cm", "camera_focal_length_norm", "grasp_stop_cm"):
        if result[key] is not None and (not math.isfinite(float(result[key])) or float(result[key]) <= 0):
            raise ValueError(f"control.{key} must be positive or null")
    if not math.isfinite(float(result["camera_to_sonar_cm"])):
        raise ValueError("camera_to_sonar_cm must be finite")
    if result["grasp_stop_cm"] is not None:
        if result["grasp_stop_cm"] - result["grasp_range_tolerance_cm"] <= result["min_clearance_cm"]:
            raise ValueError("Grasp range must leave clearance beyond the emergency stop distance")
    return result


class RobotController:
    AUTO_PHASES = {"acquiring", "aligning", "approaching", "gripping", "lifting"}

    def __init__(self, config: dict | None, hardware, *, clock=time.monotonic, autostart=True):
        self.config = validate_control_config(config)
        self.hardware = hardware
        self.clock = clock
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._threads = []
        self._enabled = False
        self._phase = "stopped"
        self._reason = "Enable controls to begin"
        self._heartbeat_at = None
        self._drive_at = None
        self._manual = (0.0, 0.0)
        self._motor = (0.0, 0.0)
        self._vision = BallObservation()
        self._range_cm = None
        self._range_at = None
        self._range_sequence = 0
        self._grip_closed = None
        self._request_started = None
        self._stage_deadline = None
        self._preparing = False
        self._prepared = False
        self._locked_track = None
        self._last_seen_at = None
        self._align_frames = 0
        self._last_align_frame = None
        self._close_samples = 0
        self._last_close_sample = None
        self._release_opened = False
        self.hardware.stop()
        if autostart:
            self.start()

    def start(self):
        with self._lock:
            if self._threads or self._closed.is_set():
                return
            self._threads = [
                threading.Thread(target=self._control_loop, name="robot-control", daemon=True),
                threading.Thread(target=self._range_loop, name="range-sampling", daemon=True),
            ]
            for thread in self._threads:
                thread.start()

    def _control_loop(self):
        while not self._closed.wait(1 / self.config["tick_hz"]):
            try:
                self.tick()
            except Exception as exc:
                with self._lock:
                    self._abort(f"Control error: {exc}")

    def _range_loop(self):
        while not self._closed.is_set():
            try:
                cm = self.hardware.distance_cm()
            except Exception:
                cm = None
            self.update_range(cm, self.clock())
            self._closed.wait(self.config["range_poll_s"])

    def update_range(self, cm: float | None, captured_at: float | None = None):
        now = self.clock()
        captured_at = now if captured_at is None else float(captured_at)
        if not math.isfinite(captured_at) or captured_at > now + 0.05:
            return
        if cm is not None:
            try:
                cm = float(cm)
                if not math.isfinite(cm) or not 0 < cm <= 400:
                    cm = None
            except (TypeError, ValueError):
                cm = None
        with self._lock:
            if self._range_at is not None and captured_at < self._range_at:
                return
            is_new_sample = self._range_at is None or captured_at > self._range_at
            self._range_cm = cm
            self._range_at = captured_at
            if is_new_sample:
                self._range_sequence += 1

    def update_vision(self, observation: BallObservation):
        now = self.clock()
        if not math.isfinite(observation.captured_at) or observation.captured_at > now + 0.05:
            return
        if observation.valid and not all((
            math.isfinite(observation.x), math.isfinite(observation.y), math.isfinite(observation.radius),
            math.isfinite(observation.confidence), 0 <= observation.x <= 1, 0 <= observation.y <= 1,
            0 < observation.radius < 0.5, 0 <= observation.confidence <= 1,
        )):
            observation = BallObservation(captured_at=observation.captured_at)
        with self._lock:
            if observation.captured_at >= self._vision.captured_at:
                self._vision = observation

    def heartbeat(self):
        with self._lock:
            self._heartbeat_at = self.clock()
            return self._result()

    def set_enabled(self, enabled: bool):
        with self._lock:
            if enabled and getattr(self.hardware, "fault", None):
                self._abort(self.hardware.fault)
                return self._result(False)
            self._halt()
            self._cancel_auto()
            self._enabled = bool(enabled)
            if enabled:
                self._heartbeat_at = self.clock()
                self._phase = "holding" if self._grip_closed else "idle"
                self._reason = "Controls enabled; tracks stopped"
            else:
                self._heartbeat_at = None
                self._phase = "holding" if self._grip_closed else "stopped"
                self._reason = "Controls disabled; gripper position preserved"
            return self._result()

    def stop(self, reason="Stopped by operator"):
        with self._lock:
            self._enabled = False
            self._heartbeat_at = None
            self._halt()
            self._cancel_auto()
            self._phase = "holding" if self._grip_closed else "stopped"
            self._reason = str(reason)
            return self._result()

    def drive(self, left: float, right: float, source="web"):
        with self._lock:
            try:
                left, right = float(left), float(right)
                if not math.isfinite(left) or not math.isfinite(right):
                    raise ValueError
            except (TypeError, ValueError):
                self._abort("Rejected an invalid drive command")
                return self._result(False)
            if left == 0 and right == 0:
                self._halt()
                self._cancel_auto()
                self._phase = "holding" if self._grip_closed else ("idle" if self._enabled else "stopped")
                self._reason = "Tracks stopped"
                return self._result()
            if not self._enabled or not self._heartbeat_fresh(self.clock()):
                self.stop("Enable controls before driving")
                return self._result(False)
            # Explicit Direct input cancels Grab. It never opens a closed grip.
            self._cancel_auto()
            self._phase = "holding" if self._grip_closed else "idle"
            self._manual = (max(-1, min(1, left)), max(-1, min(1, right)))
            self._drive_at = self.clock()
            self._reason = "Direct control"
            try:
                self._apply_manual(self.clock())
            except Exception as exc:
                self._abort(f"Drive output failed: {exc}")
            return self._result(self._enabled)

    def calibration_error(self):
        if not self.config["autonomy_calibrated"]:
            return "Grab needs physical camera, sonar, motor-direction, and gripper calibration"
        if not self.hardware.manipulator_ready:
            return "Gripper and arm servo calibration is required"
        if self.config["grasp_stop_cm"] is None:
            return "Set the measured sonar range for the ball at the pickup point"
        if self.config["require_geometry_check"] and any(
            self.config[key] is None for key in ("ball_diameter_cm", "camera_focal_length_norm")
        ):
            return "Set ball diameter and camera focal length for the vision/sonar cross-check"
        return None

    def grab(self):
        with self._lock:
            if self._phase in self.AUTO_PHASES:
                return self._result()
            self._halt()
            if not self._enabled or not self._heartbeat_fresh(self.clock()):
                self._reason = "Enable controls before Grab"
                return self._result(False)
            error = self.calibration_error()
            if error:
                self._reason = error
                return self._result(False)
            if self._grip_closed:
                self._reason = "Gripper is already closed; use Release before another Grab"
                return self._result(False)
            self._halt()
            self._cancel_auto()
            self._phase = "acquiring"
            self._reason = "Waiting stationary for a ball in view"
            self._request_started = self.clock()
            return self._result()

    def release(self):
        """Explicit release is possible even with controls/video disabled.

        The server must authenticate this action. There is no spontaneous
        release from a timeout, lost target, Stop, or disconnection.
        """
        with self._lock:
            if self._phase == "releasing":
                return self._result()
            self._halt()
            self._cancel_auto()
            if not self.hardware.manipulator_ready:
                self._reason = "Release requires the configured servo calibration"
                return self._result(False)
            try:
                self.hardware.arm("pickup")
                self._phase = "releasing"
                self._reason = "Lowering arm for explicit release"
                self._release_opened = False
                self._stage_deadline = self.clock() + self.config["release_lower_s"]
                return self._result()
            except Exception as exc:
                self._abort(f"Release failed: {exc}")
                return self._result(False)

    def _cancel_auto(self):
        hold = getattr(self.hardware, "hold_manipulator", None)
        if hold is not None:
            hold()
        self._request_started = None
        self._stage_deadline = None
        self._preparing = self._prepared = False
        self._locked_track = None
        self._last_seen_at = None
        self._align_frames = 0
        self._last_align_frame = None
        self._close_samples = 0
        self._last_close_sample = None
        self._release_opened = False

    def _halt(self):
        self._manual = (0.0, 0.0)
        self._drive_at = None
        self._motor = (0.0, 0.0)
        self.hardware.stop()

    def _set_motors(self, left: float, right: float):
        self._motor = (left, right)
        if left == 0 and right == 0:
            self.hardware.stop()
        else:
            self.hardware.drive(left, right)

    def _abort(self, reason):
        self._enabled = False
        self._heartbeat_at = None
        try:
            self._halt()
        except Exception:
            self._motor = (0.0, 0.0)
        self._cancel_auto()
        self._phase = "holding" if self._grip_closed else "error"
        self._reason = str(reason)

    def _heartbeat_fresh(self, now):
        return self._heartbeat_at is not None and now - self._heartbeat_at < self.config["heartbeat_timeout_s"]

    def _sensor_error(self, now):
        if self._vision.captured_at <= 0 or now - self._vision.captured_at > self.config["vision_max_age_s"]:
            return "Camera frames are stale; controls stopped"
        if self._range_at is None or now - self._range_at > self.config["range_max_age_s"]:
            return "Ultrasonic range is stale; controls stopped"
        if self._range_cm is None:
            return "No valid ultrasonic echo; controls stopped"
        return None

    def _apply_manual(self, now):
        error = self._sensor_error(now)
        if error:
            self._abort(error)
            return
        if self._range_cm <= self.config["min_clearance_cm"] and max(self._manual) > 0:
            self._abort("Front clearance is below the stop distance")
            return
        speed = self.config["manual_max_speed"]
        self._set_motors(self._manual[0] * speed, self._manual[1] * speed)

    def tick(self):
        with self._lock:
            now = self.clock()
            if getattr(self.hardware, "fault", None):
                self._abort(self.hardware.fault)
                return
            if self._phase == "releasing":
                self._set_motors(0, 0)
                if now >= self._stage_deadline:
                    if not self._release_opened:
                        self.hardware.grip(True)
                        self._grip_closed = False
                        self._release_opened = True
                        self._reason = "Opening gripper"
                        self._stage_deadline = now + self.config["release_open_s"]
                    else:
                        self._phase = "idle" if self._enabled else "stopped"
                        self._reason = "Release complete"
                        self._cancel_auto()
                return
            if not self._enabled:
                return
            if not self._heartbeat_fresh(now):
                self.stop("Controller connection expired; enable again to resume")
                return
            if self._phase in self.AUTO_PHASES:
                self._tick_grab(now)
                return
            if self._drive_at is not None:
                if now - self._drive_at >= self.config["manual_timeout_s"]:
                    self._halt()
                    self._reason = "Drive command expired; tracks stopped"
                else:
                    self._apply_manual(now)

    def _tick_grab(self, now):
        if self._request_started is None:
            self._abort("Grab state was interrupted")
            return
        if now - self._request_started > self.config["autonomy_timeout_s"]:
            self._abort("Grab timed out; no automatic retry")
            return
        # Once closing starts, wheels remain stopped and servo deadlines alone
        # advance the sequence. Ball occlusion by the gripper is expected.
        if self._phase == "gripping":
            self._set_motors(0, 0)
            if now >= self._stage_deadline:
                self.hardware.arm("carry")
                self._phase = "lifting"
                self._reason = "Lifting the closed gripper; possession is unverified"
                self._stage_deadline = now + self.config["lift_settle_s"]
            return
        if self._phase == "lifting":
            self._set_motors(0, 0)
            if now >= self._stage_deadline:
                self._phase = "holding"
                self._reason = "Gripper held closed until Release; possession is unverified"
                self._cancel_auto()
            return
        error = self._sensor_error(now)
        if error:
            self._abort(error)
            return
        if self._range_cm <= self.config["min_clearance_cm"]:
            self._abort("Front obstacle is inside the stop distance")
            return
        target = self._vision
        target_visible = target.valid and target.confidence >= self.config["target_confidence_min"]
        if not target_visible:
            self._set_motors(0, 0)
            self._align_frames = self._close_samples = 0
            if self._last_seen_at is not None and now - self._last_seen_at > self.config["target_lost_timeout_s"]:
                self._abort("Ball lost; tracks stopped and Grab cancelled")
            elif self._last_seen_at is None and now - self._request_started > self.config["acquire_timeout_s"]:
                self._abort("No ball found in view; Search Mode is not implemented")
            else:
                self._reason = "Waiting stationary for the selected ball"
            return
        self._last_seen_at = target.captured_at
        if self._locked_track is None:
            self._locked_track = target.track_id
        elif target.track_id != self._locked_track:
            self._abort("Selected ball changed; press Grab to select again")
            return
        if not self._prepared:
            self._set_motors(0, 0)
            if not self._preparing:
                self.hardware.grip(True)
                self._grip_closed = False
                self.hardware.arm("pickup")
                self._preparing = True
                self._stage_deadline = now + self.config["prepare_s"]
                self._reason = "Preparing the calibrated pickup position"
                return
            if now < self._stage_deadline:
                return
            self._prepared = True
            self._preparing = False
        error_x = target.x - self.config["grasp_x"]
        if abs(error_x) > self.config["alignment_tolerance"]:
            self._phase = "aligning"
            self._reason = "Centering the selected ball"
            self._align_frames = self._close_samples = 0
            turn = max(-self.config["auto_turn_speed"], min(self.config["auto_turn_speed"],
                                                          error_x * self.config["steering_kp"]))
            self._set_motors(turn, -turn)
            return
        self._set_motors(0, 0)
        if target.captured_at != self._last_align_frame:
            self._align_frames += 1
            self._last_align_frame = target.captured_at
        if self._align_frames < self.config["alignment_frames"]:
            self._phase = "aligning"
            self._reason = "Confirming alignment across fresh frames"
            return
        if self.config["require_geometry_check"]:
            estimated_cm = (self.config["ball_diameter_cm"] * self.config["camera_focal_length_norm"]
                            / (2 * target.radius) - self.config["ball_diameter_cm"] / 2
                            - self.config["camera_to_sonar_cm"])
            tolerance = max(self.config["geometry_tolerance_cm"],
                            estimated_cm * self.config["geometry_relative_tolerance"])
            if estimated_cm <= 0 or abs(self._range_cm - estimated_cm) > tolerance:
                self._abort("Camera and ultrasonic range disagree; the echo may be a different object")
                return
        stop_cm = self.config["grasp_stop_cm"]
        if self._range_cm < stop_cm - self.config["grasp_range_tolerance_cm"]:
            self._abort("Ball/obstacle is closer than the calibrated pickup range")
            return
        if self._range_cm <= stop_cm + self.config["grasp_range_tolerance_cm"]:
            if not (abs(target.y - self.config["grasp_y"]) <= self.config["grasp_y_tolerance"]
                    and self.config["grasp_min_radius"] <= target.radius <= self.config["grasp_max_radius"]):
                self._abort("Ball is outside the calibrated gripper capture area")
                return
            if self._range_sequence != self._last_close_sample:
                self._close_samples += 1
                self._last_close_sample = self._range_sequence
            self._phase = "approaching"
            self._reason = "Stopped at pickup point; confirming range"
            if self._close_samples >= self.config["close_range_samples"]:
                self.hardware.grip(False)
                self._grip_closed = True
                self._phase = "gripping"
                self._reason = "Closing gripper; no possession sensor is fitted"
                self._stage_deadline = now + self.config["grip_settle_s"]
            return
        if target.radius > self.config["grasp_max_radius"]:
            self._abort("Ball image is too large for the configured pickup geometry")
            return
        self._close_samples = 0
        self._phase = "approaching"
        self._reason = "Approaching with fresh camera and ultrasonic measurements"
        remaining = self._range_cm - stop_cm
        speed = self.config["auto_creep_speed"] if remaining < self.config["approach_slowdown_cm"] else self.config["auto_approach_speed"]
        trim = max(-speed * 0.5, min(speed * 0.5, error_x * self.config["steering_kp"]))
        self._set_motors(speed + trim, speed - trim)

    def _result(self, ok=True):
        return {"ok": ok, **self.snapshot()}

    def snapshot(self):
        with self._lock:
            now = self.clock()
            target = {key: getattr(self._vision, key) for key in ("valid", "x", "y", "radius", "confidence")}
            target["valid"] = bool(target["valid"] and self._vision.captured_at > 0
                                   and now - self._vision.captured_at <= self.config["vision_max_age_s"])
            return {
                "enabled": self._enabled, "mode": "direct", "phase": self._phase,
                "grip_closed": self._grip_closed, "possession_confirmed": False,
                "autonomy_calibrated": self.calibration_error() is None,
                "reason": self._reason,
                "distance_cm": self._range_cm if self._range_at is not None
                    and now - self._range_at <= self.config["range_max_age_s"] else None,
                "target": target, "motor": {"left": self._motor[0], "right": self._motor[1]},
                "vision_age_s": max(0, now - self._vision.captured_at) if self._vision.captured_at > 0 else None,
                "range_age_s": max(0, now - self._range_at) if self._range_at is not None else None,
                "heartbeat_age_s": max(0, now - self._heartbeat_at) if self._heartbeat_at is not None else None,
                "search_available": False, "simulated": bool(getattr(self.hardware, "simulated", False)),
            }

    def shutdown(self):
        if self._closed.is_set():
            return
        self.stop("Server shutting down; gripper was not commanded open")
        self._closed.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self.hardware.close()
