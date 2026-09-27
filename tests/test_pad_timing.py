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
    assert abs(elapsed - 200) < (60 if SLOPPY_CLOCK else 8), elapsed
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
