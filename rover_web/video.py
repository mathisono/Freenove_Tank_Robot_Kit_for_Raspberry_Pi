"""WebRTC tracks: pass Pi hardware H.264 directly to aiortc's RTP packetizer."""

from __future__ import annotations

import asyncio
from fractions import Fraction
import time

import av
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

from .camera import CameraService, H264Subscription


class H264PacketTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, camera: CameraService):
        super().__init__()
        self.camera = camera
        self._subscription: H264Subscription | None = None

    async def recv(self) -> av.Packet:
        if self.readyState != "live":
            raise MediaStreamError
        if self._subscription is None:
            self._subscription = self.camera.subscribe_h264()
        try:
            while self.readyState == "live":
                frame = await asyncio.wait_for(self._subscription.recv(), timeout=3.0)
                if time.monotonic() - frame.captured_at > 0.5:
                    self._subscription.resync()
                    continue
                # Returning Packet, not VideoFrame, makes RTCRtpSender call
                # H264Encoder.pack(). No decode/software re-encode occurs.
                packet = av.Packet(frame.data)
                packet.pts = packet.dts = frame.timestamp_us
                packet.time_base = Fraction(1, 1_000_000)
                return packet
        except (TimeoutError, EOFError):
            self.stop()
            raise MediaStreamError from None
        raise MediaStreamError

    def stop(self) -> None:
        if self._subscription is not None:
            self.camera.unsubscribe_h264(self._subscription)
            self._subscription = None
        super().stop()


class DemoVideoTrack(MediaStreamTrack):
    """Software video is deliberately restricted to the hardware-free demo."""

    kind = "video"

    def __init__(self, camera: CameraService):
        super().__init__()
        self.camera = camera
        self._last_sequence = -1
        self._started_at: float | None = None
        self._last_pts = -1

    async def recv(self) -> av.VideoFrame:
        deadline = time.monotonic() + 3.0
        while self.readyState == "live":
            snapshot = self.camera.video_snapshot()
            if snapshot is not None and snapshot.sequence != self._last_sequence:
                if time.monotonic() - snapshot.captured_at <= self.camera.stale_after:
                    self._last_sequence = snapshot.sequence
                    if self._started_at is None:
                        self._started_at = snapshot.captured_at
                    frame = av.VideoFrame.from_ndarray(snapshot.image, format="bgr24")
                    frame.pts = max(self._last_pts + 1,
                                    round((snapshot.captured_at - self._started_at) * 90_000))
                    frame.time_base = Fraction(1, 90_000)
                    self._last_pts = frame.pts
                    return frame
            if time.monotonic() > deadline:
                self.stop()
                raise MediaStreamError
            await asyncio.sleep(1 / self.camera.fps)
        raise MediaStreamError


def create_video_track(camera: CameraService) -> MediaStreamTrack:
    """The server must negotiate H.264 when using the hardware packet track."""
    return DemoVideoTrack(camera) if camera.demo else H264PacketTrack(camera)
