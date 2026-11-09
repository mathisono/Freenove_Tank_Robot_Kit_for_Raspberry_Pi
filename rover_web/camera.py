"""One camera owner for hardware H.264 video and fresh, low-resolution vision.

Picamera2 is imported only for real hardware. The demonstration source runs on a
normal computer and never imports the Raspberry Pi GPIO/camera libraries.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import math
import re
import threading
import time
from typing import Callable

import numpy as np

LOG = logging.getLogger(__name__)


def validate_h264_settings(width: int, height: int, fps: int, bitrate: int) -> None:
    """Keep the hardware stream within WebRTC's advertised H.264 level 3.1."""
    macroblocks = ((width + 15) // 16) * ((height + 15) // 16)
    if width > 1280 or height > 720 or fps > 30 or bitrate > 8_000_000:
        raise ValueError("WebRTC H264 is limited to 1280x720, 30 fps, and 8 Mbps")
    if macroblocks > 3600 or macroblocks * fps > 108_000:
        raise ValueError("Camera format exceeds H264 level 3.1 macroblock limits")


def create_h264_encoder(*, bitrate: int, fps: int):
    """Configure the Pi 4 V4L2 encoder for the level aiortc actually advertises.

Picamera2's H264Encoder has no public level argument. This narrow adapter uses
the same _controls -> VIDIOC_S_EXT_CTRLS startup path as the upstream encoder.
It is checked against Picamera2 0.3.39; unsupported APIs fail at startup. Output
SPS validation below also rejects drivers which ignore the requested controls.
No bitstream bytes or negotiated SDP are rewritten.
    """
    try:
        # Import the hardware implementation explicitly: do not silently use
        # the Pi 5's software encoder under the common H264Encoder alias.
        from picamera2.encoders.h264_encoder import H264Encoder as PiH264Encoder
        from picamera2.encoders.v4l2_encoder import V4L2Encoder
        from videodev2 import (
            V4L2_CID_MPEG_VIDEO_H264_I_PERIOD,
            V4L2_CID_MPEG_VIDEO_H264_LEVEL,
            V4L2_CID_MPEG_VIDEO_H264_PROFILE,
            V4L2_CID_MPEG_VIDEO_REPEAT_SEQ_HEADER,
            V4L2_MPEG_VIDEO_H264_LEVEL_3_1,
            V4L2_MPEG_VIDEO_H264_PROFILE_CONSTRAINED_BASELINE,
        )
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Hardware WebRTC requires Picamera2's Pi 4 V4L2 encoder and level 3.1 controls; "
                           "update python3-picamera2 through apt") from exc

    if not issubclass(PiH264Encoder, V4L2Encoder) or not hasattr(V4L2Encoder, "_start"):
        raise RuntimeError("Unsupported Picamera2 V4L2 encoder API; hardware video was not started")

    class WebRTCH264Encoder(PiH264Encoder):
        def _start(self):
            try:
                validate_h264_settings(self.width, self.height, fps, bitrate)
                outputs = self._output
                if not outputs or not callable(self._send_streams):
                    raise AttributeError("missing output startup interface")
            except AttributeError as exc:
                raise RuntimeError("Unsupported Picamera2 encoder startup API") from exc
            self._controls = [
                (V4L2_CID_MPEG_VIDEO_H264_PROFILE, V4L2_MPEG_VIDEO_H264_PROFILE_CONSTRAINED_BASELINE),
                (V4L2_CID_MPEG_VIDEO_H264_LEVEL, V4L2_MPEG_VIDEO_H264_LEVEL_3_1),
                (V4L2_CID_MPEG_VIDEO_H264_I_PERIOD, max(1, fps // 2)),
                (V4L2_CID_MPEG_VIDEO_REPEAT_SEQ_HEADER, 1),
            ]
            for output in outputs:
                self._send_streams(output)
            # This is the upstream step that applies _controls before starting
            # streaming. Calling H264Encoder._start would replace our level.
            V4L2Encoder._start(self)

    try:
        return WebRTCH264Encoder(bitrate=bitrate, repeat=True, iperiod=max(1, fps // 2),
                                framerate=fps, enable_sps_framerate=True,
                                profile="constrained baseline")
    except TypeError as exc:
        raise RuntimeError("Unsupported Picamera2 H264Encoder arguments; update python3-picamera2") from exc


def _h264_nals(data: bytes):
    starts = list(re.finditer(b"\x00\x00\x00\x01|\x00\x00\x01", data))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
        yield data[match.end():end]


def h264_sps_info(data: bytes) -> list[dict]:
    """Read baseline SPS metadata to verify the actual hardware output.

Annex-B start codes and emulation-prevention bytes are handled explicitly. This
parses only the baseline fields needed for level/frame-buffer checks and does
not modify the stream. Invalid or unsupported SPS data is rejected.
    """
    result = []
    for nal in _h264_nals(data):
        if not nal or nal[0] & 31 != 7:
            continue
        rbsp = nal[1:].replace(b"\x00\x00\x03", b"\x00\x00")
        if len(rbsp) < 4:
            raise ValueError("Truncated H264 SPS")
        profile, constraints, level = rbsp[:3]
        if profile != 66 or not constraints & 0x40 or level != 31:
            raise ValueError(f"Encoder emitted {rbsp[:3].hex()}, expected constrained-baseline level 3.1")
        position = 24

        def bits(count):
            nonlocal position
            if position + count > len(rbsp) * 8:
                raise ValueError("Truncated H264 SPS fields")
            value = 0
            for _ in range(count):
                value = (value << 1) | ((rbsp[position // 8] >> (7 - position % 8)) & 1)
                position += 1
            return value

        def unsigned_golomb():
            zeros = 0
            while bits(1) == 0:
                zeros += 1
                if zeros > 31:
                    raise ValueError("Invalid H264 SPS integer")
            return (1 << zeros) - 1 + bits(zeros)

        unsigned_golomb()  # seq_parameter_set_id
        unsigned_golomb()  # log2_max_frame_num_minus4
        order_type = unsigned_golomb()
        if order_type == 0:
            unsigned_golomb()  # log2_max_pic_order_cnt_lsb_minus4
        elif order_type == 1:
            bits(1)
            unsigned_golomb()  # signed code uses the same bit length
            unsigned_golomb()
            cycle = unsigned_golomb()
            if cycle > 255:
                raise ValueError("Invalid H264 picture-order cycle")
            for _ in range(cycle):
                unsigned_golomb()
        elif order_type != 2:
            raise ValueError("Invalid H264 picture-order type")
        reference_frames = unsigned_golomb()
        bits(1)
        width_mbs = unsigned_golomb() + 1
        height_map_units = unsigned_golomb() + 1
        frame_mbs_only = bits(1)
        macroblocks = width_mbs * height_map_units * (2 - frame_mbs_only)
        if not frame_mbs_only or macroblocks > 3600 or reference_frames * macroblocks > 18_000:
            raise ValueError("Encoder SPS exceeds level 3.1 frame/reference-buffer limits")
        result.append({"profile_level_id": rbsp[:3].hex(), "macroblocks": macroblocks,
                       "reference_frames": reference_frames})
    return result


@dataclass(frozen=True)
class FrameSnapshot:
    image: np.ndarray  # OpenCV BGR, copied out of libcamera-owned storage
    captured_at: float  # time.monotonic() clock, not wall time
    sequence: int


@dataclass(frozen=True)
class H264Frame:
    data: bytes  # One Annex-B access unit, including headers on keyframes
    timestamp_us: int
    captured_at: float
    keyframe: bool


class H264Subscription:
    """Bounded cross-thread queue which preserves H.264 decoding dependencies.

Dropping an arbitrary P-frame would corrupt subsequent dependent frames. When
full, discard the backlog and wait for the next keyframe instead. Only one
event-loop wakeup can be pending, so a blocked loop cannot accumulate callbacks.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, capacity: int = 3,
                 on_resync: Callable[[], None] | None = None):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._loop = loop
        self._capacity = capacity
        self._on_resync = on_resync
        self._frames: deque[H264Frame] = deque()
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._notification_scheduled = False
        self._closed = False
        self._needs_keyframe = True
        self.dropped_frames = 0

    def _wake_locked(self) -> None:
        if not self._notification_scheduled and not self._event.is_set():
            self._notification_scheduled = True
            try:
                self._loop.call_soon_threadsafe(self._notify)
            except RuntimeError:  # The browser/server event loop has closed.
                self._closed = True
                self._frames.clear()

    def _notify(self) -> None:
        with self._lock:
            self._notification_scheduled = False
            if self._frames or self._closed:
                self._event.set()

    def push(self, frame: H264Frame) -> None:
        request_keyframe = False
        with self._lock:
            if self._closed:
                return
            if len(self._frames) >= self._capacity:
                self.dropped_frames += len(self._frames)
                self._frames.clear()
                self._needs_keyframe = True
                request_keyframe = True
            if self._needs_keyframe and not frame.keyframe:
                self.dropped_frames += 1
            else:
                self._needs_keyframe = False
                self._frames.append(frame)
                self._wake_locked()
        if request_keyframe and self._on_resync:
            self._on_resync()

    def resync(self) -> None:
        with self._lock:
            self.dropped_frames += len(self._frames)
            self._frames.clear()
            self._needs_keyframe = True
        if self._on_resync:
            self._on_resync()

    async def recv(self) -> H264Frame:
        while True:
            with self._lock:
                if self._closed:
                    raise EOFError("camera stream closed")
                if self._frames:
                    frame = self._frames.popleft()
                    if not self._frames:
                        self._event.clear()
                    return frame
                self._event.clear()
            await self._event.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._frames.clear()
            self._wake_locked()


def sensor_capture_time(metadata: dict, *, now: float | None = None) -> tuple[float, str]:
    """Translate libcamera CLOCK_BOOTTIME nanoseconds to the monotonic clock."""
    now = time.monotonic() if now is None else now
    sensor_ns = metadata.get("SensorTimestamp")
    if sensor_ns is None or not hasattr(time, "CLOCK_BOOTTIME"):
        return now, "arrival"
    age = time.clock_gettime(time.CLOCK_BOOTTIME) - int(sensor_ns) / 1_000_000_000
    # A slightly future timestamp can result from sampling the two clocks. A
    # grossly inconsistent timestamp must not make an old frame look fresh.
    if age < -0.05 or not math.isfinite(age):
        raise ValueError("invalid camera SensorTimestamp")
    return now - max(0.0, age), "sensor"


class CameraService:
    def __init__(self, *, demo: bool = False, width: int = 960, height: int = 540,
                 fps: int = 30, bitrate: int = 2_000_000,
                 vision_size: tuple[int, int] = (320, 180),
                 vision_fps: int = 15, rotation180: bool = True,
                 stale_after: float = 0.75):
        if min(width, height, *vision_size, fps, vision_fps, bitrate) <= 0:
            raise ValueError("camera dimensions, rates, and bitrate must be positive")
        if any(dimension % 2 for dimension in (width, height, *vision_size)):
            raise ValueError("YUV420 camera dimensions must be even")
        if vision_size[0] > width or vision_size[1] > height:
            raise ValueError("vision stream cannot be larger than the video stream")
        if not demo:
            validate_h264_settings(width, height, fps, bitrate)
        self.demo = demo
        self.width, self.height = width, height
        self.fps, self.bitrate = fps, bitrate
        self.vision_size, self.vision_fps = vision_size, vision_fps
        self.rotation180, self.stale_after = rotation180, stale_after
        self._lock = threading.Lock()
        self._latest: FrameSnapshot | None = None
        self._video_latest: FrameSnapshot | None = None
        self._sequence = 0
        self._running = False
        self._error: str | None = None
        self._video_error: str | None = None
        self._h264_validated = False
        self._h264_profile_level_id: str | None = None
        self._timestamp_source = "demo" if demo else "unavailable"
        self._last_encoded_at: float | None = None
        self._subscribers: set[H264Subscription] = set()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._camera = None
        self._encoder = None
        self._last_vision_arrival = 0.0
        self._last_keyframe_request = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        with self._lock:
            self._error = None
            self._latest = None
            self._video_latest = None
            self._last_encoded_at = None
            self._video_error = None
            self._h264_validated = False
            self._h264_profile_level_id = None
        self._running = True
        try:
            if self.demo:
                self._thread = threading.Thread(target=self._demo_loop, name="demo-camera", daemon=True)
                self._thread.start()
            else:
                self._start_picamera()
        except Exception as exc:
            self._error = str(exc)
            self.stop()
            raise RuntimeError(f"Camera startup failed: {exc}") from exc

    def _start_picamera(self) -> None:
        import cv2
        from libcamera import Transform
        from picamera2 import Picamera2
        from picamera2.outputs import Output

        service = self

        class PacketOutput(Output):
            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                if self.recording and not audio and frame:
                    service._publish_h264(bytes(frame), bool(keyframe), timestamp)

        camera = Picamera2()
        self._camera = camera
        config = camera.create_video_configuration(
            main={"size": (self.width, self.height), "format": "YUV420"},
            lores={"size": self.vision_size, "format": "YUV420"},
            controls={"FrameRate": self.fps},
            transform=Transform(hflip=self.rotation180, vflip=self.rotation180),
            buffer_count=4,
            queue=False,
        )
        camera.configure(config)

        def collect_vision(request):
            now = time.monotonic()
            if self._stop_event.is_set() or now - self._last_vision_arrival < 1 / self.vision_fps:
                return
            self._last_vision_arrival = now
            try:
                captured_at, source = sensor_capture_time(request.get_metadata(), now=now)
                # Pi 4 lores supports YUV, not RGB. make_array returns a copy,
                # so neither inference nor a browser retains a camera buffer.
                yuv = request.make_array("lores")
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                bgr = bgr[:self.vision_size[1], :self.vision_size[0]].copy()
                self._publish_vision(bgr, captured_at, source)
            except Exception as exc:
                # Keep the last timestamp unchanged; the controller must stop
                # once it becomes stale instead of trusting a repeated frame.
                with self._lock:
                    self._error = f"Vision capture failed: {exc}"

        camera.post_callback = collect_vision
        self._encoder = create_h264_encoder(bitrate=self.bitrate, fps=self.fps)
        camera.start_recording(self._encoder, PacketOutput(), name="main")

    def _publish_vision(self, image: np.ndarray, captured_at: float, source: str,
                        video_image: np.ndarray | None = None) -> None:
        image.setflags(write=False)
        with self._lock:
            self._sequence += 1
            self._latest = FrameSnapshot(image, captured_at, self._sequence)
            if video_image is not None:
                video_image.setflags(write=False)
                self._video_latest = FrameSnapshot(video_image, captured_at, self._sequence)
            self._timestamp_source = source
            self._error = None

    def _publish_h264(self, data: bytes, keyframe: bool, timestamp: int | None) -> None:
        now = time.monotonic()
        try:
            sps = h264_sps_info(data)
            if keyframe and (not sps or 8 not in {nal[0] & 31 for nal in _h264_nals(data) if nal}):
                raise ValueError("Encoder keyframe is missing repeated SPS/PPS headers")
            if sps:
                if any(info["macroblocks"] * self.fps > 108_000 for info in sps):
                    raise ValueError("Encoder SPS exceeds level 3.1 macroblocks per second")
                with self._lock:
                    self._h264_validated = True
                    self._h264_profile_level_id = sps[-1]["profile_level_id"]
                    self._video_error = None
        except ValueError as exc:
            with self._lock:
                self._video_error = f"H264 compatibility check failed: {exc}"
                self._h264_validated = False
                self._h264_profile_level_id = None
                subscribers = tuple(self._subscribers)
            for subscriber in subscribers:
                subscriber.resync()
            return
        if not self._h264_validated:
            return
        captured_at = now
        origin_us = getattr(self._encoder, "firsttimestamp", None)
        if timestamp is not None and origin_us is not None:
            try:
                # Picamera2 encodes relative microsecond timestamps. Preserve
                # capture age even if an encoder becomes slow under load.
                captured_at, _ = sensor_capture_time(
                    {"SensorTimestamp": (origin_us + timestamp) * 1000}, now=now)
            except ValueError:
                return
        frame = H264Frame(data, int(timestamp if timestamp is not None else now * 1_000_000), captured_at, keyframe)
        with self._lock:
            self._last_encoded_at = captured_at
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.push(frame)

    def request_keyframe(self) -> None:
        # Recent Picamera2 versions provide force_key_frame. The short periodic
        # GOP is also required: aiortc cannot forward browser PLI requests to an
        # external encoder, and older Picamera2 versions lack this method.
        now = time.monotonic()
        if now - self._last_keyframe_request < 0.2:
            return
        self._last_keyframe_request = now
        encoder = self._encoder
        if encoder is not None and hasattr(encoder, "force_key_frame"):
            try:
                encoder.force_key_frame()
            except Exception:
                LOG.debug("Could not request an immediate keyframe", exc_info=True)

    def subscribe_h264(self, *, capacity: int = 3) -> H264Subscription:
        if self.demo:
            raise RuntimeError("The demonstration source supplies raw video frames")
        subscriber = H264Subscription(asyncio.get_running_loop(), capacity=capacity,
                                      on_resync=self.request_keyframe)
        with self._lock:
            self._subscribers.add(subscriber)
        self.request_keyframe()
        return subscriber

    def unsubscribe_h264(self, subscriber: H264Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
        subscriber.close()

    def snapshot(self, *, copy: bool = True) -> FrameSnapshot | None:
        with self._lock:
            frame = self._latest if self._running else None
        if frame is None or not copy:
            return frame
        return FrameSnapshot(frame.image.copy(), frame.captured_at, frame.sequence)

    def video_snapshot(self) -> FrameSnapshot | None:
        """A raw frame is available only for the explicit demonstration source."""
        with self._lock:
            return self._video_latest if self._running else None

    def jpeg_snapshot(self, quality: int = 75) -> bytes | None:
        import cv2
        frame = self.snapshot(copy=False)
        if frame is None or time.monotonic() - frame.captured_at > self.stale_after:
            return None
        ok, encoded = cv2.imencode(".jpg", frame.image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return encoded.tobytes() if ok else None

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            frame_age = None if self._latest is None else max(0.0, now - self._latest.captured_at)
            video_age = None if self._last_encoded_at is None else max(0.0, now - self._last_encoded_at)
            video_ready = self.demo or (self._h264_validated and video_age is not None and video_age <= self.stale_after)
            return {
                "available": self._running and video_ready and frame_age is not None and frame_age <= self.stale_after,
                "running": self._running,
                "source": "demo" if self.demo else "picamera2",
                "frame_age": frame_age,
                "encoded_frame_age": video_age,
                "sequence": self._sequence,
                "timestamp_source": self._timestamp_source,
                "error": self._error or self._video_error,
                "h264_profile_level_id": self._h264_profile_level_id,
                "width": self.width, "height": self.height, "fps": self.fps,
                "rotation180": self.rotation180,
                "video_codec": "software-demo" if self.demo else "hardware-h264",
            }

    def _demo_loop(self) -> None:
        try:
            import cv2
            started = time.monotonic()
            next_frame = started
            while not self._stop_event.is_set():
                now = time.monotonic()
                elapsed = now - started
                canvas = np.full((self.height, self.width, 3), (22, 27, 32), dtype=np.uint8)
                horizon = int(self.height * 0.43)
                canvas[horizon:] = (35, 42, 45)
                for x in range(-self.width, self.width * 2, max(1, self.width // 8)):
                    cv2.line(canvas, (self.width // 2, horizon), (x, self.height), (59, 68, 72), 1)
                for fraction in (0.5, 0.65, 0.85):
                    y = int(self.height * fraction)
                    cv2.line(canvas, (0, y), (self.width, y), (59, 68, 72), 1)
                ball_x = int(self.width * (0.5 + 0.14 * math.sin(elapsed * 0.35)))
                ball_y = int(self.height * 0.65)
                radius = max(6, int(self.height * 0.065))
                cv2.ellipse(canvas, (ball_x, ball_y + radius), (radius + 8, max(4, radius // 4)),
                            0, 0, 360, (12, 15, 17), -1)
                cv2.circle(canvas, (ball_x, ball_y), radius, (35, 55, 235), -1, cv2.LINE_AA)
                cv2.circle(canvas, (ball_x - radius // 3, ball_y - radius // 3), max(2, radius // 6),
                           (130, 145, 255), -1, cv2.LINE_AA)
                cv2.putText(canvas, "SIMULATED CAMERA", (22, 35), cv2.FONT_HERSHEY_SIMPLEX,
                            0.65, (170, 185, 190), 1, cv2.LINE_AA)
                cv2.putText(canvas, "Hardware disconnected", (22, self.height - 22), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (140, 155, 160), 1, cv2.LINE_AA)
                lores = cv2.resize(canvas, self.vision_size, interpolation=cv2.INTER_AREA)
                self._publish_vision(lores, now, "demo", video_image=canvas)
                next_frame += 1 / self.fps
                if next_frame < time.monotonic():
                    next_frame = time.monotonic()
                self._stop_event.wait(max(0.0, next_frame - time.monotonic()))
        except Exception as exc:
            LOG.exception("Demonstration camera stopped")
            with self._lock:
                self._error = str(exc)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        with self._lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.stop_recording()
            except Exception:
                LOG.debug("Camera recording was already stopped", exc_info=True)
            finally:
                camera.close()
        self._encoder = None
