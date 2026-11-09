import json

from rover_web.config import load_config
from rover_web.status_led import (BoardStatusLed, LedState, SpiPixel, encode_grb,
                                  select_state, state_color, validate_status_led_config)


def test_status_reports_only_web_and_control_state():
    assert select_state(web_connected=False, control_enabled=False) == LedState.WAITING
    assert select_state(web_connected=False, control_enabled=True) == LedState.WAITING
    assert select_state(web_connected=True, control_enabled=False) == LedState.CONTROL_DISABLED
    assert select_state(web_connected=True, control_enabled=True) == LedState.CONTROL_ENABLED
    assert state_color(LedState.UNAVAILABLE, 0) == (0, 0, 0)
    assert state_color(LedState.WAITING, 0) == (0, 20, 0)
    assert state_color(LedState.WAITING, 0.5) == (0, 0, 0)
    assert state_color(LedState.CONTROL_DISABLED, 0.5) == (20, 0, 0)
    assert state_color(LedState.CONTROL_ENABLED, 0.5) == (0, 20, 0)


def test_spi_encoding_targets_only_led3_in_v2_grb_order():
    encoded = encode_grb((255, 0, 0))
    assert len(encoded) == 4 * 3 * 8
    assert encoded[:72] == [0x80] * 72  # LED0–LED2 off
    assert encoded[72:80] == [0x80] * 8  # green first
    assert encoded[80:88] == [0xF8] * 8  # red second
    assert encoded[88:] == [0x80] * 8  # blue off


def test_spi_writer_only_writes_changes_and_turns_off_before_close():
    class FakeSpi:
        def __init__(self):
            self.writes = []
            self.closed = False

        def xfer2(self, data):
            self.writes.append(data)

        def close(self):
            self.closed = True

    spi = FakeSpi()
    pixel = SpiPixel(spi)
    pixel.show((0, 20, 0))
    pixel.show((0, 20, 0))
    pixel.close()
    assert spi.mode == 0
    assert spi.max_speed_hz == 6_400_000
    assert len(spi.writes) == 2
    assert spi.writes[-1] == encode_grb((0, 0, 0))
    assert spi.closed


def test_status_led_can_be_enabled_without_a_sense_pin():
    config = {"enabled": False}
    validate_status_led_config(config, {})
    config["enabled"] = True
    validate_status_led_config(config, {})


def test_old_s2_settings_do_not_block_enabling_led(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"status_led": {"enabled": True,
                                                "s2_sense_gpio": None,
                                                "s2_active_high": True}}))
    assert load_config(path)["status_led"] == {"enabled": True}


def test_led_state_follows_web_control():
    class Pixel:
        def __init__(self):
            self.colors = []

        def show(self, color):
            self.colors.append(color)

    pixel = Pixel()
    led = BoardStatusLed({"enabled": True}, pixel=pixel, clock=lambda: 0)
    led.tick()
    assert led.status()["state"] == "waiting"
    assert pixel.colors[-1] == (0, 20, 0)
    led.set_web_state(connected=True, enabled=False)
    led.tick()
    assert led.status()["state"] == "control_disabled"
    assert pixel.colors[-1] == (20, 0, 0)
    led.set_web_state(connected=True, enabled=True)
    led.tick()
    assert led.status()["state"] == "control_enabled"
    assert pixel.colors[-1] == (0, 20, 0)
    assert "s2_on" not in led.status()
