"""Screen capture on a real Windows desktop (the CI runner). Run with RELAYMCP_DEVICE_TESTS=1."""

import io
import os
import statistics
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")

GREEN = ("import sys, tkinter as t; r = t.Tk(); r.title(sys.argv[1]); r.geometry(sys.argv[2]); r.configure(bg='#00ff00'); "
         "r.attributes('-topmost', True); r.after(60000, r.destroy); r.mainloop()")


@pytest.fixture(scope="module")
def green_rect():
    pytest.importorskip("tkinter")
    from relaymcp.device import focus
    proc = subprocess.Popen([sys.executable, "-c", GREEN, "RelayCaptureGreen", "400x300+300+200"])
    deadline, info = time.time() + 20, None
    while time.time() < deadline and not info:
        info = focus.find_window("RelayCaptureGreen")
        time.sleep(0.2)
    if not info:
        proc.kill()
        pytest.skip("test window didn't appear (no interactive desktop?)")
    focus.focus_window("RelayCaptureGreen")
    time.sleep(0.8)  # let it paint
    x0, y0, x1, y1 = focus.find_window("RelayCaptureGreen")["rect"]
    yield [x0 + 40, y0 + 60, x1 - 40, y1 - 40]  # inside the client area, away from the frame
    proc.kill()


def _center_rgb(jpeg: bytes):
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    return img.getpixel((img.width // 2, img.height // 2))


def _is_green(rgb) -> bool:
    r, g, b = rgb
    return g > 200 and r < 80 and b < 80


def test_screenshot_sees_the_window_and_is_fast(green_rect):
    from relaymcp.device import capture
    g = capture.Grabber()
    first = g.screenshot(region=green_rect)
    assert first["jpeg"] and _is_green(_center_rgb(first["jpeg"])), first["meta"]
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        shot = g.screenshot()
        times.append((time.perf_counter() - t0) * 1000)
    print(f"\ncapture: source={shot['meta']['source']} full-screen screenshot p50 {statistics.median(times):.0f} ms "
          f"(grab {shot['meta']['grab_ms']} ms), {len(shot['jpeg']) // 1024} KB, image {shot['meta']['image']}")
    assert statistics.median(times) < 1000
    again = g.screenshot(region=green_rect, only_if_changed=True)
    assert again["jpeg"] is None and again["meta"]["unchanged"]
    g.release()


def test_gdi_fallback_sees_the_window(green_rect):
    from relaymcp.device import capture
    frame = capture.Grabber()._grab_gdi()
    jpeg, meta = capture.encode(frame, region=green_rect)
    assert meta["to_screen"].startswith("screen = (x+") and _is_green(_center_rgb(jpeg))


def test_dxgi_when_available(green_rect):
    from relaymcp.device import capture
    g = capture.Grabber()
    try:
        frame = g._grab_dxgi()
    except Exception as e:
        pytest.skip(f"DXGI duplication isn't available on this machine: {e}")
    jpeg, _ = capture.encode(frame, region=green_rect)
    assert _is_green(_center_rgb(jpeg))
    g.release()
