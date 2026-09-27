"""Camera control against a simulated plant: a camera over a wrap-around panorama, driven by a stick with a deadzone,
an expo curve, an acceleration lag, input latency and pitch limits, with a HUD drawn over every frame. Nothing here
is a particular game: the same code has to identify and steer whatever the plant turns out to be."""

import math
import os
import sys
import threading
import time
from collections import deque

import pytest

np = pytest.importorskip("numpy")

from relaymcp.device import control  # noqa: E402

SLOPPY = sys.platform == "darwin" and bool(os.environ.get("CI"))  # shared macOS runners oversleep


def _texture(h, w, block, seed):
    rng = np.random.default_rng(seed)
    cells = rng.integers(20, 236, size=(h // block + 1, w // block + 1, 3), dtype=np.uint8)
    return np.kron(cells, np.ones((block, block, 1), np.uint8))[:h, :w]


class CameraSim:
    """A pinhole camera (480x270, `hfov` degrees across) inside a textured sphere, yawing about the vertical and
    pitching without roll, like a first-person game. The stick sets turn rates through a deadzone and expo curve,
    reached through a first-order lag (`accel_s` ~ time to full speed), `latency_s` before an input has any effect;
    pitch stops at +-`limit`; a HUD is drawn over every frame; `fps` = new frames only that often."""

    W, H = 480, 270
    TW, TH = 3072, 1536  # the world's texture (equirectangular)

    def __init__(self, hfov=90.0, max_rate=240.0, deadzone=0.3, expo=2.0, accel_s=0.12, latency_s=0.04,
                 limit=80.0, invert_y=False, hud=True, seed=11, fps=None, pitch=0.0, slow_ms=0.0):
        self.fps, self._vsync, self._shown = fps, None, None
        self.slow_ms = slow_ms  # extra time per new frame, like a slow machine or capture
        self.max_rate, self.deadzone, self.expo = max_rate, deadzone, expo
        self.accel_s, self.latency_s, self.limit, self.invert = accel_s, latency_s, limit, invert_y
        self.focal = (self.W / 2) / math.tan(math.radians(hfov / 2))
        self.ppd = self.focal * math.pi / 180  # px per degree at the middle of the picture
        self.tex = _texture(self.TH, self.TW, 8, seed)
        xs = np.arange(self.W) - self.W / 2 + 0.5
        ys = np.arange(self.H) - self.H / 2 + 0.5
        gx, gy = np.meshgrid(xs, ys)
        rays = np.stack([gx, gy, np.full_like(gx, self.focal)], -1)
        self.rays = (rays / np.linalg.norm(rays, axis=-1, keepdims=True)).reshape(-1, 3)
        self.hud = _texture(40, 90, 5, seed + 1) if hud else None
        self.yaw, self._pitch, self.vx, self.vy = 0.0, float(pitch), 0.0, 0.0
        self._turned = 0.0  # total yaw, unwrapped (degrees, right +)
        self.cmds = deque([(0.0, 0.0, 0.0)])
        self.t = time.perf_counter()
        self.lock = threading.Lock()

    # The simulated clock only runs when something looks at it: reading the state has to advance it too (a turn
    # still coasting when a test reads its angle would otherwise be missed).
    @property
    def turned(self):
        with self.lock:
            self._advance()
            return self._turned

    @property
    def pitch(self):
        with self.lock:
            self._advance()
            return self._pitch

    @pitch.setter
    def pitch(self, value):
        with self.lock:
            self._advance()
            self._pitch = float(value)

    def paint(self, yaw_deg, pitch_deg, rgb, size_deg=8.0):
        """Something distinctive in the world at (yaw, pitch)."""
        u0 = int(((yaw_deg / 360 + 0.5) % 1) * self.TW)
        v0 = int((0.5 - pitch_deg / 180) * self.TH)
        du, dv = int(size_deg / 360 * self.TW / 2), int(size_deg / 180 * self.TH / 2)
        self.tex[v0 - dv:v0 + dv, u0 - du:u0 + du] = rgb[::-1]  # BGR

    def rate_for(self, d):
        a = abs(d)
        if a <= self.deadzone:
            return 0.0
        return math.copysign(self.max_rate * ((a - self.deadzone) / (1 - self.deadzone)) ** self.expo, d)

    def _command_at(self, t):
        cx = cy = 0.0
        for ct, x, y in self.cmds:
            if ct > t - self.latency_s:
                break
            cx, cy = x, y
        return cx, cy

    def _advance(self):
        now = time.perf_counter()
        tau = max(1e-3, self.accel_s / 3)
        while self.t < now:
            dt = min(0.002, now - self.t)
            self.t += dt
            cx, cy = self._command_at(self.t)
            tx, ty = self.rate_for(cx), self.rate_for(cy) * (-1 if self.invert else 1)
            a = min(1.0, dt / tau)
            self.vx += (tx - self.vx) * a
            self.vy += (ty - self.vy) * a
            self.yaw = (self.yaw + self.vx * dt) % 360
            self._turned += self.vx * dt
            p = self._pitch + self.vy * dt
            if abs(p) > self.limit:
                p, self.vy = math.copysign(self.limit, p), 0.0
            self._pitch = p
        while len(self.cmds) > 2 and self.cmds[1][0] <= now - self.latency_s - 0.5:
            self.cmds.popleft()

    def stick(self, x, y):
        with self.lock:
            self._advance()
            self.cmds.append((time.perf_counter(), float(x), float(y)))

    def render(self, yaw_deg, pitch_deg):
        t, psi = math.radians(pitch_deg), math.radians(yaw_deg)
        r_pitch = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])
        r_yaw = np.array([[math.cos(psi), 0, math.sin(psi)], [0, 1, 0], [-math.sin(psi), 0, math.cos(psi)]])
        w = self.rays @ (r_yaw @ r_pitch).T
        lon = np.arctan2(w[:, 0], w[:, 2])
        lat = np.arcsin(np.clip(-w[:, 1], -1, 1))
        u = ((lon / (2 * math.pi) + 0.5) * self.TW).astype(np.int64) % self.TW
        v = np.clip(((0.5 - lat / math.pi) * self.TH).astype(np.int64), 0, self.TH - 1)
        out = np.empty((self.H, self.W, 4), np.uint8)
        out[..., :3] = self.tex[v, u].reshape(self.H, self.W, 3)
        out[..., 3] = 255
        if self.hud is not None:
            out[222:262, 380:470, :3] = self.hud                       # a HUD panel
            out[self.H // 2 - 1:self.H // 2 + 2, self.W // 2 - 8:self.W // 2 + 8, :3] = 255  # crosshair
            out[self.H // 2 - 8:self.H // 2 + 8, self.W // 2 - 1:self.W // 2 + 2, :3] = 255
        return out

    def frame(self):
        with self.lock:
            self._advance()
            if self.fps:
                n = int(time.perf_counter() * self.fps)
                if n == self._vsync:
                    return self._shown  # the same image object, as the capture hands back between frames
                self._vsync = n
            yaw, pitch = self.yaw, self._pitch
        if self.slow_ms:
            time.sleep(self.slow_ms / 1000)
        self._shown = self.render(yaw, pitch)
        return self._shown


def io_for(sim):
    return control.PlantIO(frame=sim.frame, stick=sim.stick, sleep=time.sleep)


def true_profile(sim):
    pts = (0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.85, 1.0)
    return {"px_per_deg": sim.ppd, "focal_px": sim.focal, "pitch_bottom_deg": -sim.limit,
            "look": {"curve": [[d, sim.rate_for(d)] for d in pts], "latency_s": sim.latency_s,
                     "accel_s": sim.accel_s, "y_gain": -1.0 if sim.invert else 1.0}}


def test_response_curve_and_its_inverse():
    c = control.ResponseCurve([[0.2, 0], [0.3, 0], [0.4, 10], [0.6, 60], [1.0, 200]])
    assert c.deadzone == 0.3 and c.min_rate == 10 and c.max_rate == 200
    assert abs(c.deflection(60) - 0.6) < 1e-9 and c.deflection(-35) == pytest.approx(-0.5)
    assert c.deflection(1) == pytest.approx(0.4)  # below the slowest turn the game does: ask for that one
    assert c.deflection(0) == 0.0 and c.deflection(999) == 1.0


def test_odometry_follows_a_turn_and_the_overlay_is_learned():
    sim = CameraSim(deadzone=0.0, expo=1.0)
    frames = []
    sim.stick(0.5, 0.0)
    for _ in range(25):
        frames.append(sim.frame())
        time.sleep(0.02)
    sim.stick(0.0, 0.0)
    time.sleep(0.2)
    overlay = control.Overlay.learn(frames, 1)
    hud = overlay.mask[222:262, 380:470]
    assert hud.mean() > 0.9 and overlay.fraction() < 0.08, (hud.mean(), overlay.fraction())

    odo = control.Odometry(sim.frame(), overlay)
    start = sim.turned
    sim.stick(0.7, -0.4)  # right and down
    for _ in range(60):
        odo.update(sim.frame())
        time.sleep(0.008)
    sim.stick(0.0, 0.0)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.2:  # odometry follows the coast too: it's updated on every frame
        odo.update(sim.frame())
        time.sleep(0.008)
    true_yaw, got_yaw = sim.turned - start, -odo.x / sim.ppd
    assert true_yaw > 40 and abs(got_yaw - true_yaw) < 0.03 * true_yaw + 0.5, (got_yaw, true_yaw)
    assert abs(odo.y / sim.ppd - sim.pitch) < 0.03 * abs(sim.pitch) + 0.5, (odo.y / sim.ppd, sim.pitch)


def test_turns_land_in_degrees_through_deadzone_curve_lag_and_latency():
    sim = CameraSim()
    cam = control.Camera(io_for(sim), true_profile(sim))
    start = sim.turned
    r = cam.turn(yaw=90)
    assert abs((sim.turned - start) - 90) < 2.0, (r, sim.turned - start)
    start = sim.turned
    r = cam.turn(yaw=-45, pitch=20)
    assert abs((sim.turned - start) + 45) < 2.0 and abs(sim.pitch - 20) < 2.0, (r, sim.turned - start, sim.pitch)


def test_pitch_limits_and_level():
    sim = CameraSim(invert_y=True)
    cam = control.Camera(io_for(sim), true_profile(sim))
    r = cam.turn(pitch=130)
    assert r.get("pitch_limit") and sim.pitch == pytest.approx(sim.limit, abs=0.5), (r, sim.pitch)
    sim.pitch = 37.0
    cam.level()
    assert abs(sim.pitch) < 2.0, sim.pitch


def test_look_at_puts_a_point_under_the_crosshair():
    sim = CameraSim()
    cam = control.Camera(io_for(sim), true_profile(sim))
    y0, p0 = sim.turned, sim.pitch
    cam.look_at(400, 80)  # 160 px right and 55 px up of the middle, through a 90-degree pinhole camera
    want_yaw = math.degrees(math.atan2(160, sim.focal))
    want_pitch = math.degrees(math.atan2(55, math.hypot(160, sim.focal)))
    assert abs((sim.turned - y0) - want_yaw) < 1.5 and abs((sim.pitch - p0) - want_pitch) < 1.5, \
        (sim.turned - y0, want_yaw, sim.pitch - p0, want_pitch)
    with pytest.raises(ValueError, match="overlay"):
        prof = true_profile(sim)
        frames = []
        sim.stick(0.6, 0.0)
        for _ in range(20):
            frames.append(sim.frame())
            time.sleep(0.02)
        sim.stick(0.0, 0.0)
        prof["overlay"] = control.Overlay.learn(frames, 1).to_json()
        control.Camera(io_for(sim), prof).look_at(425, 242)  # the HUD panel


def test_scan_finds_the_best_view_and_turns_back_to_it():
    sim = CameraSim()
    sim.paint(200, 0, (230, 20, 20), size_deg=9)  # something red, 200 degrees round
    cam = control.Camera(io_for(sim), true_profile(sim))
    start = sim.turned

    def red(frame):
        c = frame[100:170, 190:290]
        return float(((c[..., 2] > 200) & (c[..., 1] < 60) & (c[..., 0] < 60)).mean())

    r = cam.scan(red)
    assert abs(r["heading"] - 200) < 8 and r["score"] > 0.1, r
    assert abs(((sim.turned - start) % 360) - 200) < 8, sim.turned - start


def test_calibration_identifies_an_unknown_plant():
    sim = CameraSim(max_rate=240.0, deadzone=0.3, expo=2.0, accel_s=0.12, latency_s=0.05)
    events = []
    io = control.PlantIO(frame=sim.frame, stick=sim.stick, sleep=time.sleep,
                         log=lambda msg, **d: events.append((msg, d)))
    prof = control.calibrate(io, points=(0.2, 0.3, 0.4, 0.5, 0.7, 1.0), hold_s=0.35)
    look = prof["look"]
    assert prof["px_per_deg"] == pytest.approx(sim.ppd, rel=0.03), (prof["px_per_deg"], events)
    assert prof["focal_px"] == pytest.approx(sim.focal, rel=0.03), prof["focal_px"]
    assert 0.2 <= look["deadzone"] <= 0.4 and look["max_deg_s"] == pytest.approx(240, rel=0.15), look
    assert 0.02 <= look["latency_s"] <= (0.2 if SLOPPY else 0.12), look
    assert look["y_gain"] == pytest.approx(1.0, abs=0.15), look
    ov = control.Overlay.from_json(prof["overlay"])
    assert ov.mask[222:262, 380:470].mean() > 0.9
    assert abs(sim.pitch) < 2.0, sim.pitch  # left looking level


def test_profiles_are_stored_per_app(tmp_path):
    store = control.ProfileStore(tmp_path)
    store.save("Minecraft.Windows.exe", {"px_per_deg": 16.3})
    assert store.load("minecraft.windows")["px_per_deg"] == 16.3 and store.load("Other.exe") is None


class SimOutputs:
    """Behavior outputs onto the simulated plant (the right stick is the camera)."""

    def __init__(self, sim):
        self.sim = sim

    def stick(self, side, x, y):
        if side == "right_stick":
            self.sim.stick(x, y)

    def hold(self, state):
        rs = state.get("right_stick") or [0, 0]
        self.sim.stick(rs[0], rs[1])

    def seq(self, steps):
        pass

    def release(self, used=()):
        self.sim.stick(0.0, 0.0)


def _runtime(sim, store):
    from relaymcp.device import behave
    return behave, behave.Runtime(lambda region=None: (sim.frame(), time.perf_counter()), SimOutputs(sim),
                                  profiles=store, app=lambda: "Sim.exe")


def test_programs_turn_level_and_hold_turn_rates_in_degrees(tmp_path):
    sim = CameraSim()
    store = control.ProfileStore(tmp_path)
    store.save("Sim.exe", true_profile(sim))
    behave, rt = _runtime(sim, store)
    start = sim.turned
    st = behave.run_tool(rt, "start", "program", {"code": "r = turn(yaw=-60)\nresult = [r, camera['px_per_deg']]",
                                                   "wait": True}, max_s=10)
    assert st["state"] == "done" and st["result"][1] == pytest.approx(sim.ppd), st
    assert abs((sim.turned - start) + 60) < 2.5, sim.turned - start
    sim.pitch = -25.0
    st = behave.run_tool(rt, "start", "program", {"code": "level()", "wait": True}, max_s=10)
    assert st["state"] == "done" and abs(sim.pitch) < 2.5, (st, sim.pitch)
    start = sim.turned
    st = behave.run_tool(rt, "start", "program", {"code": "look_rate(60)\nwait(1000)\nlook_rate(0)", "wait": True},
                         max_s=10)
    assert st["state"] == "done" and 40 < sim.turned - start < 85, sim.turned - start  # ~60 deg/s for ~1 s


def test_calibrate_kind_saves_the_profile_for_the_app_in_front(tmp_path):
    sim = CameraSim()
    store = control.ProfileStore(tmp_path)
    behave, rt = _runtime(sim, store)
    st = behave.run_tool(rt, "start", "calibrate", {"points": [0.3, 0.5, 1.0], "hold_s": 0.3, "full_turn": False,
                                                     "pitch": False, "wait": True}, max_s=30)
    assert st["state"] == "done" and "Sim.exe" in st["reason"], st
    saved = store.load("Sim.exe")
    # without the full turn, degrees come from the measured focal length (a pinhole camera's middle moves f px/rad)
    assert saved and saved["px_per_deg_from"] == "focal length", saved
    assert saved["px_per_deg"] == pytest.approx(sim.ppd, rel=0.08), saved["px_per_deg"]
    start = sim.turned
    st = behave.run_tool(rt, "start", "program", {"code": "turn(yaw=30)", "wait": True}, max_s=8)
    assert st["state"] == "done" and abs((sim.turned - start) - 30) < 3.0, (st, sim.turned - start)


def test_a_camera_too_fast_to_follow_is_calibrated_and_driven_at_speeds_it_can_follow():
    """60 frames a second, and a stick whose full deflection turns 67 degrees a frame (most of the picture):
    calibration has to notice it lost track, measure at a lower deflection, and control has to stay below that."""
    sim = CameraSim(max_rate=4000.0, fps=60)
    events = []
    io = control.PlantIO(frame=sim.frame, stick=sim.stick, sleep=time.sleep,
                         log=lambda msg, **d: events.append((msg, d)))
    prof = control.calibrate(io, points=(0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0), hold_s=0.35, pitch=False)
    assert any(m == "too fast to follow" for m, _ in events), events
    assert prof["px_per_deg"] == pytest.approx(sim.ppd, rel=0.03), (prof["px_per_deg"], events)
    assert prof["look"]["max_deflection"] < 1.0, prof["look"]
    start = sim.turned
    control.Camera(io, prof).turn(yaw=120)
    assert abs((sim.turned - start) - 120) < 2.5, sim.turned - start


def test_duplicate_frames_cost_nothing_and_a_stopped_view_reads_as_stopped():
    sim = CameraSim(fps=30)
    odo = control.Odometry(sim.frame())
    sim.stick(0.8, 0.0)
    time.sleep(0.3)
    for _ in range(40):
        odo.update(sim.frame())
        time.sleep(0.004)
    sim.stick(0.0, 0.0)
    assert odo.updates < 30  # ~30 new frames in that time, not 40 measurements
    time.sleep(0.4)
    for _ in range(30):
        odo.update(sim.frame())
        time.sleep(0.004)
    assert abs(odo.rate(0.08)[0]) < 5, odo.rate(0.08)


def test_the_gyro_reads_yaw_whatever_the_tilt_and_measures_the_tilt():
    """Yawing a tilted camera rolls the picture: yaw must still come out right, and the roll gives the tilt."""
    for tilt in (-35.0, 25.0):
        sim = CameraSim(pitch=tilt, fps=60)
        odo = control.Odometry(sim.frame(), focal_px=sim.focal)
        start = sim.turned
        sim.stick(0.8, 0.0)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 1.0:
            if time.perf_counter() - t0 > 0.6:
                sim.stick(0.0, 0.0)
            odo.update(sim.frame())
            time.sleep(0.003)
        true = sim.turned - start
        assert true > 40 and abs(math.degrees(odo.yaw) - true) < 0.02 * true + 0.5, (tilt, math.degrees(odo.yaw), true)
        assert abs(math.degrees(odo.theta) - tilt) < 2.0, (tilt, math.degrees(odo.theta))


def test_calibration_from_a_tilted_view_and_level_without_a_pitch_limit():
    sim = CameraSim(pitch=28.0, hfov=75.0, fps=60)
    io = control.PlantIO(frame=sim.frame, stick=sim.stick, sleep=time.sleep)
    prof = control.calibrate(io, points=(0.3, 0.4, 0.5, 0.7, 1.0), hold_s=0.35)
    assert prof["px_per_deg"] == pytest.approx(sim.ppd, rel=0.03), prof["px_per_deg"]
    assert abs(prof["tilt_at_start_deg"] - 28.0) < 3.0, prof["tilt_at_start_deg"]
    assert abs(sim.pitch) < 2.0, sim.pitch  # calibration ends level
    sim.pitch = -33.0
    out = control.Camera(io, prof).level()
    assert abs(sim.pitch) < 1.5 and abs(out["was_deg"] + 33.0) < 2.0, (sim.pitch, out)


def test_a_slow_capture_is_calibrated_at_speeds_it_can_follow():
    """A slow machine or capture (~20 new frames a second): the same plant allows less speed, and nothing may read
    wrong because of it (shared CI runners are like this)."""
    sim = CameraSim(slow_ms=40.0)
    events = []
    io = control.PlantIO(frame=sim.frame, stick=sim.stick, sleep=time.sleep,
                         log=lambda msg, **d: events.append((msg, d)))
    prof = control.calibrate(io, points=(0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0), hold_s=0.35, pitch=False)
    assert prof["px_per_deg"] == pytest.approx(sim.ppd, rel=0.04), (prof["px_per_deg"], events)
    assert prof["look"]["capture_fps"] < 30
    start = sim.turned
    control.Camera(io, prof).turn(yaw=-70)
    assert abs((sim.turned - start) + 70) < 3.0, sim.turned - start


def test_pitch_stopping_at_its_limit_mid_turn_doesnt_run_on():
    sim = CameraSim(deadzone=0.0, expo=1.0, slow_ms=25.0)
    odo = control.Odometry(sim.frame(), focal_px=sim.focal)
    sim.stick(0.6, -0.5)  # yaw right and pitch down, into the -80 limit
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.4:
        if time.perf_counter() - t0 > 1.1:
            sim.stick(0.0, 0.0)
        odo.update(sim.frame())
    assert abs(math.degrees(odo.pitch) - sim.pitch) < 3.0, (math.degrees(odo.pitch), sim.pitch)


def test_a_turn_that_runs_out_of_time_says_so():
    sim = CameraSim()
    cam = control.Camera(io_for(sim), true_profile(sim))
    out = cam.turn(yaw=150, timeout=0.3)
    assert out.get("timed_out") is True, out
    assert "timed_out" not in cam.turn(yaw=-20), "a turn that got there doesn't carry the flag"
