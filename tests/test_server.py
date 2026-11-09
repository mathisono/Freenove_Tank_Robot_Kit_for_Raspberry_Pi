"""In-process HTTP/WebSocket integration with the real control state machine.

Only physical GPIO and camera ownership are replaced. Every receive is bounded
so a missing response fails a test rather than hanging the test run.
"""
import asyncio
from collections import deque
import json
import time
import unittest

from aiohttp import CookieJar, WSServerHandshakeError, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from rover_web.config import default_config
from rover_web.control import RobotController
from rover_web.hardware import SimHardware
from rover_web.server import CONSOLE_KEY, create_app
from rover_web.vision import BallObservation


TOKEN = "test-rover-key-at-least-twenty-characters"


class StubCamera:
    def __init__(self):
        self.available = True

    def status(self):
        return {"available": self.available, "source": "test", "age_s": 0}

    def jpeg_snapshot(self):
        return b"\xff\xd8test-jpeg\xff\xd9"


class FakeRuntime:
    def __init__(self, config):
        self.camera = StubCamera()
        self.hardware = SimHardware(config["hardware"])
        self.controller = RobotController(config["control"], self.hardware, autostart=False)

    def fresh(self):
        now = time.monotonic()
        self.controller.update_vision(BallObservation(captured_at=now))
        self.controller.update_range(60, now)

    def start(self):
        self.fresh()

    def snapshot(self):
        return {**self.controller.snapshot(), "camera": self.camera.status(), "demo": False, "version": "test"}

    def stop(self):
        self.controller.shutdown()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = default_config()
        self.config["hardware"]["servo_calibrated"] = True
        self.config["control"]["release_lower_s"] = 0.001
        self.config["control"]["release_open_s"] = 0.001
        self.runtime = FakeRuntime(self.config)
        self.app = create_app(self.config, demo=False, token=TOKEN, runtime=self.runtime)
        self.console = self.app[CONSOLE_KEY]
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.tickets = {}
        self.websockets = []

    async def asyncTearDown(self):
        for ws in self.websockets:
            await asyncio.wait_for(ws.close(), 1)
        await asyncio.wait_for(self.client.close(), 3)

    async def login(self):
        response = await self.client.post("/api/login", json={"token": TOKEN})
        self.assertEqual(response.status, 200)
        return response

    async def receive(self, ws, kind="state", predicate=lambda message: True):
        async def read():
            while True:
                message = await ws.receive_json(timeout=1)
                if message.get("type") == "state":
                    self.tickets[id(ws)] = message["ticket"]
                if message.get("type") == kind and predicate(message):
                    return message
        return await asyncio.wait_for(read(), 1.5)

    async def socket(self):
        await self.login()
        ws = await self.client.ws_connect("/ws")
        self.websockets.append(ws)
        await self.receive(ws)
        return ws

    async def send(self, ws, kind, **fields):
        await ws.send_json({"type": kind, "ticket": self.tickets[id(ws)], **fields})

    async def enable(self, ws):
        self.runtime.fresh()
        await self.send(ws, "heartbeat", video_ok=True)
        await self.send(ws, "action", action="enable")
        return await self.receive(ws, predicate=lambda message: message["state"]["control_owned"])

    async def drive(self, ws, seq=1, throttle=0.5, steering=0):
        self.runtime.fresh()
        await self.send(ws, "drive", seq=seq, throttle=throttle, steering=steering)
        return await self.receive(ws, predicate=lambda message: message["state"]["motor"]["left"] != 0)

    async def test_real_mode_rejects_unauthenticated_control_and_images(self):
        status = await (await self.client.get("/api/status")).json()
        self.assertFalse(status["authenticated"])
        self.assertNotIn("state", status)
        response = await self.client.get("/api/snapshot.jpg")
        self.assertEqual(response.status, 401)
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect("/ws")
        self.assertEqual(caught.exception.status, 401)

    async def test_login_sets_session_cookie_and_grants_status(self):
        wrong = await self.client.post("/api/login", json={"token": "wrong"})
        self.assertEqual(wrong.status, 401)
        response = await self.login()
        cookie = response.cookies["rover_session"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        status = await (await self.client.get("/api/status")).json()
        self.assertTrue(status["authenticated"])
        self.assertIn("state", status)

    async def test_cross_origin_login_and_websocket_are_rejected(self):
        response = await self.client.post("/api/login", json={"token": TOKEN},
                                          headers={"Origin": "http://unrelated.example"})
        self.assertEqual(response.status, 403)
        await self.login()
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect("/ws", headers={"Origin": "http://unrelated.example"})
        self.assertEqual(caught.exception.status, 403)

    async def test_exact_same_origin_login_is_allowed(self):
        origin = str(self.client.make_url("/")).rstrip("/")
        response = await self.client.post("/api/login", json={"token": TOKEN}, headers={"Origin": origin})
        self.assertEqual(response.status, 200)

    async def test_only_one_connection_owns_drive(self):
        first, second = await self.socket(), await self.socket()
        await self.enable(first)
        owner = self.console.owner
        await self.send(second, "heartbeat", video_ok=True)
        await self.send(second, "action", action="enable")
        error = await self.receive(second, "error")
        self.assertIn("Another browser", error["message"])
        self.assertIs(self.console.owner, owner)

    async def test_enable_requires_report_of_rendered_video(self):
        ws = await self.socket()
        await self.send(ws, "action", action="enable")
        error = await self.receive(ws, "error")
        self.assertIn("live video", error["message"])
        self.assertFalse(self.runtime.controller.snapshot()["enabled"])

    async def test_ticket_older_than_500_ms_revokes_driver(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.drive(ws)
        ticket = self.tickets[id(ws)]
        self.assertEqual(self.config["server"]["ticket_ttl_s"], 0.5)
        self.console.owner.tickets = deque([(ticket, time.monotonic() - 0.51)], maxlen=16)
        await self.send(ws, "drive", seq=2, throttle=0.5, steering=0)
        error = await self.receive(ws, "error")
        self.assertIn("stale_ticket", error["message"])
        self.assertIsNone(self.console.owner)
        self.assertEqual((self.runtime.hardware.left, self.runtime.hardware.right), (0, 0))

    async def test_old_drive_sequence_does_not_update_command(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.drive(ws, seq=10)
        last_drive = self.runtime.controller._drive_at
        await self.send(ws, "drive", seq=9, throttle=-1, steering=0)
        error = await self.receive(ws, "error")
        self.assertIn("Out-of-order", error["message"])
        self.assertEqual(self.console.owner.last_sequence, 10)
        self.assertEqual(self.runtime.controller._drive_at, last_drive)

    async def test_nan_motor_command_is_rejected_and_stops(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.drive(ws)
        await ws.send_str(json.dumps({"type": "drive", "ticket": self.tickets[id(ws)],
                                     "seq": 2, "throttle": 0.5, "steering": float("nan")}))
        error = await self.receive(ws, "error")
        self.assertIn("finite", error["message"])
        self.assertIsNone(self.console.owner)
        self.assertEqual((self.runtime.hardware.left, self.runtime.hardware.right), (0, 0))

    async def test_video_loss_heartbeat_stops_and_cannot_auto_resume(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.drive(ws)
        await self.send(ws, "heartbeat", video_ok=False)
        await self.receive(ws, predicate=lambda message: not message["state"]["enabled"])
        self.assertIsNone(self.console.owner)
        self.assertEqual(self.runtime.hardware.left, 0)
        await self.send(ws, "heartbeat", video_ok=True)
        await self.send(ws, "drive", seq=2, throttle=0.5, steering=0)
        error = await self.receive(ws, "error")
        self.assertIn("Enable controls", error["message"])

    async def test_stale_camera_source_revokes_even_if_browser_claims_video(self):
        ws = await self.socket()
        await self.enable(ws)
        self.runtime.camera.available = False
        await self.send(ws, "drive", seq=1, throttle=0.5, steering=0)
        await self.receive(ws, "error")
        self.assertIsNone(self.console.owner)
        self.assertFalse(self.runtime.controller.snapshot()["enabled"])

    async def test_observer_can_stop_without_a_command_ticket(self):
        driver, observer = await self.socket(), await self.socket()
        await self.enable(driver)
        await self.drive(driver)
        await observer.send_json({"type": "action", "action": "stop"})
        await self.receive(observer, predicate=lambda message: not message["state"]["enabled"])
        self.assertIsNone(self.console.owner)
        self.assertEqual((self.runtime.hardware.left, self.runtime.hardware.right), (0, 0))

    async def test_explicit_release_allowed_while_disabled(self):
        ws = await self.socket()
        self.runtime.hardware.grip(False)
        self.runtime.controller._grip_closed = True
        await self.send(ws, "action", action="release")
        state = await self.receive(ws, predicate=lambda message: message["state"]["phase"] == "releasing")
        self.assertFalse(state["state"]["enabled"])
        await asyncio.sleep(0.01)
        self.runtime.controller.tick()
        self.assertTrue(self.runtime.hardware.gripper_open)
        self.assertFalse(self.runtime.controller.snapshot()["grip_closed"])

    async def test_uncalibrated_grab_returns_an_error(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.send(ws, "action", action="grab")
        error = await self.receive(ws, "error")
        self.assertIn("calibration", error["message"])
        self.assertEqual(self.runtime.hardware.events, [])

    async def test_driver_disconnect_clears_ownership_and_stops(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.drive(ws)
        await ws.close()
        deadline = asyncio.get_running_loop().time() + 0.5
        while self.console.owner is not None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertIsNone(self.console.owner)
        self.assertEqual((self.runtime.hardware.left, self.runtime.hardware.right), (0, 0))

    async def test_expired_session_closes_live_control_and_stops(self):
        ws = await self.socket()
        await self.enable(ws)
        await self.drive(ws)
        self.console.owner.session_expires = time.monotonic() - 1
        await self.send(ws, "drive", seq=2, throttle=0.5, steering=0)

        async def wait_closed():
            while True:
                message = await ws.receive(timeout=1)
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                    return
        await asyncio.wait_for(wait_closed(), 1.5)
        self.assertIsNone(self.console.owner)
        self.assertFalse(self.runtime.controller.snapshot()["enabled"])
        self.assertEqual((self.runtime.hardware.left, self.runtime.hardware.right), (0, 0))


if __name__ == "__main__":
    unittest.main()
