"""Camera control the robotics way: identify the plant, estimate ego-motion from pixels, close the loop in degrees.

Whatever is on the other end (Minecraft, a shooter, an FPV drone's feed, a robot's camera) is treated as an unknown
plant with a camera: stick deflections go in, pixels come out. Nothing here knows which game it is.

- System identification (`calibrate`): probe the look stick once and learn which pixels are a static overlay (HUD,
  crosshair, held item), the input-to-picture latency, the acceleration ramp, the response curve (deflection -> turn
  rate, with its deadzone), pixels per degree (turn all the way round until the view comes back), the vertical gain
  (and whether it's inverted) and the bottom pitch limit. Saved per app as a profile.
- Visual odometry (`Odometry`): how far the camera turned, from how the middle of the picture moved (keyframe phase
  correlation, overlay masked, sub-pixel): a gyro made of pixels.
- Control (`Camera`): turns in degrees, closed on odometry, with a speed profile that fits the measured acceleration
  and leads by the measured latency, fed forward through the inverse response curve (so deadzones and expo curves
  don't matter), finishing with short pulses; `level` uses the pitch limit as a reference; `look_at` feeds forward
  with the camera model, then corrects on what it sees (visual servoing); `scan` turns round scoring each view.

Frames are BGRA numpy arrays of the whole screen; positions are screen pixels.
"""

from __future__ import annotations

import base64
import collections
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Callable

MIN_PEAK = 0.03      # phase-correlation peaks below this are noise (a flat or changed view)
WORK_WIDTH = 480     # frames are downscaled to about this width for estimation


def _np():
    import numpy as np
    return np


def scale_for(width: int) -> int:
    return max(1, int(round(width / WORK_WIDTH)))


def gray(frame, k: int):
    np = _np()
    f = frame[::k, ::k, :3].astype(np.float32)
    return f[..., 2] * 0.299 + f[..., 1] * 0.587 + f[..., 0] * 0.114  # BGRA


def central(a):
    """The middle half of the picture both ways: rotation moves it nearly uniformly (the edges move up to 2x faster)."""
    h, w = a.shape[:2]
    return a[h // 4:h - h // 4, w // 4:w - w // 4]


def _hanning(shape):
    np = _np()
    return np.outer(np.hanning(shape[0]), np.hanning(shape[1])).astype(np.float32)


def spectrum(a, weight, total: float | None = None):
    """The weighted, mean-removed spectrum of a picture, for shift_between (cache it for a picture used often)."""
    np = _np()
    total = float(weight.sum()) + 1e-6 if total is None else total
    return np.fft.rfft2((a - float((a * weight).sum()) / total) * weight)


def image_shift(a, b, weight) -> tuple[float, float, float]:
    """(dx, dy, peak): how far picture b is shifted from picture a (same shape), weighted (0 = ignore), sub-pixel."""
    total = float(weight.sum()) + 1e-6
    return shift_between(spectrum(a, weight, total), spectrum(b, weight, total), a.shape)


def shift_between(fa, fb, shape) -> tuple[float, float, float]:
    np = _np()
    cross = fb * np.conj(fa)
    r = np.fft.irfft2(cross / (np.abs(cross) + 1e-9), s=shape)
    h, w = r.shape
    y, x = np.unravel_index(int(np.argmax(r)), r.shape)
    peak = float(r[y, x])

    def vertex(c0, before, after):
        d = before - 2 * c0 + after
        return 0.0 if abs(d) < 1e-12 else max(-0.5, min(0.5, 0.5 * (before - after) / d))

    dx = x + vertex(peak, r[y, (x - 1) % w], r[y, (x + 1) % w])
    dy = y + vertex(peak, r[(y - 1) % h, x], r[(y + 1) % h, x])
    return (dx - w if dx > w / 2 else dx), (dy - h if dy > h / 2 else dy), peak


# ---------------------------------------------------------------------------------------------------- overlay

class Overlay:
    """Pixels that are drawn over the world (HUD, crosshair, the held item): they stay put while the view turns."""

    def __init__(self, mask, k: int):
        self.mask, self.k = mask, k  # bool (h, w) at 1/k scale, True = overlay

    @classmethod
    def learn(cls, frames: list, k: int) -> "Overlay | None":
        """From frames taken while the view turned; None if nothing moved."""
        np = _np()
        g = np.stack([gray(f, k) for f in frames])
        spread = g.std(axis=0)
        moving = float(np.percentile(spread, 75))
        if moving < 2.0:
            return None
        still = spread < max(1.5, 0.08 * moving)
        grown = still.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            grown |= np.roll(still, (dy, dx), axis=(0, 1))
        return cls(grown, k)

    def weight(self):
        """1 for the world, 0 for the overlay, with softened edges (for correlation)."""
        np = _np()
        w = (~self.mask).astype(np.float32)
        acc = w.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            acc += np.roll(w, (dy, dx), axis=(0, 1))
        return acc / 5.0

    def fraction(self) -> float:
        return float(self.mask.mean())

    def covers(self, x: float, y: float, size: int = 120) -> float:
        """How much of the box around a screen point is overlay (0-1)."""
        k, half = self.k, max(1, size // (2 * self.k))
        cx, cy = int(x) // k, int(y) // k
        box = self.mask[max(0, cy - half):cy + half, max(0, cx - half):cx + half]
        return float(box.mean()) if box.size else 0.0

    def to_json(self) -> dict:
        np = _np()
        return {"k": self.k, "shape": list(self.mask.shape),
                "bits": base64.b64encode(np.packbits(self.mask.astype(np.uint8))).decode()}

    @classmethod
    def from_json(cls, d: dict) -> "Overlay":
        np = _np()
        h, w = d["shape"]
        bits = np.unpackbits(np.frombuffer(base64.b64decode(d["bits"]), np.uint8))[:h * w]
        return cls(bits.reshape(h, w).astype(bool), int(d["k"]))


# ---------------------------------------------------------------------------------------------------- odometry

class Odometry:
    """How far the camera has turned since it was made, from the picture alone. x, y: how far the picture moved, in
    screen px (turning right moves it left, looking up moves it down): yaw = -x / px_per_deg, pitch = y /
    px_per_deg.

    Robust global motion: a grid of tiles over the middle of the picture, each phase-correlated with the same tile
    of a keyframe; tiles that touch the overlay are skipped (a static crosshair pulls a whole-picture correlation
    toward "no motion", masked or not), and the median of the confident tiles wins (a mob walking through one tile,
    or a tile of plain sky, doesn't matter). Keyframes are re-taken as the view moves on, so slow turns add up
    exactly. With focal_px (from calibration), each tile is corrected for perspective (a pinhole camera's edges
    move faster than its middle)."""

    GRID = (3, 3)  # columns, rows over the middle 60% x 70% of the picture

    def __init__(self, frame, overlay: Overlay | None = None, focal_px: float | None = None):
        np = _np()
        self.k = scale_for(frame.shape[1])
        g = gray(frame, self.k)
        h, w = g.shape
        cols, rows = self.GRID
        x0, x1, y0, y1 = int(w * 0.2), int(w * 0.8), int(h * 0.15), int(h * 0.85)
        tw, th = (x1 - x0) // cols, (y1 - y0) // rows
        usable = overlay is not None and overlay.mask.shape == g.shape
        tiles = []
        for r in range(rows):
            for c in range(cols):
                tx, ty = x0 + c * tw, y0 + r * th
                if usable and overlay.mask[ty:ty + th, tx:tx + tw].mean() > 0.02:
                    continue
                gain_x = gain_y = 1.0
                if focal_px:
                    fx = focal_px / self.k
                    cx, cy = tx + tw / 2 - w / 2, ty + th / 2 - h / 2
                    gain_x, gain_y = 1 + (cx / fx) ** 2, 1 + (cy / fx) ** 2
                tiles.append((tx, ty, gain_x, gain_y))
        if len(tiles) < 2:  # an overlay over most of the middle: use every tile rather than none
            tiles = [(x0 + c * tw, y0 + r * th, 1.0, 1.0) for r in range(rows) for c in range(cols)]
        self.tiles, self.tw, self.th = tiles, tw, th
        self.window = _hanning((th, tw))
        self._total = float(self.window.sum()) + 1e-6
        self._np = np
        self._key(g)
        self.base, self.cur = [0.0, 0.0], [0.0, 0.0]
        self.peak, self.used, self.lost = 1.0, len(tiles), 0
        self.updates, self.lost_total = 0, 0  # measured frames, and how many of them couldn't be measured
        self.tile_shifts: list = []  # (tile x, tile y, raw dx, raw dy) from the last measurement, for calibration
        self.size = (w, h)
        self.samples: collections.deque = collections.deque(maxlen=64)
        self.samples.append((time.perf_counter(), 0.0, 0.0))
        self._last = frame

    def _key(self, g) -> None:
        """Make g the keyframe: its tiles' spectra are computed once, not on every frame."""
        self.ref = g
        self._ref_tiles = []
        for tx, ty, _gx, _gy in self.tiles:
            a = g[ty:ty + self.th, tx:tx + self.tw]
            flat = float(a.std()) < 2.0  # plain sky or a flat wall: nothing to lock onto
            self._ref_tiles.append(None if flat else spectrum(a, self.window, self._total))

    @property
    def x(self) -> float:
        return (self.base[0] + self.cur[0]) * self.k

    @property
    def y(self) -> float:
        return (self.base[1] + self.cur[1]) * self.k

    def _measure(self, g, pred: tuple[float, float]) -> tuple[float, float, float, int] | None:
        """Shift of frame g from the keyframe (working px), searching where the motion so far says it should be:
        tile correlation only works for small shifts, so each tile is cut from g at the predicted place and only
        the rest is measured. None unless most tiles agree."""
        np = self._np
        h, w = g.shape
        ix, iy = int(round(pred[0])), int(round(pred[1]))
        found = []
        for (tx, ty, gx, gy), fa in zip(self.tiles, self._ref_tiles):
            bx, by = tx + ix, ty + iy
            if fa is None or bx < 0 or by < 0 or bx + self.tw > w or by + self.th > h:
                continue
            fb = spectrum(g[by:by + self.th, bx:bx + self.tw], self.window, self._total)
            rx, ry, peak = shift_between(fa, fb, (self.th, self.tw))
            found.append(((ix + rx) / gx, (iy + ry) / gy, peak, tx, ty, ix + rx, iy + ry))
        if len(found) < 2:
            return None
        arr = np.array(found)
        mx, my = float(np.median(arr[:, 0])), float(np.median(arr[:, 1]))
        # agreement: 1.5 px, plus 20% for perspective before calibration measured it (edges move faster)
        agree = (np.abs(arr[:, 0] - mx) <= 1.5 + 0.2 * abs(mx)) & (np.abs(arr[:, 1] - my) <= 1.5 + 0.2 * abs(my))
        if int(agree.sum()) < max(2, (len(found) + 1) // 2):
            return None
        good = arr[agree]
        self.tile_shifts = [(int(r[3]), int(r[4]), float(r[5]), float(r[6])) for r in good]
        return float(good[:, 0].mean()), float(good[:, 1].mean()), float(good[:, 2].mean()), int(agree.sum())

    def _coarse(self, g) -> tuple[float, float]:
        """A rough whole-picture shift (half resolution, overlay not excluded): only a hypothesis for the tiles to
        confirm, for when frames come far apart."""
        a, b = central(self.ref[::2, ::2]), central(g[::2, ::2])
        dx, dy, _ = image_shift(a, b, _hanning(a.shape))
        return self.cur[0] + 2 * dx, self.cur[1] + 2 * dy

    def update(self, frame) -> tuple[float, float]:
        now = time.perf_counter()
        if frame is self._last:  # no new frame yet (capture hands back the same image): nothing to measure, but
            self.samples.append((now, self.x, self.y))  # the time counts: a view that stopped reads as stopped
            return self.x, self.y
        self._last = frame
        self.updates += 1
        g = gray(frame, self.k)
        vx, vy = self.rate(0.05)
        gap = now - self.samples[-1][0]
        dt = min(gap, 0.05)
        px, py = self.cur[0] + vx / self.k * dt, self.cur[1] + vy / self.k * dt
        cx, cy = self.cur
        # the motion so far, then one axis stopping (a pitch limit, a released stick), then no motion at all
        guesses = [(px, py), (px, cy), (cx, py), (cx, cy)]
        got = None
        for pred in guesses + [None]:
            got = self._measure(g, pred if pred is not None else self._coarse(g))
            if got is not None:
                break
        if got is None:
            self.lost += 1
            self.lost_total += 1
            self.peak, self.used = 0.0, 0
            if gap < 0.1:
                self.cur = list(guesses[0])  # dead reckoning across a frame or two
        else:
            dx, dy, self.peak, self.used = got
            self.cur, self.lost = [dx, dy], 0
        h, w = g.shape
        if self.lost > 3 or abs(self.cur[0]) > 0.18 * w or abs(self.cur[1]) > 0.12 * h:
            self.base = [self.base[0] + self.cur[0], self.base[1] + self.cur[1]]
            self.cur, self.lost = [0.0, 0.0], 0
            self._key(g)
        self.samples.append((now, self.x, self.y))
        return self.x, self.y

    def health(self) -> float:
        """The fraction of frames it could measure (1 = all)."""
        return 1.0 - self.lost_total / max(1, self.updates)

    def rate(self, span: float = 0.1) -> tuple[float, float]:
        """Picture motion (screen px/s) over about the last `span` seconds."""
        t1, x1, y1 = self.samples[-1]
        t0, x0, y0 = self.samples[0]
        for t, x, y in reversed(self.samples):
            t0, x0, y0 = t, x, y
            if t1 - t >= span:
                break
        dt = max(1e-3, t1 - t0)
        return (x1 - x0) / dt, (y1 - y0) / dt


# ---------------------------------------------------------------------------------------------------- response curve

class ResponseCurve:
    """Stick deflection (0-1) -> turn rate (deg/s), as measured, with its inverse for feedforward."""

    def __init__(self, points: list):
        np = _np()
        pts = sorted((float(d), max(0.0, float(r))) for d, r in points if 0 < float(d) <= 1)
        d = np.array([0.0] + [p[0] for p in pts])
        r = np.maximum.accumulate(np.array([0.0] + [p[1] for p in pts]))
        self.d, self.r = d, r
        self.max_rate = float(r[-1])
        moving = r > 0.03 * max(self.max_rate, 1e-9)
        i = int(np.argmax(moving)) if moving.any() else len(r) - 1
        self.deadzone = float(d[max(0, i - 1)])
        self.min_rate = float(r[i])  # the slowest turn seen: asking for less does nothing in the game
        self._inv_d, self._inv_r = [self.deadzone], [0.0]
        for dd, rr in zip(d[i:], r[i:]):
            if rr > self._inv_r[-1] + 1e-6:
                self._inv_d.append(float(dd))
                self._inv_r.append(float(rr))

    def rate(self, deflection: float) -> float:
        np = _np()
        return math.copysign(float(np.interp(abs(deflection), self.d, self.r)), deflection)

    def deflection(self, rate: float) -> float:
        np = _np()
        if rate == 0 or self.max_rate <= 0:
            return 0.0
        want = max(min(abs(rate), self.max_rate), self.min_rate)
        return math.copysign(float(np.interp(want, self._inv_r, self._inv_d)), rate)


# ---------------------------------------------------------------------------------------------------- profiles

class ProfileStore:
    """Calibration profiles, one JSON file per app (by process name)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    @staticmethod
    def key(app: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", (app or "").lower().removesuffix(".exe")).strip("-") or "default"

    def path(self, app: str) -> Path:
        return self.folder / f"{self.key(app)}.json"

    def load(self, app: str) -> dict | None:
        try:
            return json.loads(self.path(app).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, app: str, profile: dict) -> Path:
        path = self.path(app)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile), encoding="utf-8")
        return path


def summary(profile: dict) -> dict:
    """The profile without the overlay bitmap (what a model needs to see)."""
    look = profile.get("look", {})
    return {"app": profile.get("app"), "px_per_deg": profile.get("px_per_deg"),
            "deadzone": look.get("deadzone"), "max_deg_s": look.get("max_deg_s"),
            "latency_ms": round(1000 * look.get("latency_s", 0)), "accel_ms": round(1000 * look.get("accel_s", 0)),
            "coast_ms": round(1000 * look.get("coast_s", 0)),
            "y_gain": look.get("y_gain"), "overlay": profile.get("overlay_fraction"),
            "curve": look.get("curve")}


# ---------------------------------------------------------------------------------------------------- plant I/O

class PlantIO:
    """What calibration and control need: frame() -> BGRA array; stick(x, y) sets the look stick (the rest of the
    controller as it was); sleep(s) waits, and raises if the caller should stop; log(msg, **data)."""

    def __init__(self, frame: Callable, stick: Callable, sleep: Callable, log: Callable = lambda *a, **k: None):
        self.frame, self.stick, self.sleep, self.log = frame, stick, sleep, log


def _steady_rate(trace: list, after: float) -> float:
    """Slope (units/s) of (t, value) samples taken after `after` seconds (least squares)."""
    np = _np()
    pts = [(t, v) for t, v in trace if t >= after]
    if len(pts) < 3:
        pts = trace[-3:]
    if len(pts) < 2:
        return 0.0
    t = np.array([p[0] for p in pts])
    v = np.array([p[1] for p in pts])
    tc = t - t.mean()
    den = float((tc * tc).sum())
    return 0.0 if den <= 0 else float((tc * (v - v.mean())).sum() / den)


def _trace(io: PlantIO, odo: Odometry, seconds: float, axis: int = 0, tiles: list | None = None) -> list:
    t0, out = time.perf_counter(), []
    while True:
        t = time.perf_counter() - t0
        if t >= seconds:
            return out
        odo.update(io.frame())
        out.append((time.perf_counter() - t0, -odo.x if axis == 0 else odo.y))
        if tiles is not None and odo.used:
            tiles.append(list(odo.tile_shifts))
        io.sleep(0.004)


def step_response(trace: list, k: int) -> tuple[float, float, float]:
    """(latency s, top speed px/s, ramp s) from a trace of a full-deflection step that started at t=0."""
    moved = next((t for t, v in trace if abs(v) > 2.5 * k), None)
    if moved is None:
        return 0.0, 0.0, 0.0
    tail = trace[-1][0]
    top = _steady_rate(trace, max(moved, tail - 0.35))
    ramp = 0.0
    for i in range(2, len(trace)):
        (t0, v0), (t1, v1) = trace[i - 2], trace[i]
        if t1 > t0 and abs(v1 - v0) / (t1 - t0) >= 0.9 * abs(top):
            ramp = max(0.0, (t0 + t1) / 2 - moved)
            break
    return moved, abs(top), ramp


def focal_from_flow(rows: list, k: int, size: tuple[int, int]) -> float | None:
    """A pinhole camera's focal length (screen px) from tile shifts seen during a pure yaw turn: a tile at x from
    the middle moves (1 + (x/f)^2) times as fast as the middle. None if the edges don't move faster (a flat view)."""
    np = _np()
    w = size[0]
    ratios, offsets = [], []
    for tiles in rows:
        by_col: dict[int, list] = {}
        for tx, _ty, dx, _dy in tiles:
            by_col.setdefault(tx, []).append(dx)
        cols = sorted(by_col)
        if len(cols) < 3:
            continue
        centre = float(np.median(by_col[cols[len(cols) // 2]]))
        if abs(centre) < 4:
            continue
        tw = cols[1] - cols[0]
        for c in (cols[0], cols[-1]):
            ratios.append(float(np.median(by_col[c])) / centre)
            offsets.append(abs(c + tw / 2 - w / 2))
    if len(ratios) < 5:
        return None
    gain, off = float(np.median(ratios)), float(np.median(offsets))
    return None if gain <= 1.02 else off / math.sqrt(gain - 1) * k


def _full_turn(io: PlantIO, overlay: Overlay, deflection: float, W: int, H: int, k: int,
               limit_s: float = 25.0, focal_px: float | None = None) -> float | None:
    """Pixels in a full turn: turn steadily until the view comes back (a patch of the first view reappears near
    where it was), adding up odometry on the way. None if it never comes back (a camera that can't turn round)."""
    from .behave import match_template
    first = io.frame()
    g0 = gray(first, k)
    half = max(8, 60 // k)
    best = None
    for fy in (0.35, 0.5, 0.65):  # the most textured candidate (not sky)
        py, px = int(H * fy) // k, (W // 2) // k
        patch = g0[py - half:py + half, px - half:px + half]
        if patch.size and (best is None or patch.std() > best[0]):
            best = (float(patch.std()), py, px, patch.copy())
    if best is None or best[0] < 4:
        return None
    _, py, px, patch = best
    band_top = max(0, py - half - 12)
    odo = Odometry(first, overlay, focal_px)
    io.stick(deflection, 0.0)
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < limit_s:
            frame = io.frame()
            odo.update(frame)
            turned = -odo.x
            if turned > 1.2 * W:
                band = gray(frame, k)[band_top:py + half + 12]
                x, _, score = match_template(band, patch)
                tx = (x + half) * k
                if score > 0.8 and abs(tx - px * k) < W / 8:
                    return turned + tx - px * k
            if turned > 60 * W:
                return None
            io.sleep(0.002)
        return None
    finally:
        io.stick(0.0, 0.0)


def _drive_to_limit(io: PlantIO, overlay: Overlay | None, y: float, top_px: float, limit_s: float = 4.0) -> list:
    """Hold the stick up or down until the picture stops moving (the pitch limit); returns the (t, y px) trace."""
    odo = Odometry(io.frame(), overlay)
    io.stick(0.0, y)
    t0, still, trace = time.perf_counter(), 0, []
    try:
        while time.perf_counter() - t0 < limit_s:
            odo.update(io.frame())
            t = time.perf_counter() - t0
            trace.append((t, odo.y))
            _, vy = odo.rate(0.08)
            still = still + 1 if t > 0.25 and abs(vy) < 0.03 * top_px and odo.used >= 2 else 0
            if still >= 6:
                break
            io.sleep(0.004)
        return trace
    finally:
        io.stick(0.0, 0.0)


def calibrate(io: PlantIO, points=(0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.85, 1.0), hold_s: float = 0.5,
              full_turn: bool = True, pitch: bool = True, app: str = "") -> dict:
    """Identify the look stick (see the module docstring). Takes ~20-30 s; the camera ends roughly level if pitch."""
    io.stick(0.0, 0.0)
    io.sleep(0.25)
    first = io.frame()
    H, W = first.shape[:2]
    k = scale_for(W)

    frames = []
    for d in (0.8, -0.8):
        io.stick(d, 0.0)
        end = time.perf_counter() + 0.8
        while time.perf_counter() < end:
            frames.append(io.frame())
            io.sleep(0.04)
    io.stick(0.0, 0.0)
    io.sleep(0.3)
    overlay = Overlay.learn(frames, k)
    if overlay is None:
        raise RuntimeError("the picture didn't move when the right stick did (is the game in front, in its "
                           "gameplay view, and is the right stick its camera?)")
    io.log("overlay", fraction=round(overlay.fraction(), 3))

    # Step response, at full deflection unless the picture then moves too fast to follow between frames (a fast
    # camera, or a slow capture): then at the fastest deflection that can be followed, which control never exceeds.
    for step_d in (1.0, 0.7, 0.5, 0.35):
        odo = Odometry(io.frame(), overlay)
        io.stick(step_d, 0.0)
        tiles: list = []
        trace = _trace(io, odo, 1.0, tiles=tiles)
        io.stick(0.0, 0.0)
        tail = _trace(io, odo, 0.6)  # after letting go: how far it coasts (latency, then slowing down)
        if odo.health() >= 0.9:
            break
        io.log("too fast to follow", deflection=step_d, measured=round(odo.health(), 2))
    else:
        raise RuntimeError("the picture moves too fast to follow even at a third of the stick (a very slow capture?)")
    latency, top_px, ramp = step_response(trace, k)
    if top_px <= 0:
        raise RuntimeError("no steady turn when the right stick was pushed")
    released_at = trace[-1][1]
    coast_s = max(latency, (tail[-1][1] - released_at) / top_px) if tail else latency + ramp / 3
    focal = focal_from_flow(tiles, odo.k, odo.size)
    io.log("step", deflection=step_d, latency_ms=round(latency * 1000), top_px_s=round(top_px),
           ramp_ms=round(ramp * 1000), coast_ms=round(coast_s * 1000), focal_px=focal and round(focal))

    window = max(hold_s, 2 * ramp + 0.25)
    curve_px = []
    for d in points:
        if d > step_d + 1e-9:
            break
        rates, health = [], 1.0
        for sign in (1, -1):
            odo = Odometry(io.frame(), overlay, focal)
            io.stick(sign * d, 0.0)
            tr = _trace(io, odo, window)
            io.stick(0.0, 0.0)
            rates.append(abs(_steady_rate(tr, window * 0.45)))
            health = min(health, odo.health())
            io.sleep(latency + 0.12)
        if health < 0.85:  # faster than it can follow: the curve (and control) stops below this
            io.log("too fast to follow", deflection=d, measured=round(health, 2))
            break
        curve_px.append((d, sum(rates) / 2))
    if not curve_px:
        raise RuntimeError("couldn't measure any turn rate")
    io.log("curve", px_s=[[d, round(r)] for d, r in curve_px])

    ppd = None
    if full_turn:
        want = 0.45 * top_px
        d_turn = min(curve_px, key=lambda p: abs(p[1] - want) if p[1] > 0.05 * top_px else 1e12)[0]
        px_round = _full_turn(io, overlay, d_turn, W, H, k, focal_px=focal)
        if px_round and px_round > W:
            ppd = px_round / 360.0
        io.log("full turn", px=round(px_round or 0), px_per_deg=ppd and round(ppd, 3))
        io.sleep(latency + 0.2)

    look: dict[str, Any] = {"stick": "right_stick", "latency_s": round(latency, 3), "accel_s": round(ramp, 3),
                            "coast_s": round(coast_s, 3), "y_gain": 1.0, "max_deflection": curve_px[-1][0]}
    profile: dict[str, Any] = {"app": app, "screen": [W, H], "px_per_deg": ppd and round(ppd, 4),
                               "focal_px": focal and round(focal, 1),
                               "overlay": overlay.to_json(), "overlay_fraction": round(overlay.fraction(), 3),
                               "pitch_bottom_deg": -90.0, "calibrated": time.strftime("%Y-%m-%d %H:%M"), "look": look}
    if ppd:
        look["curve"] = [[d, round(r / ppd, 2)] for d, r in curve_px]
        c = ResponseCurve(look["curve"])
        look.update(deadzone=round(c.deadzone, 3), max_deg_s=round(c.max_rate, 1))
    else:
        look["curve_px"] = [[d, round(r, 1)] for d, r in curve_px]

    if pitch and ppd:
        trace = _drive_to_limit(io, overlay, -step_d, top_px)  # the same deflection as the yaw step: compare rates
        moving = [(t, v) for t, v in trace if t <= trace[-1][0] - 0.1]
        vy = _steady_rate(moving, 0.3) if len(moving) > 4 else 0.0
        if abs(vy) > 0.05 * top_px:
            look["y_gain"] = round(math.copysign(abs(vy) / top_px, -vy), 3)  # stick down: picture moves up (-y)
        io.sleep(latency + 0.15)
        Camera(io, profile).turn(pitch=-profile["pitch_bottom_deg"], tol=1.5)  # back up to level
    return profile


# ---------------------------------------------------------------------------------------------------- control

class Camera:
    """Look control in degrees for a calibrated plant (see calibrate)."""

    def __init__(self, io: PlantIO, profile: dict):
        look = profile.get("look") or {}
        if not profile.get("px_per_deg") or not look.get("curve"):
            raise RuntimeError("this camera's profile has no degrees (calibration couldn't turn it all the way round)")
        self.io, self.profile = io, profile
        self.curve = ResponseCurve(look["curve"])
        self.ppd = float(profile["px_per_deg"])
        self.latency = float(look.get("latency_s", 0.05))
        self.accel = max(0.03, float(look.get("accel_s", 0.1)))
        self.y_gain = float(look.get("y_gain", 1.0)) or 1.0
        self.max_d = float(look.get("max_deflection", 1.0))  # faster than this, odometry can't follow
        # how far (in seconds of the current speed) it keeps turning after the stick lets go
        self.coast = float(look.get("coast_s") or (self.latency + self.accel / 3))
        ov = profile.get("overlay")
        self.overlay = Overlay.from_json(ov) if ov else None

    def odometry(self) -> Odometry:
        frame = self.io.frame()
        ov = self.overlay if self.overlay is not None and self.overlay.k == scale_for(frame.shape[1]) else None
        return Odometry(frame, ov, focal_px=self.profile.get("focal_px"))

    def _want(self, err: float, speed: float, tol: float) -> float:
        """The turn rate (deg/s) to ask for with `err` degrees to go, turning at `speed` now."""
        coming = abs(speed) * self.coast  # already on its way: it keeps turning this far after letting go
        left = abs(err) - coming
        if abs(err) <= tol or left <= 0:
            return 0.0
        decel = self.curve.max_rate / max(self.coast, 0.03)
        return math.copysign(min(self.curve.max_rate, math.sqrt(1.2 * decel * left), 5.0 * left), err)

    def _settle(self, odo: Odometry, timeout: float = 0.8) -> None:
        """Wait until the view has stopped (a servo's in-position check): the last input only shows after the
        latency, and then the camera slows down gradually."""
        io = self.io
        io.sleep(self.latency)
        t0, still = time.perf_counter(), 0
        while time.perf_counter() - t0 < timeout:
            odo.update(io.frame())
            vx, vy = odo.rate(0.04)
            still = still + 1 if math.hypot(vx, vy) < self.ppd else 0  # under 1 deg/s
            if still >= 3:
                return
            io.sleep(0.008)

    def turn(self, yaw: float = 0.0, pitch: float = 0.0, tol: float = 1.0, timeout: float = 6.0) -> dict:
        """Turn by yaw (right +) and pitch (up +) degrees, closed on odometry. Returns what it measured."""
        io, odo = self.io, self.odometry()
        t0, at_limit, still = time.perf_counter(), False, 0
        try:
            while time.perf_counter() - t0 < timeout:
                odo.update(io.frame())
                done_y, done_p = -odo.x / self.ppd, odo.y / self.ppd
                ey, ep = yaw - done_y, (0.0 if at_limit else pitch - done_p)
                if abs(ey) <= tol and abs(ep) <= tol:
                    break
                vx, vy = odo.rate(0.06)
                ry = self._want(ey, -vx / self.ppd, tol)
                rp = self._want(ep, vy / self.ppd, tol)
                if ry == 0 and rp == 0 and (abs(ey) > tol or abs(ep) > tol):
                    io.stick(0.0, 0.0)  # coasting in on the inputs already sent
                else:
                    io.stick(self.curve.deflection(ry), self.curve.deflection(rp / self.y_gain) if rp else 0.0)
                started = time.perf_counter() - t0 > self.latency + self.accel + 0.1  # (not moving yet is not a limit)
                still = still + 1 if started and rp and abs(vy) < 0.03 * self.curve.max_rate * self.ppd else 0
                if still >= 8:  # pushing the pitch and the picture doesn't move: a pitch limit
                    at_limit = True
                io.sleep(1 / 120)
            io.stick(0.0, 0.0)
            self._settle(odo)
            for _ in range(4):  # finish with short pulses sized to what's left, settling after each
                ey = yaw - (-odo.x / self.ppd)
                ep = 0.0 if at_limit else pitch - odo.y / self.ppd
                if abs(ey) <= tol and abs(ep) <= tol:
                    break
                for axis, err in ((0, ey), (1, ep)):
                    if abs(err) > tol:
                        want = min(0.3 * self.curve.max_rate, max(self.curve.min_rate, abs(err) / 0.15))
                        d = self.curve.deflection(math.copysign(want, err))
                        rate = max(abs(self.curve.rate(d)), 1e-6)  # what that deflection really turns
                        io.stick(d if axis == 0 else 0.0, (d / self.y_gain) if axis == 1 else 0.0)
                        io.sleep(min(0.4, abs(err) / rate))  # a lagging camera still turns rate x time in all
                        io.stick(0.0, 0.0)
                self._settle(odo)
        finally:
            io.stick(0.0, 0.0)
        got_y, got_p = -odo.x / self.ppd, odo.y / self.ppd
        out = {"yaw": round(got_y, 1), "pitch": round(got_p, 1),
               "error": round(max(abs(yaw - got_y), 0.0 if at_limit else abs(pitch - got_p)), 1)}
        if at_limit:
            out["pitch_limit"] = True
        return out

    def level(self, pitch: float = 0.0) -> dict:
        """Look straight ahead (or at `pitch` degrees): down to the pitch limit as a reference, then up."""
        top = self.curve.max_rate * self.ppd
        _drive_to_limit(self.io, self.overlay, -math.copysign(self.max_d, self.y_gain), top)
        self.io.sleep(self.latency + self.coast)
        return self.turn(pitch=pitch - float(self.profile.get("pitch_bottom_deg", -90.0)), tol=1.5)

    def angles_to(self, x: float, y: float, w: int, h: int) -> tuple[float, float]:
        """Yaw and pitch (degrees) to a screen point: a pinhole model with the measured focal length, or linear if
        calibration found a flat view."""
        f = self.profile.get("focal_px")
        if not f:
            return (x - w / 2) / self.ppd, -(y - h / 2) / self.ppd
        return math.degrees(math.atan2(x - w / 2, f)), -math.degrees(math.atan2(y - h / 2, f))

    def look_at(self, x: float, y: float, refine: bool = True, within: float = 12) -> dict:
        """Put screen point (x, y) under the crosshair: feed forward with the camera model, then correct on the
        picture (the patch that was there, found again)."""
        from .behave import Aimer
        frame = self.io.frame()
        h, w = frame.shape[:2]
        if self.overlay is not None and self.overlay.k == scale_for(w) and self.overlay.covers(x, y, 32) > 0.5:
            raise ValueError("that point is mostly overlay (HUD, crosshair or held item): pick a point in the world")
        aimer = Aimer(frame, (x, y), 120) if refine else None
        yaw, pitch = self.angles_to(x, y, w, h)
        out = self.turn(yaw, pitch)
        if aimer is not None:
            for _ in range(2):
                tx, ty, score = aimer.find(self.io.frame(), refresh=False)
                if score < 0.5:
                    out["refine"] = "lost it"
                    break
                err = math.hypot(tx - w / 2, ty - h / 2)
                out["off_px"] = round(err)
                if err <= within:
                    break
                dy, dp = self.angles_to(tx, ty, w, h)
                self.turn(dy, dp, tol=0.3)
        return out

    def rates(self, yaw_rate: float = 0.0, pitch_rate: float = 0.0) -> tuple[float, float]:
        """Stick deflections that turn at these rates (deg/s): feedforward only."""
        return (self.curve.deflection(yaw_rate) if yaw_rate else 0.0,
                self.curve.deflection(pitch_rate / self.y_gain) if pitch_rate else 0.0)

    def scan(self, score: Callable, degrees: float = 360.0, rate: float | None = None) -> dict:
        """Turn round (right) calling score(frame) on each view; then turn back to the best one. Returns its heading
        (degrees right of where the scan started) and score."""
        io, odo = self.io, self.odometry()
        speed = rate or min(90.0, 0.5 * self.curve.max_rate)
        seen: list[tuple[float, float]] = []
        io.stick(self.curve.deflection(speed), 0.0)
        t0 = time.perf_counter()
        try:
            while time.perf_counter() - t0 < degrees / speed * 2 + 3:
                frame = io.frame()
                odo.update(frame)
                heading = -odo.x / self.ppd
                if heading >= degrees:
                    break
                seen.append((heading, float(score(frame))))
                io.sleep(0.002)
        finally:
            io.stick(0.0, 0.0)
        self._settle(odo)
        if not seen:
            return {"heading": None, "score": None, "samples": 0}
        i = max(range(len(seen)), key=lambda j: seen[j][1])
        top = seen[i][1]
        lo = hi = i  # the middle of the plateau around the best view, not its first sample
        while lo > 0 and seen[lo - 1][1] >= 0.95 * top:
            lo -= 1
        while hi < len(seen) - 1 and seen[hi + 1][1] >= 0.95 * top:
            hi += 1
        best = (seen[lo][0] + seen[hi][0]) / 2
        back = (best - (-odo.x / self.ppd) + 180) % 360 - 180  # the short way round
        self.turn(yaw=back)
        return {"heading": round(best, 1), "score": round(top, 3), "samples": len(seen)}
