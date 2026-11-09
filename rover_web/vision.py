"""Small, local colored-ball detector. Frames must be BGR NumPy arrays.

Coordinates are fractions of image width/height; radius is a fraction of width.
The detector does not import OpenCV or touch a camera until ``detect`` is called.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time


VISION_DEFAULTS = {
    "hsv_ranges": [[[0, 110, 65], [12, 255, 255]], [[168, 110, 65], [179, 255, 255]]],
    "min_area_px": 90,
    "min_radius_px": 6,
    "max_radius_fraction": 0.40,
    "min_circularity": 0.56,
    "min_fill": 0.58,
    "track_match_distance": 0.24,
    "track_lost_after_s": 0.75,
}


def validate_vision_config(config: dict | None) -> dict:
    result = {**VISION_DEFAULTS, **(config or {})}
    ranges = result["hsv_ranges"]
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("vision.hsv_ranges must contain lower/upper HSV pairs")
    for pair in ranges:
        if len(pair) != 2 or any(len(v) != 3 for v in pair):
            raise ValueError("Each HSV range needs two three-element arrays")
        for i, limit in enumerate((179, 255, 255)):
            if not 0 <= pair[0][i] <= pair[1][i] <= limit:
                raise ValueError("HSV bounds are out of order or out of range")
    for key in ("min_area_px", "min_radius_px", "track_lost_after_s"):
        if not math.isfinite(float(result[key])) or float(result[key]) <= 0:
            raise ValueError(f"vision.{key} must be positive")
    for key in ("max_radius_fraction", "min_circularity", "min_fill", "track_match_distance"):
        if not 0 < float(result[key]) <= 1:
            raise ValueError(f"vision.{key} must be in (0, 1]")
    return result


@dataclass(frozen=True)
class BallObservation:
    """A completed detection, timestamped with ``time.monotonic()`` at capture.

    A frame without a ball still has a fresh ``captured_at``. ``valid`` means a
    color/shape candidate, not that the object is certainly a ball. ``track_id``
    changes on acquisition of a new target, never simply because a frame missed.
    """

    valid: bool = False
    x: float = 0.5
    y: float = 0.5
    radius: float = 0.0
    confidence: float = 0.0
    captured_at: float = 0.0
    track_id: int | None = None
    frame_width: int = 640
    frame_height: int = 480


class HSVBallDetector:
    def __init__(self, config: dict | None = None):
        self.config = validate_vision_config(config)
        self._previous: BallObservation | None = None
        self._next_track_id = 1

    def detect(self, frame, captured_at: float | None = None) -> BallObservation:
        import cv2
        import numpy as np

        captured_at = time.monotonic() if captured_at is None else captured_at
        if frame is None or len(frame.shape) != 3 or frame.shape[2] != 3:
            raise ValueError("Detector needs a nonempty BGR image")
        height, width = frame.shape[:2]
        if width < 2 or height < 2:
            raise ValueError("Image is too small")
        hsv = cv2.cvtColor(cv2.GaussianBlur(frame, (5, 5), 0), cv2.COLOR_BGR2HSV)
        mask = np.zeros((height, width), dtype=np.uint8)
        for lower, upper in self.config["hsv_ranges"]:
            mask |= cv2.inRange(hsv, np.array(lower, dtype=np.uint8), np.array(upper, dtype=np.uint8))
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            perimeter = float(cv2.arcLength(contour, True))
            if area < self.config["min_area_px"] or perimeter <= 0:
                continue
            (cx, cy), radius = cv2.minEnclosingCircle(contour)
            if radius < self.config["min_radius_px"] or radius / width > self.config["max_radius_fraction"]:
                continue
            circularity = min(1.0, 4 * math.pi * area / perimeter**2)
            fill = min(1.0, area / (math.pi * radius**2))
            if circularity < self.config["min_circularity"] or fill < self.config["min_fill"]:
                continue
            # A clipped circle cannot give a trustworthy apparent diameter.
            if cx - radius <= 1 or cy - radius <= 1 or cx + radius >= width - 1 or cy + radius >= height - 1:
                continue
            candidates.append((cx / width, cy / height, radius / width, (fill + circularity) / 2))

        previous = self._previous
        if previous is not None and captured_at - previous.captured_at > self.config["track_lost_after_s"]:
            previous = None
            self._previous = None
        chosen = None
        if previous is not None:
            ranked = []
            for candidate in candidates:
                distance = math.hypot(candidate[0] - previous.x, candidate[1] - previous.y)
                ratio = candidate[2] / max(previous.radius, 1e-6)
                if distance <= self.config["track_match_distance"] and 0.5 <= ratio <= 2.0:
                    ranked.append((distance + abs(math.log(ratio)) * 0.08, candidate))
            if ranked:
                chosen = min(ranked, key=lambda pair: pair[0])[1]
        elif candidates:
            chosen = max(candidates, key=lambda candidate: candidate[2] ** 2 * candidate[3])
        if chosen is None:
            return BallObservation(captured_at=captured_at, track_id=previous.track_id if previous else None,
                                   frame_width=width, frame_height=height)
        track_id = previous.track_id if previous else self._next_track_id
        if previous is None:
            self._next_track_id += 1
        observation = BallObservation(True, *chosen, captured_at, track_id, width, height)
        self._previous = observation
        return observation
