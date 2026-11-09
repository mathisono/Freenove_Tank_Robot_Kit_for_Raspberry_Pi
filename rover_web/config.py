"""Validated settings; local calibration and secrets stay outside Git."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from .control import CONTROL_DEFAULTS, validate_control_config
from .hardware import HARDWARE_DEFAULTS, validate_hardware_config
from .status_led import STATUS_LED_DEFAULTS, validate_status_led_config
from .vision import VISION_DEFAULTS, validate_vision_config

CAMERA_DEFAULTS = {
    "width": 960, "height": 540, "fps": 30, "bitrate": 2_000_000,
    "vision_size": [320, 180], "vision_fps": 15,
    "rotation180": True, "stale_after": 0.75,
}
SERVER_DEFAULTS = {
    "host": "0.0.0.0", "port": 8080, "max_viewers": 3,
    "ticket_ttl_s": 0.5, "telemetry_interval_s": 0.1,
    "session_ttl_s": 43_200,
}


def default_config() -> dict:
    return deepcopy({"server": SERVER_DEFAULTS, "camera": CAMERA_DEFAULTS,
                     "hardware": HARDWARE_DEFAULTS, "status_led": STATUS_LED_DEFAULTS,
                     "control": CONTROL_DEFAULTS,
                     "vision": VISION_DEFAULTS})


def _merge(target: dict, source: dict, prefix: str = "") -> None:
    for key, value in source.items():
        if key not in target:
            raise ValueError(f"Unknown setting: {prefix}{key}")
        if isinstance(target[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"{prefix}{key} must be an object")
            _merge(target[key], value, f"{prefix}{key}.")
        else:
            target[key] = value


def load_config(path: str | Path | None = None) -> dict:
    result = default_config()
    if path is not None:
        with Path(path).open(encoding="utf-8") as handle:
            supplied = json.load(handle)
        if not isinstance(supplied, dict):
            raise ValueError("Configuration must contain a JSON object")
        # Older installations may still carry the unused S2 sense settings.
        status_led = supplied.get("status_led")
        if isinstance(status_led, dict):
            status_led.pop("s2_sense_gpio", None)
            status_led.pop("s2_active_high", None)
        _merge(result, supplied)
    validate_config(result)
    return result


def validate_config(config: dict) -> None:
    validate_hardware_config(config["hardware"])
    validate_status_led_config(config["status_led"], config["hardware"])
    validate_control_config(config["control"])
    validate_vision_config(config["vision"])
    server, camera = config["server"], config["camera"]
    if not isinstance(server["host"], str) or not server["host"]:
        raise ValueError("server.host must be a nonempty address")
    for name, low, high in [("port", 1024, 65535), ("max_viewers", 1, 8)]:
        value = server[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"server.{name} must be an integer in {low}..{high}")
    for name, low, high in [("ticket_ttl_s", 0.2, 0.8),
                            ("telemetry_interval_s", 0.05, 0.2),
                            ("session_ttl_s", 60, 86400)]:
        value = server[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"server.{name} must be in {low}..{high}")
    if server["telemetry_interval_s"] >= server["ticket_ttl_s"] / 2:
        raise ValueError("Telemetry must update faster than half the command ticket lifetime")
    for name, low, high in [("width", 320, 1280), ("height", 180, 720),
                            ("fps", 5, 30), ("vision_fps", 5, 30),
                            ("bitrate", 200_000, 8_000_000)]:
        value = camera[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"camera.{name} must be an integer in {low}..{high}")
    dims = camera["vision_size"]
    if not isinstance(dims, (list, tuple)) or len(dims) != 2 or any(type(v) is not int or v < 64 for v in dims):
        raise ValueError("camera.vision_size must contain two positive dimensions >= 64")
    if any(v % 2 for v in [camera["width"], camera["height"], *dims]):
        raise ValueError("YUV420 dimensions must all be even")
    if dims[0] > camera["width"] or dims[1] > camera["height"]:
        raise ValueError("Vision dimensions cannot exceed video dimensions")
    if dims[0] * camera["height"] != dims[1] * camera["width"]:
        raise ValueError("Video and vision must share an aspect ratio for target overlays")
    macroblocks = math.ceil(camera["width"] / 16) * math.ceil(camera["height"] / 16)
    if macroblocks > 3600 or macroblocks * camera["fps"] > 108000:
        raise ValueError("Camera settings exceed WebRTC H264 level 3.1 limits")
    if type(camera["rotation180"]) is not bool:
        raise ValueError("camera.rotation180 must be a boolean")
    if not isinstance(camera["stale_after"], (int, float)) or not 0.15 <= camera["stale_after"] <= 1:
        raise ValueError("camera.stale_after must be 0.15..1 seconds")
