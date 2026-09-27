"""Behaviors against a simulated screen (numpy frames) and recorded outputs: no Windows, no real input."""

import os
import sys
import threading
import time

import pytest

# Behaviors run on Windows handhelds. Shared macOS CI machines oversleep by 15-25 ms, so strict timing is only
# checked on Windows and on developer machines.
SLOPPY_CLOCK = sys.platform == "darwin" and bool(os.environ.get("CI"))

np = pytest.importorskip("numpy")

from relaymcp.device import behave  # noqa: E402


class World:
    """A 400x300 screen: a colored square the tests move around, a flash panel, and a cursor that mouse moves push."""

    def __init__(self):
        self.lock = threading.Lock()
        self.target = [300.0, 200.0]
        self.flash = False
        self.cursor = [50.0, 50.0]
        self.keys, self.pads, self.clicks, self.moves, self.sticks, self.released = [], [], 0, [], [], 0
        self.text = []

    def frame(self, region=None):
        with self.lock:
            img = np.zeros((300, 400, 4), np.uint8)
            x, y = int(self.target[0]), int(self.target[1])
            img[max(0, y - 6):y + 6, max(0, x - 6):x + 6] = (0, 0, 255, 255)  # BGRA red square
            if self.flash:
                img[0:40, 0:40] = (0, 255, 0, 255)  # green panel top-left
        if region:
            img = img[region[1]:region[3], region[0]:region[2]]
        return img, time.perf_counter()


class Outputs:
    def __init__(self, world):
        self.w = world

    def key(self, keys):
        self.w.keys.append(tuple(keys))

    def pad(self, buttons):
        self.w.pads.append(tuple(buttons))

    def click(self):
        self.w.clicks += 1

    def mouse(self, dx, dy):
        with self.w.lock:
            self.w.cursor[0] += dx
            self.w.cursor[1] += dy
        self.w.moves.append((dx, dy))

    def stick(self, side, x, y):
        self.w.sticks.append((side, round(x, 2), round(y, 2)))

    def release(self):
        self.w.released += 1


@pytest.fixture
def world():
    return World()


@pytest.fixture
def rt(world):
    runtime = behave.Runtime(world.frame, Outputs(world), read_text=lambda region: world.text,
                             cursor=lambda: list(world.cursor))
    yield runtime
    runtime.stop()


def wait_state(rt, rid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s = rt.status(rid)
        if s["state"] != "running":
            return s
        time.sleep(0.02)
    return rt.status(rid)


def test_react_to_a_color_quickly_and_only_once(rt, world):
    rid = rt.start("react", {"region": [0, 0, 40, 40], "when": {"color": [0, 255, 0], "tol": 30},
                             "do": {"key": ["space"]}, "repeat": False}, max_s=3)["id"]
    time.sleep(0.1)
    assert world.keys == []
    world.flash = True
    s = wait_state(rt, rid)
    assert s["state"] == "done" and s["reason"] == "reacted" and world.keys == [("space",)]
    reacted = [e for e in s["new_events"] if e["event"] == "reacted"][0]
    assert reacted["ms_from_frame"] < 50 and reacted["fraction"] > 0.9
    assert world.released >= 1  # input released when it ended


def test_react_on_change_with_cooldown(rt, world):
    rid = rt.start("react", {"region": [0, 0, 40, 40], "when": {"change": 20}, "do": {"pad": ["a"]},
                             "cooldown_ms": 200}, max_s=1.2)["id"]
    time.sleep(0.15)
    world.flash = True
    time.sleep(0.4)
    world.flash = False
    s = wait_state(rt, rid)
    assert s["state"] == "done" and "time limit" in s["reason"] and world.pads == [("a",), ("a",)]


def test_track_moves_the_cursor_onto_a_moving_target(rt, world):
    rid = rt.start("track", {"color": [255, 0, 0], "tol": 40, "aim": "cursor", "output": "mouse", "gain": 0.7},
                   max_s=2.5)["id"]

    def move():
        t0 = time.time()
        while time.time() - t0 < 2.4:
            a = (time.time() - t0) * 2.0
            with world.lock:
                world.target = [200 + 120 * np.cos(a), 150 + 90 * np.sin(a)]
            time.sleep(0.01)

    mover = threading.Thread(target=move)
    mover.start()
    mover.join()
    s = wait_state(rt, rid)
    assert s["state"] == "done" and s["hz"] > (10 if SLOPPY_CLOCK else 30)
    ex, ey = world.cursor[0] - world.target[0], world.cursor[1] - world.target[1]
    assert (ex * ex + ey * ey) ** 0.5 < 30, (world.cursor, world.target)
    assert s["error_px_p50"] < 30


def test_track_with_a_stick_points_toward_the_target(rt, world):
    world.target = [350.0, 60.0]  # right of and above a fixed crosshair at the screen center
    rid = rt.start("track", {"color": [255, 0, 0], "aim": [200, 150], "output": "right_stick"}, max_s=0.3)["id"]
    wait_state(rt, rid)
    side, x, y = world.sticks[0]
    assert side == "right_stick" and x > 0 and y > 0  # right, and up is positive


def test_press_until_text_appears(rt, world):
    rid = rt.start("press_until", {"do": {"pad": ["dpad_down"]}, "every_ms": 50, "until": {"text": "Play Online"},
                                   "max_presses": 50}, max_s=5)["id"]

    def later():
        time.sleep(0.3)
        world.text = [{"text": "Play  online", "box": [0, 0, 1, 1]}]

    threading.Thread(target=later).start()
    s = wait_state(rt, rid)
    assert s["state"] == "done" and 3 <= len(world.pads) <= 12 and s["presses"] == len(world.pads)


def test_press_until_gives_up(rt, world):
    rid = rt.start("press_until", {"do": {"key": ["down"]}, "every_ms": 20, "until": {"text": "never"},
                                   "max_presses": 5}, max_s=5)["id"]
    s = wait_state(rt, rid)
    assert s["state"] == "stopped" and "gave up after 5" in s["reason"] and len(world.keys) == 5


def test_watch_reports_each_appearance(rt, world):
    rid = rt.start("watch", {"region": [0, 0, 40, 40], "when": {"color": [0, 255, 0]}, "every_ms": 20}, max_s=1.0)["id"]
    for on in (True, False, True):
        time.sleep(0.15)
        world.flash = on
    s = wait_state(rt, rid)
    assert [e["event"] for e in s["new_events"]].count("seen") == 2 and world.keys == []


def test_stop_takeover_limits_and_errors(world):
    who = {"reason": None}
    rt = behave.Runtime(world.frame, Outputs(world), takeover=lambda: who["reason"])
    rid = rt.start("watch", {"region": [0, 0, 40, 40], "when": {"change": 5}}, max_s=30)["id"]
    time.sleep(0.1)
    who["reason"] = "controller slot 0"
    s = wait_state(rt, rid)
    assert s["state"] == "stopped" and "you took over" in s["reason"] and world.released >= 1
    who["reason"] = None
    rid = rt.start("watch", {"when": {"change": 5}}, max_s=30)["id"]
    time.sleep(0.05)
    assert rt.stop(rid) == [rid] and rt.status(rid)["state"] == "stopped"
    ids = [rt.start("watch", {"when": {"change": 5}}, max_s=5)["id"] for _ in range(behave.MAX_RUNS)]
    with pytest.raises(RuntimeError, match="at most"):
        rt.start("watch", {"when": {"change": 5}})
    rt.stop()
    assert all(rt.status(i)["state"] == "stopped" for i in ids)
    bad = rt.start("react", {"when": {}, "do": {"key": ["x"]}}, max_s=2)["id"]
    s = wait_state(rt, bad)
    assert s["state"] == "failed" and "color" in s["reason"]
    with pytest.raises(ValueError):
        rt.start("dance", {})
    assert behave.run_tool(rt, "kinds")["kinds"].keys() == behave.KINDS.keys()


@pytest.mark.skipif(SLOPPY_CLOCK, reason="shared macOS CI machines oversleep")
def test_sleep_until_is_precise():
    lateness = []
    for _ in range(20):
        target = time.perf_counter() + 0.005
        behave.sleep_until(target)
        lateness.append((time.perf_counter() - target) * 1000)
    assert sorted(lateness)[int(len(lateness) * 0.9)] < 2.0, lateness
