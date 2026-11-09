"""Camera/transport checks that exercise freshness and decodable bounded queues."""

import asyncio
from fractions import Fraction
import time
import sys
import types

import numpy as np
import pytest

from rover_web.camera import (CameraService, H264Frame, H264Subscription, create_h264_encoder,
                              h264_sps_info, sensor_capture_time, validate_h264_settings)


def encoded(number, *, keyframe=False):
    return H264Frame(b"\x00\x00\x00\x01" + (b"\x65" if keyframe else b"\x41") + bytes([number]),
                     number * 33_333, time.monotonic(), keyframe)


def test_h264_stream(level="3.1"):
    """Make a standards-conforming encoder fixture without Pi hardware."""
    av = pytest.importorskip("av")
    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height = 320, 180
    codec.pix_fmt = "yuv420p"
    codec.time_base, codec.framerate = Fraction(1, 30), Fraction(30, 1)
    codec.options = {"profile": "baseline", "level": level, "tune": "zerolatency", "preset": "ultrafast"}
    frame = av.VideoFrame.from_ndarray(np.zeros((180, 320, 3), dtype=np.uint8), format="bgr24")
    frame.pts = 0
    return b"".join(bytes(packet) for packet in [*codec.encode(frame), *codec.encode(None)])


test_h264_stream.__test__ = False


def test_new_viewer_waits_for_keyframe():
    async def check():
        subscriber = H264Subscription(asyncio.get_running_loop())
        subscriber.push(encoded(1))
        subscriber.push(encoded(2, keyframe=True))
        frame = await subscriber.recv()
        assert frame.timestamp_us == 2 * 33_333
        assert frame.keyframe
        assert subscriber.dropped_frames == 1
        subscriber.close()
    asyncio.run(check())


def test_slow_viewer_discards_dependent_backlog_and_resyncs():
    async def check():
        requests = []
        subscriber = H264Subscription(asyncio.get_running_loop(), capacity=2,
                                      on_resync=lambda: requests.append(True))
        subscriber.push(encoded(1, keyframe=True))
        subscriber.push(encoded(2))
        subscriber.push(encoded(3))  # Overflow loses the reference chain.
        subscriber.push(encoded(4))
        subscriber.push(encoded(5, keyframe=True))
        frame = await subscriber.recv()
        assert frame.timestamp_us == 5 * 33_333
        assert subscriber.dropped_frames == 4
        assert requests
        subscriber.close()
    asyncio.run(check())


def test_event_loop_wakeups_are_bounded_even_when_consumer_runs_inline():
    class DeferredLoop:
        def __init__(self):
            self.callbacks = []

        def call_soon_threadsafe(self, callback):
            self.callbacks.append(callback)

    async def check():
        loop = DeferredLoop()
        subscriber = H264Subscription(loop)
        for index in range(50):
            subscriber.push(encoded(index, keyframe=True))
            await subscriber.recv()  # A buffered result returns without yielding.
        assert len(loop.callbacks) == 1
        loop.callbacks.pop()()
        subscriber.close()
    asyncio.run(check())


def test_close_wakes_waiting_receiver():
    async def check():
        subscriber = H264Subscription(asyncio.get_running_loop())
        receiver = asyncio.create_task(subscriber.recv())
        await asyncio.sleep(0)
        subscriber.close()
        with pytest.raises(EOFError):
            await asyncio.wait_for(receiver, 0.5)
    asyncio.run(check())


def test_sensor_age_is_preserved_instead_of_marking_old_frame_fresh(monkeypatch):
    monkeypatch.setattr(time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(time, "clock_gettime", lambda clock: 500.0)
    timestamp, source = sensor_capture_time({"SensorTimestamp": 498_500_000_000}, now=100.0)
    assert timestamp == 98.5
    assert source == "sensor"
    with pytest.raises(ValueError):
        sensor_capture_time({"SensorTimestamp": 501_000_000_000}, now=100.0)


def test_snapshot_copy_and_stale_status():
    camera = CameraService(demo=True, stale_after=0.5)
    camera._running = True
    camera._publish_vision(np.ones((4, 4, 3), dtype=np.uint8), time.monotonic(), "demo")
    snapshot = camera.snapshot()
    snapshot.image[:] = 9
    assert camera.snapshot().image[0, 0, 0] == 1
    assert camera.status()["available"]
    camera._publish_vision(np.ones((4, 4, 3), dtype=np.uint8), time.monotonic() - 1, "demo")
    assert not camera.status()["available"]
    camera.stop()
    assert camera.snapshot() is None


def test_demo_runs_without_picamera_and_provides_video_and_vision():
    pytest.importorskip("cv2")
    camera = CameraService(demo=True, width=320, height=180, vision_size=(160, 90))
    try:
        camera.start()
        deadline = time.monotonic() + 2
        while camera.snapshot() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert camera.snapshot().image.shape == (90, 160, 3)
        assert camera.video_snapshot().image.shape == (180, 320, 3)
        assert camera.status()["source"] == "demo"
        assert camera.jpeg_snapshot().startswith(b"\xff\xd8")
    finally:
        camera.stop()


def test_h264_track_returns_packet_with_camera_timestamp():
    av = pytest.importorskip("av")
    pytest.importorskip("aiortc")
    from aiortc.codecs.h264 import H264Encoder
    from rover_web.video import H264PacketTrack

    async def check():
        camera = CameraService()
        track = H264PacketTrack(camera)
        receiver = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        camera._publish_h264(test_h264_stream(), True, 123_456)
        packet = await asyncio.wait_for(receiver, 0.5)
        assert isinstance(packet, av.Packet)
        assert packet.pts == packet.dts == 123_456
        assert packet.time_base == Fraction(1, 1_000_000)
        packetizer = H264Encoder()
        payloads, timestamp = packetizer.pack(packet)
        assert payloads
        assert timestamp == 11_111
        assert packetizer.codec is None  # No software codec was instantiated.
        track.stop()
        assert not camera._subscribers
    asyncio.run(check())


def test_actual_sps_must_match_negotiated_level_and_reference_budget():
    stream = test_h264_stream()
    info = h264_sps_info(stream)[0]
    assert info["profile_level_id"].startswith("42")
    assert info["profile_level_id"].endswith("1f")
    assert info["macroblocks"] == 240
    assert info["reference_frames"] * info["macroblocks"] <= 18_000
    with pytest.raises(ValueError, match="expected constrained-baseline level 3.1"):
        h264_sps_info(test_h264_stream("4.0"))


def test_mismatched_hardware_output_makes_camera_unavailable():
    camera = CameraService(width=320, height=180, vision_size=(160, 90))
    camera._running = True
    camera._publish_vision(np.zeros((90, 160, 3), dtype=np.uint8), time.monotonic(), "sensor")
    camera._publish_h264(test_h264_stream("4.0"), True, 0)
    assert not camera.status()["available"]
    assert "compatibility check failed" in camera.status()["error"]
    # A new vision frame must not mask the encoder's error.
    camera._publish_vision(np.zeros((90, 160, 3), dtype=np.uint8), time.monotonic(), "sensor")
    assert "compatibility check failed" in camera.status()["error"]
    camera._publish_h264(test_h264_stream(), True, 33_333)
    assert camera.status()["available"]
    camera.stop()


def test_hardware_settings_reject_high_resolution_before_opening_devices():
    with pytest.raises(ValueError, match="1280x720"):
        CameraService(width=1920, height=1080)
    with pytest.raises(ValueError, match="1280x720"):
        validate_h264_settings(1280, 720, 31, 2_000_000)
    validate_h264_settings(1280, 720, 30, 8_000_000)


def test_hardware_adapter_sets_level_before_v4l2_streaming(monkeypatch):
    """Exercise the adapter boundary where upstream applies kernel controls."""
    v4l2_module = types.ModuleType("picamera2.encoders.v4l2_encoder")
    h264_module = types.ModuleType("picamera2.encoders.h264_encoder")
    constants = types.ModuleType("videodev2")
    names = ["V4L2_CID_MPEG_VIDEO_H264_I_PERIOD", "V4L2_CID_MPEG_VIDEO_H264_LEVEL",
             "V4L2_CID_MPEG_VIDEO_H264_PROFILE", "V4L2_CID_MPEG_VIDEO_REPEAT_SEQ_HEADER",
             "V4L2_MPEG_VIDEO_H264_LEVEL_3_1", "V4L2_MPEG_VIDEO_H264_PROFILE_CONSTRAINED_BASELINE"]
    for index, name in enumerate(names):
        setattr(constants, name, index + 100)

    class V4L2Encoder:
        def _start(self):
            self.applied_before_streaming = dict(self._controls)

    class PiH264Encoder(V4L2Encoder):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._output = [object()]
            self.width, self.height = 960, 540
            self.notified = []

        def _send_streams(self, output):
            self.notified.append(output)

        def _start(self):
            raise AssertionError("Upstream H264 startup must not overwrite requested level")

    h264_module.H264Encoder = PiH264Encoder
    v4l2_module.V4L2Encoder = V4L2Encoder
    monkeypatch.setitem(sys.modules, "picamera2.encoders.h264_encoder", h264_module)
    monkeypatch.setitem(sys.modules, "picamera2.encoders.v4l2_encoder", v4l2_module)
    monkeypatch.setitem(sys.modules, "videodev2", constants)
    encoder = create_h264_encoder(bitrate=2_000_000, fps=30)
    encoder._start()
    applied = encoder.applied_before_streaming
    assert applied[constants.V4L2_CID_MPEG_VIDEO_H264_LEVEL] == constants.V4L2_MPEG_VIDEO_H264_LEVEL_3_1
    assert applied[constants.V4L2_CID_MPEG_VIDEO_H264_PROFILE] == constants.V4L2_MPEG_VIDEO_H264_PROFILE_CONSTRAINED_BASELINE
    assert applied[constants.V4L2_CID_MPEG_VIDEO_REPEAT_SEQ_HEADER] == 1
    assert applied[constants.V4L2_CID_MPEG_VIDEO_H264_I_PERIOD] == 15
    assert encoder.kwargs["enable_sps_framerate"]
    assert encoder.notified
