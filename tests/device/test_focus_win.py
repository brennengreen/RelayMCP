"""Focus handling on a real Windows desktop (the CI runner). Run with RELAYMCP_DEVICE_TESTS=1."""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")

WINDOW = ("import sys, tkinter as t; r = t.Tk(); r.title(sys.argv[1]); r.geometry(sys.argv[2]); "
          "r.after(60000, r.destroy); r.mainloop()")


@pytest.fixture(scope="module")
def windows_ab():
    pytest.importorskip("tkinter")
    from relaymcp.device import focus
    procs = [subprocess.Popen([sys.executable, "-c", WINDOW, title, geo])
             for title, geo in (("RelayFocusA", "400x300+50+50"), ("RelayFocusB", "400x300+500+50"))]
    deadline = time.time() + 20
    while time.time() < deadline and not (focus.find_window("RelayFocusA") and focus.find_window("RelayFocusB")):
        time.sleep(0.2)
    if not focus.find_window("RelayFocusB"):
        pytest.skip("test windows didn't appear (no interactive desktop?)")
    yield focus
    for p in procs:
        p.kill()


def test_focus_window_switches_and_verifies(windows_ab):
    focus = windows_ab
    for title in ("RelayFocusA", "RelayFocusB", "RelayFocusA"):
        r = focus.focus_window(title)
        assert r["ok"], r
        assert focus.foreground()["title"] == title


def test_input_target_is_refocused_and_reported(windows_ab):
    focus = windows_ab
    focus.set_target("RelayFocusA")
    try:
        assert focus.focus_window("RelayFocusB")["ok"]
        pre = focus.before_input()
        assert pre.get("refocused") is True
        report = focus.annotate({}, pre)
        assert report["foreground"]["target"] is True and "warning" not in report
    finally:
        focus.set_target(None)


def test_missing_window_is_reported_truthfully(windows_ab):
    r = windows_ab.focus_window("NoSuchWindowRelay")
    assert r["ok"] is False and "no window matches" in r["error"]
