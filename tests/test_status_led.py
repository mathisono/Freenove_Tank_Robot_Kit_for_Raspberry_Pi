from rover_web.hardware import HARDWARE_DEFAULTS
from rover_web.status_led import (BoardStatusLed, LedState, SpiPixel, encode_grb,
                                  select_state, state_color, validate_status_led_config)


def test_status_requires_measured_s2_before_claiming_power_state():
    assert select_state(web_connected=False, s2_on=None, control_enabled=False) == LedState.WAITING
    assert select_state(web_connected=True, s2_on=None, control_enabled=True) == LedState.S2_UNKNOWN
    assert select_state(web_connected=True, s2_on=False, control_enabled=True) == LedState.S2_OFF
    assert select_state(web_connected=True, s2_on=False, control_enabled=False) == LedState.S2_OFF
    assert select_state(web_connected=True, s2_on=True, control_enabled=False) == LedState.CONTROL_DISABLED
    assert select_state(web_connected=True, s2_on=True, control_enabled=True) == LedState.CONTROL_ENABLED
    assert state_color(LedState.S2_UNKNOWN, 0) == (0, 0, 0)
    assert state_color(LedState.WAITING, 0) == (0, 20, 0)
    assert state_color(LedState.WAITING, 0.5) == (0, 0, 0)
    assert state_color(LedState.S2_OFF, 0) == (20, 14, 0)
    assert state_color(LedState.S2_OFF, 0.5) == (0, 0, 0)
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


def test_status_led_requires_a_real_nonconflicting_sense_pin():
    config = {"enabled": False, "s2_sense_gpio": None, "s2_active_high": True}
    validate_status_led_config(config, HARDWARE_DEFAULTS)
    config["enabled"] = True
    try:
        validate_status_led_config(config, HARDWARE_DEFAULTS)
    except ValueError as exc:
        assert "physically wired" in str(exc)
    else:
        raise AssertionError("Enabled LED accepted no S2 sense GPIO")
    for pin in (10, 12, 16, 22, 24):
        config["s2_sense_gpio"] = pin
        try:
            validate_status_led_config(config, HARDWARE_DEFAULTS)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Reserved GPIO {pin} was accepted")
    config["s2_sense_gpio"] = 25
    validate_status_led_config(config, HARDWARE_DEFAULTS)


def test_led_state_follows_sensed_s2_and_web_control():
    class Sensor:
        value = 0

    class Pixel:
        def __init__(self):
            self.colors = []

        def show(self, color):
            self.colors.append(color)

    sensor, pixel = Sensor(), Pixel()
    led = BoardStatusLed({"s2_active_high": True}, sensor=sensor, pixel=pixel, clock=lambda: 0)
    led.tick()
    assert led.status()["state"] == "waiting"
    assert pixel.colors[-1] == (0, 20, 0)
    led.set_web_state(connected=True, enabled=False)
    led.tick()
    assert led.status()["state"] == "s2_off"
    assert pixel.colors[-1] == (20, 14, 0)
    sensor.value = 1
    led.tick()
    assert led.status()["state"] == "control_disabled"
    assert pixel.colors[-1] == (20, 0, 0)
    led.set_web_state(connected=True, enabled=True)
    led.tick()
    assert led.status()["state"] == "control_enabled"
    assert pixel.colors[-1] == (0, 20, 0)
