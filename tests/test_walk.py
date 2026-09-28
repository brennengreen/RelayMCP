"""walk_to on telemetry against a kinematic walker: the left stick moves the player relative to the camera's yaw,
velocity follows through a short lag, and telemetry is a tick late at 20 Hz (Minecraft's)."""

import math
import threading
import time

import pytest

np = pytest.importorskip("numpy")

from relaymcp.device import behave  # noqa: E402

WALK = 4.317


class Walker:
    def __init__(self, yaw=30.0, delay=0.05):
        self.x, self.z, self.vx, self.vz, self.yaw = 0.5, 0.5, 0.0, 0.0, yaw
        self.stick = (0.0, 0.0)
        self.delay, self.lock, self.hist, self.latest = delay, threading.Lock(), [], None
        self._stop = threading.Event()
        self.tick = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        t, next_tel = time.perf_counter(), time.perf_counter()
        while not self._stop.is_set():
            time.sleep(0.004)
            now = time.perf_counter()
            dt, t = now - t, now
            with self.lock:
                sx, sy = self.stick
                y = math.radians(self.yaw)
                fx, fz, rx, rz = -math.sin(y), math.cos(y), -math.cos(y), -math.sin(y)
                tx, tz = WALK * (sy * fx + sx * rx), WALK * (sy * fz + sx * rz)
                k = min(1.0, dt / 0.08)
                self.vx += (tx - self.vx) * k
                self.vz += (tz - self.vz) * k
                self.x += self.vx * dt
                self.z += self.vz * dt
                self.hist.append((now, self.x, self.z, self.vx, self.vz))
                self.hist = self.hist[-200:]
                if now >= next_tel:
                    old = next((h for h in reversed(self.hist) if h[0] <= now - self.delay), self.hist[0])
                    self.latest = {"tick": self.tick, "x": old[1], "y": -60.0, "z": old[2], "vx": old[3] / 20,
                                   "vz": old[4] / 20, "yaw": self.yaw, "pitch": 0.0, "recv": now}
                    self.tick += 1
                    next_tel += 0.05

    def telemetry(self):
        s = self.latest
        return None if s is None else {**s, "age_ms": (time.perf_counter() - s["recv"]) * 1000}

    def stop(self):
        self._stop.set()


class Legs:
    def __init__(self, w):
        self.w = w

    def hold(self, state):
        ls = state.get("left_stick") or [0, 0]
        with self.w.lock:
            self.w.stick = (float(ls[0]), float(ls[1]))

    def tap(self, buttons, ms=60):
        pass

    def release(self, used=()):
        with self.w.lock:
            self.w.stick = (0.0, 0.0)


def test_walk_to_arrives_in_a_straight_line_whatever_way_the_camera_faces():
    w = Walker(yaw=30.0)
    frame = np.zeros((60, 80, 4), np.uint8)
    rt = behave.Runtime(lambda region=None: (frame, time.perf_counter()), Legs(w), telemetry=w.telemetry)
    time.sleep(0.15)
    try:
        out = behave.run_tool(rt, "start", "program", {"code": "result = walk_to(6.5, -3.5)", "wait": True}, max_s=10)
        time.sleep(0.4)  # coming to rest
    finally:
        w.stop()
    r = out["result"]
    dist = math.hypot(6.5 - 0.5, -3.5 - 0.5)
    assert r["arrived"] and r["seconds"] < dist / WALK + 1.2, (r, out.get("cadence"))
    assert math.hypot(w.x - 6.5, w.z + 3.5) < 0.45, (w.x, w.z)  # stopped near the point, not past it
