"""Local HTTP/WebRTC console with a separate, leased WebSocket control path.

RobotController applies validated drive commands promptly and monitors their
deadlines on a separate thread; video never refreshes a motor lease. Browser
commands echo a short-lived server ticket so TCP backlog cannot
revive old motion after a connection stalls.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hmac
import json
import logging
import math
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType

from . import __version__
from .camera import CameraService
from .control import RobotController
from .hardware import FreenoveHardware, SimHardware
from .status_led import BoardStatusLed
from .vision import HSVBallDetector

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class RoverRuntime:
    def __init__(self, config: dict, demo: bool):
        self.config, self.demo = config, demo
        self.camera = CameraService(demo=demo, **config["camera"])
        self.controller = None
        self.hardware = None
        self._halt = threading.Event()
        self._vision_thread = None
        self.vision_error = None

    def start(self) -> None:
        self.hardware = (SimHardware if self.demo else FreenoveHardware)(self.config["hardware"])
        try:
            self.controller = RobotController(self.config["control"], self.hardware)
            self.camera.start()
            self._vision_thread = threading.Thread(target=self._vision_loop, name="ball-vision", daemon=True)
            self._vision_thread.start()
        except Exception:
            self.stop()
            raise

    def _vision_loop(self) -> None:
        detector = HSVBallDetector(self.config["vision"])
        sequence = -1
        while not self._halt.wait(0.015):
            frame = self.camera.snapshot()
            if frame is None or frame.sequence == sequence:
                continue
            sequence = frame.sequence
            try:
                observation = detector.detect(frame.image, frame.captured_at)
                self.controller.update_vision(observation)
                self.vision_error = None
            except Exception as exc:
                self.vision_error = str(exc)
                self.controller.stop("Vision processing failed")
                self.controller.set_enabled(False)
                LOG.exception("Ball detector failed")
                self._halt.wait(0.5)

    def snapshot(self) -> dict:
        state = self.controller.snapshot()
        state.update(camera=self.camera.status(), demo=self.demo, version=__version__,
                     vision_error=self.vision_error)
        return state

    def stop(self) -> None:
        self._halt.set()
        if self.controller:
            self.controller.shutdown()
        elif self.hardware:
            self.hardware.close()
        if self._vision_thread:
            self._vision_thread.join(timeout=1)
        self.camera.stop()


@dataclass(eq=False)
class Client:
    ws: web.WebSocketResponse
    identity: str = field(default_factory=lambda: secrets.token_hex(8))
    tickets: deque = field(default_factory=lambda: deque(maxlen=16))
    last_sequence: int = -1
    video_ok: bool = False
    video_report_at: float = 0
    last_message_at: float = 0
    session_expires: float = math.inf


class Console:
    def __init__(self, config: dict, *, demo=False, token="", runtime=None):
        self.config, self.demo, self.token = config, demo, token
        if not demo and len(token) < 20:
            raise ValueError("An access key of at least 20 characters is required in hardware mode")
        self.runtime = runtime or RoverRuntime(config, demo)
        self.clients: set[Client] = set()
        self.pending_websockets = 0
        self.owner: Client | None = None
        self.sessions: dict[str, float] = {}
        self.login_attempts: dict[str, deque] = {}
        self.peers: set = set()
        self.peer_expiry_tasks: set[asyncio.Task] = set()
        self.telemetry_task = None
        self.status_led: BoardStatusLed | None = None
        self.status_led_error: str | None = None

    @property
    def controller(self):
        return self.runtime.controller

    def authenticated(self, request: web.Request) -> bool:
        if self.demo and not self.token:
            return True
        session = request.cookies.get("rover_session", "")
        return self.sessions.get(session, 0) > time.monotonic()

    def check_auth(self, request: web.Request) -> None:
        if not self.authenticated(request):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "Sign in with the rover access key"}), content_type="application/json")

    @staticmethod
    def check_origin(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin:
            # Allow local CLI health checks and authenticated API clients.
            return
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != request.host or parsed.scheme != request.scheme:
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed")

    def issue_ticket(self, client: Client) -> str:
        value = secrets.token_urlsafe(12)
        client.tickets.append((value, time.monotonic()))
        return value

    def valid_ticket(self, client: Client, message: dict) -> bool:
        value = message.get("ticket")
        if not isinstance(value, str):
            return False
        now = time.monotonic()
        return any(hmac.compare_digest(value, token) and 0 <= now - issued <= self.config["server"]["ticket_ttl_s"]
                   for token, issued in client.tickets)

    def video_fresh(self, client: Client) -> bool:
        camera = self.runtime.camera.status()
        return bool(client.video_ok and time.monotonic() - client.video_report_at < 0.65 and camera.get("available"))

    def state_for(self, client: Client | None = None) -> dict:
        state = self.runtime.snapshot()
        state.update(control_owned=bool(client and self.owner is client),
                     owner=self.owner.identity if self.owner else None,
                     search_available=False)
        state["status_led"] = (self.status_led.status() if self.status_led else
                               {"state": "unavailable", "error": self.status_led_error})
        return state

    def revoke(self, reason: str) -> None:
        if self.controller:
            self.controller.stop(reason)
        self.owner = None

    @staticmethod
    def accepted(result: dict) -> None:
        if result.get("ok") is False:
            raise ValueError(result.get("reason", "Command rejected"))

    async def start(self, app: web.Application) -> None:
        if not self.demo and self.config["status_led"]["enabled"]:
            try:
                self.status_led = BoardStatusLed.open(self.config["status_led"])
                self.status_led.start()
            except Exception as exc:
                self.status_led_error = str(exc)
                LOG.exception("Status LED unavailable; rover controls remain independent")
                if self.status_led:
                    self.status_led.close()
                    self.status_led = None
        try:
            await asyncio.to_thread(self.runtime.start)
        except BaseException:
            if self.status_led:
                self.status_led.close()
                self.status_led = None
            raise
        self.telemetry_task = asyncio.create_task(self.telemetry(), name="console-telemetry")

    async def close(self, app: web.Application) -> None:
        self.revoke("Server stopping")
        if self.telemetry_task:
            self.telemetry_task.cancel()
            await asyncio.gather(self.telemetry_task, return_exceptions=True)
        for task in list(self.peer_expiry_tasks):
            task.cancel()
        await asyncio.gather(*self.peer_expiry_tasks, return_exceptions=True)
        await asyncio.gather(*(peer.close() for peer in list(self.peers)), return_exceptions=True)
        await asyncio.gather(*(client.ws.close(code=1001, message=b"Server stopping") for client in list(self.clients)), return_exceptions=True)
        try:
            await asyncio.to_thread(self.runtime.stop)
        finally:
            if self.status_led:
                await asyncio.to_thread(self.status_led.close)

    async def send_state(self, client: Client) -> None:
        try:
            if time.monotonic() >= client.session_expires:
                if self.owner is client:
                    self.revoke("Login session expired")
                await client.ws.close(code=1008, message=b"Login session expired")
                return
            await asyncio.wait_for(client.ws.send_json({"type": "state", "ticket": self.issue_ticket(client),
                                                      "state": self.state_for(client)}), timeout=0.2)
        except (TimeoutError, ConnectionError, RuntimeError):
            if self.owner is client:
                self.revoke("Control connection stalled")
            await client.ws.close()

    async def telemetry(self) -> None:
        while True:
            if self.owner and (not self.video_fresh(self.owner) or not self.controller.snapshot().get("enabled")):
                self.revoke("Video or control heartbeat expired")
            if self.status_led:
                self.status_led.set_web_state(
                    connected=bool(self.clients),
                    enabled=bool(self.owner and self.controller.snapshot().get("enabled")))
            if self.clients:
                await asyncio.gather(*(self.send_state(client) for client in tuple(self.clients)))
            await asyncio.sleep(self.config["server"]["telemetry_interval_s"])

    async def status(self, request: web.Request) -> web.Response:
        authenticated = self.authenticated(request)
        body = {"authenticated": authenticated, "demo": self.demo, "version": __version__,
                "modes": [{"id": "direct", "name": "Direct", "available": True},
                          {"id": "search", "name": "Search Mode", "available": False, "label": "Coming soon"}]}
        if authenticated:
            body["state"] = self.state_for()
        return web.json_response(body)

    async def login(self, request: web.Request) -> web.Response:
        self.check_origin(request)
        now = time.monotonic()
        # Keep both the session set and failed-attempt cache bounded.
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        remote = request.remote or "unknown"
        if remote not in self.login_attempts and len(self.login_attempts) >= 256:
            self.login_attempts.clear()
        attempts = self.login_attempts.setdefault(remote, deque(maxlen=12))
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 10:
            return web.json_response({"error": "Too many attempts; wait one minute"}, status=429)
        try:
            body = await request.json()
            supplied = body.get("token", "")
        except (ValueError, AttributeError):
            return web.json_response({"error": "Expected a JSON access key"}, status=400)
        if not isinstance(supplied, str) or not (self.demo and not self.token) and not hmac.compare_digest(supplied, self.token):
            attempts.append(now)
            return web.json_response({"error": "Incorrect rover access key"}, status=401)
        if len(self.sessions) >= 64:
            return web.json_response({"error": "Too many active sessions"}, status=429)
        session = secrets.token_urlsafe(32)
        ttl = self.config["server"]["session_ttl_s"]
        self.sessions[session] = now + ttl
        response = web.json_response({"authenticated": True})
        response.set_cookie("rover_session", session, httponly=True, samesite="Strict", secure=request.secure,
                            max_age=int(ttl), path="/")
        return response

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        self.check_auth(request)
        self.check_origin(request)
        if len(self.clients) + self.pending_websockets >= 12:
            raise web.HTTPServiceUnavailable(text="Too many control connections")
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=4096, compress=False, timeout=1)
        self.pending_websockets += 1
        try:
            await ws.prepare(request)
        finally:
            self.pending_websockets -= 1
        client = Client(ws)
        if not (self.demo and not self.token):
            client.session_expires = self.sessions.get(request.cookies.get("rover_session", ""), 0)
        self.clients.add(client)
        await self.send_state(client)
        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    if raw.type == WSMsgType.ERROR:
                        break
                    continue
                try:
                    message = json.loads(raw.data)
                    if not isinstance(message, dict):
                        raise ValueError("Commands must be JSON objects")
                    await self.handle(client, message)
                except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
        finally:
            if self.owner is client:
                self.revoke("Driver disconnected")
            self.clients.discard(client)
        return ws

    async def handle(self, client: Client, message: dict) -> None:
        if time.monotonic() >= client.session_expires:
            if self.owner is client:
                self.revoke("Login session expired")
            await client.ws.close(code=1008, message=b"Login session expired")
            return
        kind = message.get("type")
        action = message.get("action")
        if kind == "ping":
            await client.ws.send_json({"type": "pong", "sent_at": message.get("sent_at")})
            return
        if kind == "action" and action == "stop":
            self.revoke("Operator stop")
            await self.send_state(client)
            return
        if not self.valid_ticket(client, message):
            if self.owner is client:
                self.revoke("Stale command rejected")
            raise ValueError("stale_ticket: command expired; enable controls again")
        if kind == "heartbeat":
            client.video_ok = message.get("video_ok") is True
            client.video_report_at = time.monotonic()
            if self.owner is client:
                if not self.video_fresh(client):
                    self.revoke("Live video stopped")
                else:
                    self.controller.heartbeat()
            return
        if kind == "action" and action == "enable":
            if self.owner and self.owner is not client:
                raise ValueError("Another browser currently controls the rover")
            if not self.video_fresh(client):
                raise ValueError("Wait for live video before enabling controls")
            self.accepted(self.controller.set_enabled(True))
            self.controller.heartbeat()
            self.owner = client
        elif kind == "action" and action == "release":
            if self.owner and self.owner is not client:
                raise ValueError("The current driver must release the claw")
            self.accepted(self.controller.release())
        elif kind == "action" and action == "disable":
            if self.owner is client:
                self.revoke("Controls disabled")
        else:
            if self.owner is not client or not self.controller.snapshot().get("enabled"):
                raise ValueError("Enable controls to drive or grab")
            if not self.video_fresh(client):
                self.revoke("Live video stopped")
                raise ValueError("Fresh live video is required")
            if kind == "drive":
                seq = message.get("seq")
                if type(seq) is not int or seq <= client.last_sequence:
                    raise ValueError("Out-of-order drive command")
                throttle, steering = message.get("throttle"), message.get("steering")
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -1 <= v <= 1
                       for v in (throttle, steering)):
                    self.revoke("Invalid drive command")
                    raise ValueError("Drive values must be finite numbers from -1 to 1")
                client.last_sequence = seq
                # Right steer: left track faster, right track slower.
                left, right = throttle + steering, throttle - steering
                scale = max(1.0, abs(left), abs(right))
                self.accepted(self.controller.drive(left / scale, right / scale))
                return  # Telemetry sends the state; driving never waits for an ACK.
            elif kind == "action" and action == "grab":
                self.accepted(self.controller.grab())
            else:
                raise ValueError("Unsupported command; Search Mode is coming soon")
        await self.send_state(client)

    async def offer(self, request: web.Request) -> web.Response:
        self.check_auth(request)
        self.check_origin(request)
        if len(self.peers) >= self.config["server"]["max_viewers"]:
            return web.json_response({"error": "Maximum number of video viewers reached"}, status=409)
        if not self.runtime.camera.status().get("available"):
            return web.json_response({"error": "Camera is unavailable or stale"}, status=503)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("type") != "offer" or not isinstance(body.get("sdp"), str):
                raise ValueError("Expected a WebRTC SDP offer")
            # Parsing yields to other offers. Recheck atomically with allocation.
            if len(self.peers) >= self.config["server"]["max_viewers"]:
                return web.json_response({"error": "Maximum number of video viewers reached"}, status=409)
            from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
            from .video import create_video_track
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.peers.add(pc)
            track = create_video_track(self.runtime.camera)
            sender = pc.addTrack(track)
            transceiver = next(t for t in pc.getTransceivers() if t.sender is sender)
            capabilities = RTCRtpSender.getCapabilities("video").codecs
            codecs = [codec for codec in capabilities if codec.mimeType.lower() == "video/h264"]
            # External hardware packets are H264 and must never negotiate VP8.
            codecs.sort(key=lambda codec: codec.parameters.get("profile-level-id", "").startswith("42e0"), reverse=True)
            transceiver.setCodecPreferences(codecs)

            @pc.on("connectionstatechange")
            async def on_state():
                if pc.connectionState in {"failed", "closed", "disconnected"}:
                    track.stop()
                    self.peers.discard(pc)
                    if pc.connectionState != "closed":
                        await pc.close()

            async def expire_unconnected():
                await asyncio.sleep(15)
                if pc.connectionState != "connected":
                    track.stop()
                    self.peers.discard(pc)
                    await pc.close()

            expiry = asyncio.create_task(expire_unconnected())
            self.peer_expiry_tasks.add(expiry)
            expiry.add_done_callback(self.peer_expiry_tasks.discard)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="offer"))
            await asyncio.wait_for(pc.setLocalDescription(await pc.createAnswer()), timeout=8)
            return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})
        except Exception as exc:
            if "pc" in locals():
                self.peers.discard(pc)
                if "track" in locals():
                    track.stop()
                await pc.close()
            LOG.warning("Video negotiation failed: %s", exc)
            return web.json_response({"error": "Video negotiation failed; check the rover log and browser H264 support"}, status=400)

    async def snapshot(self, request: web.Request) -> web.Response:
        self.check_auth(request)
        data = await asyncio.to_thread(self.runtime.camera.jpeg_snapshot)
        if data is None:
            raise web.HTTPServiceUnavailable(text="No camera image")
        return web.Response(body=data, content_type="image/jpeg")


CONSOLE_KEY = web.AppKey("console", Console)


@web.middleware
async def headers(request, handler):
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


def create_app(config: dict, *, demo: bool = False, token: str = "", runtime=None) -> web.Application:
    app = web.Application(client_max_size=262_144, middlewares=[headers])
    console = Console(config, demo=demo, token=token, runtime=runtime)
    app[CONSOLE_KEY] = console
    async def index(request):
        return web.FileResponse(STATIC / "index.html")

    app.router.add_get("/", index)
    app.router.add_get("/api/status", console.status)
    app.router.add_post("/api/login", console.login)
    app.router.add_post("/api/offer", console.offer)
    app.router.add_get("/api/snapshot.jpg", console.snapshot)
    app.router.add_get("/ws", console.websocket)
    app.router.add_static("/static/", STATIC)
    app.on_startup.append(console.start)
    app.on_shutdown.append(console.close)
    return app
