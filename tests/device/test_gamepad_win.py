"""The virtual gamepad on a real Windows machine with ViGEmBus (the CI runner). Run with RELAYMCP_DEVICE_TESTS=1."""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")


def test_connect_is_ready_quickly_and_unplugs():
    from relaymcp.device import gamepad
    t0 = time.monotonic()
    r = gamepad.PAD.connect(keep_plugged=True)
    assert r["connected"] and r["kept_plugged"] and r["xinput_slot"] is not None
    assert time.monotonic() - t0 < 2.0, r  # no multi-second notice wait without Armoury Crate
    slot = r["xinput_slot"]
    assert any(s["slot"] == slot and s["connected"] for s in gamepad.xinput_states())
    assert gamepad.PAD.run_steps([{"buttons": ["a"], "ms": 40}])["steps"] == 1
    assert gamepad.PAD.disconnect()
    deadline = time.time() + 3
    while time.time() < deadline and any(s["slot"] == slot and s["connected"] for s in gamepad.xinput_states()):
        time.sleep(0.1)
    assert not any(s["slot"] == slot and s["connected"] for s in gamepad.xinput_states())
