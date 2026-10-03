"""A camera servo on a fused attitude: continuous closed-loop look control at frame rate.

The attitude is the latest exact sample (game telemetry: absolute yaw and pitch, a tick late) plus how far the
camera turned since: from the visual gyro while it agrees with the stick model, from the calibrated stick model alone
when it doesn't (the gyro can lock onto a repeating texture and read nothing, or the wrong thing). The servo runs in
the background at up to 120 Hz: a program sets targets as often as it likes (a direction, a world point, a moving
mob) and keeps the left stick and buttons (walk while looking). Each tick it reads a frame (the gyro, and the 20 fps
floor), takes new telemetry, and asks for the turn rate that closes the error where the camera will be once the new
command takes effect (commands already sent are still arriving: latency).

Conventions (as telemetry.py): yaw grows turning right (Minecraft's own: 0 = south +z, 90 = west), pitch up +.
"""

from __future__ import annotations

import collections
import math
import threading
import time
from typing import Any, Callable

GYRO_TRUST_MIN_HEALTH = 0.85


def wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def facing_point(eye: tuple[float, float, float], point: tuple[float, float, float]) -> tuple[float, float]:
    """(yaw, pitch) from an eye position to a world point, Minecraft axes (x east, y up, z south; yaw 0 = +z,
    90 = -x)."""
    dx, dy, dz = point[0] - eye[0], point[1] - eye[1], point[2] - eye[2]
    return math.degrees(math.atan2(-dx, dz)), math.degrees(math.atan2(dy, math.hypot(dx, dz)))


class StickModel:
    """What the camera does for a history of right-stick commands: each takes effect `latency` later, and the turn
    rate follows through a first-order lag, quicker speeding up than coasting to a stop (the open-loop lag measured
    in calibration). Rates come from the response curve (radial or per-axis stick), with each axis' open-loop scale."""

    def __init__(self, cam, keep_s: float = 4.0):
        self.cam = cam
        self.latency = float(cam.latency)
        self.tau_up = max(0.004, float(cam.accel) / 3)
        lag_yaw = cam.open[0][1] if getattr(cam, "open", None) else 0.0
        self.tau_down = max(self.tau_up, self.tau_up + max(0.0, float(lag_yaw)))
        self.scale = (cam.open[0][0], cam.open[1][0]) if getattr(cam, "open", None) else (1.0, 1.0)
        self.keep_s = keep_s
        self.log: collections.deque = collections.deque()  # (t, yaw_rate, pitch_rate) commanded
        self.before = (0.0, 0.0)  # the rate in effect before the oldest command kept (the camera starts still)
        self._lock = threading.Lock()

    def rates(self, x: float, y: float) -> tuple[float, float]:
        """Steady (yaw, pitch) deg/s for a stick position."""
        c = self.cam
        if c.radial and x and y:
            m = math.hypot(x, y)
            total = abs(c.curve.rate(min(1.0, m)))
            yr, pr = total * x / m, total * y / m
        else:
            yr, pr = c.curve.rate(x), c.curve.rate(y)
        return yr * self.scale[0], pr * c.y_gain * self.scale[1]

    def command(self, t: float, x: float, y: float) -> None:
        yr, pr = self.rates(x, y)
        with self._lock:
            if self.log and abs(self.log[-1][1] - yr) < 1e-9 and abs(self.log[-1][2] - pr) < 1e-9:
                return
            self.log.append((t, yr, pr))
            while len(self.log) > 2 and self.log[1][0] < t - self.keep_s:
                _, by, bp = self.log.popleft()
                self.before = (by, bp)

    def rotation(self, t0: float, t1: float) -> tuple[float, float]:
        """Degrees turned (yaw, pitch) between t0 and t1."""
        if t1 <= t0:
            return 0.0, 0.0
        with self._lock:
            log, before = list(self.log), self.before
        if not log:
            return 0.0, 0.0
        out = [0.0, 0.0]
        rate = [before[0], before[1]]  # settled on what came before the oldest command kept
        for i, (tc, yr, pr) in enumerate(log):
            a = tc + self.latency
            b = (log[i + 1][0] + self.latency) if i + 1 < len(log) else max(t1, a)
            if b <= a:
                continue
            for k, c in enumerate((yr, pr)):
                r0 = rate[k]
                tau = self.tau_up if abs(c) >= abs(r0) else self.tau_down
                u, v = max(a, t0), min(b, t1)
                if v > u:
                    out[k] += c * (v - u) + (r0 - c) * tau * (math.exp(-(u - a) / tau) - math.exp(-(v - a) / tau))
                rate[k] = c + (r0 - c) * math.exp(-(b - a) / tau)
            if b >= t1:
                break
        # before the first command's effect: at the rate before it
        first = log[0][0] + self.latency
        if t0 < first:
            span = min(first, t1) - t0
            out[0] += before[0] * span
            out[1] += before[1] * span
        return out[0], out[1]

    def rate_at(self, t: float) -> tuple[float, float]:
        d = 0.01
        y, p = self.rotation(t - d, t)
        return y / d, p / d


class Attitude:
    """Where the camera looks: an absolute sample (telemetry) plus the turn since it, from the gyro while it agrees
    with the stick model, else from the model."""

    def __init__(self, model: StickModel, delay_s: float = 0.05, keep: int = 720):
        self.model = model
        self.delay = delay_s  # telemetry's angles are this much older than its timestamp (the game's tick)
        self.base: tuple[float, float, float] | None = None  # (t, yaw, pitch), perf clock
        self.gyro: collections.deque = collections.deque(maxlen=keep)  # (t, yaw_rel, pitch_rel, healthy)
        self.used = collections.Counter()

    def absolute(self, t: float, yaw: float, pitch: float) -> None:
        self.base = (t - self.delay, float(yaw), float(pitch))

    def gyro_sample(self, t: float, yaw_rel: float, pitch_rel: float, healthy: bool) -> None:
        self.gyro.append((t, yaw_rel, pitch_rel, healthy))

    def _gyro_at(self, t: float):
        g = self.gyro
        if len(g) < 2 or t < g[0][0] or t > g[-1][0] + 0.02:
            return None
        lo, hi = 0, len(g) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if g[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        (ta, ya, pa, ha), (tb, yb, pb, hb) = g[lo], g[hi]
        if tb <= ta or t >= tb:
            return (yb, pb, hb) if t >= tb else (ya, pa, ha)
        k = (t - ta) / (tb - ta)
        return ya + (yb - ya) * k, pa + (pb - pa) * k, ha and hb

    def delta(self, t0: float, t1: float) -> tuple[float, float, str]:
        m = self.model.rotation(t0, t1)
        g0, g1 = self._gyro_at(t0), self._gyro_at(t1)
        if g0 and g1 and g0[2] and g1[2]:
            gy, gp = g1[0] - g0[0], g1[1] - g0[1]
            if abs(gy - m[0]) <= max(1.5, 0.25 * abs(m[0])) and abs(gp - m[1]) <= max(1.5, 0.25 * abs(m[1])):
                return gy, gp, "gyro"
        return m[0], m[1], "model"

    def estimate(self, t: float) -> tuple[float, float, str] | None:
        if self.base is None:
            return None
        t0, y0, p0 = self.base
        dy, dp, how = self.delta(t0, t)
        self.used[how] += 1
        return y0 + dy, max(-90.0, min(90.0, p0 + dp)), how


class Servo:
    """Keep the camera on a target, in the background. target: (yaw, pitch), or a function returning one (called
    every tick: a moving mob, a point as the player walks), or None to let go of the stick."""

    def __init__(self, cam, look: Callable[[], Any], stick: Callable[[float, float], None],
                 telemetry: Callable[[], dict | None] | None, hz: float = 120.0, tol: float = 0.5,
                 on_thread: Callable[[int], None] | None = None, stop_evt: threading.Event | None = None,
                 delay_s: float = 0.05):
        self.cam, self.look, self.stick, self.telemetry = cam, look, stick, telemetry
        self.model = StickModel(cam)
        self.att = Attitude(self.model, delay_s)
        self.hz, self.tol = hz, tol
        self.on_thread, self.stop_evt = on_thread, stop_evt or threading.Event()
        self.target: Any = None
        self.error: tuple[float, float] | None = None
        self.settled = 0  # ticks in a row on target, still, and confirmed by telemetry
        self.settled_fast = 0  # ticks in a row the stick model says it will come to rest on target
        self.ticks = 0
        self.last_stick = (0.0, 0.0)
        self.failure: str | None = None
        self._last_tick = None
        self._wall_minus_perf = time.time() - time.perf_counter()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._odo = None
        self.period = 1.0 / hz  # the loop's measured tick period (a busy machine ticks slower than asked)
        self.use_gyro = True  # False: telemetry and the stick model only (the gyro can't follow a scene)
        self._tel_at: float | None = None  # when the latest new telemetry sample arrived (perf clock)
        self._parts = collections.defaultdict(list)  # stage -> ms per tick (look, gyro, telemetry, control)
        self._worst: tuple[float, dict] = (0.0, {})
        # The finish on a still target: games read the stick once per frame, so corrections shorter than a few
        # frames are partly dropped while the model counts them (it then dithers). Near the target it stops, waits
        # until telemetry shows where the camera really is, then sends one pulse of at least PULSE_MIN_S.
        self.near = 0.6  # degrees (after what is in flight): closer than this, finish with pulses
        self._pulse_end = 0.0
        self._pulse_stick = (0.0, 0.0)
        self._confirm_after = 0.0  # camera time the last stick change has fully played out by
        self._prev_target: tuple[float, float, float] | None = None  # (t, yaw, pitch) of a moving target
        self._target_rate = (0.0, 0.0)  # its smoothed rate (deg/s): fed forward, and where it will be

    # -- control ------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="camera-servo", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._send(0.0, 0.0)

    def set(self, target) -> None:
        self.target = target
        self.settled = self.settled_fast = 0
        self._prev_target, self._target_rate = None, (0.0, 0.0)
        self._pulse_end = 0.0

    def on_target(self, ticks: int = 3, confirmed: bool = False) -> bool:
        """On target: by default as soon as the stick model says the camera comes to rest within tol (what it
        sent is still playing out; telemetry checks it meanwhile and a miss gets a correction); confirmed = once
        telemetry shows it there."""
        return (self.settled if confirmed else self.settled_fast) >= ticks

    def facing(self, t: float | None = None):
        e = self.att.estimate(time.perf_counter() if t is None else t)
        return None if e is None else (round(wrap(e[0]), 2), round(e[1], 2))

    # -- the loop -----------------------------------------------------------------------------------------------------

    def _send(self, x: float, y: float) -> None:
        if (x, y) != self.last_stick:
            # played out: the camera stops turning a latency and a few lag constants after the stick changes
            self._confirm_after = time.perf_counter() + self.model.latency + 4 * self.model.tau_down
        self.last_stick = (x, y)
        self.stick(x, y)
        self.model.command(time.perf_counter(), x, y)

    PULSE_MIN_S = 0.025  # a few game frames
    PULSE_RATE = 30.0  # deg/s for finishing pulses
    TEL_GAP_S = 1.0  # telemetry seen: the stick model alone bridges a gap this long (a hitch) before the gyro starts

    def _pulse(self, ey: float, ep: float, now: float) -> tuple[float, float]:
        """A stick position and duration that turn (ey, ep) degrees: at PULSE_RATE, or the game's slowest turn, for
        at least PULSE_MIN_S."""
        c = self.cam
        g = abs(c.y_gain) * self.model.scale[1]
        mag = math.hypot(ey, ep / g if g else 0.0)  # in yaw-rate terms (what the stick's length sets)
        if mag <= 1e-6:
            return 0.0, 0.0
        rate = max(c.curve.min_rate, min(self.PULSE_RATE, mag / self.PULSE_MIN_S))
        dur = max(self.PULSE_MIN_S, mag / rate)
        rate = mag / dur
        if rate < c.curve.min_rate:  # smaller than the slowest turn for the shortest pulse: within reach anyway
            return 0.0, 0.0
        x, y = c.stick_for(ey / dur, ep / dur)
        self._pulse_end = now + dur
        self._pulse_stick = (x, y)
        return x, y

    FINAL_GAIN = 30.0  # 1/s for still targets: the last frames before rest move under half a degree each (games
    # read the stick once a frame; at 60 fps a faster finish lands a frame's turn off, ~1 degree)

    def _rate(self, err: float, tol: float, top: float, feed: float = 0.0, still: bool = False) -> float:
        """Turn rate for an error at the moment the command takes effect: the target's own rate, plus the error over
        the time the camera takes to come to rest (its coast lag and a tick; a still target: at FINAL_GAIN), capped
        at the fastest, and at least the slowest turn the game makes while outside half the tolerance."""
        if abs(err) <= tol * 0.5 and abs(feed) < 1e-6:
            return 0.0
        m = self.model
        # over one tick of the loop as it runs now: a gain for 120 Hz over-corrects on a loop that ticks at 40
        gain = 1.0 / (m.tau_down + max(1.0 / self.hz, self.period))
        r = feed + err * (min(gain, self.FINAL_GAIN) if still else gain)
        r = max(-top, min(top, r))
        if abs(err) <= tol * 0.5:
            return r
        slowest = self.cam.curve.min_rate
        if abs(r) < slowest:
            r = math.copysign(slowest, err)
        return r

    def _telemetry(self) -> None:
        if not self.telemetry:
            return
        s = self.telemetry()
        if not s or s.get("tick") == self._last_tick or "yaw" not in s or "pitch" not in s:
            return
        self._last_tick = s.get("tick")
        self._tel_at = time.perf_counter()
        t = s.get("t")
        at = (float(t) / 1000.0 - self._wall_minus_perf) if isinstance(t, (int, float)) else s.get("recv",
                                                                                                    time.perf_counter())
        self.att.absolute(at, float(s["yaw"]), float(s["pitch"]))

    def _gyro(self, parts: dict) -> None:
        t0 = time.perf_counter()
        frame = self.look()
        now = time.perf_counter()
        parts["look"] = (now - t0) * 1000
        if frame is None or not self.use_gyro:
            return
        try:
            if self._odo is None:
                self._odo = self.cam.odometry()
            self._odo.update(frame)
            ppd = self.cam.ppd
            self.att.gyro_sample(now, -self._odo.x / ppd, self._odo.y / ppd,
                                 self._odo.health() >= GYRO_TRUST_MIN_HEALTH)
        except Exception:
            self._odo = None  # e.g. a frame of another size (a menu): start the gyro again
        parts["gyro"] = (time.perf_counter() - now) * 1000

    def _target(self, now: float):
        t = self.target
        if not callable(t):
            self._prev_target, self._target_rate = None, (0.0, 0.0)
            return t
        try:
            t = t()
        except Exception as e:
            self.failure = f"target: {type(e).__name__}: {e}"
            return None
        if t is None:
            self._prev_target = None
            return None
        prev = self._prev_target
        if prev is not None and now - prev[0] > 1e-4:
            dy, dp = wrap(float(t[0]) - prev[1]) / (now - prev[0]), (float(t[1]) - prev[2]) / (now - prev[0])
            if abs(dy) > 720 or abs(dp) > 720:  # a jump (a new mob, a new point): not a speed
                self._target_rate = (0.0, 0.0)
            else:
                ry, rp = self._target_rate
                self._target_rate = (ry + 0.3 * (dy - ry), rp + 0.3 * (dp - rp))
        self._prev_target = (now, float(t[0]), float(t[1]))
        return t

    def _run(self) -> None:
        if self.on_thread:
            self.on_thread(threading.get_ident())
        period = 1.0 / self.hz
        next_t = last_tick = time.perf_counter()
        c = self.cam
        top_yaw = c.curve.max_rate * self.model.scale[0]
        top_pitch = c.curve.max_rate * abs(c.y_gain) * self.model.scale[1]
        try:
            while not self._stop.is_set() and not self.stop_evt.is_set():
                parts: dict = {}
                tick0 = time.perf_counter()
                self._telemetry()
                t1 = time.perf_counter()
                parts["telemetry"] = (t1 - tick0) * 1000
                # Telemetry flowing: it and the stick model are the estimate, so the picture isn't needed (a frame
                # and the gyro cost ~10-40 ms a tick on the handheld, and starting the gyro on a 1080p frame ~0.3 s,
                # which stalls the program's own loop too). Read it first, so even the first tick skips the gyro.
                # The model bridges a game hitch; without telemetry (never seen, or lost) the gyro carries it.
                if self._tel_at is None or t1 - self._tel_at > self.TEL_GAP_S:
                    self._gyro(parts)
                now = time.perf_counter()
                self.period += 0.2 * (min(0.25, now - last_tick) - self.period)
                last_tick = now
                target = self._target(now)
                est = self.att.estimate(now)
                if target is None or est is None:
                    if self.last_stick != (0.0, 0.0):
                        self._send(0.0, 0.0)
                    self.error, self.settled = None, 0
                else:
                    lat = self.model.latency
                    ahead = self.model.rotation(now, now + lat)  # already on its way
                    fy, fp = self._target_rate  # a moving target moves on while the command travels
                    ey = wrap(float(target[0]) - est[0])
                    ep = max(-89.5, min(89.5, float(target[1]))) - est[1]
                    self.error = (round(ey, 2), round(ep, 2))
                    ay, ap = wrap(ey + fy * lat - ahead[0]), ep + fp * lat - ahead[1]  # once in-flight lands
                    moving_target = math.hypot(fy, fp) > 3.0
                    confirmed = self.att.base is not None and self.att.base[0] >= self._confirm_after
                    if moving_target or max(abs(ay), abs(ap)) > self.near:
                        ry = self._rate(ay, self.tol, top_yaw, fy, still=not moving_target)
                        rp = self._rate(ap, self.tol, top_pitch, fp, still=not moving_target)
                        x, y = c.stick_for(ry, rp) if (ry or rp) else (0.0, 0.0)
                        self._pulse_end = 0.0
                    elif now < self._pulse_end:
                        x, y = self._pulse_stick  # a finishing pulse, played in full
                    elif not confirmed:
                        x, y = 0.0, 0.0  # let it play out, and telemetry show where it went
                    elif abs(ey) > self.tol * 0.6 or abs(ep) > self.tol * 0.6:
                        x, y = self._pulse(ey, ep, now)
                    else:
                        x, y = 0.0, 0.0
                    if (x, y) != self.last_stick:
                        self._send(x, y)
                    on = abs(ey) <= self.tol and abs(ep) <= self.tol
                    if moving_target:
                        moving = self.model.rate_at(now)
                        still = abs(moving[0] - fy) < 3.0 and abs(moving[1] - fp) < 3.0
                        self.settled = self.settled + 1 if on and still else 0
                        self.settled_fast = self.settled
                    else:
                        idle = self.last_stick == (0.0, 0.0) and now >= self._pulse_end
                        rest = self.model.rotation(now, now + lat + 4 * self.model.tau_down)  # all it still turns
                        lands = abs(wrap(ey - rest[0])) <= self.tol and abs(ep - rest[1]) <= self.tol
                        self.settled = self.settled + 1 if on and idle and confirmed else 0
                        self.settled_fast = self.settled_fast + 1 if idle and lands else 0
                done = time.perf_counter()
                parts["control"] = (done - now) * 1000
                total = (done - tick0) * 1000
                for k, v in parts.items():
                    self._parts[k].append(v)
                    if len(self._parts[k]) > 2000:
                        del self._parts[k][:1000]
                if total > self._worst[0]:
                    self._worst = (total, {k: round(v, 1) for k, v in parts.items()})
                self.ticks += 1
                next_t += period
                left = next_t - time.perf_counter()
                if left < -period:
                    next_t = time.perf_counter()
                elif left > 0:
                    self._stop.wait(left)
        except Exception as e:
            self.failure = f"{type(e).__name__}: {e}"
        finally:
            try:
                self._send(0.0, 0.0)
            except Exception:
                pass

    def summary(self) -> dict:
        out = {"ticks": self.ticks, "hz": round(1.0 / max(self.period, 1e-3)), "error": self.error,
               "on_target": self.on_target(), "estimate_from": dict(self.att.used)}
        if self._parts:
            out["stage_ms_p50"] = {k: round(sorted(v)[len(v) // 2], 1) for k, v in self._parts.items() if v}
            out["worst_tick_ms"] = [round(self._worst[0], 1), self._worst[1]]
        if self.failure:
            out["failure"] = self.failure
        return out
