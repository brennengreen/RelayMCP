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
# Following the picture only works while it moves little between two captures: phase correlation wraps around at
# the tile size, so a big miss aliases to a small, consistent, wrong answer (measured: +44% at 41 degrees a frame,
# with every frame "tracked"). Calibration only admits stick deflections under this, and control stays below them.
SAFE_FRAME_FRACTION = 0.08   # of the picture's width per captured frame
LOOP_FRAME_FRACTION = 0.05   # the full turn goes slower still, so the first view is matched as it comes back


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

DEFAULT_HFOV_DEG = 90.0  # a camera's horizontal field of view until calibration measures its focal length


def flow(p, x, y, f):
    """Picture motion (u, v) at image offsets (x, y) from the middle, for a pinhole camera of focal length f turned by
    p = (pitch up, yaw right, roll) radians, to first order (the rotational optical flow of a camera)."""
    wx, wy, wz = p
    return (x * y / f * wx - (f + x * x / f) * wy + y * wz,
            (f + y * y / f) * wx - x * y / f * wy - x * wz)


def rotation_matrix(p):
    """The rotation (new camera axes in the old camera's coordinates) for rotation vector p (Rodrigues)."""
    np = _np()
    wx, wy, wz = (float(v) for v in p)
    k = np.array([[0.0, -wz, wy], [wz, 0.0, -wx], [-wy, wx, 0.0]])
    th = math.sqrt(wx * wx + wy * wy + wz * wz)
    if th < 1e-9:
        return np.eye(3) + k
    return np.eye(3) + math.sin(th) / th * k + (1 - math.cos(th)) / (th * th) * (k @ k)


def rotated(p, x, y, f):
    """Exact picture motion (u, v) at offsets (x, y) from the middle when a pinhole camera turns by rotation vector
    p: a pure rotation maps the picture by the homography K R^T K^-1, whatever the scene's depth."""
    np = _np()
    x, y = np.asarray(x, float), np.asarray(y, float)
    d = rotation_matrix(p).T @ np.stack([x, y, np.full_like(x, f)])
    return f * d[0] / d[2] - x, f * d[1] / d[2] - y


def _linear_rotation(a, f):
    """First-order least squares (small turns): the starting point for the exact fit."""
    np = _np()
    bx, by, bu, bv, bw = a.T
    m = np.zeros((2 * len(bx), 3))
    m[0::2, 0], m[0::2, 1], m[0::2, 2] = bx * by / f, -(f + bx * bx / f), by
    m[1::2, 0], m[1::2, 1], m[1::2, 2] = f + by * by / f, -bx * by / f, -bx
    rhs = np.empty(2 * len(bx))
    rhs[0::2], rhs[1::2] = bu, bv
    sw = np.sqrt(np.repeat(bw, 2))
    return np.linalg.lstsq(m * sw[:, None], rhs * sw, rcond=None)[0]


def _refine_rotation(p, a, f, steps: int = 4):
    """Gauss-Newton on the exact model (rotated), from p."""
    np = _np()
    x, y, u, v, w = a.T
    sw = np.sqrt(np.repeat(w, 2))
    for _ in range(steps):
        pu, pv = rotated(p, x, y, f)
        r = np.empty(2 * len(x))
        r[0::2], r[1::2] = pu - u, pv - v
        jac = np.empty((2 * len(x), 3))
        for j in range(3):
            dp = np.zeros(3)
            dp[j] = 1e-4
            qu, qv = rotated(p + dp, x, y, f)
            jac[0::2, j], jac[1::2, j] = (qu - pu) / 1e-4, (qv - pv) / 1e-4
        delta = np.linalg.lstsq(jac * sw[:, None], -r * sw, rcond=None)[0]
        p = p + delta
        if float(np.abs(delta).max()) < 1e-7:
            break
    return p


def fit_rotation(rows, f: float, max_rms: float = 1.5):
    """The camera rotation that best explains tile shifts. rows: (x, y, u, v, weight): a tile's offset from the middle
    and how far its content moved. Tiles that don't fit (a mob walking through, a wrong match) are dropped. Returns
    (rotation vector: pitch up, yaw right, roll; rms px; tiles used) or None when fewer than 3 tiles agree."""
    np = _np()
    a = np.asarray(rows, float)
    use = np.ones(len(a), bool)
    x, y, u, v, _w = a.T
    p = None
    for _ in range(4):
        if use.sum() < 3:
            return None
        p = _refine_rotation(_linear_rotation(a[use], f) if p is None else p, a[use], f)
        pu, pv = rotated(p, x, y, f)
        res = np.hypot(pu - u, pv - v)
        keep = res <= max(1.0, 3.0 * float(np.median(res[use])))
        if (keep == use).all():
            break
        use = keep
    if use.sum() < 3:
        return None
    rms = float(np.sqrt(np.mean(res[use] ** 2)))
    return (p, rms, int(use.sum())) if rms <= max_rms else None


def yaw_with_focal(segments, f: float) -> float:
    """Total yaw (radians) of recorded keyframe segments (rows, rotation, focal they were fitted with; odometry px),
    refitted as if the focal length were f: loop closure finds the f that makes a full turn exactly 2 pi."""
    total = 0.0
    for rows, p, f0 in segments:
        fit = fit_rotation(rows, f, max_rms=1e9) if rows else None
        # nothing measured since the keyframe: scale it, first order
        total += world_turn(fit[0])[0] if fit is not None else world_turn(p)[0] * f0 / f
    return total


def grid_angle(img, mask=None, smooth: int = 4) -> tuple[float, float]:
    """The orientation of a picture's straight edges modulo 90 degrees, in (-45, 45]: pixel-art textures seen square
    on (a voxel world's ground from straight above), floor tiles, a grid. A compass for grid worlds: looking straight
    down, it is how far the camera's yaw is off the world's axes. Angles follow the picture's axes (x right, y down):
    positive = the grid appears turned clockwise. Returns (degrees, strength 0-1: how much of the edge energy agrees;
    below ~0.2 there is no clear grid). smooth: average k x k blocks first (a turned edge is drawn as a staircase of
    single-pixel steps, whose own edges are square)."""
    np = _np()
    g = np.asarray(img, np.float64)
    if g.ndim == 3:
        g = g[..., :3].mean(axis=2)
    if smooth > 1:
        h, w = (g.shape[0] // smooth) * smooth, (g.shape[1] // smooth) * smooth
        g = g[:h, :w].reshape(h // smooth, smooth, w // smooth, smooth).mean(axis=(1, 3))
        if mask is not None:
            mask = np.asarray(mask, bool)[:h, :w].reshape(h // smooth, smooth, w // smooth, smooth).all(axis=(1, 3))
    # Scharr gradients: nearly the same response in every direction (plain differences bias the angle by degrees)
    gx = 3 * (g[:-2, 2:] - g[:-2, :-2]) + 10 * (g[1:-1, 2:] - g[1:-1, :-2]) + 3 * (g[2:, 2:] - g[2:, :-2])
    gy = 3 * (g[2:, :-2] - g[:-2, :-2]) + 10 * (g[2:, 1:-1] - g[:-2, 1:-1]) + 3 * (g[2:, 2:] - g[:-2, 2:])
    r2 = gx * gx + gy * gy
    if mask is not None:
        m = np.asarray(mask, bool)[1:-1, 1:-1]
        gx, gy, r2 = gx[m], gy[m], r2[m]
    total = float(r2.sum())
    if total <= 0:
        return 0.0, 0.0
    # r^2 e^(4i theta) = (gx^2 - gy^2 + 2i gx gy)^2 / r^2: a grid's edges at theta, theta + 90, ... all agree
    c, s = gx * gx - gy * gy, 2 * gx * gy
    ok = r2 > 1e-9
    zr = float(((c * c - s * s)[ok] / r2[ok]).sum())
    zi = float(((2 * c * s)[ok] / r2[ok]).sum())
    return math.degrees(math.atan2(zi, zr) / 4), math.hypot(zr, zi) / total


def world_turn(p, theta: float | None = None) -> tuple[float, float]:
    """(yaw right, pitch up) radians from a camera rotation vector, for cameras that turn about the world's vertical
    axis and tilt without rolling (first-person games, gimbals): yawing while tilted rolls the picture, so yaw is the
    size of the yaw-and-roll part (whatever the tilt), with the yaw part's sign. Looking nearly straight up or down a
    yaw is almost all roll and its yaw part is noise; then the camera's pitch theta (radians), if known, gives the
    sign (yaw = wy cos(theta) - wz sin(theta))."""
    wx, wy, wz = (float(v) for v in p)
    size = math.hypot(wy, wz)
    if theta is not None and abs(wz) > abs(wy):
        return math.copysign(size, wy * math.cos(theta) - wz * math.sin(theta)), wx
    return math.copysign(size, wy), wx


class Odometry:
    """A gyro made of pixels: how far the camera has turned since it was made, from the picture alone.

    Tiles over the middle of the picture (none touching the HUD) are phase-correlated with the same tiles of a
    keyframe, each searched where the current rotation estimate says it went; the camera rotation that explains
    their shifts (pinhole rotational flow: perspective, and the roll a tilted camera shows when it yaws) is fitted by
    robust least squares. Keyframes are re-taken every few degrees, so long turns add up exactly.

    yaw, pitch: radians turned (right, up); x, y: the same as picture motion in screen px at the middle of the
    picture (turning right moves it left): x = -yaw * f, y = pitch * f. theta: the camera's absolute pitch, estimated
    from how much the picture rolls while it yaws (roll/yaw = tan(pitch)), or None before any yaw."""

    GRID = (3, 3)  # columns, rows over the middle 60% x 70% of the picture
    REKEY = 0.07   # of the width: a new keyframe (first-order flow stays exact for small turns)

    def __init__(self, frame, overlay: Overlay | None = None, focal_px: float | None = None,
                 tolerance: float = 1.0):
        np = _np()
        self.tolerance = tolerance  # x the fit's allowed error (a focal length not measured yet fits less well)
        self.k = scale_for(frame.shape[1])
        g = gray(frame, self.k)
        h, w = g.shape
        cols, rows = self.GRID
        x0, x1, y0, y1 = int(w * 0.2), int(w * 0.8), int(h * 0.15), int(h * 0.85)
        tw, th = (x1 - x0) // cols, (y1 - y0) // rows
        usable = overlay is not None and overlay.mask.shape == g.shape
        cells = [(x0 + c * tw, y0 + r * th) for r in range(rows) for c in range(cols)]
        tiles = [(tx, ty) for tx, ty in cells if not (usable and overlay.mask[ty:ty + th, tx:tx + tw].mean() > 0.02)]
        if len(tiles) < 3:  # an overlay over most of the middle: use every tile rather than none
            tiles = cells
        self.tiles = [(tx, ty, tx + tw / 2 - w / 2, ty + th / 2 - h / 2) for tx, ty in tiles]
        self._cx = np.array([t[2] for t in self.tiles], float)
        self._cy = np.array([t[3] for t in self.tiles], float)
        self.tw, self.th, self.size = tw, th, (w, h)
        self.f = focal_px / self.k if focal_px else (w / 2) / math.tan(math.radians(DEFAULT_HFOV_DEG / 2))
        self.window = _hanning((th, tw))
        gy, gx = np.mgrid[0:th, 0:tw]
        self._offs = (gx - tw / 2.0, gy - th / 2.0)  # tile pixels about the tile's middle, for warped sampling
        self._total = float(self.window.sum()) + 1e-6
        half = g[::2, ::2]
        self._coarse_w = _hanning(half.shape)
        if usable:
            self._coarse_w = self._coarse_w * overlay.weight()[::2, ::2][:half.shape[0], :half.shape[1]]
        self._np = np
        self._key(g)
        self.p = np.zeros(3)       # rotation since the keyframe
        self.p_rate = np.zeros(3)  # per second, for predicting where tiles went
        self.base_yaw = self.base_pitch = 0.0
        self.theta: float | None = None
        self.peak, self.used, self.lost, self.rms = 1.0, len(self.tiles), 0, 0.0
        self.updates, self.lost_total = 0, 0
        self.breaks = 0  # times it lost track long enough to start again from a new keyframe (motion was dropped)
        self.born = time.perf_counter()
        self.tile_rows: list = []  # the last measurement's (x, y, u, v, weight) rows, for calibration
        self.p_rows: list = []  # the rows p was fitted from ([]: nothing measured since the keyframe)
        self.segments: list | None = None  # a list to record each keyframe's (rows, p, f) in (loop closure)
        self.samples: collections.deque = collections.deque(maxlen=64)
        self._t_meas = time.perf_counter()
        self.samples.append((self._t_meas, 0.0, 0.0))
        self._last = frame

    def _key(self, g) -> None:
        """Make g the keyframe: its tiles' spectra are computed once, not on every frame."""
        self.ref = g
        self._ref_tiles = []
        for tx, ty, _cx, _cy in self.tiles:
            a = g[ty:ty + self.th, tx:tx + self.tw]
            flat = float(a.std()) < 2.0  # plain sky or a flat wall: nothing to lock onto
            self._ref_tiles.append(None if flat else spectrum(a, self.window, self._total))

    @property
    def yaw(self) -> float:
        return self.base_yaw + world_turn(self.p, self.theta)[0]

    @property
    def pitch(self) -> float:
        return self.base_pitch + world_turn(self.p, self.theta)[1]

    @property
    def x(self) -> float:
        return -self.yaw * self.f * self.k

    @property
    def y(self) -> float:
        return self.pitch * self.f * self.k

    def _measure(self, g, pred):
        np = self._np
        h, w = g.shape
        rows = []
        pu, pv = rotated(pred, self._cx, self._cy, self.f)  # where each tile went, if the camera turned by pred
        # how each tile's neighbourhood is stretched and turned (the local warp): a roll, or yawing while looking
        # steeply down (mostly roll then), turns tile content, which a plain shift can't match
        ju, jv = rotated(pred, self._cx + 1.0, self._cy, self.f)
        ku, kv = rotated(pred, self._cx, self._cy + 1.0, self.f)
        reach = max(self.tw, self.th) / 2.0
        for i, ((tx, ty, cx, cy), fa, up, vp) in enumerate(zip(self.tiles, self._ref_tiles, pu, pv)):
            if fa is None:
                continue
            j = ((1.0 + ju[i] - up, ku[i] - up), (jv[i] - vp, 1.0 + kv[i] - vp))
            if max(abs(j[0][0] - 1), abs(j[0][1]), abs(j[1][0]), abs(j[1][1] - 1)) * reach < 1.0:
                ix, iy = int(round(up)), int(round(vp))
                bx, by = tx + ix, ty + iy
                if bx < 0 or by < 0 or bx + self.tw > w or by + self.th > h:
                    continue
                win, base, jac = g[by:by + self.th, bx:bx + self.tw], (ix, iy), None
            else:
                ox, oy = self._offs
                xs = np.rint(tx + self.tw / 2.0 + up + j[0][0] * ox + j[0][1] * oy).astype(int)
                ys = np.rint(ty + self.th / 2.0 + vp + j[1][0] * ox + j[1][1] * oy).astype(int)
                if xs.min() < 0 or ys.min() < 0 or xs.max() >= w or ys.max() >= h:
                    continue
                win, base, jac = g[ys, xs], (up, vp), j
            rx, ry, peak = shift_between(fa, spectrum(win, self.window, self._total), (self.th, self.tw))
            # near half a tile the answer may have wrapped around (a big miss read as a small one): don't trust it
            if peak >= 0.05 and abs(rx) <= 0.3 * self.tw and abs(ry) <= 0.3 * self.th:
                if jac is not None:  # a shift in the warped tile is a shift along the warp in the picture
                    rx, ry = jac[0][0] * rx + jac[0][1] * ry, jac[1][0] * rx + jac[1][1] * ry
                rows.append((cx, cy, base[0] + rx, base[1] + ry, peak))
        if len(rows) < 3:
            return None
        motion = float(self._np.median([math.hypot(r[2], r[3]) for r in rows]))
        # a model error (a slightly wrong focal length, a little parallax) grows with the motion it explains
        fit = fit_rotation(rows, self.f, max_rms=self.tolerance * (1.5 + 0.08 * motion))
        if fit is None or fit[2] < max(3, (len(rows) + 1) // 2):
            return None
        self.tile_rows = rows
        return fit

    def _coarse(self, g):
        """A rough whole-picture shift since the keyframe (half resolution, HUD masked out): only a guess for the
        tiles to confirm, for when frames come far apart. None if there's no clear answer."""
        np = self._np
        dx, dy, peak = image_shift(self.ref[::2, ::2], g[::2, ::2], self._coarse_w)
        if peak < MIN_PEAK:
            return None
        return np.array([2 * dy / self.f, -2 * dx / self.f, self.p[2]])

    def update(self, frame) -> tuple[float, float]:
        now = time.perf_counter()
        if frame is self._last:  # no new frame yet (capture hands back the same image): nothing to measure, but
            self.samples.append((now, self.x, self.y))  # the time counts: a view that stopped reads as stopped
            return self.x, self.y
        self._last = frame
        self.updates += 1
        g = gray(frame, self.k)
        gap = now - self._t_meas
        # where it went at the last speed; after slowing down (a camera coasting to a stop) or speeding up; with the
        # pitch stopped but the yaw going on (a pitch limit), or the other way round; stopped
        step = self.p_rate * min(gap, 0.3)
        np = self._np
        guesses = [self.p + f * step for f in (1.0, 0.6, 1.4, 0.3)]
        guesses += [self.p + step * np.array([0.0, 1.0, 1.0]), self.p + step * np.array([1.0, 0.0, 0.0]), self.p.copy()]
        got = None
        for pred in guesses:
            got = self._measure(g, pred)
            if got is not None:
                break
        if got is None:
            coarse = self._coarse(g)
            if coarse is not None:
                got = self._measure(g, coarse)
        if got is None:
            self.lost += 1
            self.lost_total += 1
            self.peak, self.used = 0.0, 0  # p stays at the last measurement: the next frame's guesses extrapolate
            # from it over the whole gap (dead reckoning here overshoots when motion stops, e.g. at a pitch limit)
        else:
            p, self.rms, self.used = got
            self.p_rows = self.tile_rows
            self.peak = float(np.mean([r[4] for r in self.tile_rows]))
            if gap > 1e-4:
                self.p_rate = 0.5 * self.p_rate + 0.5 * (p - self.p) / max(gap, 1e-3)
            self.p, self.lost, self._t_meas = p, 0, now
            if abs(self.p[1]) > 0.03 and abs(self.p[1]) > abs(self.p[2]):  # yawing: the roll it shows gives the
                # tilt (not near straight up or down, where the yaw part vanishes and the estimate flips sign)
                est = -math.atan(self.p[2] / self.p[1])
                self.theta = est if self.theta is None else 0.7 * self.theta + 0.3 * est
        w = self.size[0]
        if self.lost > 3 or math.hypot(self.p[0], self.p[1]) * self.f > self.REKEY * w or abs(self.p[2]) > 0.08:
            self.breaks += self.lost > 3
            dyaw, dpitch = world_turn(self.p, self.theta)
            if self.segments is not None:
                self.segments.append((self.p_rows, self.p.copy(), self.f))
            self.p_rows = []
            self.base_yaw += dyaw
            self.base_pitch += dpitch
            if self.theta is not None:
                self.theta += dpitch
            self.p, self.lost = self._np.zeros(3), 0
            self._key(g)
        self.samples.append((now, self.x, self.y))
        return self.x, self.y

    def health(self) -> float:
        """The fraction of frames it could measure (1 = all)."""
        return 1.0 - self.lost_total / max(1, self.updates)

    def fps(self) -> float:
        """New frames per second since it was made (the capture rate, as seen by whoever updates it)."""
        span = time.perf_counter() - self.born
        return self.updates / span if span > 0.05 and self.updates else 0.0

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


def focal_from_rows(samples: list, width: int, k: int) -> float | None:
    """The focal length (screen px) under which the rotation model best explains the tile shifts seen while the
    camera turned: the edges of a pinhole camera's picture move faster than its middle, by an amount that depends
    on it. samples: Odometry.tile_rows lists (working px). None when the view can't tell (too little motion)."""
    np = _np()
    usable = [rows for rows in samples if len(rows) >= 5 and max(abs(r[2]) for r in rows) > 4]
    if len(usable) < 5:
        return None
    usable = usable[::max(1, len(usable) // 15)][:15]
    w = width / k
    candidates = np.geomspace(0.25 * w, 4.0 * w, 28)

    def score(f):
        errs = []
        for rows in usable:
            fit = fit_rotation(rows, float(f), max_rms=1e9)
            errs.append(fit[1] ** 2 if fit else 100.0)
        return float(np.median(errs))

    scores = [score(f) for f in candidates]
    i = int(np.argmin(scores))
    if i in (0, len(candidates) - 1) or scores[i] > 0.9 * min(scores[0], scores[-1]):
        return None  # no clear best: a flat (orthographic-looking) view or too little motion
    a, b, c = scores[i - 1], scores[i], scores[i + 1]
    lf = np.log(candidates)
    d = a - 2 * b + c
    off = 0.0 if d <= 0 else max(-0.5, min(0.5, 0.5 * (a - c) / d))
    return float(np.exp(lf[i] + off * (lf[i + 1] - lf[i]))) * k


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
            tiles.append(list(odo.tile_rows))
        io.sleep(0.004)


def _hold_steady(io: PlantIO, odo: Odometry, x: float, seconds: float, most: float = 1.4,
                 tiles: list | None = None) -> tuple[float, list]:
    """Hold the stick until the turn rate stops changing (at least `seconds`, at most `most`), following the
    picture; then let go and follow it to a stop. Returns (steady px/s, trace)."""
    io.stick(x, 0.0)
    t0, trace = time.perf_counter(), []
    try:
        while True:
            t = time.perf_counter() - t0
            odo.update(io.frame())
            trace.append((time.perf_counter() - t0, -odo.x))
            if tiles is not None and odo.used:
                tiles.append(list(odo.tile_rows))
            if t >= seconds:
                late = _steady_rate(trace, t - 0.15)
                early = _steady_rate([p for p in trace if p[0] <= t - 0.15], t - 0.3)
                if t >= most or (abs(late) > 0 and abs(late - early) <= 0.06 * abs(late)) or abs(late) < 1e-6:
                    break
            io.sleep(0.004)
    finally:
        io.stick(0.0, 0.0)
    rate = abs(_steady_rate(trace, max(0.0, trace[-1][0] - 0.2)))
    _trace(io, odo, 0.3)  # follow it to a stop
    return rate, trace


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


def _smooth(a):
    """A 3x3 box blur: a match that doesn't hinge on sub-pixel alignment or a little perspective change."""
    np = _np()
    p = np.pad(np.asarray(a, np.float32), 1, mode="edge")
    h, w = a.shape
    return sum(p[i:i + h, j:j + w] for i in range(3) for j in range(3)) / 9.0


def _full_turn(io: PlantIO, overlay: Overlay, deflection: float, W: int, H: int, k: int,
               limit_s: float = 25.0, focal_px: float | None = None) -> float | None:
    """Pixels in a full turn (loop closure): turn steadily, adding up odometry, until the first view comes back: a
    patch of it (textured, off the HUD) is looked for around where a revolution should end (from the focal length,
    if known), and the best matches there decide, with the focal length solved so the turn is exactly 360 degrees.
    -1 if it couldn't keep up or missed the view (slower might work); None if it never got round (a camera that can't
    turn round)."""
    from .behave import match_template
    np = _np()
    first = io.frame()
    g0 = gray(first, k)
    half = max(8, 60 // k)
    best = None
    for fy in (0.3, 0.4, 0.6, 0.7):  # the most textured candidate (not sky), away from the crosshair and HUD
        for fx in (0.4, 0.6):
            py, px = int(H * fy) // k, int(W * fx) // k
            patch = g0[py - half:py + half, px - half:px + half]
            if not patch.size or overlay.covers(px * k, py * k, 2 * half * k) > 0.02:
                continue
            if best is None or patch.std() > best[0]:
                best = (float(patch.std()), py, px, patch.copy())
    if best is None or best[0] < 4:
        return None
    _, py, px, patch = best
    patch = _smooth(patch)
    band_top = max(0, py - half - 16)
    expected = 2 * math.pi * focal_px if focal_px else None
    lo, hi = (0.8 * expected, 1.3 * expected) if expected else (1.2 * W, 60.0 * W)
    odo = Odometry(first, overlay, focal_px)
    odo.segments = []
    io.stick(deflection, 0.0)
    t0 = time.perf_counter()
    seen: list = []  # (score, px in a revolution, what the odometry had then) around where it should close
    best_score, in_window = 0.0, 0
    try:
        while time.perf_counter() - t0 < limit_s:
            frame = io.frame()
            odo.update(frame)
            turned = -odo.x
            if turned > hi:
                break
            if turned >= lo:
                band = _smooth(gray(frame, k)[band_top:py + half + 16])
                x, _, score = match_template(band, patch)
                tx = (x + half) * k
                in_window += 1
                if abs(tx - px * k) < W / 6:
                    best_score = max(best_score, score)
                if score > 0.8 and abs(tx - px * k) < W / 6:
                    at = odo.segments + [(odo.p_rows, odo.p.copy(), odo.f)]
                    seen.append((score, turned + tx - px * k, at, tx))
                elif seen and max(sc[0] for sc in seen) > 0.9:
                    break  # past it
            io.sleep(0.002)
    finally:
        io.stick(0.0, 0.0)
    # a break (lost track for several frames) drops motion: the distance turned is short, whatever matched
    kept_up = not odo.breaks and odo.health() >= 0.9 and (-odo.x / max(odo.updates, 1)) <= SAFE_FRAME_FRACTION * W
    io.log("loop closure", turned_px=round(-odo.x), frames=odo.updates, in_window=in_window,
           best_score=round(best_score, 2), health=round(odo.health(), 2), breaks=odo.breaks,
           s=round(time.perf_counter() - t0, 1))
    if seen and not kept_up:
        return -1.0
    if not seen:  # -1: couldn't follow the turn, or went round without catching the view (try slower); None: never
        return -1.0 if not kept_up or -odo.x >= lo else None  # got round (a camera that can't turn round)
    top = max(s[0] for s in seen)
    best = sorted((s for s in seen if s[0] >= top - 0.03), key=lambda s: -s[0])[:5]
    rounds = [_closing_focal(at, tx, px * k, W, odo.k, v / (2 * math.pi)) for _sc, v, at, tx in best]
    px_round = float(np.median([2 * math.pi * f for f in rounds]))
    return px_round if lo <= px_round <= hi else -1.0  # outside where it could close: something slipped


def _closing_focal(segments, tx: float, x0: float, W: int, k: int, f_guess: float) -> float:
    """The focal length (screen px) at which the recorded turn plus where the first view's patch is now (tx; it
    started at x0) is exactly one revolution. The odometry's focal length when it ran was a guess, and its
    off-middle tiles carry that guess into the distance turned; refitting removes it. Secant steps in 1/f."""
    def error(f):
        return (yaw_with_focal(segments, f / k) + math.atan((tx - W / 2) / f) - math.atan((x0 - W / 2) / f)
                - 2 * math.pi)
    f_ran = segments[0][2] * k
    a, b = 1 / f_ran, 1 / max(f_guess, 1.0)
    if abs(a - b) < 1e-9:
        b = a * 1.01
    ea, eb = error(1 / a), error(1 / b)
    for _ in range(12):
        if abs(eb - ea) < 1e-12:
            break
        a, b, ea = b, b - eb * (b - a) / (eb - ea), eb
        if not (0.3 / f_ran < b < 3 / f_ran):
            return f_guess  # didn't converge sensibly: the plain estimate
        eb = error(1 / b)
        if abs(b - a) < 1e-9 * abs(b):
            break
    return 1 / b


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
    """Identify the look stick (see the module docstring). Takes ~20-30 s; ends looking level if pitch."""
    io.stick(0.0, 0.0)
    io.sleep(0.25)
    first = io.frame()
    H, W = first.shape[:2]
    k = scale_for(W)

    # 1. The overlay: pixels that stay put while the view turns (no tracking needed, so any speed does).
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

    # 2. The response curve, slow to fast, stopping before the picture moves too far between two captures to be
    #    followed. Until the focal length is known, the rotation model is followed loosely and the tile shifts are
    #    kept to measure it.
    focal, rows_seen, curve_px, fps, fps_seen = None, [], [], 0.0, []
    for d in sorted(points):
        rates, health, fps_here, broke = [], 1.0, [], 0
        for sign in (1, -1):
            odo = Odometry(io.frame(), overlay, focal, tolerance=1.0 if focal else 3.0)
            rate, _ = _hold_steady(io, odo, sign * d, hold_s, tiles=rows_seen if focal is None else None)
            rates.append(rate)
            health = min(health, odo.health())
            fps_here.append(odo.fps())
            broke += odo.breaks
        fps = max(fps, *fps_here)
        fps_seen += fps_here
        rate = sum(rates) / 2
        per_frame = rate / max(min(fps_here), 1.0)  # at the rate frames came just now (a busy machine is slower)
        if health < 0.85 or broke or per_frame > SAFE_FRAME_FRACTION * W:
            io.log("too fast to follow", deflection=d, px_per_frame=round(per_frame), measured=round(health, 2),
                   breaks=broke)
            break
        curve_px.append((d, rate, health))
        if focal is None and rate > 0:
            focal = focal_from_rows(rows_seen, W, k)
            if focal:
                io.log("focal", focal_px=round(focal), deflection=d)
    moving = [c for c in curve_px if c[1] > 0]
    if not moving:
        raise RuntimeError("couldn't measure a turn the picture can be followed at (too fast even at the "
                           "smallest deflection, or too little texture)")
    io.log("curve", px_s=[[d, round(r)] for d, r, _h in curve_px], capture_fps=round(fps))

    # 3. Step response at the fastest safe deflection: latency, ramp up, coast after letting go.
    d_step = moving[-1][0]
    odo = Odometry(io.frame(), overlay, focal)
    io.stick(d_step, 0.0)
    tiles: list = []
    trace = _trace(io, odo, 1.0, tiles=tiles)
    io.stick(0.0, 0.0)
    tail = _trace(io, odo, 0.6)
    latency, top_px, ramp = step_response(trace, k)
    if top_px <= 0:
        raise RuntimeError("no steady turn when the right stick was pushed")
    coast_s = max(latency, (tail[-1][1] - trace[-1][1]) / top_px) if tail else latency + ramp / 3
    focal = focal_from_rows(rows_seen + tiles, W, k) or focal  # more samples; the full turn measures it exactly
    tilt, focal_at_step = odo.theta, odo.f * odo.k
    io.log("step", deflection=d_step, latency_ms=round(latency * 1000), top_px_s=round(top_px),
           ramp_ms=round(ramp * 1000), coast_ms=round(coast_s * 1000), focal_px=focal and round(focal),
           tilt_deg=tilt is not None and round(math.degrees(tilt), 1))

    # 4. Degrees: a full turn (loop closure) at a speed the first view can be matched at as it comes back.
    ppd, ppd_from = None, None
    if full_turn:
        fps_typical = float(_np().median(fps_seen)) if fps_seen else fps
        slow_enough = [c for c in moving if c[1] / max(fps_typical, 1.0) <= LOOP_FRAME_FRACTION * W] or moving[:1]
        f_guess = focal or (W / 2) / math.tan(math.radians(DEFAULT_HFOV_DEG / 2))
        for d_turn, rate_px, _h in sorted(slow_enough, key=lambda c: -c[1])[:3]:  # slower if it can't keep up
            expected_s = 2 * math.pi * f_guess / max(rate_px, 1e-6)
            if expected_s > 45:
                io.log("full turn skipped", reason=f"a turn would take ~{expected_s:.0f} s")
                break
            px_round = _full_turn(io, overlay, d_turn, W, H, k, limit_s=1.6 * expected_s + 3, focal_px=focal)
            io.sleep(latency + 0.2)
            if px_round == -1.0:
                io.log("full turn", deflection=d_turn, result="couldn't keep up or missed the view; slower")
                continue
            if px_round and px_round > W:
                ppd, ppd_from = px_round / 360.0, "full turn"
            io.log("full turn", deflection=d_turn, px=round(px_round or 0), px_per_deg=ppd and round(ppd, 3))
            break
    if ppd is None and focal:
        ppd, ppd_from = focal * math.pi / 180, "focal length"  # a pinhole camera's middle moves f px per radian
    curve_px = [(d, r) for d, r, _h in curve_px]

    look: dict[str, Any] = {"stick": "right_stick", "latency_s": round(latency, 3), "accel_s": round(ramp, 3),
                            "coast_s": round(coast_s, 3), "y_gain": 1.0, "max_deflection": d_step,
                            "capture_fps": round(fps, 1)}
    profile: dict[str, Any] = {"app": app, "screen": [W, H], "px_per_deg": ppd and round(ppd, 4),
                               "px_per_deg_from": ppd_from,
                               # a pinhole camera moves its middle f px per radian: the full turn gives f exactly
                               "focal_px": round(ppd * 180 / math.pi, 1) if ppd_from == "full turn"
                               else (focal and round(focal, 1)),
                               "overlay": overlay.to_json(), "overlay_fraction": round(overlay.fraction(), 3),
                               "pitch_bottom_deg": -90.0, "calibrated": time.strftime("%Y-%m-%d %H:%M"), "look": look}
    if tilt is not None:
        if ppd_from == "full turn":  # roll's flow doesn't depend on f, yaw's does: the tilt read with a rough f scales
            tilt = math.atan(math.tan(tilt) * (ppd * 180 / math.pi) / focal_at_step)
        profile["tilt_at_start_deg"] = round(math.degrees(tilt), 1)
    if ppd:
        look["curve"] = [[d, round(r / ppd, 2)] for d, r in curve_px]
        c = ResponseCurve(look["curve"])
        look.update(deadzone=round(c.deadzone, 3), max_deg_s=round(c.max_rate, 1))
    else:
        look["curve_px"] = [[d, round(r, 1)] for d, r in curve_px]

    if pitch and ppd:
        # 5. Vertical gain: up/down at a moderate deflection, toward level (away from a pitch limit), long enough to
        #    get past the latency and the ramp up; compared with how fast that deflection yaws.
        cam = Camera(io, profile)
        tilt_now = cam.tilt()
        down = tilt_now is None or tilt_now > 0
        d_p, rate_yaw = min(((d, r) for d, r in curve_px if r > 0), key=lambda c: abs(c[1] / ppd - 60.0))
        steady_after = latency + 1.5 * max(ramp, 0.03)
        seconds = steady_after + max(0.12, 15.0 / max(rate_yaw / ppd, 1e-6))
        odo = cam.odometry()
        io.stick(0.0, -d_p if down else d_p)
        trace = _trace(io, odo, seconds, axis=1)
        io.stick(0.0, 0.0)
        cam._settle(odo)
        vy = _steady_rate(trace, steady_after)
        if abs(vy) > 0.05 * rate_yaw:
            # stick down normally moves the picture up (-y), stick up moves it down (+y)
            look["y_gain"] = round(math.copysign(abs(vy) / rate_yaw, -vy if down else vy), 3)
        io.log("pitch", deflection=d_p, y_gain=look["y_gain"])
        # 6. Radial or per-axis stick: most games apply the deadzone and response curve to the stick's length (a
        #    diagonal push turns both ways at the curve's rate for its full length), some to each axis alone. A
        #    servo that assumes the wrong one overshoots when both axes move (seen on Minecraft: +7 degrees).
        a = d_p / math.sqrt(2)
        px_curve = ResponseCurve([[d, r] for d, r in curve_px])
        odo = cam.odometry()
        io.stick(a, -a if down else a)
        trace = _trace(io, odo, seconds, axis=0)
        io.stick(0.0, 0.0)
        cam._settle(odo)
        vx = abs(_steady_rate(trace, steady_after))
        radial_px, axial_px = rate_yaw / math.sqrt(2), abs(px_curve.rate(a))
        radial = abs(math.log(max(vx, 1.0) / max(radial_px, 1.0))) <= abs(math.log(max(vx, 1.0) / max(axial_px, 1.0)))
        look["stick"] = "radial" if radial else "axial"
        io.log("stick", kind=look["stick"], diagonal_px_s=round(vx), radial_px_s=round(radial_px),
               axial_px_s=round(axial_px))
        Camera(io, profile).level()
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
        self.radial = look.get("stick", "radial") != "axial"  # most games; calibration says which
        self.max_d = float(look.get("max_deflection", 1.0))  # faster than this, odometry can't follow
        # how far (in seconds of the current speed) it keeps turning after the stick lets go
        self.coast = float(look.get("coast_s") or (self.latency + self.accel / 3))
        self._tilt: float | None = None  # the last tilt measured (degrees)
        ov = profile.get("overlay")
        self.overlay = Overlay.from_json(ov) if ov else None

    def odometry(self) -> Odometry:
        frame = self.io.frame()
        ov = self.overlay if self.overlay is not None and self.overlay.k == scale_for(frame.shape[1]) else None
        odo = Odometry(frame, ov, focal_px=self.profile.get("focal_px"))
        if self._tilt is not None:
            odo.theta = math.radians(self._tilt)  # where it looks now: signs yaws seen straight up or down
        return odo

    def set_pitch(self, degrees: float) -> None:
        """Tell the camera its pitch now (degrees, up +), e.g. after holding the stick into a pitch limit (Minecraft's
        is exactly -90 / +90). Turns keep it up to date."""
        self._tilt = float(degrees)

    def _want(self, err: float, speed: float, tol: float) -> float:
        """The turn rate (deg/s) to ask for with `err` degrees to go, turning at `speed` now."""
        coming = abs(speed) * self.coast  # already on its way: it keeps turning this far after letting go
        left = abs(err) - coming
        if abs(err) <= tol or left <= 0:
            return 0.0
        decel = self.curve.max_rate / max(self.coast, 0.03)
        return math.copysign(min(self.curve.max_rate, math.sqrt(1.2 * decel * left), 5.0 * left), err)

    def stick_for(self, yaw_rate: float, pitch_rate: float) -> tuple[float, float]:
        """Stick (x, y) for yaw and pitch rates (degrees/s). A radial stick turns at the curve's rate for the stick's
        length, split by its direction; a per-axis one takes each axis through the curve alone."""
        yr, pr = yaw_rate, (pitch_rate / self.y_gain if pitch_rate else 0.0)  # pitch as the yaw rate it takes
        if self.radial and yr and pr:
            mag = math.hypot(yr, pr)
            m = abs(self.curve.deflection(mag))
            return m * yr / mag, m * pr / mag
        return (self.curve.deflection(yr) if yr else 0.0), (self.curve.deflection(pr) if pr else 0.0)

    def _hold(self, odo: Odometry, x: float, y: float, seconds: float) -> None:
        """Hold the stick for `seconds`, then center it, following the picture all the while (odometry can't
        recover from a long blind gap in a fast turn)."""
        io = self.io
        io.stick(x, y)
        end = time.perf_counter() + seconds
        try:
            while time.perf_counter() < end:
                odo.update(io.frame())
                io.sleep(0.004)
        finally:
            io.stick(0.0, 0.0)

    def _settle(self, odo: Odometry, timeout: float = 0.8) -> None:
        """Wait until the view has stopped (a servo's in-position check): the last input only shows after the
        latency, and then the camera slows down gradually."""
        io = self.io
        end = time.perf_counter() + self.latency
        while time.perf_counter() < end:  # (following the picture: it's still moving)
            odo.update(io.frame())
            io.sleep(0.004)
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
        t0, at_limit, still, timed_out = time.perf_counter(), False, 0, True
        try:
            while time.perf_counter() - t0 < timeout:
                odo.update(io.frame())
                done_y, done_p = -odo.x / self.ppd, odo.y / self.ppd
                ey, ep = yaw - done_y, (0.0 if at_limit else pitch - done_p)
                if abs(ey) <= tol and abs(ep) <= tol:
                    timed_out = False
                    break
                vx, vy = odo.rate(0.06)
                ry = self._want(ey, -vx / self.ppd, tol)
                rp = self._want(ep, vy / self.ppd, tol)
                if ry == 0 and rp == 0 and (abs(ey) > tol or abs(ep) > tol):
                    io.stick(0.0, 0.0)  # coasting in on the inputs already sent
                else:
                    io.stick(*self.stick_for(ry, rp))
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
                        g = self.y_gain if axis == 1 else 1.0  # pitch turns g x as fast as yaw at a deflection
                        want = min(0.3 * self.curve.max_rate, max(self.curve.min_rate, abs(err) / 0.15 / abs(g)))
                        d = self.curve.deflection(math.copysign(want, err / g))
                        rate = max(abs(self.curve.rate(d) * g), 1e-6)  # what that deflection really turns
                        # (a lagging camera still turns rate x time in all)
                        self._hold(odo, d if axis == 0 else 0.0, d if axis == 1 else 0.0, min(0.4, abs(err) / rate))
                self._settle(odo)
        finally:
            io.stick(0.0, 0.0)
        got_y, got_p = -odo.x / self.ppd, odo.y / self.ppd
        if self._tilt is not None:
            self._tilt = max(-90.0, min(90.0, self._tilt + got_p))
        out = {"yaw": round(got_y, 1), "pitch": round(got_p, 1),
               "error": round(max(abs(yaw - got_y), 0.0 if at_limit else abs(pitch - got_p)), 1)}
        if at_limit:
            out["pitch_limit"] = True
        if timed_out:
            out["timed_out"] = True  # didn't get within tol in time (the pulses after may still have finished it)
        return out

    def tilt(self) -> float | None:
        """The camera's pitch now (degrees, up +), from how the picture rolls during a small yaw wiggle there and
        back; None if it can't tell (a flat view)."""
        odo = self.odometry()
        odo.theta = None  # measured afresh
        rate = max(1.5 * self.curve.min_rate, min(60.0, 0.3 * self.curve.max_rate))
        d = self.curve.deflection(rate)
        seconds = min(0.3, 6.0 / abs(self.curve.rate(d)) + self.accel)
        for sign in (1, -1):
            self._hold(odo, sign * d, 0.0, seconds)
            self._settle(odo)
        self._tilt = None if odo.theta is None else math.degrees(odo.theta)
        return self._tilt

    def level(self, pitch: float = 0.0) -> dict:
        """Look at `pitch` degrees (0 = straight ahead). The tilt comes from a small yaw wiggle, so no pitch limit
        is needed; without one (a flat view), the bottom limit is the reference."""
        now = self.tilt()
        if now is None:
            top = self.curve.max_rate * self.ppd
            _drive_to_limit(self.io, self.overlay, -math.copysign(self.max_d, self.y_gain), top)
            self.io.sleep(self.latency + self.coast)
            now = float(self.profile.get("pitch_bottom_deg", -90.0))
        out = self.turn(pitch=pitch - now, tol=1.0)
        out["was_deg"] = round(now, 1)
        return out

    def angles_to(self, x: float, y: float, w: int, h: int, tilt: float | None = None) -> tuple[float, float]:
        """Yaw and pitch (degrees) that bring a screen point to the middle, for a camera that yaws about the vertical
        and is tilted `tilt` degrees (the last known tilt if not given, else level)."""
        f = self.profile.get("focal_px")
        if not f:
            return (x - w / 2) / self.ppd, -(y - h / 2) / self.ppd
        t = math.radians(self._tilt if tilt is None and self._tilt is not None else (tilt or 0.0))
        xc, yc = x - w / 2, y - h / 2
        fwd = yc * math.sin(t) + f * math.cos(t)  # the point's direction in the level frame: forward and up
        up = f * math.sin(t) - yc * math.cos(t)
        yaw = math.degrees(math.atan2(xc, fwd))
        elevation = math.degrees(math.atan2(up, math.hypot(xc, fwd)))
        return yaw, elevation - math.degrees(t)

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
            for _ in range(2):  # the turn put it near the middle: look only there (a far look-alike would pull away)
                tx, ty, score = aimer.find(self.io.frame(), refresh=False, near=(w / 2, h / 2, 0.08 * w))
                if score < 0.6:
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
        return self.stick_for(yaw_rate, pitch_rate)

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
