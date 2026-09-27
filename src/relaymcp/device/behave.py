"""Real-time behaviors: small control loops that run on the handheld at 30-120 Hz, so reacting to the screen takes
tens of milliseconds instead of a model round trip (seconds). The model starts a behavior with parameters, then
reads compact events; the loop does the watching and the pressing.

Built-in behaviors:
- react: when a screen region changes, or shows a color, send an input (a key, a gamepad press, a click)
- track: steer the mouse or a stick until a colored target sits at an aim point (the cursor, or a crosshair)
- press_until: repeat an input until text, a color or a change appears
- watch: report when a region changes or shows a color (no input)
- program: a short Python program (the model writes it) with a controller and perception API, for real-time play:
  hold several axes at once, aim while walking, wait for text or a change, guard against danger; one call per batch.
  Programs that worked can be saved as named skills (skills.py) and run again by name.
- guard: a standing safety check, separate from the programs that come and go: when its condition holds, every
  program stops (a turn-based game then pauses) and an optional reflex program runs; alerts reach the model in the
  next tool results.

Safety: every run has a time limit, all held input is released when it ends (however it ends), and input on a
physical controller (someone picked up the handheld) stops it. Perception uses numpy on DXGI frames; no model runs.
"""

from __future__ import annotations

import collections
import threading
import time
import uuid
from typing import Any, Callable

from .timing import HiResTimer, sleep_until  # noqa: F401  (sleep_until is re-exported for tests)

MAX_RUNS = 6
MAX_QUEUED = 4   # programs waiting their turn (params.after): the next chunks, planned while one plays
MAX_GUARDS = 3
MAX_SECONDS = 600.0
GUARD_MAX_SECONDS = 4 * 3600.0  # guards only watch (their reflex is a separate, bounded program)
EVENTS_KEPT = 300

KINDS = {
    "react": 'region [l,t,r,b]; when {"color": [r,g,b], "tol": 40, "min_fraction": 0.2} or {"change": 12}; do {"key": '
             '["space"]} | {"pad": ["a"]} | {"click": true}; repeat (true); cooldown_ms (300); hz (120)',
    "track": 'color [r,g,b], tol (50), region (search area); or at [x,y]: whatever is there now (a tree, a door), '
             'size (120 px); aim "cursor" or [x,y] (default for at: screen center = crosshair); output "mouse" | '
             '"right_stick" | "left_stick"; gain; within px; hold_frames: done when on target that long (follow: true '
             'keeps following until max_s)',
    "press_until": 'do (as react, or {"hold": {"right_trigger": 1}} to keep a controller state, e.g. mining); '
                   'every_ms (400); until {"text": "Play"} | {"color": [r,g,b], "region": [...]} | {"change": 12, '
                   '"region": [...]}; max_presses (20)',
    "watch": 'region; when (as react); every_ms (100): reports each time it happens',
    "navigate": 'text (the menu item to reach); region (the menu); with "pad" (d-pad + A) | "keys" (arrows + Enter); '
                'confirm (false): press A/Enter on it; direction ("down") while it is off screen; max_moves (30)',
    "script": 'a small state machine: start (state name); states {name: {do (as react, repeated every_ms 250), until '
              '(text | color | change | {"state": {"topic", "match"}}), ms (instead of until: stay this long), next '
              '(state or "done"), timeout_s (30), on_timeout (state, else stop)}}',
    "program": 'code: Python run on the handheld, for real-time play in one call (params.wait = true returns when it '
               'ends, with its events). Controller: pad(buttons=[], ls=(x,y), rs=(x,y), lt=0, rt=0) holds that whole '
               'state until changed; press("a", ms=80) taps on top of it; seq([gamepad_sequence steps]); release(). '
               'Time: wait(ms); until(fn, timeout=5, hz=30) -> fn\'s value or None; elapsed(). Screen (px, region '
               '[l,t,r,b]): frame(region) -> BGRA numpy array; diff(a, b) -> 0-255; text(region) -> [[text, x, y]]; '
               'sees("Mine", region) -> [x, y] or None (OCR, ~50 ms for a small region); color([r,g,b], region, tol=40) '
               '-> fraction. aim(x, y, within=24, timeout=3, until=None) turns the right stick until what is at (x, y) '
               'sits at the screen center (the crosshair), keeping the held left stick and buttons (walk while '
               'aiming); until = keep steering until fn() is truthy -> {"on_target", "error_px", "match", '
               '"min_deflection" (the stick deadzone it learned; pass it back next time)}. '
               't = track(x, y, size=120): follow what is at (x, y) yourself: t.find() -> (x, y, score) in the '
               'latest frame. shift(a, b) -> (dx, dy, peak): how far the view moved between two frames of the same '
               'region (phase correlation; crop away the HUD), e.g. to calibrate camera turns. numbers(region, '
               'font="minecraft") -> ints drawn in a game\'s pixel font (HUD coordinates, counts), read exactly; '
               'pixel_text(region); grid_angle(region) -> (degrees, strength): straight edges\' turn off square, '
               'modulo 90 (looking straight down at a grid world: the yaw off its axes). '
               'guard(fn, "hurt"): checked during every wait, stops the program when fn() is truthy. log(msg, **data) '
               '-> an event; result = {...} is returned. Also W, H, CX, CY, np, math. Everything held is released '
               'when it ends; a stop request, max_s or a real controller moving ends it. skill("name", X=1) runs a '
               'saved skill and returns its result. Unknown names fail before anything moves. Camera skills, in degrees, '
               'for an app that was calibrated (kind calibrate): turn(yaw=0, pitch=0) right/up +, closed on visual '
               'odometry; level(pitch=0); look_at(x, y, refine=True): put a screen point under the crosshair (refine: then '
               'check the picture near the middle and correct); scan(score_fn, '
               'degrees=360): turn round calling score_fn(frame), end facing the best view -> {"heading", "score"}; '
               'look_rate(yaw_dps, pitch_dps): hold a turn rate (walk and turn); camera: the profile summary; '
               'set_pitch(deg): the pitch now, if known (after holding the stick into a limit), so turns looking '
               'straight down or up read right. '
               'Chunks: params.after = a run id queues this program to start the moment that run finishes (plan the '
               'next chunk while one plays; cancelled if that one fails or is stopped); params.replace = a run id '
               'stops that run and starts this one at once, keeping what it holds (no snap to neutral).',
    "calibrate": 'learn the camera controls of the app in front, once per app (~30 s: pass max_s 90; somewhere safe '
                 'with a textured view, in the gameplay view): which pixels are HUD, input latency, acceleration, the '
                 'right stick\'s deadzone and response curve, degrees per pixel (turns all the way round), focal '
                 'length, vertical gain, pitch limit and whether the stick is radial. Saved on the handheld; programs then get '
                 'turn/level/look_at/'
                 'scan. params: points (deflections to measure), full_turn (true), pitch (true).',
    "guard": 'a standing safety check, separate from programs (they come and go, it stays): setup (code run once: '
             'regions, baselines, e.g. RED0 = color([190,30,30], HEARTS)); when (a Python expression over frame, '
             'diff, color, sees, text, elapsed: "color([190,30,30], HEARTS) < 0.5 * RED0"); every_ms (100); then '
             '"stop" (every program and loop stops; a turn-based game pauses) or "note"; program (code: a reflex '
             'started after stopping, e.g. back away; program_max_s 10); repeat (false), cooldown_ms (3000); name. '
             'Checks only while the game runs; fires show as "alerts" in the next tool results. max_s up to 4 h.',
}

ACTIONS = {
    "start": 'kind, params, max_s; params.wait = true returns when it ends. kind "program" with params.skill = a '
             'saved skill\'s name and params.args = its inputs runs it by name',
    "status": "id (and since = the last event number seen): new events; no id = every behavior",
    "stop": "id, or no id = all (guards too)",
    "save": 'kind = a name for a program that worked; params: code, description (what it does and needs: how it is '
            'found later), common (every app); inputs are worked out from the code',
    "skills": "the saved skills for the app in front (and common ones): name, description, inputs, runs, ok",
    "forget": "kind = a skill's name",
}


# ---------------------------------------------------------------------------------------------------- perception

def _np():
    import numpy as np
    return np


def color_mask(frame, rgb, tol: int = 40):
    """Pixels within tol of rgb on every channel. frame: BGRA uint8 array (h, w, 4)."""
    np = _np()
    f = frame.astype(np.int16)
    r, g, b = (int(v) for v in rgb)
    return (np.abs(f[..., 2] - r) <= tol) & (np.abs(f[..., 1] - g) <= tol) & (np.abs(f[..., 0] - b) <= tol)


def centroid(mask) -> tuple[float, float, int] | None:
    np = _np()
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return float(xs.mean()), float(ys.mean()), int(len(xs))


def difference(a, b) -> float:
    """Mean absolute difference (0-255) between two frames of the same size."""
    np = _np()
    if a is None or b is None or a.shape != b.shape:
        return 255.0
    return float(np.abs(a[..., :3].astype(np.int16) - b[..., :3].astype(np.int16)).mean())


def gray_small(frame, k: int = 4):
    """A downscaled grayscale copy (every k-th pixel) for fast matching."""
    np = _np()
    f = frame[::k, ::k, :3].astype(np.float32)
    return f[..., 2] * 0.299 + f[..., 1] * 0.587 + f[..., 0] * 0.114  # BGRA frames


def match_template(img, tmpl) -> tuple[int, int, float]:
    """(x, y, score) of the best normalized cross-correlation match of tmpl (top-left corner) in img, via FFT. Score is
    -1..1; flat windows (open sky, a plain wall) can't match a textured patch and score 0. Sums are float64: in
    float32 their rounding error swamped a flat window's tiny variance, and sky "matched" with scores in the
    thousands (found aiming at a tree in Minecraft)."""
    np = _np()
    img = np.asarray(img, np.float64)
    t = np.asarray(tmpl, np.float64)
    ih, iw = img.shape
    th, tw = t.shape
    if th > ih or tw > iw:
        raise ValueError("template larger than the image")
    t = t - t.mean()
    energy = float((t * t).sum())
    tnorm = float(np.sqrt(energy)) + 1e-9
    corr = np.fft.irfft2(np.fft.rfft2(img) * np.fft.rfft2(t[::-1, ::-1], s=img.shape), s=img.shape)[th - 1:, tw - 1:]
    pad = np.pad(img, ((1, 0), (1, 0)))
    s1 = pad.cumsum(0).cumsum(1)
    s2 = (pad * pad).cumsum(0).cumsum(1)

    def box(a):
        return a[th:, tw:] - a[:-th, tw:] - a[th:, :-tw] + a[:-th, :-tw]

    n = th * tw
    var = box(s2) - box(s1) ** 2 / n
    textured = var > max(1e-6, 0.01 * energy)  # a window needs some of the patch's contrast to be a candidate
    ncc = np.where(textured, corr / (np.sqrt(np.maximum(var, 1e-9)) * tnorm), 0.0)
    ncc = np.clip(ncc, -1.0, 1.0)
    y, x = divmod(int(np.argmax(ncc)), ncc.shape[1])
    return x, y, float(ncc[y, x])


def phase_shift(a, b, k: int = 4) -> tuple[int, int, float]:
    """How far the picture moved from frame a to frame b (same size), in screen px, by phase correlation at 1/k scale:
    (dx, dy, peak); a peak near 0 means no clear answer. Static overlays (a HUD) pull toward (0, 0): crop them away."""
    np = _np()
    ga, gb = gray_small(a, k), gray_small(b, k)
    if ga.shape != gb.shape:
        raise ValueError("shift needs two frames of the same size")
    win = np.outer(np.hanning(ga.shape[0]), np.hanning(ga.shape[1]))
    fa, fb = np.fft.fft2((ga - ga.mean()) * win), np.fft.fft2((gb - gb.mean()) * win)
    cross = fb * np.conj(fa)
    r = np.fft.ifft2(cross / (np.abs(cross) + 1e-6)).real
    y, x = np.unravel_index(int(np.argmax(r)), r.shape)
    h, w = r.shape
    x = x - w if x > w // 2 else x
    y = y - h if y > h // 2 else y
    return int(x * k), int(y * k), round(float(r.max()), 3)


def matches(when: dict, frame, previous) -> tuple[bool, dict]:
    """Does the frame satisfy `when` ({"color", "tol", "min_fraction"} or {"change"})? Returns (hit, measurements)."""
    if "color" in when:
        mask = color_mask(frame, when["color"], int(when.get("tol", 40)))
        frac = float(mask.mean())
        return frac >= float(when.get("min_fraction", 0.2)), {"fraction": round(frac, 3)}
    if "change" in when:
        d = difference(frame, previous)
        return previous is not None and d >= float(when["change"]), {"change": round(d, 1)}
    raise ValueError('when needs "color" or "change"')


# ---------------------------------------------------------------------------------------------------- runs

class Aimer:
    """Follow whatever was at a screen point (a tree, a door): a patch of the screen around it, found again in each
    new frame (normalized cross-correlation at 1/4 scale) and refreshed as the view changes."""

    def __init__(self, frame, at, size: int = 120, k: int = 4):
        self.k = k
        img = gray_small(frame, k)
        size = max(32, min(int(size), 400))
        self.half = half = size // (2 * k)
        cx, cy = int(at[0]) // k, int(at[1]) // k
        if not (half <= cx < img.shape[1] - half and half <= cy < img.shape[0] - half):
            raise ValueError("at is too close to the edge of the screen for its size")
        self.tmpl = img[cy - half:cy + half, cx - half:cx + half].copy()
        if float(self.tmpl.std()) < 3:
            raise ValueError("nothing distinctive at that point to follow (a flat area)")
        self.n = 0

    def find(self, frame, refresh: bool = True, near=None) -> tuple[float, float, float]:
        """(x, y) of the patch's center in this frame (screen px) and the match score (0-1). near = (x, y, radius):
        look only within radius px of where it should be (blocky worlds are full of look-alikes)."""
        img = gray_small(frame, self.k)
        ox = oy = 0
        if near is not None:
            cx, cy, r = (int(v) // self.k for v in near)
            ox, oy = max(0, cx - r - self.half), max(0, cy - r - self.half)
            x1, y1 = min(img.shape[1], cx + r + self.half), min(img.shape[0], cy + r + self.half)
            if x1 - ox < 2 * self.half or y1 - oy < 2 * self.half:
                return float(near[0]), float(near[1]), 0.0
        x, y, score = match_template(img[oy:, ox:] if near is None else img[oy:y1, ox:x1], self.tmpl)
        x, y = x + ox, y + oy
        self.n += 1
        if refresh and score > 0.8 and self.n % 10 == 0:  # only on a confident match
            self.tmpl = img[y:y + 2 * self.half, x:x + 2 * self.half].copy()
        return (x + self.half) * self.k, (y + self.half) * self.k, score


def deflection(error_px: float, full_px: float, gain: float, within: float, nudge: float) -> float:
    """Stick deflection that turns toward a target error_px away: proportional, at least `nudge` (games ignore small
    deflections), zero once close."""
    if abs(error_px) <= within / 2:
        return 0.0
    d = max(-1.0, min(1.0, error_px / full_px * gain * 2))
    return d if abs(d) >= nudge else nudge * (1 if d > 0 else -1)


class Steer:
    """Screen-px error -> stick deflection, learning the game's deadzone on the way: when the target doesn't move for
    a few frames while the stick is deflected, the smallest deflection goes up (Minecraft ignores anything below
    ~0.4; aiming there with a 0.22 floor stalled)."""

    def __init__(self, full_px: float, gain: float, within: float, nudge: float):
        self.full_px, self.gain, self.within, self.nudge = full_px, gain, within, nudge
        self.last: tuple[float, float] | None = None
        self.stuck, self.pinned, self.magnitude = 0, 0, 0.0

    def __call__(self, tx: float, ty: float, ex: float, ey: float) -> tuple[float, float]:
        moved = self.last is None or abs(tx - self.last[0]) + abs(ty - self.last[1]) > 3
        full = self.magnitude >= 0.95
        self.stuck = self.stuck + 1 if self.magnitude and not full and not moved else 0
        self.pinned = self.pinned + 1 if full and not moved else 0
        if self.pinned >= 8:  # not a deadzone: the stick is all the way over and the "target" stays put
            raise ValueError("what is at that point doesn't move when the camera turns (part of the HUD or the "
                             "held item?): aim at a point away from them")
        if self.stuck >= 4 and self.nudge < 0.8:
            self.nudge, self.stuck = min(0.8, self.nudge + 0.06), 0
        self.last = (tx, ty)
        dx = deflection(ex, self.full_px, self.gain, self.within, self.nudge)
        dy = -deflection(ey, self.full_px, self.gain, self.within, self.nudge)
        self.magnitude = max(abs(dx), abs(dy))
        return dx, dy

    def still(self) -> None:
        """The stick went back to center (on target, or the target was lost)."""
        self.magnitude, self.stuck, self.pinned = 0.0, 0, 0


class Run:
    def __init__(self, kind: str, params: dict, max_s: float, cap: float = MAX_SECONDS):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.params = kind, params
        self.max_s = max(0.5, min(float(max_s), cap))
        self.started = time.perf_counter()
        self.state, self.reason = "running", None
        self.events: collections.deque = collections.deque(maxlen=EVENTS_KEPT)
        self.seq = 0
        self.stop_evt = threading.Event()
        self.stats: dict[str, Any] = {"ticks": 0, "actions": 0}
        self.used: set[str] = set()  # which outputs this run touched (only those are released when it ends)
        self.ended: float | None = None
        self._frame_ms: collections.deque = collections.deque(maxlen=240)
        self._lock = threading.Lock()
        self.thread_id: int | None = None
        self.after: str | None = None   # queued behind this run: starts when it finishes
        self.next: list["Run"] = []     # runs queued behind this one
        self.handoff: "Run | None" = None  # replaced by this run: it takes over what this one holds
        self.stop_reason = ""  # why it was asked to stop (e.g. "replaced by <id>")

    def emit(self, kind: str, **data) -> None:
        with self._lock:
            self.seq += 1
            self.events.append({"n": self.seq, "t": round(time.perf_counter() - self.started, 3), "event": kind, **data})

    def since(self, n: int) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["n"] > n]

    def summary(self) -> dict:
        elapsed = (self.ended or time.perf_counter()) - self.started
        ticks = self.stats["ticks"]
        out = {"id": self.id, "kind": self.kind, "state": self.state, "reason": self.reason,
               "seconds": round(elapsed, 1), "ticks": ticks, "hz": round(ticks / elapsed, 1) if elapsed > 0 else 0,
               "actions": self.stats["actions"], "events": self.seq}
        if self.after and self.state == "queued":
            out["after"] = self.after
        if self._frame_ms:
            s = sorted(self._frame_ms)
            out["frame_ms_p50"] = round(s[len(s) // 2], 1)
        for k, v in self.stats.items():
            if k not in ("ticks", "actions") and k not in out:  # never let a behavior's stat hide the run's own fields
                out[k] = v
        return out


class Runtime:
    """grab(region) -> (BGRA array, capture time on time.perf_counter); outputs: key(keys), pad(buttons),
    click(), mouse(dx, dy), stick(side, x, y), release(); takeover() -> reason or None; read_text(region) -> lines
    [{"text", "box"}]; cursor() -> [x, y]."""

    def __init__(self, grab: Callable, outputs, takeover: Callable | None = None, read_text: Callable | None = None,
                 cursor: Callable | None = None, state_events: Callable | None = None,
                 on_start: Callable | None = None, on_end: Callable | None = None,
                 profiles=None, app: Callable | None = None, skills=None, active: Callable | None = None):
        self.grab, self.out = grab, outputs
        self.profiles, self.app = profiles, app  # camera calibrations (control.ProfileStore) by foreground app
        self.skills = skills  # saved programs (skills.SkillStore)
        self.active = active or (lambda: True)  # is the game running (guards don't judge a paused game)?
        self.alerts: collections.deque = collections.deque(maxlen=20)
        self._alert_seq, self._alerts_taken = 0, 0
        self.on_start, self.on_end = on_start, on_end  # e.g. resume a paused game, and pause it again (turns.py)
        self.takeover = takeover or (lambda: None)
        self.read_text = read_text
        self.cursor = cursor
        self.state_events = state_events  # (topic, since) -> (events [{"n", "topic", "data"}], cursor): the inbox
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self, kind: str, params: dict | None, max_s: float = 30.0) -> dict:
        """params.after = a run id: queue this one to start the moment that run finishes (the next chunk of play,
        planned while the current one plays; cancelled if that run fails or is stopped). params.replace = a run id:
        stop that run and start this one at once, taking over the inputs it holds (no snap to neutral between)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        params = dict(params or {})
        after, replace = str(params.pop("after", "") or ""), str(params.pop("replace", "") or "")
        if after and replace:
            raise ValueError("params.after or params.replace, not both")
        with self._lock:
            prev = self.runs.get(after or replace) if (after or replace) else None
            if (after or replace) and prev is None:
                raise KeyError(f"no behavior {after or replace!r}")
            if prev is not None and "guard" in (kind, prev.kind):
                raise ValueError("guards stand alone: they aren't queued, replaced or replacing")
            if after and prev.state in ("stopped", "failed", "cancelled"):
                raise RuntimeError(f"behavior {prev.id} already {prev.state} ({prev.reason})")
            if replace and prev.state != "running":
                raise RuntimeError(f"behavior {prev.id} isn't running ({prev.state})")
            queue = bool(after) and prev.state in ("running", "queued")
            active = [r for r in self.runs.values() if r.state == "running"]
            if queue and sum(r.state == "queued" for r in self.runs.values()) >= MAX_QUEUED:
                raise RuntimeError(f"at most {MAX_QUEUED} behaviors queued (let some play first)")
            if not queue and not replace and len(active) >= MAX_RUNS:
                raise RuntimeError(f"at most {MAX_RUNS} behaviors at a time (stop one first)")
            if kind == "guard" and sum(r.kind == "guard" for r in active) >= MAX_GUARDS:
                raise RuntimeError(f"at most {MAX_GUARDS} guards at a time (stop one first)")
            for rid in [rid for rid, r in self.runs.items() if r.state not in ("running", "queued")][:-8]:
                del self.runs[rid]  # keep the last few finished ones for status
            run = Run(kind, params, max_s, GUARD_MAX_SECONDS if kind == "guard" else MAX_SECONDS)
            self.runs[run.id] = run
            if queue:
                run.state, run.after = "queued", prev.id
                prev.next.append(run)
                return {"id": run.id, "kind": kind, "max_s": run.max_s, "state": "queued", "after": prev.id}
            if replace:
                run.state = "starting"
                prev.handoff = run
        if replace:
            self.stop(prev.id, reason=f"replaced by {run.id}")  # its thread starts this run as it ends
            end = time.perf_counter() + 3.0
            while prev.ended is None and time.perf_counter() < end:
                time.sleep(0.01)
            with self._lock:  # it ended on its own just before (or never ended): start this run here instead
                unclaimed = prev.handoff is run
                if unclaimed:
                    prev.handoff = None
            if unclaimed:
                self._launch(run)
            return {"id": run.id, "kind": kind, "max_s": run.max_s, "replaced": prev.id}
        self._launch(run)
        return {"id": run.id, "kind": kind, "max_s": run.max_s}

    def _launch(self, run: Run) -> None:
        run.state = "running"
        if self.on_start:
            try:
                self.on_start(run)
            except Exception as e:
                run.emit("note", text=f"before starting: {e}")
        run.started = time.perf_counter()  # the time limit counts from now (resuming a game can take a moment)
        threading.Thread(target=self._thread, args=(run,), name=f"behavior-{run.id}", daemon=True).start()

    def _cancel(self, run: Run, reason: str) -> list[str]:
        """Cancel a queued run and everything queued behind it."""
        with self._lock:
            if run.state != "queued":
                return []
            run.state, run.reason, run.ended = "cancelled", reason, time.perf_counter()
            behind, run.next = run.next, []
        run.emit("end", state=run.state, reason=reason)
        out = [run.id]
        for q in behind:
            out += self._cancel(q, f"{run.id} before it was cancelled")
        return out

    def wait(self, run_id: str, events: int = 40) -> dict:
        """Block until a run ends; its summary and last events. A queued run first waits its turn (the runs ahead of
        it have time limits, and a failure ahead cancels it)."""
        r = self.runs[run_id]
        while r.state in ("queued", "starting"):
            time.sleep(0.02)
        end = r.started + r.max_s + 10
        while r.state == "running" and time.perf_counter() < end:
            time.sleep(0.02)
        while r.ended is None and time.perf_counter() < end + 2:  # the end event comes right after the state changes
            time.sleep(0.01)
        return {**r.summary(), "events": r.since(0)[-events:]}

    def stop(self, run_id: str = "", reason: str = "") -> list[str]:
        stopped = []
        for r in list(self.runs.values()):  # queued ones first, so none starts as the ones ahead of it stop
            if (not run_id or r.id == run_id) and r.state == "queued":
                stopped += self._cancel(r, reason or "stopped before its turn")
        for r in list(self.runs.values()):
            if (not run_id or r.id == run_id) and r.state == "running":
                r.stop_reason = reason
                r.stop_evt.set()
                stopped.append(r.id)
        for r in (self.runs[s] for s in stopped):
            for _ in range(100):
                if r.state != "running":
                    break
                time.sleep(0.02)
            if r.state == "running":
                _interrupt(r)  # a program busy in a loop that never waits
        return stopped

    def stop_others(self, keep: Run) -> list[str]:
        """Stop every running or queued behavior but guards (a guard firing stops what it guards, and its plans)."""
        ids = [r.id for r in list(self.runs.values())
               if r.state in ("running", "queued") and r is not keep and r.kind != "guard"]
        for rid in ids:
            self.stop(rid)
        return ids

    def _alert(self, alert: dict) -> None:
        with self._lock:
            self._alert_seq += 1
            self.alerts.append({"n": self._alert_seq, **alert})

    def take_alerts(self) -> list[dict] | None:
        """Alerts not handed out yet (tool results carry them, so the model hears about a guard firing)."""
        with self._lock:
            new = [a for a in self.alerts if a["n"] > self._alerts_taken]
            self._alerts_taken = self._alert_seq
        return [{k: v for k, v in a.items() if k != "n"} for a in new] or None

    def status(self, run_id: str = "", since: int = 0) -> dict:
        if run_id:
            r = self.runs.get(run_id)
            if not r:
                raise KeyError(f"no behavior {run_id!r}")
            return {**r.summary(), "new_events": r.since(int(since))}
        return {"behaviors": [r.summary() for r in self.runs.values()]}

    def _thread(self, run: Run) -> None:
        try:
            with HiResTimer():
                getattr(self, f"_run_{run.kind}")(run, run.params)
            if run.state == "running":
                run.state, run.reason = "done", run.reason or "finished"
        except _Stopped as s:
            run.state, run.reason = "stopped", str(s)
        except Exception as e:
            run.state, run.reason = "failed", f"{type(e).__name__}: {e}"
        finally:
            with self._lock:  # claimed here, so exactly one side starts what comes next
                hand, behind, run.next, run.handoff = run.handoff, run.next, [], None
            if hand is not None:  # replaced: the new run takes over what this one holds (no snap to neutral)
                hand.used |= run.used
                run.reason = f"replaced by {hand.id}"
            else:
                try:
                    self.out.release(run.used)  # only what this run holds: other runs and the agent's input are theirs
                except Exception:
                    pass
            # what comes next starts before this run's end hook, so a turn-based game doesn't pause in between
            if hand is not None:
                self._launch(hand)
            for q in behind:
                if run.state == "done" and q.state == "queued":
                    self._launch(q)
                else:
                    self._cancel(q, f"{run.id} before it ended {run.state}")
            if self.on_end:
                try:
                    self.on_end(run)
                except Exception as e:
                    run.emit("note", text=f"after ending: {e}")
            run.ended = time.perf_counter()
            run.emit("end", state=run.state, reason=run.reason)

    # --- shared loop pieces -----------------------------------------------------------------------------------------

    def _ticks(self, run: Run, hz: float, takeover_stops: bool = True):
        """Yield once per tick until the time limit, a stop request or a takeover."""
        period = 1.0 / max(0.01, min(float(hz), 240.0))  # anything from every 100 s to 240 times a second
        next_t = time.perf_counter()
        deadline = run.started + run.max_s
        while True:
            if run.stop_evt.is_set():
                raise _Stopped(run.stop_reason or "stopped on request")
            if time.perf_counter() >= deadline:
                run.reason = f"time limit ({run.max_s:g} s)"
                return
            who = self.takeover() if takeover_stops else None
            if who:
                raise _Stopped(f"you took over ({who})")
            yield
            run.stats["ticks"] += 1
            next_t += period
            now = time.perf_counter()
            if next_t < now - period:  # fell behind (a slow frame): don't try to catch up in a burst
                next_t = now
            wake = min(next_t, deadline)
            # Long waits go in short slices: a stop request, a takeover or the time limit is noticed within 0.25 s.
            # (Short waits skip the extra takeover check: the top of the loop does it once per tick.)
            long_wait = wake - time.perf_counter() > 0.25
            while wake - time.perf_counter() > 0.005:
                if run.stop_evt.wait(min(wake - time.perf_counter() - 0.003, 0.25)):
                    break
                who = self.takeover() if long_wait and takeover_stops else None
                if who:
                    raise _Stopped(f"you took over ({who})")
            if not run.stop_evt.is_set():
                sleep_until(wake)

    def _frame(self, run: Run, region):
        t0 = time.perf_counter()
        frame, captured = self.grab(region)
        run._frame_ms.append((time.perf_counter() - t0) * 1000)
        return frame, captured

    def _act(self, run: Run, do: dict) -> None:
        if "hold" in do:  # a controller state kept until the behavior (or script state) ends, e.g. mining
            run.used.add("hold")
            self.out.hold(do["hold"] or {})
            run.stats["actions"] += 1
            return
        if "key" in do:
            run.used.add("key")
            self.out.key(do["key"] if isinstance(do["key"], list) else [do["key"]])
        elif "pad" in do:
            run.used.add("pad")
            self.out.pad(do["pad"] if isinstance(do["pad"], list) else [do["pad"]])
        elif do.get("click"):
            run.used.add("click")
            self.out.click()
        else:
            raise ValueError('do needs "key", "pad" or "click"')
        run.stats["actions"] += 1

    # --- behaviors --------------------------------------------------------------------------------------------------

    def _run_react(self, run: Run, p: dict) -> None:
        region, when, do = p.get("region"), p.get("when") or {}, p.get("do") or {}
        repeat, cooldown = bool(p.get("repeat", True)), float(p.get("cooldown_ms", 300)) / 1000
        previous, armed_at, latencies = None, 0.0, []
        for _ in self._ticks(run, p.get("hz", 120)):
            frame, captured = self._frame(run, region)
            hit, measured = matches(when, frame, previous)
            previous = frame
            if hit and time.perf_counter() >= armed_at:
                self._act(run, do)
                latency = (time.perf_counter() - captured) * 1000
                latencies.append(latency)
                run.emit("reacted", ms_from_frame=round(latency, 1), **measured)
                run.stats["reaction_ms_p50"] = round(sorted(latencies)[len(latencies) // 2], 1)
                armed_at = time.perf_counter() + cooldown
                if not repeat:
                    run.reason = "reacted"
                    return

    def _run_watch(self, run: Run, p: dict) -> None:
        region, when = p.get("region"), p.get("when") or {}
        hz = 1000.0 / max(10.0, float(p.get("every_ms", 100)))
        previous, was = None, False
        for _ in self._ticks(run, hz):
            frame, _captured = self._frame(run, region)
            hit, measured = matches(when, frame, previous)
            if hit and not was:
                run.emit("seen", **measured)
            was = hit
            previous = frame

    def _run_press_until(self, run: Run, p: dict) -> None:
        do, until = p.get("do") or {}, p.get("until") or {}
        every = max(0.05, float(p.get("every_ms", 400)) / 1000)
        max_presses = max(1, min(int(p.get("max_presses", 20)), 500))
        baseline = None
        if "change" in until:
            baseline, _ = self._frame(run, until.get("region"))
        presses = 0
        for _ in self._ticks(run, 1 / every):
            if self._condition(run, until, baseline):
                run.reason = f"condition met after {presses} presses"
                run.emit("done", presses=presses)
                return
            if presses >= max_presses:
                raise _Stopped(f"gave up after {presses} presses")
            self._act(run, do)
            presses += 1
            run.stats["presses"] = presses

    def _condition(self, run: Run, until: dict, baseline, since: int = 0) -> bool:
        if "state" in until:
            if not self.state_events:
                raise RuntimeError("state conditions need the state inbox")
            want = until["state"] or {}
            from .inbox import matches as inbox_matches
            events, _ = self.state_events(want.get("topic", ""), since)
            return any(inbox_matches(e["data"], want.get("match", "")) for e in events)
        if "text" in until:
            if not self.read_text:
                raise RuntimeError("text conditions need OCR")
            from .ocr import find
            return find(self.read_text(until.get("region")), str(until["text"])) is not None
        frame, _ = self._frame(run, until.get("region"))
        hit, _m = matches(until, frame, baseline)
        return hit

    def _run_track_at(self, run: Run, p: dict) -> None:
        """Aim at whatever is at screen point `at` (e.g. a tree seen in a screenshot): remember a patch of the screen
        around it, find it again in every frame, and turn (stick or mouse) until it sits at the aim point, by
        default the middle of the screen, where 3D games put the crosshair."""
        np = _np()
        frame, _ = self._frame(run, None)
        h, w = frame.shape[:2]
        aimer = Aimer(frame, p["at"], p.get("size", 120))
        aim = p.get("aim") or [w / 2, h / 2]
        output, gain = p.get("output", "right_stick"), float(p.get("gain", 0.8))
        within, hold = float(p.get("within", 24)), int(p.get("hold_frames", 3))
        scale, nudge = float(p.get("full_deflection_px", 500)), float(p.get("min_deflection", 0.22))
        min_score = float(p.get("min_score", 0.45))
        on_target, lost = 0, 0
        steer = Steer(scale, gain, within, nudge)
        run.used.add("mouse" if output == "mouse" else "stick")
        for _ in self._ticks(run, p.get("hz", 30)):
            frame, _ = self._frame(run, None)
            tx, ty, score = aimer.find(frame, refresh=on_target == 0)
            run.stats["match"] = round(score, 2)
            if score < min_score:
                lost += 1
                run.stats["frames_without_target"] = lost
                steer.still()
                if output != "mouse":
                    self.out.stick(output, 0.0, 0.0)
                continue
            ex, ey = tx - aim[0], ty - aim[1]
            err = (ex * ex + ey * ey) ** 0.5
            run.stats.update(error_px=round(err, 1), target=[int(tx), int(ty)])
            if err <= within:
                on_target += 1
                steer.still()
                if output != "mouse":
                    self.out.stick(output, 0.0, 0.0)
                if hold and on_target >= hold and not p.get("follow", False):
                    run.reason = "on target"
                    run.emit("on_target", error_px=round(err, 1), match=round(score, 2))
                    return
                continue
            on_target = 0
            if output == "mouse":
                self.out.mouse(int(round(np.clip(ex * gain * 0.5, -200, 200))), int(round(np.clip(ey * gain * 0.5, -200, 200))))
            else:
                self.out.stick(output, *steer(tx, ty, ex, ey))
                run.stats["min_deflection"] = round(steer.nudge, 2)
            run.stats["actions"] += 1

    def _run_calibrate(self, run: Run, p: dict) -> None:
        from . import control
        if self.profiles is None or self.app is None:
            raise RuntimeError("calibration needs a profile store")
        app = self.app() or "unknown"
        deadline = run.started + run.max_s

        def sleep(seconds: float) -> None:
            end = time.perf_counter() + seconds
            while True:
                if run.stop_evt.is_set():
                    raise _Stopped(run.stop_reason or "stopped on request")
                if time.perf_counter() >= deadline:
                    raise _Stopped(f"time limit ({run.max_s:g} s): calibration takes ~30 s, pass max_s 90")
                who = self.takeover()
                if who:
                    raise _Stopped(f"you took over ({who})")
                left = end - time.perf_counter()
                if left <= 0:
                    return
                time.sleep(min(left, 0.02))

        run.used.add("stick")
        io = control.PlantIO(frame=lambda: self._frame(run, None)[0],
                             stick=lambda x, y: self.out.stick("right_stick", x, y), sleep=sleep,
                             log=lambda msg, **data: run.emit(msg, **data))
        points = tuple(p.get("points") or (0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.85, 1.0))
        profile = control.calibrate(io, points=points, hold_s=float(p.get("hold_s", 0.5)),
                                    full_turn=bool(p.get("full_turn", True)), pitch=bool(p.get("pitch", True)), app=app)
        self.profiles.save(app, profile)
        run.stats["profile"] = control.summary(profile)
        run.reason = f"saved the camera profile for {app}"

    def _run_guard(self, run: Run, p: dict) -> None:
        import ast
        when = str(p.get("when") or "").strip()
        if not when:
            raise ValueError('guard needs when: a Python expression, e.g. "color([190,30,30], HEARTS) < 0.5 * RED0"')
        then = str(p.get("then") or "stop")
        if then not in ("stop", "note"):
            raise ValueError('then must be "stop" or "note"')
        api = Program(self, run, perception_only=True)
        ns = api.namespace()
        setup = str(p.get("setup") or "")
        try:
            setup_tree, when_tree = ast.parse(setup, "<guard setup>"), ast.parse(when, "<guard>", mode="eval")
        except SyntaxError as e:
            raise ValueError(f"guard {e.filename.strip('<>')} line {e.lineno}: {e.msg}") from None
        preflight(setup_tree, ns, "guard setup")
        preflight(when_tree, set(ns) | defined_names(setup_tree), "guard")
        reflex = str(p.get("program") or "")
        if reflex:
            try:
                preflight(ast.parse(reflex, "<reflex>"), Program.NAMES, "reflex program")
            except SyntaxError as e:
                raise ValueError(f"reflex program line {e.lineno}: {e.msg}") from None
        setup_code, cond = compile(setup_tree, "<guard setup>", "exec"), compile(when_tree, "<guard>", "eval")
        ready = False  # setup samples baselines: it runs on the first tick the game is running, not on a pause menu
        name = str(p.get("name") or when)[:60]
        repeat, cooldown = bool(p.get("repeat", False)), float(p.get("cooldown_ms", 3000)) / 1000
        every = max(20.0, float(p.get("every_ms", 100)))
        last = -1e9
        run.stats["fired"] = 0
        for _ in self._ticks(run, 1000 / every, takeover_stops=False):
            if not self.active():
                continue  # a paused game shows its pause menu, not the world
            if not ready:
                exec(setup_code, ns)
                ready = True
                continue
            try:
                hit = eval(cond, ns)
            except _Stopped:
                raise
            except Exception as e:
                raise RuntimeError(f"guard condition failed: {type(e).__name__}: {e}") from None
            if not hit or time.perf_counter() - last < cooldown:
                continue
            last = time.perf_counter()
            stopped = self.stop_others(run) if then == "stop" else []
            started = None
            if reflex:
                who = self.takeover()
                if who:
                    run.emit("note", text=f"no reflex: {who} is in use")
                else:
                    started = self.start("program", {"code": reflex}, float(p.get("program_max_s", 10)))["id"]
            alert = {"guard": name, "at": time.strftime("%H:%M:%S"), "stopped": stopped or None, "reflex": started}
            run.emit("fired", **{k: v for k, v in alert.items() if v is not None})
            self._alert({k: v for k, v in alert.items() if v is not None})
            run.stats["fired"] += 1
            if not repeat:
                run.reason = f"fired: {name}"
                return

    def _run_program(self, run: Run, p: dict) -> None:
        import ast
        code, skill_name, args = str(p.get("code") or ""), p.get("skill"), dict(p.get("args") or {})
        app = (self.app() if self.app else None) or "unknown"
        if skill_name:
            if self.skills is None:
                raise RuntimeError("no skill library")
            saved = self.skills.get(app, str(skill_name))
            if not saved:
                raise ValueError(f"no skill {skill_name!r} for {app} (behavior action skills lists them)")
            code = saved["code"]
            missing = [n for n in saved.get("inputs") or [] if n not in args]
            if missing:
                raise ValueError(f"skill {saved['name']} needs {', '.join(missing)} (params.args)")
            run.stats["skill"] = saved["name"]
        if not code.strip():
            raise ValueError("program needs code (or params.skill)")
        try:
            tree = ast.parse(code, "<program>")
        except SyntaxError as e:
            raise ValueError(f"program line {e.lineno}: {e.msg}") from None
        api = Program(self, run)
        ns = {**api.namespace(), **args}
        preflight(tree, ns)  # before anything moves: a typo shouldn't surface after the controller started
        compiled = compile(tree, "<program>", "exec")
        run.thread_id = threading.get_ident()
        _interrupt_after_deadline(run)
        outcome = "done"
        try:
            exec(compiled, ns)
        except _Stopped as e:
            outcome = f"stopped: {e}"
            raise
        except Exception as e:
            line = next((f.lineno for f in reversed(_frames(e.__traceback__)) if f.filename == "<program>"), None)
            outcome = f"{type(e).__name__}: {e}" + (f" (program line {line})" if line else "")
            raise RuntimeError(outcome) from None
        finally:
            run.thread_id = None
            if skill_name and self.skills is not None:
                self.skills.record(app, str(skill_name), outcome)
        if "result" in ns:
            run.stats["result"] = _jsonable(ns["result"])

    def _run_navigate(self, run: Run, p: dict) -> None:
        if not self.read_text:
            raise RuntimeError("navigate needs OCR")
        want = " ".join(str(p.get("text") or "").lower().split())
        if not want:
            raise ValueError("navigate needs text (the item to reach)")
        region, with_pad = p.get("region"), p.get("with", "pad") == "pad"
        max_moves, moves = max(1, min(int(p.get("max_moves", 30)), 200)), 0
        settle = max(0.05, float(p.get("settle_ms", 150)) / 1000)
        names = {"up": "dpad_up", "down": "dpad_down", "left": "dpad_left", "right": "dpad_right"} if with_pad else \
            {"up": "up", "down": "down", "left": "left", "right": "right"}
        search = str(p.get("direction", "down"))
        for _ in self._ticks(run, 1 / settle):
            lines = self.read_text(region)
            frame, _ = self._frame(run, region)
            target = best_line(lines, want)
            current = highlighted(lines, frame, region)
            run.stats["highlighted"] = current["text"] if current else None
            run.stats["seen"] = [ln["text"] for ln in lines[:8]]  # what the menu looked like (for "why didn't it work")
            if target and current and target is current:
                if p.get("confirm"):
                    self._act(run, {"pad": ["a"]} if with_pad else {"key": ["enter"]})
                run.reason = f"reached {target['text']!r} in {moves} moves"
                run.emit("reached", text=target["text"], moves=moves)
                return
            if moves >= max_moves:
                raise _Stopped(f"didn't reach {want!r} in {moves} moves (highlighted: "
                               f"{current['text'] if current else 'unknown'})")
            if target and current:
                (tx, ty), (cx, cy) = center_of(target["box"]), center_of(current["box"])
                if abs(ty - cy) >= abs(tx - cx):
                    step = "down" if ty > cy else "up"
                else:
                    step = "right" if tx > cx else "left"
            else:
                step = search  # not on screen yet (a longer list): keep going the way we were told
            run.used.add("pad" if with_pad else "key")
            (self.out.pad if with_pad else self.out.key)([names[step]])
            run.stats["actions"] += 1
            moves += 1
            run.stats["moves"] = moves

    def _run_script(self, run: Run, p: dict) -> None:
        states = p.get("states") or {}
        name = p.get("start") or next(iter(states), None)
        if not name or name not in states:
            raise ValueError("script needs states and a start state that exists")
        visits = 0
        while name != "done":
            if name not in states:
                raise ValueError(f"no state {name!r}")
            visits += 1
            if visits > 200:
                raise _Stopped("script went through 200 states (a loop?)")
            st = states[name]
            run.emit("state", name=name)
            run.stats["current"] = name
            outcome = self._script_state(run, st)
            if outcome == "timeout":
                if st.get("on_timeout"):
                    name = st["on_timeout"]
                    continue
                raise _Stopped(f"state {name!r} timed out")
            name = st.get("next", "done")
        run.reason = "script finished"

    def _script_state(self, run: Run, st: dict) -> str:
        """Run one state until its condition holds ('next'), its time is up ('next' for ms states), or its timeout."""
        do, until = st.get("do"), st.get("until")
        every = max(0.02, float(st.get("every_ms", 250)) / 1000)
        entered = time.perf_counter()
        hold_s = float(st["ms"]) / 1000 if "ms" in st and not until else None
        timeout = float(st.get("timeout_s", 30))
        baseline = self._frame(run, until.get("region"))[0] if until and "change" in until else None
        cursor = self.state_events("", 0)[1] if until and "state" in until and self.state_events else 0
        next_do = entered
        for _ in self._ticks(run, 1 / every if not hold_s else 50):
            now = time.perf_counter()
            if hold_s is not None and now - entered >= hold_s:
                return "next"
            if until and self._condition(run, until, baseline, cursor):
                return "next"
            if now - entered >= timeout:
                return "timeout"
            if do and now >= next_do - 0.005:  # on a schedule: a tick that lands a hair early still counts
                self._act(run, do)
                next_do = max(next_do + every, now + every * 0.5)
        raise _Stopped("the script ran out of time")

    def _run_track(self, run: Run, p: dict) -> None:
        if p.get("at"):
            return self._run_track_at(run, p)
        region = p.get("region")
        color, tol = p.get("color"), int(p.get("tol", 50))
        if not color:
            raise ValueError("track needs color [r, g, b], or at [x, y]")
        output, gain = p.get("output", "mouse"), float(p.get("gain", 0.6))
        within, hold = float(p.get("within", 10)), int(p.get("hold_frames", 5))
        aim, max_step = p.get("aim", "cursor"), float(p.get("max_step", 200))
        ox, oy = (region[0], region[1]) if region else (0, 0)
        on_target, errors, lost = 0, collections.deque(maxlen=600), 0
        for _ in self._ticks(run, p.get("hz", 60)):
            frame, _captured = self._frame(run, region)
            c = centroid(color_mask(frame, color, tol))
            if c is None:
                lost += 1
                run.stats["frames_without_target"] = lost
                if output != "mouse":
                    self.out.stick(output, 0.0, 0.0)
                continue
            tx, ty = c[0] + ox, c[1] + oy
            ax, ay = (self.cursor() if aim == "cursor" and self.cursor else aim)
            ex, ey = tx - ax, ty - ay
            err = (ex * ex + ey * ey) ** 0.5
            errors.append(err)
            run.stats["error_px"] = round(err, 1)
            run.stats["error_px_p50"] = round(sorted(errors)[len(errors) // 2], 1)
            if err <= within:
                on_target += 1
                if output != "mouse":
                    self.out.stick(output, 0.0, 0.0)
                if hold and on_target >= hold and not p.get("follow", False):
                    run.reason = "on target"
                    run.emit("on_target", error_px=round(err, 1))
                    return
                continue
            on_target = 0
            run.used.add("mouse" if output == "mouse" else "stick")
            if output == "mouse":
                dx = max(-max_step, min(max_step, ex * gain))
                dy = max(-max_step, min(max_step, ey * gain))
                self.out.mouse(int(round(dx)), int(round(dy)))
            else:  # a stick: deflection proportional to the error (screen px -> -1..1), y up is positive
                scale = float(p.get("full_deflection_px", 300))
                self.out.stick(output, max(-1.0, min(1.0, ex / scale * gain * 2)),
                               max(-1.0, min(1.0, -ey / scale * gain * 2)))
            run.stats["actions"] += 1


class _Stopped(Exception):
    pass


def defined_names(tree) -> set:
    """Every name the code defines anywhere (assignments, loops, defs, arguments, imports, except-as, walrus)."""
    import ast
    out: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
        elif type(node).__name__ in ("MatchAs", "MatchStar") and getattr(node, "name", None):
            out.add(node.name)
    return out


def free_names(tree, known) -> list:
    """Names the code uses that nothing defines (nor the API, nor Python): (name, line) in order of first use."""
    import ast
    import builtins
    defined, seen, out = defined_names(tree), set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            n = node.id
            if n in defined or n in known or hasattr(builtins, n) or n in seen:
                continue
            seen.add(n)
            out.append((n, node.lineno))
    return sorted(out, key=lambda x: x[1])


def preflight(tree, known, what: str = "program") -> None:
    """Fail before anything moves when the code uses a name nothing defines: a typo or an API it doesn't have."""
    import difflib
    unknown = free_names(tree, known)
    if unknown:
        name, line = unknown[0]
        hint = difflib.get_close_matches(name, [k for k in known if not k.startswith("_")] + sorted(defined_names(tree)),
                                         n=1, cutoff=0.7)
        more = f" (and {', '.join(n for n, _ in unknown[1:4])})" if len(unknown) > 1 else ""
        raise ValueError(f"{what} line {line}: unknown name {name!r}" + (f": did you mean {hint[0]!r}?" if hint else "")
                         + more)


class _Interrupted(_Stopped):
    def __init__(self, *args):
        super().__init__(*(args or ("interrupted: the program kept running without waiting",)))


def _interrupt(run: Run) -> None:
    """Raise _Interrupted inside a program's thread (for loops that never call wait/until)."""
    import ctypes
    if run.thread_id is not None:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(run.thread_id), ctypes.py_object(_Interrupted))


def _interrupt_after_deadline(run: Run, grace_s: float = 1.0) -> None:
    def watch():
        while run.state == "running" and not run.stop_evt.is_set() and time.perf_counter() < run.started + run.max_s:
            time.sleep(0.05)
        end = time.perf_counter() + grace_s  # waits notice the limit themselves; this is for loops that never wait
        while run.state == "running" and time.perf_counter() < end:
            time.sleep(0.05)
        if run.state == "running":
            _interrupt(run)
    threading.Thread(target=watch, name=f"watchdog-{run.id}", daemon=True).start()


def _frames(tb):
    import traceback
    return traceback.extract_tb(tb)


def _jsonable(value):
    import json
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)[:500]


class Program:
    """What a program may use (KINDS["program"]). Every wait checks the stop request, the time limit, a takeover and
    the program's guards, so a program can't outlive its limits or keep pressing after something went wrong."""

    NAMES: set = set()  # every name a program gets (filled in below the class)

    def __init__(self, rt: Runtime, run: Run, perception_only: bool = False):
        self.rt, self.run, self.perception_only = rt, run, perception_only
        self.state: dict = {"buttons": [], "left_stick": [0.0, 0.0], "right_stick": [0.0, 0.0],
                            "left_trigger": 0.0, "right_trigger": 0.0}
        self.guards: list[tuple[Callable, str]] = []
        self._guarding = False
        frame0, _ = rt._frame(run, None)
        self.h, self.w = frame0.shape[:2]
        self.app = (rt.app() if rt.app else None) or "unknown"
        self.profile = rt.profiles.load(self.app) if rt.profiles is not None else None
        self._camera = None

    def cam(self):
        """The calibrated camera of the app in front (control.Camera), driving the right stick; the held state
        (left stick, buttons) stays as it is, so it can walk and turn."""
        from . import control
        if self._camera is None:
            if not self.profile:
                raise RuntimeError(f"no camera profile for {self.app}: run behavior kind calibrate once (max_s 90)")
            io = control.PlantIO(frame=self.frame,
                                 stick=lambda x, y: self._apply({**self.state, "right_stick": [x, y]}),
                                 sleep=lambda seconds: self.wait(seconds * 1000), log=self.log)
            self._camera = control.Camera(io, self.profile)
        return self._camera

    def look_rate(self, yaw_dps: float = 0.0, pitch_dps: float = 0.0) -> None:
        self.state = {**self.state, "right_stick": list(self.cam().rates(yaw_dps, pitch_dps))}
        self._apply(self.state)

    def namespace(self) -> dict:
        import math
        seeing = {"elapsed": self.elapsed, "frame": self.frame, "diff": difference, "text": self.text,
                  "sees": self.sees, "color": self.color, "track": self.track, "shift": phase_shift, "log": self.log,
                  "W": self.w, "H": self.h, "CX": self.w // 2, "CY": self.h // 2, "np": _np(), "math": math,
                  "time": time, "numbers": self.numbers, "pixel_text": self.pixel_text, "grid_angle": self.grid_angle}
        if self.perception_only:  # guards watch; only their reflex program touches the controller
            return seeing
        return {**seeing, "pad": self.pad, "press": self.press, "seq": self.seq, "release": self.release,
                "wait": self.wait, "until": self.until, "aim": self.aim, "guard": self.guard, "skill": self.skill,
                "turn": lambda yaw=0.0, pitch=0.0, tol=1.0: self.cam().turn(yaw, pitch, tol),
                "level": lambda pitch=0.0: self.cam().level(pitch),
                "look_at": lambda x, y, **kw: self.cam().look_at(x, y, **kw),
                "scan": lambda score, degrees=360.0: self.cam().scan(score, degrees),
                "look_rate": self.look_rate, "camera": self._summary(),
                "set_pitch": lambda degrees: self.cam().set_pitch(degrees)}

    def skill(self, name: str, **inputs):
        """Run a saved skill here (same controller state and guards) and return its result."""
        import ast
        if self.rt.skills is None:
            raise RuntimeError("no skill library")
        saved = self.rt.skills.get(self.app, name)
        if not saved:
            raise ValueError(f"no skill {name!r} for {self.app}")
        missing = [n for n in saved.get("inputs") or [] if n not in inputs]
        if missing:
            raise ValueError(f"skill {saved['name']} needs {', '.join(missing)}")
        ns = {**self.namespace(), **inputs}
        tree = ast.parse(saved["code"], f"<skill {saved['name']}>")
        outcome = "done"
        try:
            exec(compile(tree, f"<skill {saved['name']}>", "exec"), ns)
        except Exception as e:
            outcome = f"{type(e).__name__}: {e}"
            raise
        finally:
            self.rt.skills.record(self.app, saved["name"], outcome)
        return ns.get("result")

    def _summary(self):
        from . import control
        return control.summary(self.profile) if self.profile else None

    # --- limits ---------------------------------------------------------------------------------------------------

    def check(self) -> None:
        run = self.run
        if run.stop_evt.is_set():
            raise _Stopped(run.stop_reason or "stopped on request")
        if time.perf_counter() >= run.started + run.max_s:
            raise _Stopped(f"time limit ({run.max_s:g} s)")
        who = self.rt.takeover()
        if who:
            raise _Stopped(f"you took over ({who})")
        if self.guards and not self._guarding:
            self._guarding = True
            try:
                for cond, name in self.guards:
                    if cond():
                        run.emit("guard", name=name)
                        raise _Stopped(f"guard: {name}")
            finally:
                self._guarding = False

    def guard(self, cond: Callable, name: str = "guard") -> None:
        self.guards.append((cond, str(name)))

    def elapsed(self) -> float:
        return round(time.perf_counter() - self.run.started, 3)

    def log(self, msg="", **data) -> None:
        self.run.emit("log", msg=str(msg), **{k: _jsonable(v) for k, v in data.items()})

    # --- controller -----------------------------------------------------------------------------------------------

    def _apply(self, state: dict) -> None:
        self.run.used.add("hold")
        self.rt.out.hold(state)
        self.run.stats["actions"] += 1

    def pad(self, buttons=(), ls=(0, 0), rs=(0, 0), lt: float = 0.0, rt: float = 0.0) -> None:
        self.check()
        self.state = {"buttons": [buttons] if isinstance(buttons, str) else list(buttons),
                      "left_stick": [float(ls[0]), float(ls[1])], "right_stick": [float(rs[0]), float(rs[1])],
                      "left_trigger": float(lt), "right_trigger": float(rt)}
        self._apply(self.state)

    def release(self) -> None:
        self.pad()

    def press(self, *buttons, ms: float = 80) -> None:
        self.check()
        self._apply({**self.state, "buttons": list(self.state["buttons"]) + list(buttons)})
        try:
            self.wait(ms)
        finally:
            self._apply(self.state)

    def seq(self, steps: list) -> None:
        """Timed gamepad_sequence steps (runs to the end), then back to the held state."""
        self.check()
        self.run.used.add("hold")
        self.rt.out.seq(steps)
        self._apply(self.state)

    # --- time -----------------------------------------------------------------------------------------------------

    def wait(self, ms: float) -> None:
        end = time.perf_counter() + max(0.0, float(ms)) / 1000
        while True:
            self.check()
            left = end - time.perf_counter()
            if left <= 0.004:
                break
            if self.run.stop_evt.wait(min(left - 0.002, 1 / 30)):
                continue
        sleep_until(end)

    def until(self, cond: Callable, timeout: float = 5.0, hz: float = 30):
        end = time.perf_counter() + float(timeout)
        period_ms = 1000 / max(1.0, min(float(hz), 120.0))
        while True:
            self.check()
            value = cond()
            if value:
                return value
            if time.perf_counter() >= end:
                return None
            self.wait(period_ms)

    # --- screen ---------------------------------------------------------------------------------------------------

    def frame(self, region=None):
        return self.rt._frame(self.run, region)[0]

    def pixel_text(self, region, font: str = "minecraft", threshold: int = 245) -> str:
        """Text drawn in a game's pixel font, read exactly ("?" for glyphs the font table doesn't have)."""
        from . import pixfont
        return pixfont.read(self.frame(region), font, threshold)

    def numbers(self, region, font: str = "minecraft", threshold: int = 245) -> list:
        """Whole numbers in a HUD region drawn in a pixel font (after its last ":"), e.g. [x, y, z]."""
        from . import pixfont
        return pixfont.numbers(self.pixel_text(region, font, threshold))

    def grid_angle(self, region=None) -> tuple:
        """(degrees, strength): how far the picture's straight edges are turned off square, modulo 90."""
        from .control import grid_angle
        return grid_angle(self.frame(region))

    def _lines(self, region):
        if not self.rt.read_text:
            raise RuntimeError("text needs OCR")
        return self.rt.read_text(region)

    def text(self, region=None) -> list:
        return [[ln["text"], *(int(v) for v in center_of(ln["box"]))] for ln in self._lines(region)]

    def sees(self, query: str, region=None):
        from .ocr import find
        hit = find(self._lines(region), str(query))
        return [int(v) for v in center_of(hit["box"])] if hit else None

    def color(self, rgb, region=None, tol: int = 40) -> float:
        return float(color_mask(self.frame(region), rgb, tol).mean())

    def track(self, x: float, y: float, size: int = 120):
        program, aimer = self, Aimer(self.frame(), (x, y), size)

        class Tracker:
            def find(self):
                return aimer.find(program.frame())
        return Tracker()

    def aim(self, x: float, y: float, within: float = 24, timeout: float = 3.0, until: Callable | None = None,
            size: int = 120, gain: float = 0.8, min_deflection: float | None = None, full_deflection_px: float = 500,
            min_score: float = 0.45, hz: float = 30) -> dict:
        if min_deflection is None:  # just past the calibrated deadzone, if this app was calibrated
            dz = ((self.profile or {}).get("look") or {}).get("deadzone")
            min_deflection = min(0.8, dz + 0.05) if dz else 0.22
        aimer = Aimer(self.frame(), (x, y), size)
        steer = Steer(full_deflection_px, gain, within, min_deflection)
        end = time.perf_counter() + float(timeout)
        period_ms = 1000 / max(1.0, min(float(hz), 120.0))
        on_target, out = 0, {"on_target": False, "error_px": None, "match": 0.0}
        try:
            while True:
                self.check()
                tx, ty, score = aimer.find(self.frame(), refresh=on_target == 0)
                out["match"] = round(score, 2)
                rs = [0.0, 0.0]
                if score >= min_score:
                    ex, ey = tx - self.w / 2, ty - self.h / 2
                    err = (ex * ex + ey * ey) ** 0.5
                    out.update(error_px=round(err, 1), target=[int(tx), int(ty)])
                    if err <= within:
                        on_target += 1
                        steer.still()
                    else:
                        on_target = 0
                        rs = list(steer(tx, ty, ex, ey))
                else:
                    steer.still()
                out["on_target"] = on_target > 0
                self._apply({**self.state, "right_stick": rs})
                if until is not None:
                    if until():
                        out["until"] = True
                        break
                elif on_target >= 3:
                    break
                if time.perf_counter() >= end:
                    out["timed_out"] = True
                    break
                self.wait(period_ms)
        finally:
            self._apply(self.state)
        out["min_deflection"] = round(steer.nudge, 2)  # what this game needed: pass it next time
        return out


Program.NAMES = {"elapsed", "frame", "diff", "text", "sees", "color", "track", "shift", "log", "W", "H", "CX", "CY",
                 "np", "math", "time", "numbers", "pixel_text", "grid_angle", "pad", "press", "seq", "release", "wait", "until", "aim", "guard", "skill",
                 "turn", "level", "look_at", "scan", "look_rate", "camera", "set_pitch"}


def center_of(box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def best_line(lines: list[dict], want: str) -> dict | None:
    """The OCR line for a menu item: exact, then starts-with, then contains, then a close OCR misreading (case and
    spacing ignored; see ocr.find)."""
    from .ocr import find
    return find(lines, want)


def _dominant(pixels) -> float:
    """The most common brightness in a patch (quantized to 16 levels): its background, however much text is on it."""
    np = _np()
    if pixels.size == 0:
        return 0.0
    q = (pixels.max(axis=-1) // 16).ravel()
    return float(np.bincount(q, minlength=16).argmax() * 16 + 8)


def highlighted(lines: list[dict], frame, region=None, min_contrast: float = 25.0) -> dict | None:
    """The menu item that stands out, or None when that isn't clear (never guess: navigate confirms on it).

    With three or more items the highlight is the odd one out among the items' backgrounds, and it must stand apart
    from every other item. With one or two, an item counts only if it differs from the menu's own background."""
    if not lines or frame is None:
        return None
    np = _np()
    ox, oy = (region[0], region[1]) if region else (0, 0)
    h, w = frame.shape[:2]
    covered = np.zeros((h, w), bool)
    levels = []
    for ln in lines:
        x0, y0, x1, y1 = ln["box"]
        pad = max(2, int((y1 - y0) * 0.35))
        a, b = max(0, int(x0 - ox) - pad), min(w, int(x1 - ox) + pad)
        c, d = max(0, int(y0 - oy) - pad), min(h, int(y1 - oy) + pad)
        levels.append(_dominant(frame[c:d, a:b, :3]))
        covered[c:d, a:b] = True
    if len(levels) >= 3:
        typical = float(np.median(levels))
        devs = sorted(((abs(v - typical), i) for i, v in enumerate(levels)), reverse=True)
        (top, i), (second, _) = devs[0], devs[1]
        return lines[i] if top >= min_contrast and top - second >= min_contrast / 2 else None
    background = _dominant(frame[..., :3][~covered]) if (~covered).any() else None
    if background is None:
        return None
    standing_out = [i for i, v in enumerate(levels) if abs(v - background) >= min_contrast]
    return lines[standing_out[0]] if len(standing_out) == 1 else None


def run_tool(rt: Runtime, action: str, kind: str = "", params: dict | None = None, run_id: str = "",
             since: int = 0, max_s: float = 30.0) -> dict:
    action = (action or "").lower()
    if action in ("save", "skills", "forget"):
        return skill_action(rt, action, kind, params or {})
    if action == "start":
        if kind == "program" and (params or {}).get("skill"):  # a wrong name or missing input fails here, not later
            if rt.skills is None:
                raise RuntimeError("no skill library")
            app = (rt.app() if rt.app else None) or "unknown"
            saved = rt.skills.get(app, str(params["skill"]))
            if not saved:
                raise ValueError(f"no skill {params['skill']!r} for {app} (action skills lists them)")
            missing = [n for n in saved.get("inputs") or [] if n not in (params.get("args") or {})]
            if missing:
                raise ValueError(f"skill {saved['name']} needs {', '.join(missing)} (params.args)")
        started = rt.start(kind, params, max_s)
        return rt.wait(started["id"]) if (params or {}).get("wait") else started
    if action == "status":
        return rt.status(run_id, since)
    if action == "stop":
        return {"stopped": rt.stop(run_id)}
    if action == "kinds":
        return {"kinds": KINDS, "actions": ACTIONS,
                "limits": {"max_s": MAX_SECONDS, "guard_max_s": GUARD_MAX_SECONDS, "at_once": MAX_RUNS}}
    raise ValueError("action must be start, status, stop, kinds, save, skills or forget")


def skill_action(rt: Runtime, action: str, name: str, params: dict) -> dict:
    import ast
    if rt.skills is None:
        raise RuntimeError("no skill library")
    app = (rt.app() if rt.app else None) or "unknown"
    if action == "skills":
        return {"app": app, "skills": rt.skills.list(app)}
    if action == "forget":
        return {"forgot": rt.skills.forget(app, name)}
    code = str(params.get("code") or "")
    try:
        tree = ast.parse(code, "<skill>")
    except SyntaxError as e:
        raise ValueError(f"skill line {e.lineno}: {e.msg}") from None
    if not code.strip():
        raise ValueError("save needs params.code")
    inputs = [n for n, _ in free_names(tree, Program.NAMES)]  # what callers pass in (params.args, or skill(X=...))
    saved = rt.skills.save(app, name, code, str(params.get("description") or ""), inputs, bool(params.get("common")))
    return {"saved": saved["name"], "app": saved["app"], "inputs": inputs or None}
