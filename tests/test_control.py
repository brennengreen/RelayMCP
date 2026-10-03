"""Camera control against a simulated plant: a camera over a wrap-around panorama, driven by a stick with a deadzone,
an expo curve, an acceleration lag, input latency and pitch limits, with a HUD drawn over every frame. Nothing here
is a particular game: the same code has to identify and steer whatever the plant turns out to be."""

import math
import threading
import time
from collections import deque

import pytest

np = pytest.importorskip("numpy")

from relaymcp.device import control  # noqa: E402

# The simulator runs on the wall clock and the camera is steered through visual odometry: a runner stall at the wrong
# moment costs a frame it can't follow (see conftest). The few pure-math tests here can't fail intermittently anyway.
pytestmark = pytest.mark.timing


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
                 limit=80.0, invert_y=False, hud=True, seed=11, fps=None, pitch=0.0, slow_ms=0.0, y_gain=1.0,
                 radial=False, input_hz=None):
        self.input_hz = input_hz  # the game reads the stick only this often (once per frame): shorter moves drop
        self.fps, self._vsync, self._shown = fps, None, None
        self.slow_ms = slow_ms  # extra time per new frame, like a slow machine or capture
        self.max_rate, self.deadzone, self.expo = max_rate, deadzone, expo
        self.accel_s, self.latency_s, self.limit, self.invert = accel_s, latency_s, limit, invert_y
        self.y_gain = y_gain  # pitch turns this much slower than yaw at the same deflection (Minecraft: ~0.67)
        self.radial = radial  # deadzone and curve on the stick's length (Minecraft), not on each axis
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

    def pose(self):
        """(time, yaw turned, pitch) at one instant, as a game's tick records them."""
        with self.lock:
            self._advance()
            return self.t, self._turned, self._pitch

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
        seen = t - self.latency_s
        if self.input_hz:
            seen = math.floor(seen * self.input_hz) / self.input_hz  # as of the last frame's input read
        for ct, x, y in self.cmds:
            if ct > seen:
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
            if self.radial:
                m = min(1.0, math.hypot(cx, cy))
                r = self.rate_for(m) / m if m > 0 else 0.0
                tx, ty = r * cx, r * cy * self.y_gain * (-1 if self.invert else 1)
            else:
                tx, ty = self.rate_for(cx), self.rate_for(cy) * self.y_gain * (-1 if self.invert else 1)
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
                     "accel_s": sim.accel_s, "y_gain": sim.y_gain * (-1.0 if sim.invert else 1.0),
                     "stick": "radial" if sim.radial else "axial"}}


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
    assert 0.02 <= look["latency_s"] <= 0.12, look
    assert look["y_gain"] == pytest.approx(1.0, abs=0.15), look
    ov = control.Overlay.from_json(prof["overlay"])
    assert ov.mask[222:262, 380:470].mean() > 0.9
    assert abs(sim.pitch) < 2.0, sim.pitch  # left looking level
    # open loop measured (the simulator's first-order lag turns exactly rate x time held)
    ol = look.get("open_loop") or {}
    assert set(ol) == {"yaw", "pitch"}, (ol, [e for e in events if e[0] == "open loop"])
    for axis in ol.values():  # (the scale also corrects the curve's interpolation at the deflection used)
        assert 0.85 <= axis["scale"] <= 1.15 and abs(axis["lag_s"]) < 0.04, ol
    y0, p0 = sim.turned, sim.pitch
    control.Camera(io, prof).turn_open(yaw=50.0, pitch=-20.0)
    assert abs((sim.turned - y0) - 50.0) < 1.5 and abs((sim.pitch - p0) + 20.0) < 1.5, (sim.turned - y0, sim.pitch - p0)


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
    out = control.Camera(io, prof).turn(yaw=120)
    assert abs((sim.turned - start) - 120) < 2.5, (sim.turned - start, out)


def test_duplicate_frames_cost_nothing_and_a_stopped_view_reads_as_stopped():
    sim = CameraSim(fps=30)
    shown = [sim.frame()]
    odo = control.Odometry(shown[0])
    sim.stick(0.8, 0.0)
    time.sleep(0.3)
    for _ in range(40):
        shown.append(sim.frame())
        odo.update(shown[-1])
        time.sleep(0.004)
    sim.stick(0.0, 0.0)
    # the capture hands back the same image until the next frame: each new one is measured once, the repeats not at
    # all (how many are new depends on how fast this machine ran the loop, not on the odometry)
    new = len({id(f) for f in shown}) - 1
    assert odo.updates == new < 40, (odo.updates, new)
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


@pytest.mark.parametrize("radial", [True, False])
def test_small_combined_turns_on_a_radial_or_per_axis_stick(radial):
    """Minecraft on the Ally: a radial stick (deadzone 0.4 on its length), pitch at 0.67x the yaw rate. Sending each
    axis through the curve alone pushed the stick's length far past the deadzone, and a (3.4, -2.7) turn ended at
    -9.7 pitch after 1.9 s; the finishing pulses also divided the deflection (not the rate) by the vertical gain."""
    sim = CameraSim(deadzone=0.4, max_rate=150.0, expo=1.0, y_gain=0.67, latency_s=0.06, accel_s=0.03, radial=radial)
    cam = control.Camera(io_for(sim), true_profile(sim))
    for yaw, pitch in ((3.4, -2.7), (-3.4, 2.7), (-2.0, 3.5), (0.0, -2.2), (12.0, 6.0), (-30.9, -7.0)):
        p0, y0 = sim.pitch, sim.turned
        out = cam.turn(yaw=yaw, pitch=pitch)
        assert abs((sim.pitch - p0) - pitch) < 1.2 and abs((sim.turned - y0) - yaw) < 1.2, (yaw, pitch, out,
                                                                                            sim.pitch - p0)


@pytest.mark.parametrize("radial", [True, False])
def test_calibration_tells_a_radial_stick_from_a_per_axis_one(radial):
    sim = CameraSim(deadzone=0.4, max_rate=150.0, expo=1.0, y_gain=0.67, radial=radial, limit=85.0)
    events = []
    io = control.PlantIO(frame=sim.frame, stick=sim.stick, sleep=time.sleep, log=lambda m, **d: events.append((m, d)))
    prof = control.calibrate(io, points=(0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0), hold_s=0.35, full_turn=False)
    assert prof["look"]["stick"] == ("radial" if radial else "axial"), [e for e in events if e[0] == "stick"]
    assert abs(prof["look"]["y_gain"] - 0.67) < 0.07, prof["look"]["y_gain"]
    assert abs(sim.pitch) < 3.0, sim.pitch  # ends about level (shared CI runners wobble a little more)


def test_stick_for_splits_one_length_on_a_radial_stick():
    look = {"curve": [[0.4, 0.0], [0.5, 25.0], [0.7, 75.0], [1.0, 150.0]], "y_gain": 0.5}
    prof = {"px_per_deg": 10.0, "focal_px": 573.0, "look": dict(look, stick="radial")}
    cam = control.Camera(control.PlantIO(frame=lambda: None, stick=lambda x, y: None, sleep=time.sleep), prof)
    x, y = cam.stick_for(60.0, 40.0)  # pitch 40 at gain 0.5 takes the yaw rate 80: length for hypot(60, 80) = 100
    assert math.hypot(x, y) == pytest.approx(cam.curve.deflection(100.0)) and x / y == pytest.approx(60 / 80)
    assert cam.stick_for(60.0, 0.0) == (cam.curve.deflection(60.0), 0.0)
    axial = control.Camera(cam.io, dict(prof, look=dict(look, stick="axial")))
    assert axial.stick_for(60.0, 40.0) == (axial.curve.deflection(60.0), axial.curve.deflection(80.0))


def _pixel_art(seed=4, size=900, texel=32):
    rng = np.random.default_rng(seed)
    t = rng.integers(40, 220, (size // texel + 1, size // texel + 1)).astype(np.uint8)
    return np.kron(t, np.ones((texel, texel), np.uint8))[:size, :size]


def _rotated(img, deg):
    """Content turned by deg in picture axes (x right, y down): clockwise on screen for deg > 0."""
    h, w = img.shape
    a = math.radians(deg)
    ys, xs = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    sx = np.cos(a) * (xs - cx) + np.sin(a) * (ys - cy) + cx
    sy = -np.sin(a) * (xs - cx) + np.cos(a) * (ys - cy) + cy
    return img[np.clip(sy.round().astype(int), 0, h - 1), np.clip(sx.round().astype(int), 0, w - 1)]


@pytest.mark.parametrize("deg", [0.0, 7.5, -20.0, 33.0, 44.0])
def test_grid_angle_reads_how_far_a_pixel_art_ground_is_turned(deg):
    img = _rotated(_pixel_art(), deg)[250:650, 250:650]
    got, strength = control.grid_angle(img)
    err = (got - deg + 45) % 90 - 45
    assert abs(err) < 1.0 and strength > 0.3, (got, strength)


def test_grid_angle_sees_no_grid_in_isotropic_noise():
    rng = np.random.default_rng(1)
    img = rng.normal(0, 1, (400, 400))
    k = np.exp(-np.arange(-6, 7) ** 2 / 8.0)
    img = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, img)
    img = np.apply_along_axis(lambda c: np.convolve(c, k, "same"), 0, img)  # blobs, no direction preferred
    _deg, strength = control.grid_angle((img - img.min()) / np.ptp(img) * 255)
    assert strength < 0.2, strength


def test_yawing_while_looking_straight_down_turns_the_right_way():
    """Minecraft's pitch limit is exactly -90: a yaw there is pure roll in the picture, and the yaw part the sign came
    from is noise. Told the pitch (after holding the stick into the limit), turns go the right way every time."""
    sim = CameraSim(limit=90.0, pitch=-90.0, deadzone=0.0, expo=1.0)
    cam = control.Camera(io_for(sim), true_profile(sim))
    cam.set_pitch(-90.0)
    for want in (30.0, -45.0, 12.0, -20.0):
        start = sim.turned
        out = cam.turn(yaw=want)
        assert abs((sim.turned - start) - want) < 2.0, (want, sim.turned - start, out)


def test_the_next_program_knows_where_the_camera_was_left_looking(tmp_path):
    """Turn-based play runs one program per call: the pitch set at a limit in one (Minecraft's -90) must carry over,
    or the next program's yaws seen straight down turn the wrong way."""
    sim = CameraSim(limit=90.0, pitch=-90.0, deadzone=0.0, expo=1.0)
    store = control.ProfileStore(tmp_path)
    store.save("Sim.exe", true_profile(sim))
    behave, rt = _runtime(sim, store)
    st = behave.run_tool(rt, "start", "program", {"code": "set_pitch(-90)\nturn(yaw=10)", "wait": True}, max_s=10)
    assert st["state"] == "done" and rt.pitch_known.get("Sim.exe") == pytest.approx(-90.0, abs=1.0), rt.pitch_known
    for want in (30.0, -45.0):
        start = sim.turned
        st = behave.run_tool(rt, "start", "program", {"code": f"turn(yaw={want})", "wait": True}, max_s=10)
        assert st["state"] == "done" and abs((sim.turned - start) - want) < 2.0, (want, sim.turned - start)


def test_a_stalled_picture_away_from_a_limit_is_not_a_limit():
    """Right after a jump the picture can stall for a moment: with the pitch known, only near straight up or down
    does a stall mean a pitch limit."""
    sim = CameraSim(limit=90.0, pitch=0.0)
    cam = control.Camera(io_for(sim), true_profile(sim))
    cam.set_pitch(0.0)
    real = sim.stick
    calls = [0]

    def sluggish(x, y):  # the game ignores the stick for the first ~0.35 s
        calls[0] += 1
        real(x, y) if time.perf_counter() - t0 > 0.35 else real(0.0, 0.0)
    cam.io.stick = sluggish
    t0 = time.perf_counter()
    out = cam.turn(pitch=40)
    assert "pitch_limit" not in out and abs(sim.pitch - 40) < 2.0, (out, sim.pitch)


def test_open_loop_turns_land_from_the_curve_alone():
    sim = CameraSim(deadzone=0.3, expo=2.0, y_gain=0.67, hud=False)
    prof = true_profile(sim)
    # the simulator's first-order lag gives the whole ramp back when coasting: no net lag
    prof["look"]["coast_s"] = sim.latency_s + sim.accel_s / 2
    cam = control.Camera(io_for(sim), prof)
    for yaw, pitch in ((40.0, 0.0), (-25.0, 0.0), (0.0, 30.0), (12.0, -18.0)):
        y0, p0 = sim.turned, sim.pitch
        out = cam.turn_open(yaw=yaw, pitch=pitch)
        assert out["open_loop"] and abs((sim.turned - y0) - yaw) < 1.5 and abs((sim.pitch - p0) - pitch) < 1.5, (
            yaw, pitch, sim.turned - y0, sim.pitch - p0, out)


def test_a_turn_whose_picture_goes_the_wrong_way_stops_instead_of_spinning():
    sim = CameraSim(hud=False)
    io = io_for(sim)
    real = io.stick
    io.stick = lambda x, y: real(-x, y)  # the camera turns the other way from what the picture model expects
    cam = control.Camera(io, true_profile(sim))
    t0, start = time.perf_counter(), sim.turned
    out = cam.turn(yaw=30)
    assert out.get("tracking_lost") and time.perf_counter() - t0 < 4.0, out
    assert abs(sim.turned - start) < 45, sim.turned - start


def test_pushing_away_from_a_pitch_limit_is_never_the_limit():
    """From Minecraft's -90 clamp, pitching up with a sluggish start (the game ignores the stick for a moment) must
    still pitch up, not stop there as if at a limit."""
    sim = CameraSim(limit=90.0, pitch=-90.0, hud=False)
    io = io_for(sim)
    real = io.stick
    t0 = [time.perf_counter()]
    io.stick = lambda x, y: real(x, y) if time.perf_counter() - t0[0] > 0.35 else real(0.0, 0.0)
    cam = control.Camera(io, true_profile(sim))
    cam.set_pitch(-90.0)
    t0[0] = time.perf_counter()
    out = cam.turn(pitch=50)
    assert "pitch_limit" not in out and abs(sim.pitch + 40) < 2.0, (out, sim.pitch)


def test_open_loop_turns_use_the_measured_gain_and_lag():
    """Minecraft on the Ally: the steady-rate gain for pitch was measured 4.5% low, and the lag the step response
    implied was 42 ms off; the numbers calibration's open-loop check measures correct both."""
    sim = CameraSim(deadzone=0.3, expo=2.0, y_gain=0.67, hud=False)
    prof = true_profile(sim)
    prof["look"]["y_gain"] = 0.64
    prof["look"]["coast_s"] = sim.latency_s + 0.1  # implies a lag of 0.1 - accel/2 = 0.04 s; the true one is 0
    prof["look"]["open_loop"] = {"yaw": {"scale": 1.0, "lag_s": 0.0}, "pitch": {"scale": round(0.67 / 0.64, 4),
                                                                              "lag_s": 0.0}}
    cam = control.Camera(io_for(sim), prof)
    for yaw, pitch in ((35.0, 0.0), (0.0, 30.0), (-8.0, -12.0)):
        y0, p0 = sim.turned, sim.pitch
        out = cam.turn_open(yaw=yaw, pitch=pitch)
        assert abs((sim.turned - y0) - yaw) < 1.0 and abs((sim.pitch - p0) - pitch) < 1.0, (
            yaw, pitch, sim.turned - y0, sim.pitch - p0, out)


def test_the_open_loop_is_re_measured_on_a_saved_profile():
    """Minecraft-like: a radial stick, pitch clamped at +-90; the saved profile's pitch gain is 4.5% low and its
    lag is off. Re-measuring from the bottom clamp fixes both without a whole calibration."""
    sim = CameraSim(deadzone=0.4, max_rate=150.0, expo=1.0, y_gain=0.67, latency_s=0.06, accel_s=0.03, radial=True,
                    limit=90.0, pitch=-30.0, hud=False)
    prof = true_profile(sim)
    prof["look"]["y_gain"] = 0.64
    prof["look"]["coast_s"] = sim.latency_s + 0.06
    io = io_for(sim)
    prof = control.measure_open_loop(io, prof)
    ol = prof["look"]["open_loop"]
    assert ol["pitch"]["scale"] == pytest.approx(0.67 / 0.64, abs=0.03) and abs(ol["pitch"]["lag_s"]) < 0.03, ol
    assert ol["yaw"]["scale"] == pytest.approx(1.0, abs=0.03) and abs(ol["yaw"]["lag_s"]) < 0.03, ol
    assert abs(sim.pitch) < 3.0, sim.pitch  # left about level
    cam = control.Camera(io, prof)
    for yaw, pitch in ((40.0, 0.0), (0.0, -25.0), (0.0, 25.0)):
        y0, p0 = sim.turned, sim.pitch
        cam.turn_open(yaw=yaw, pitch=pitch)
        assert abs((sim.turned - y0) - yaw) < 1.0 and abs((sim.pitch - p0) - pitch) < 1.0, (
            yaw, pitch, sim.turned - y0, sim.pitch - p0)
