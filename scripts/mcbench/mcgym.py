"""A local Minecraft-like gym for the bench's programs, from the repo's test simulators: the camera plant (CameraSim,
Minecraft-like stick), a walker on the left stick relative to the camera's yaw, game telemetry at 20 Hz a tick late
(pose, velocity in m/tick, mobs every other tick) and mobs that wander. Programs run in behave.Runtime as on the
handheld, so their bugs show up here first.

    python scripts/mcbench/mcgym.py TASK [--seed N]     TASK = step | pursuit | reaction | course
"""
import argparse
import json
import math
import random
import sys
import threading
import time

from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))  # the camera simulator the servo is tested against

from relaymcp.device import behave, control  # noqa: E402
from test_control import true_profile  # noqa: E402
from test_servo import minecraft_like  # noqa: E402

WALK = 4.317


class Gym:
    def __init__(self, seed=1, angle_lag=0.045, transit=0.054, y=-60.0):
        self.cam = minecraft_like()
        self.rng = random.Random(seed)
        self.x, self.z, self.y, self.vx, self.vz = 0.5, 0.5, y, 0.0, 0.0
        self.ls = (0.0, 0.0)
        self.mobs, self.next_id = [], 1
        self.lock = threading.Lock()
        # As measured on the handheld: a tick's angles are ~45 ms behind the picture, and the sample reaches the
        # follower ~54 ms after the tick (latency_ms); t is the tick's wall time.
        self.hist, self.tick, self.latest, self.angle_lag, self.transit = [], 0, None, angle_lag, transit
        self.pending = []
        self._wall = time.time() - time.perf_counter()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def summon(self, kind, x, z):
        with self.lock:
            self.mobs.append({"type": kind, "x": x, "z": z, "vx": 0.0, "vz": 0.0, "id": str(self.next_id)})
            self.next_id += 1

    def _run(self):
        t = next_tel = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(0.004)
            now = time.perf_counter()
            dt, t = now - t, now
            yaw, pitch = self.cam.turned, self.cam.pitch
            with self.lock:
                sx, sy = self.ls
                r = math.radians(yaw)
                fx, fz, rx, rz = -math.sin(r), math.cos(r), -math.cos(r), -math.sin(r)
                tx, tz = WALK * (sy * fx + sx * rx), WALK * (sy * fz + sx * rz)
                mag = math.hypot(tx, tz)
                if mag > WALK:
                    tx, tz = tx * WALK / mag, tz * WALK / mag
                k = min(1.0, dt / 0.08)
                self.vx += (tx - self.vx) * k
                self.vz += (tz - self.vz) * k
                self.x += self.vx * dt
                self.z += self.vz * dt
                for m in self.mobs:
                    if self.rng.random() < dt / 1.5:
                        a, sp = self.rng.uniform(0, 2 * math.pi), self.rng.choice([0.0, 0.0, 1.0])
                        m["vx"], m["vz"] = sp * math.cos(a), sp * math.sin(a)
                    m["x"] += m["vx"] * dt
                    m["z"] += m["vz"] * dt
                self.hist.append((now, self.x, self.z, self.vx, self.vz, yaw, pitch, [dict(m) for m in self.mobs]))
                self.hist = self.hist[-400:]
                if now >= next_tel:  # a game tick
                    a = next((h for h in reversed(self.hist) if h[0] <= now - self.angle_lag), self.hist[0])
                    s = {"tick": self.tick, "t": (now + self._wall) * 1000, "x": self.x, "y": self.y,
                         "ey": self.y + 1.62, "z": self.z, "vx": self.vx / 20, "vy": 0.0, "vz": self.vz / 20,
                         "yaw": a[5], "pitch": a[6], "ground": True, "fly": False, "sneak": False, "slot": 0,
                         "hp": 20, "look": None}
                    if self.tick % 2 == 0:
                        s["mobs"] = [[m["type"], m["x"], self.y, m["z"], self.y + 0.9, m["id"]] for m in self.mobs]
                    self.pending.append((now + self.transit, s))
                    self.tick += 1
                    next_tel += 0.05
                while self.pending and self.pending[0][0] <= now:  # it reaches the follower
                    _, s = self.pending.pop(0)
                    self.latest = {**s, "recv": now, "latency_ms": round((now + self._wall) * 1000 - s["t"], 1)}

    def telemetry(self):
        s = self.latest
        return None if s is None else {**s, "age_ms": round((time.perf_counter() - s["recv"]) * 1000, 1)}

    def stop(self):
        self._stop.set()


class Pad:
    def __init__(self, gym):
        self.g = gym

    def hold(self, state):
        rs = state.get("right_stick") or [0, 0]
        self.g.cam.stick(rs[0], rs[1])
        ls = state.get("left_stick") or [0, 0]
        with self.g.lock:
            self.g.ls = (float(ls[0]), float(ls[1]))

    def tap(self, buttons, ms=60):
        pass

    def release(self, used=()):
        self.g.cam.stick(0.0, 0.0)
        with self.g.lock:
            self.g.ls = (0.0, 0.0)


def runtime(gym, tmp):
    prof = true_profile(gym.cam)
    prof["look"]["coast_s"] = gym.cam.latency_s + gym.cam.accel_s / 2
    store = control.ProfileStore(tmp)
    store.save("Minecraft.Windows.exe", prof)
    return behave.Runtime(lambda region=None: (gym.cam.frame(), time.perf_counter()), Pad(gym), profiles=store,
                          app=lambda: "Minecraft.Windows.exe", telemetry=gym.telemetry)


ARGS = {
    "step": {"STEPS": [10, 30, 60, 90, 180, -90, -30], "PITCHES": [30.0, -30.0, 0.0]},
    "pursuit": {"RATE": 40.0, "PITCH": 0.0, "DUR": 6.0, "ACQUIRE": 1.5, "ANGLE_LAG": 0.045},
    "reaction": {"KIND": "pig", "WAIT_S": 8.0, "HOLD": 3.0},
    "course": {"TOL": 0.35, "STOP_MS": 1.0, "MODE": "path"},
}


def route(rng, x, z, n=10, box=None, max_turn=120.0):
    """n waypoints 3-7 m apart, each turn at most max_turn degrees (a route a person walks: no about-turns), within
    box metres of the start if given."""
    pts, heading = [], rng.uniform(-math.pi, math.pi)
    x0, z0 = x, z
    for _ in range(n):
        for _ in range(100):
            h = heading + math.radians(rng.uniform(-max_turn, max_turn))
            d = rng.uniform(3.0, 7.0)
            nx, nz = x + d * math.cos(h), z + d * math.sin(h)
            if box is None or (abs(nx - x0) < box and abs(nz - z0) < box):
                break
        x, z, heading = nx, nz, h
        pts.append([round(x, 2), round(z, 2)])
    return pts


def run(task, seed=1, args=None):
    import tempfile
    gym = Gym(seed)
    rt = runtime(gym, tempfile.mkdtemp())
    time.sleep(0.3)
    a = dict(ARGS[task], **(args or {}))
    rng = random.Random(seed)
    if task == "course":
        a.setdefault("WPS", route(rng, 0.5, 0.5))
    if task == "reaction":
        bearing = rng.uniform(-150, 150)
    code = (HERE / "tasks" / f"{task}.py").read_text()
    try:
        if task == "reaction":  # as the bench does: summon once the program has seen the mobs already there
            rid = behave.run_tool(rt, "start", "program", {"code": code, "args": a}, max_s=120)["id"]
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 10:
                st = behave.run_tool(rt, "status", run_id=rid)
                if any(e.get("msg") == "ready" for e in st.get("new_events") or st.get("events") or []):
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            s = gym.telemetry()
            th = math.radians(s["yaw"] + bearing)
            gym.summon(a["KIND"], s["x"] - math.sin(th) * 6.0, s["z"] + math.cos(th) * 6.0)
            out = rt.wait(rid)
            out["bearing_asked"] = round(bearing, 1)
        else:
            out = behave.run_tool(rt, "start", "program", {"code": code, "args": a, "wait": True}, max_s=120)
    finally:
        gym.stop()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=sorted(ARGS))
    ap.add_argument("--seed", type=int, default=1)
    o = ap.parse_args()
    res = run(o.task, o.seed)
    keep = {k: res.get(k) for k in ("state", "reason", "result", "cadence", "servo", "error", "bearing_asked")}
    print(json.dumps(keep, indent=1)[:5000])
