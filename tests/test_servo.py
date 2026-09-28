"""The camera servo against the simulated plant with simulated game telemetry: fast, exact, and able to follow."""

import collections
import math
import os
import sys
import threading
import time

import pytest

np = pytest.importorskip("numpy")

from relaymcp.device import control, servo  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_control import CameraSim, io_for, true_profile  # noqa: E402

BUSY = bool(os.environ.get("CI")) or sys.platform == "darwin"  # shared runners and laptops oversleep now and then


class SimTelemetry:
    """What the game's script reports: every `period`, the angles `delay` ago, stamped with the wall time of the tick."""

    def __init__(self, sim, period=0.05, delay=0.05):
        self.sim, self.period, self.delay = sim, period, delay
        self.hist = collections.deque(maxlen=400)
        self.latest = None
        self._stop = threading.Event()
        self._wall = time.time() - time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        tick, next_sample = 0, time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            self.hist.append((now, self.sim.turned, self.sim.pitch))
            if now >= next_sample:
                old = next((h for h in reversed(self.hist) if h[0] <= now - self.delay), self.hist[0])
                self.latest = {"tick": tick, "t": (now + self._wall) * 1000, "yaw": old[1], "pitch": old[2],
                               "recv": now}
                tick += 1
                next_sample += self.period
            time.sleep(0.004)

    def get(self):
        return dict(self.latest) if self.latest else None

    def stop(self):
        self._stop.set()


def minecraft_like(**kw):
    args = dict(deadzone=0.4, max_rate=150.0, expo=1.0, y_gain=0.67, latency_s=0.06, accel_s=0.03, radial=True,
                limit=90.0, hud=False)
    args.update(kw)
    return CameraSim(**args)


def rig(sim, profile_edit=None):
    prof = true_profile(sim)
    prof["look"]["coast_s"] = sim.latency_s + sim.accel_s / 2  # the simulator's lag gives its ramp back
    if profile_edit:
        profile_edit(prof)
    io = io_for(sim)
    cam = control.Camera(io, prof)
    tel = SimTelemetry(sim)
    sv = servo.Servo(cam, look=io.frame, stick=io.stick, telemetry=tel.get, tol=0.5)
    return cam, tel, sv


def wait_on_target(sv, timeout):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if sv.on_target():
            return time.perf_counter() - t0
        time.sleep(0.005)
    return None


def test_the_stick_model_turns_rate_times_time_after_its_latency():
    sim = minecraft_like()
    cam = control.Camera(io_for(sim), true_profile(sim))
    m = servo.StickModel(cam)
    m.command(0.0, 0.0, 0.0)
    m.command(1.0, 1.0, 0.0)
    m.command(2.0, 0.0, 0.0)
    yr, _ = m.rates(1.0, 0.0)
    total, _ = m.rotation(0.5, 3.0)
    assert total == pytest.approx(yr * 1.0, rel=0.02)  # a symmetric lag gives the ramp back when stopping
    early, _ = m.rotation(1.0, 1.0 + cam.latency)
    assert early == pytest.approx(0.0, abs=1e-6)  # nothing happens before the latency
    x, y = cam.stick_for(40.0, 30.0)
    ry, rp = m.rates(x, y)
    assert ry == pytest.approx(40.0, rel=0.03) and rp == pytest.approx(30.0, rel=0.03)  # the inverse of stick_for


def test_the_servo_faces_a_direction_fast_and_exactly():
    sim = minecraft_like()
    cam, tel, sv = rig(sim)
    sv.start()
    try:
        time.sleep(0.2)  # first telemetry
        y0 = sim.turned
        sv.set((y0 + 90.0, 0.0))
        took = wait_on_target(sv, 3.0)
        fastest = sim.latency_s + 90.0 / sim.max_rate + sim.accel_s
        # (a busy runner ticks the loop at 30-60 Hz, and the simulator renders slower too)
        assert took is not None and took < fastest * (2.4 if BUSY else 1.25), (took, fastest, sv.summary())
        time.sleep(0.1)
        assert abs(sim.turned - (y0 + 90.0)) < 0.6 and abs(sim.pitch) < 0.6, (sim.turned - y0, sim.pitch)
        sv.set((y0 + 60.0, -35.0))  # both axes at once, on a radial stick
        took = wait_on_target(sv, 3.0)
        time.sleep(0.1)
        assert took is not None and abs(sim.turned - (y0 + 60.0)) < 0.6 and abs(sim.pitch + 35.0) < 0.6, (
            sim.turned - y0, sim.pitch, sv.summary())
    finally:
        sv.stop()
        tel.stop()


def test_the_servo_follows_a_moving_target():
    sim = minecraft_like()
    cam, tel, sv = rig(sim)
    sv.start()
    try:
        time.sleep(0.2)
        y0, t0 = sim.turned, time.perf_counter()
        sv.set(lambda: (y0 + 40.0 * (time.perf_counter() - t0), 10.0 * math.sin(time.perf_counter() - t0)))
        time.sleep(0.8)  # catching up
        errs = []
        end = time.perf_counter() + 2.0
        while time.perf_counter() < end:
            t = time.perf_counter()
            yaw, pitch = sim.turned, sim.pitch
            errs.append(math.hypot(yaw - (y0 + 40.0 * (t - t0)), pitch - 10.0 * math.sin(t - t0)))
            time.sleep(0.01)
        rms = math.sqrt(sum(e * e for e in errs) / len(errs))
        assert rms < (3.0 if BUSY else 2.0), (rms, sv.summary())
    finally:
        sv.stop()
        tel.stop()


def test_the_servo_lands_on_telemetry_when_its_model_is_off():
    sim = minecraft_like()

    def slower(prof):  # the camera is 12% faster than the profile says
        prof["look"]["curve"] = [[d, r / 1.12] for d, r in prof["look"]["curve"]]

    cam, tel, sv = rig(sim, slower)
    sv.start()
    try:
        time.sleep(0.2)
        y0 = sim.turned
        peak = [0.0]
        sv.set((y0 + 70.0, 0.0))
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 2.5 and not sv.on_target(6):
            peak[0] = max(peak[0], sim.turned - y0)
            time.sleep(0.005)
        time.sleep(0.1)
        assert abs(sim.turned - (y0 + 70.0)) < 0.7, (sim.turned - y0, sv.summary())
        assert peak[0] < 70.0 + 4.0, peak[0]  # no big overshoot
    finally:
        sv.stop()
        tel.stop()


def test_without_telemetry_it_doesnt_move():
    sim = minecraft_like()
    io = io_for(sim)
    cam = control.Camera(io, true_profile(sim))
    sv = servo.Servo(cam, look=io.frame, stick=io.stick, telemetry=None)
    sv.start()
    try:
        y0 = sim.turned
        sv.set((y0 + 45.0, 0.0))
        time.sleep(0.3)
        assert sv.facing() is None and abs(sim.turned - y0) < 0.5
    finally:
        sv.stop()


def test_facing_a_point_uses_minecraft_axes():
    eye = (0.0, 1.62, 0.0)
    assert servo.facing_point(eye, (0.0, 1.62, 5.0)) == pytest.approx((0.0, 0.0))     # south, +z
    assert servo.facing_point(eye, (-5.0, 1.62, 0.0)) == pytest.approx((90.0, 0.0))   # west, -x
    yaw, pitch = servo.facing_point(eye, (3.0, 0.0, 0.0))                              # east and below
    assert yaw == pytest.approx(-90.0) and pitch == pytest.approx(-math.degrees(math.atan2(1.62, 3.0)))


class HeldOutputs:
    """Program outputs onto the simulator: the right stick is the camera; the rest of each held state is recorded."""

    def __init__(self, sim):
        self.sim, self.left = sim, []

    def hold(self, state):
        rs = state.get("right_stick") or [0, 0]
        self.sim.stick(rs[0], rs[1])
        self.left.append(tuple(state.get("left_stick") or (0, 0)))

    def release(self, used=()):
        self.sim.stick(0.0, 0.0)


def test_programs_face_in_world_angles_and_keep_facing_while_walking(tmp_path):
    from relaymcp.device import behave
    sim = minecraft_like()
    prof = true_profile(sim)
    prof["look"]["coast_s"] = sim.latency_s + sim.accel_s / 2
    store = control.ProfileStore(tmp_path)
    store.save("Sim.exe", prof)
    tel = SimTelemetry(sim)
    out = HeldOutputs(sim)
    rt = behave.Runtime(lambda region=None: (sim.frame(), time.perf_counter()), out, profiles=store,
                        app=lambda: "Sim.exe", telemetry=tel.get)
    time.sleep(0.2)
    y0 = sim.turned
    code = (f"r = face({y0 + 75.0}, -20.0)\n"
            "pad(ls=(0, 1))\n"  # walk while the servo keeps the camera on a turning target
            f"t0 = elapsed()\n"
            f"keep_facing(lambda: ({y0 + 75.0} + 30 * (elapsed() - t0), -20.0))\n"
            "until(lambda: frame() is not None and elapsed() - t0 > 1.0, timeout=3, hz=60)\n"
            "f = facing()\n"
            "stop_facing()\n"
            "pad()\n"
            "result = {'first': r, 'facing': f}\n")
    try:
        st = behave.run_tool(rt, "start", "program", {"code": code, "wait": True}, max_s=10)
    finally:
        tel.stop()
    assert st["state"] == "done", st
    first = st["result"]["first"]
    assert first["on_target"] and first["seconds"] < (2.2 if BUSY else 1.1), first
    want = y0 + 75.0 + 30 * 1.0
    assert abs(sim.turned - want) < 4.0, (sim.turned - want, st.get("servo"))  # it was moving on when stopped
    assert (0, 1) in out.left and out.left[-1] == (0, 0)  # the program's left stick was kept, then let go
    # while telemetry flows its samples are the looks: the game's 20 Hz tick, so at the floor (plus tick jitter);
    # more margin needs the picture (vision), not a 20 Hz game state
    assert st["cadence"]["p95_ms"] <= (70 if BUSY else 55), st["cadence"]
