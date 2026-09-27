"""Gamepad sequences against a fake pad: steps land on schedule (no drift) and ramps ease sticks in."""

import os
import sys
import time
import types

import pytest

from relaymcp.device import gamepad

SLOPPY_CLOCK = sys.platform == "darwin" and bool(os.environ.get("CI"))


class FakePad:
    def __init__(self):
        self.state, self.updates = {}, []

    def reset(self):
        self.state = {"buttons": 0, "left": (0.0, 0.0), "right": (0.0, 0.0), "lt": 0.0, "rt": 0.0}

    def press_button(self, button):
        self.state["buttons"] |= button

    def left_joystick_float(self, x_value_float, y_value_float):
        self.state["left"] = (x_value_float, y_value_float)

    def right_joystick_float(self, x_value_float, y_value_float):
        self.state["right"] = (x_value_float, y_value_float)

    def left_trigger_float(self, value_float):
        self.state["lt"] = value_float

    def right_trigger_float(self, value_float):
        self.state["rt"] = value_float

    def update(self):
        self.updates.append((time.perf_counter(), dict(self.state)))

    def get_index(self):
        return 1


@pytest.fixture
def pad(monkeypatch):
    p = gamepad.VirtualPad()
    p._pad, p._vg = FakePad(), types.SimpleNamespace(XUSB_BUTTON=lambda bit: bit)
    monkeypatch.setattr(p, "_ensure", lambda: None)
    monkeypatch.setattr(p, "_touch", lambda: None)
    return p


def test_steps_keep_time(pad):
    t0 = time.perf_counter()
    result = pad.run_steps([{"buttons": ["a"], "ms": 20}, {"ms": 20}] * 5)
    elapsed = (time.perf_counter() - t0) * 1000
    assert abs(elapsed - 200) < (150 if SLOPPY_CLOCK else 8), elapsed
    if os.name == "nt":  # the handheld's platform (macOS may deschedule a background process for a few ms)
        assert result["timing_ms_p95"] < 3, result
    presses = [u for u in pad._pad.updates if u[1]["buttons"] & gamepad.BUTTON_BITS["a"]]
    assert len(presses) == 5 and pad._pad.updates[-1][1]["buttons"] == 0  # ends in neutral


def test_ramp_eases_the_stick_in(pad):
    pad.run_steps([{"left_stick": [0, 1], "ms": 150, "ramp_ms": 80}, {"left_stick": [1, 0], "ms": 50}])
    ys = [u[1]["left"][1] for u in pad._pad.updates]
    ramp = ys[:ys.index(1.0) + 1]
    assert len(ramp) >= 5 and ramp[0] < 0.3 and ramp == sorted(ramp)  # rises gradually to full
    assert pad._pad.updates[-2][1]["left"] == (1.0, 0.0)  # the next step jumps (no ramp asked)
    assert pad._pad.updates[-1][1]["left"] == (0.0, 0.0)


def test_recorded_changes_become_replayable_steps():
    ev = lambda t, **k: {"t_ms": t, "slot": 0, "buttons": [], "left_stick": [0, 0], "right_stick": [0, 0],  # noqa: E731
                         "left_trigger": 0, "right_trigger": 0, **k}
    events = [ev(0, buttons=["a"]), ev(120), ev(125, left_stick=[0.03, 0.05]),       # jitter inside the deadzone
              ev(400, left_stick=[0, 1]), ev(405, left_stick=[0.01, 0.99]),          # a 5 ms blip merges
              ev(900, right_trigger=1.0, buttons=["right_shoulder"]), ev(1000),
              {**ev(1000), "slot": 1, "buttons": ["b"]}]                              # another controller: ignored
    steps = gamepad.to_steps(events, slot=0)
    assert steps == [{"buttons": ["a"], "ms": 120}, {"ms": 280}, {"left_stick": [0.0, 1.0], "ms": 500},
                     {"buttons": ["right_shoulder"], "right_trigger": 1.0, "ms": 100}, {"ms": 100}]
    assert gamepad.to_steps([], slot=0) == []


def test_a_new_pad_primes_the_game_with_a_net_zero_nudge(pad, monkeypatch):
    monkeypatch.setattr(gamepad, "PRIME_SETTLE_S", 0.0)
    assert pad._prime() is True
    rights = [u[1]["right"][0] for u in pad._pad.updates]
    assert rights[0] > 0.3 and rights[1] < -0.3 and rights[-1] == 0.0  # out and back: the camera ends where it was
    assert all(u[1]["buttons"] == 0 and u[1]["rt"] == 0 for u in pad._pad.updates)  # nothing that acts in a game


def test_priming_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(gamepad, "device_settings", lambda: {"gamepad_prime": False})
    assert gamepad.prime_enabled() is False
    monkeypatch.setattr(gamepad, "device_settings", lambda: {})
    assert gamepad.prime_enabled() is True


def test_the_pad_reprimes_after_touch_mouse_or_keyboard_input(pad, monkeypatch):
    monkeypatch.setattr(gamepad, "PRIME_SETTLE_S", 0.0)
    monkeypatch.setattr(gamepad, "device_settings", lambda: {})
    now = [1000]
    monkeypatch.setattr(gamepad, "_tick_now", lambda: now[0])
    last_input = [500]
    monkeypatch.setattr(gamepad, "_last_input_tick", lambda: last_input[0])
    pad.run_steps([{"buttons": ["a"], "ms": 10}])  # the pad's own input: nothing to switch back from
    first = len(pad._pad.updates)
    now[0] = 5000
    pad.run_steps([{"buttons": ["a"], "ms": 10}])
    assert pad.reprimes == 0 and len(pad._pad.updates) - first == 2  # press + release, no prime
    last_input[0] = 6000  # someone tapped the screen after that
    now[0] = 7000
    out = pad.run_steps([{"buttons": ["a"], "ms": 10}])
    assert pad.reprimes == 1 and "reprimed" in out
    rights = [u[1]["right"][0] for u in pad._pad.updates[-5:]]
    assert rights[0] > 0.3 and rights[1] < -0.3  # the nudge came before the press
    out = pad.run_steps([{"buttons": ["a"], "ms": 10}])
    assert pad.reprimes == 1 and "reprimed" not in out  # once is enough
    assert gamepad._after(500, 0xFFFFFFF0) and not gamepad._after(0xFFFFFFF0, 500)  # tick wrap-around
