"""Exercise the actual HTTP offer endpoint and WebRTC RTP/decoder path locally."""

import asyncio
import time

import pytest

pytest.importorskip("av")
pytest.importorskip("cv2")
pytest.importorskip("aiortc")

from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription

from rover_web.config import default_config, validate_config
from rover_web.server import CONSOLE_KEY, create_app


def test_demo_video_arrives_through_real_http_webrtc_negotiation(monkeypatch):
    # Keep the test entirely on loopback. Besides avoiding external interfaces,
    # this supports environments that disallow getifaddrs() enumeration. Only
    # address discovery is replaced; ICE, DTLS, RTP, and H264 decoding are real.
    import aioice.ice
    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"])

    async def run():
        config = default_config()
        config["camera"].update(width=320, height=180, vision_size=[160, 90], fps=15, vision_fps=15)
        validate_config(config)
        app = create_app(config, demo=True)
        http = TestClient(TestServer(app))
        receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        received_track = asyncio.get_running_loop().create_future()

        @receiver.on("track")
        def on_track(track):
            if track.kind == "video" and not received_track.done():
                received_track.set_result(track)

        try:
            await http.start_server()
            deadline = time.monotonic() + 3
            while not app[CONSOLE_KEY].runtime.camera.status()["available"]:
                assert time.monotonic() < deadline, "Demonstration camera failed to become ready"
                await asyncio.sleep(0.01)
            transceiver = receiver.addTransceiver("video", direction="recvonly")
            transceiver.setCodecPreferences([
                codec for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType.lower() == "video/h264"
                and codec.parameters.get("profile-level-id") == "42e01f"
            ])
            await receiver.setLocalDescription(await receiver.createOffer())
            response = await http.post("/api/offer", json={
                "type": receiver.localDescription.type,
                "sdp": receiver.localDescription.sdp,
            })
            answer = await response.json()
            assert response.status == 200, answer
            assert "H264/90000" in answer["sdp"]
            await receiver.setRemoteDescription(RTCSessionDescription(**answer))
            track = await asyncio.wait_for(received_track, timeout=5)
            frames = [await asyncio.wait_for(track.recv(), timeout=8) for _ in range(3)]
            assert all((frame.width, frame.height) == (320, 180) for frame in frames)
            assert frames[0].pts < frames[1].pts < frames[2].pts
            assert receiver.connectionState == "connected"
            assert len(app[CONSOLE_KEY].peers) == 1
            # The video viewer never enables or acquires the motor lease.
            assert app[CONSOLE_KEY].owner is None
            assert not app[CONSOLE_KEY].controller.snapshot()["enabled"]
        finally:
            await receiver.close()
            await http.close()

    asyncio.run(run())
