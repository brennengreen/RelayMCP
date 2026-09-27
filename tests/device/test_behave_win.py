"""Closed-loop behavior tests against a small synthetic game on a real Windows desktop (the CI runner): the game
measures the reaction time and tracking error itself, from its own clock. Run with RELAYMCP_DEVICE_TESTS=1."""

import json
import os
import statistics
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")

REACT_GAME = r"""
import json, random, sys, time, tkinter as t
r = t.Tk(); r.title("RelayReactGame"); r.geometry("400x300+200+200"); r.configure(bg="black")
r.attributes("-topmost", True)
c = t.Canvas(r, width=400, height=300, bg="black", highlightthickness=0); c.pack()
state = {"shown": None, "results": [], "misses": 0}
def flash():
    if len(state["results"]) + state["misses"] >= 8:
        print(json.dumps(state), flush=True); r.destroy(); return
    c.delete("all"); c.create_rectangle(100, 75, 300, 225, fill="#00ff00", outline="")
    r.update_idletasks(); state["shown"] = time.perf_counter()
    r.after(1000, timeout)
def timeout():
    if state["shown"] is not None:
        state["misses"] += 1; hide()
def hide():
    state["shown"] = None; c.delete("all"); r.after(random.randint(500, 900), flash)
def key(e):
    if state["shown"] is not None:
        state["results"].append(round((time.perf_counter() - state["shown"]) * 1000, 1)); hide()
r.bind("<KeyPress-space>", key)
r.after(1500, flash); r.after(30000, lambda: (print(json.dumps(state), flush=True), r.destroy()))
r.mainloop()
"""

TRACK_GAME = r"""
import json, math, time, tkinter as t
r = t.Tk(); r.title("RelayTrackGame"); r.geometry("600x450+150+150"); r.configure(bg="white")
r.attributes("-topmost", True)
c = t.Canvas(r, width=600, height=450, bg="white", highlightthickness=0); c.pack()
dot = c.create_oval(0, 0, 24, 24, fill="#ff0000", outline="")
t0 = time.perf_counter(); errors = []
def tick():
    a = (time.perf_counter() - t0) * 1.2
    x, y = 300 + 180 * math.cos(a), 225 + 130 * math.sin(a)
    c.coords(dot, x - 12, y - 12, x + 12, y + 12)
    px, py = r.winfo_pointerx() - c.winfo_rootx(), r.winfo_pointery() - c.winfo_rooty()
    if time.perf_counter() - t0 > 2.0:
        errors.append(math.hypot(px - x, py - y))
    if time.perf_counter() - t0 > 8.0:
        s = sorted(errors); print(json.dumps({"median": s[len(s) // 2], "p90": s[int(len(s) * 0.9)], "n": len(s)}), flush=True)
        r.destroy(); return
    r.after(8, tick)
r.after(200, tick); r.mainloop()
"""


def _start(code, title):
    pytest.importorskip("tkinter")
    from relaymcp.device import focus
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while time.time() < deadline and not focus.find_window(title):
        time.sleep(0.2)
    info = focus.find_window(title)
    if not info:
        proc.kill()
        pytest.skip("game window didn't appear (no interactive desktop?)")
    focus.focus_window(title)
    time.sleep(0.5)
    return proc, focus.find_window(title)


def test_react_to_a_flash(tmp_path):
    from relaymcp.device import server
    proc, info = _start(REACT_GAME, "RelayReactGame")
    left, top, right, bottom = info["rect"]
    # the flash panel's middle, in screen pixels (the window frame adds a few pixels; stay well inside the panel)
    region = [left + 150, top + 120, left + 250, top + 200]
    started = server.BEHAVIORS.start("react", {"region": region, "when": {"color": [0, 255, 0], "tol": 70,
                                                "min_fraction": 0.5}, "do": {"key": ["space"]}, "cooldown_ms": 200},
                                     max_s=25)
    try:
        out = json.loads(proc.stdout.readline() or "{}")
    finally:
        server.BEHAVIORS.stop()
        proc.kill()
    status = server.BEHAVIORS.status(started["id"])
    print(f"\nreact: game-measured reaction ms {out.get('results')}, misses {out.get('misses')}; "
          f"loop {status['hz']} Hz, frame grab p50 {status.get('frame_ms_p50')} ms")
    results = out.get("results") or []
    assert len(results) >= 6 and out.get("misses", 9) <= 2, out
    assert statistics.median(results) < 120, results


def test_track_a_moving_target():
    from relaymcp.device import server
    proc, info = _start(TRACK_GAME, "RelayTrackGame")
    left, top, right, bottom = info["rect"]
    started = server.BEHAVIORS.start("track", {"color": [255, 0, 0], "tol": 60, "region": [left, top, right, bottom],
                                               "aim": "cursor", "output": "mouse", "gain": 0.5}, max_s=10)
    try:
        out = json.loads(proc.stdout.readline() or "{}")
    finally:
        server.BEHAVIORS.stop()
        proc.kill()
    status = server.BEHAVIORS.status(started["id"])
    print(f"\ntrack: game-measured pointer error median {out.get('median', 0):.0f} px, p90 {out.get('p90', 0):.0f} px "
          f"(n={out.get('n')}); loop {status['hz']} Hz, {status['actions']} moves")
    assert out.get("n", 0) > 100 and out["median"] < 60, out
