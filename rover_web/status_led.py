"""C12-adjacent LED3 on the V2 board's four-pixel SPI/WS2812 chain.

This is an indicator, never an interlock. In particular, S2 must come from a
real, conditioned sense input; motor commands cannot reveal switch position.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
import time


LOG = logging.getLogger(__name__)
STATUS_LED_DEFAULTS = {"enabled": False, "s2_sense_gpio": None, "s2_active_high": True}


def validate_status_led_config(config: dict, hardware: dict) -> None:
    if type(config["enabled"]) is not bool or type(config["s2_active_high"]) is not bool:
        raise ValueError("status_led.enabled and s2_active_high must be booleans")
    pin = config["s2_sense_gpio"]
    if pin is None:
        if config["enabled"]:
            raise ValueError("status_led needs a physically wired S2 sense GPIO")
        return
    if type(pin) is not int or not 0 <= pin <= 27:
        raise ValueError("status_led.s2_sense_gpio must be a BCM GPIO number")
    reserved = {7, 8, 9, 10, 11, 12, 13, 16, 21, 26,
                *(hardware[key] for key in hardware if key.endswith("_gpio"))}
    if pin in reserved:
        raise ValueError("status_led.s2_sense_gpio conflicts with a rover GPIO")


class LedState(str, Enum):
    WAITING = "waiting"
    S2_OFF = "s2_off"
    CONTROL_DISABLED = "control_disabled"
    CONTROL_ENABLED = "control_enabled"
    S2_UNKNOWN = "s2_unknown"


def select_state(*, web_connected: bool, s2_on: bool | None, control_enabled: bool) -> LedState:
    if not web_connected:
        return LedState.WAITING
    if s2_on is None:
        return LedState.S2_UNKNOWN
    if not s2_on:
        return LedState.S2_OFF
    return LedState.CONTROL_ENABLED if control_enabled else LedState.CONTROL_DISABLED


def state_color(state: LedState, now: float) -> tuple[int, int, int]:
    blink_on = int(now * 2) % 2 == 0
    if state == LedState.WAITING:
        return (0, 20, 0) if blink_on else (0, 0, 0)
    if state == LedState.S2_OFF:
        return (20, 14, 0) if blink_on else (0, 0, 0)
    if state == LedState.CONTROL_DISABLED:
        return (20, 0, 0)
    if state == LedState.CONTROL_ENABLED:
        return (0, 20, 0)
    # Never claim an unmeasured S2 state by showing red, yellow, or solid green.
    return (0, 0, 0)


def encode_grb(color: tuple[int, int, int], *, pixel: int = 3, count: int = 4) -> list[int]:
    """Encode one RGB pixel for the Freenove V2 SPI WS2812 timing and GRB order."""
    if not 0 <= pixel < count or count < 1:
        raise ValueError("LED pixel is outside the chain")
    if len(color) != 3 or any(type(value) is not int or not 0 <= value <= 255 for value in color):
        raise ValueError("RGB values must be bytes")
    pixels = [(0, 0, 0)] * count
    pixels[pixel] = color
    encoded = []
    for red, green, blue in pixels:
        for value in (green, red, blue):
            encoded.extend(0xF8 if value & (1 << bit) else 0x80 for bit in range(7, -1, -1))
    return encoded


class SpiPixel:
    """A single LED3 writer; the other three board pixels remain off."""

    def __init__(self, spi, *, pixel: int = 3, count: int = 4):
        self.spi = spi
        self.pixel = pixel
        self.count = count
        self._last_color: tuple[int, int, int] | None = None
        self.spi.mode = 0
        self.spi.max_speed_hz = 6_400_000

    def show(self, color: tuple[int, int, int]) -> None:
        if color != self._last_color:
            self.spi.xfer2(encode_grb(color, pixel=self.pixel, count=self.count))
            self._last_color = color

    def close(self) -> None:
        try:
            self.show((0, 0, 0))
        finally:
            self.spi.close()


class BoardStatusLed:
    """Poll a real S2 sense input and update only LED3 in a dedicated thread."""

    def __init__(self, config: dict, *, sensor=None, pixel=None, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.sensor = sensor
        self.pixel = pixel
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._web_connected = False
        self._control_enabled = False
        self._state = LedState.WAITING
        self._s2_on: bool | None = None
        self.error: str | None = None

    @classmethod
    def open(cls, config: dict) -> BoardStatusLed:
        from gpiozero import DigitalInputDevice
        import spidev

        sensor = DigitalInputDevice(config["s2_sense_gpio"], pull_up=False)
        spi = spidev.SpiDev()
        try:
            spi.open(0, 0)
            pixel = SpiPixel(spi)
        except BaseException:
            spi.close()
            sensor.close()
            raise
        return cls(config, sensor=sensor, pixel=pixel)

    def set_web_state(self, *, connected: bool, enabled: bool) -> None:
        with self._lock:
            self._web_connected = connected
            self._control_enabled = enabled

    def tick(self) -> None:
        try:
            s2_on = bool(self.sensor.value) == self.config["s2_active_high"]
            self.error = None
        except Exception as exc:
            s2_on = None
            if self.error != str(exc):
                LOG.warning("S2 sense unavailable: %s", exc)
                self.error = str(exc)
        with self._lock:
            state = select_state(web_connected=self._web_connected,
                                 s2_on=s2_on, control_enabled=self._control_enabled)
            self._state = state
            self._s2_on = s2_on
        self.pixel.show(state_color(state, self.clock()))

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Status LED already started")
        self._thread = threading.Thread(target=self._run, name="status-led", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                LOG.exception("Status LED output failed")
                with self._lock:
                    self.error = str(exc)
                    self._state = LedState.S2_UNKNOWN
                    self._s2_on = None
                try:
                    self.pixel.show((0, 0, 0))
                except Exception:
                    pass
                break
            self._stop.wait(0.1)

    def status(self) -> dict:
        with self._lock:
            return {"state": self._state.value, "s2_on": self._s2_on, "error": self.error}

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            if self.pixel is not None:
                self.pixel.close()
        finally:
            if self.sensor is not None:
                self.sensor.close()
