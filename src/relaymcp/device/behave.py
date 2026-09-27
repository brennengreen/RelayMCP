"""Real-time behaviors: small control loops that run on the handheld at 30-120 Hz, so reacting to the screen takes
tens of milliseconds instead of a model round trip (seconds). The model starts a behavior with parameters, then
reads compact events; the loop does the watching and the pressing.

Built-in behaviors:
- react: when a screen region changes, or shows a color, send an input (a key, a gamepad press, a click)
- track: steer the mouse or a stick until a colored target sits at an aim point (the cursor, or a crosshair)
- press_until: repeat an input until text, a color or a change appears
- watch: report when a region changes or shows a color (no input)

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

MAX_RUNS = 4
MAX_SECONDS = 600.0
EVENTS_KEPT = 300

KINDS = {
    "react": 'region [l,t,r,b]; when {"color": [r,g,b], "tol": 40, "min_fraction": 0.2} or {"change": 12}; do {"key": '
             '["space"]} | {"pad": ["a"]} | {"click": true}; repeat (true); cooldown_ms (300); hz (120)',
    "track": 'color [r,g,b], tol (50), region (search area); aim "cursor" or [x,y] (e.g. the crosshair); output "mouse" '
             '| "right_stick" | "left_stick"; gain (0.6); within px (10); hold_frames (5): done when on target that long '
             '(follow: true keeps following until max_s)',
    "press_until": 'do (as react); every_ms (400); until {"text": "Play"} | {"color": [r,g,b], "region": [...]} | '
                   '{"change": 12, "region": [...]}; max_presses (20)',
    "watch": 'region; when (as react); every_ms (100): reports each time it happens',
    "navigate": 'text (the menu item to reach); region (the menu); with "pad" (d-pad + A) | "keys" (arrows + Enter); '
                'confirm (false): press A/Enter on it; direction ("down") while it is off screen; max_moves (30)',
    "script": 'a small state machine: start (state name); states {name: {do (as react, repeated every_ms 250), until '
              '(text | color | change | {"state": {"topic", "match"}}), ms (instead of until: stay this long), next '
              '(state or "done"), timeout_s (30), on_timeout (state, else stop)}}',
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

class Run:
    def __init__(self, kind: str, params: dict, max_s: float):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.params = kind, params
        self.max_s = max(0.5, min(float(max_s), MAX_SECONDS))
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
                 cursor: Callable | None = None, state_events: Callable | None = None):
        self.grab, self.out = grab, outputs
        self.takeover = takeover or (lambda: None)
        self.read_text = read_text
        self.cursor = cursor
        self.state_events = state_events  # (topic, since) -> (events [{"n", "topic", "data"}], cursor): the inbox
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self, kind: str, params: dict | None, max_s: float = 30.0) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        params = dict(params or {})
        with self._lock:
            active = [r for r in self.runs.values() if r.state == "running"]
            if len(active) >= MAX_RUNS:
                raise RuntimeError(f"at most {MAX_RUNS} behaviors at a time (stop one first)")
            for rid in [rid for rid, r in self.runs.items() if r.state != "running"][:-8]:
                del self.runs[rid]  # keep the last few finished ones for status
            run = Run(kind, params, max_s)
            self.runs[run.id] = run
        threading.Thread(target=self._thread, args=(run,), name=f"behavior-{run.id}", daemon=True).start()
        return {"id": run.id, "kind": kind, "max_s": run.max_s}

    def stop(self, run_id: str = "") -> list[str]:
        stopped = []
        for r in list(self.runs.values()):
            if (not run_id or r.id == run_id) and r.state == "running":
                r.stop_evt.set()
                stopped.append(r.id)
        for r in (self.runs[s] for s in stopped):
            for _ in range(100):
                if r.state != "running":
                    break
                time.sleep(0.02)
        return stopped

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
            run.ended = time.perf_counter()
            try:
                self.out.release(run.used)  # only what this run holds: other runs and the agent's input are theirs
            except Exception:
                pass
            run.emit("end", state=run.state, reason=run.reason)

    # --- shared loop pieces -----------------------------------------------------------------------------------------

    def _ticks(self, run: Run, hz: float):
        """Yield once per tick until the time limit, a stop request or a takeover."""
        period = 1.0 / max(0.01, min(float(hz), 240.0))  # anything from every 100 s to 240 times a second
        next_t = time.perf_counter()
        deadline = run.started + run.max_s
        while True:
            if run.stop_evt.is_set():
                raise _Stopped("stopped on request")
            if time.perf_counter() >= deadline:
                run.reason = f"time limit ({run.max_s:g} s)"
                return
            who = self.takeover()
            if who:
                raise _Stopped(f"you took over ({who})")
            yield
            run.stats["ticks"] += 1
            next_t += period
            now = time.perf_counter()
            if next_t < now - period:  # fell behind (a slow frame): don't try to catch up in a burst
                next_t = now
            left = next_t - now
            if left > 0.005 and run.stop_evt.wait(left - 0.003):  # long waits wake at once for a stop request
                continue
            sleep_until(next_t)

    def _frame(self, run: Run, region):
        t0 = time.perf_counter()
        frame, captured = self.grab(region)
        run._frame_ms.append((time.perf_counter() - t0) * 1000)
        return frame, captured

    def _act(self, run: Run, do: dict) -> None:
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
            want = " ".join(str(until["text"]).lower().split())
            return any(want in " ".join(ln["text"].lower().split()) for ln in self.read_text(until.get("region")))
        frame, _ = self._frame(run, until.get("region"))
        hit, _m = matches(until, frame, baseline)
        return hit

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
        last_do = 0.0
        for _ in self._ticks(run, 1 / every if not hold_s else 50):
            now = time.perf_counter()
            if hold_s is not None and now - entered >= hold_s:
                return "next"
            if until and self._condition(run, until, baseline, cursor):
                return "next"
            if now - entered >= timeout:
                return "timeout"
            if do and now - last_do >= every:
                self._act(run, do)
                last_do = now
        raise _Stopped("the script ran out of time")

    def _run_track(self, run: Run, p: dict) -> None:
        region = p.get("region")
        color, tol = p.get("color"), int(p.get("tol", 50))
        if not color:
            raise ValueError("track needs color [r, g, b]")
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


def center_of(box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def best_line(lines: list[dict], want: str) -> dict | None:
    """The OCR line for a menu item: exact, then starts-with, then contains (case and spacing ignored)."""
    ranked = []
    for i, ln in enumerate(lines):
        t = " ".join(ln["text"].lower().split())
        score = 3 if t == want else 2 if t.startswith(want) else 1 if want in t else 0
        if score:
            ranked.append((-score, len(t), i))
    return lines[min(ranked)[2]] if ranked else None


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
    if action == "start":
        return rt.start(kind, params, max_s)
    if action == "status":
        return rt.status(run_id, since)
    if action == "stop":
        return {"stopped": rt.stop(run_id)}
    if action == "kinds":
        return {"kinds": KINDS, "limits": {"max_s": MAX_SECONDS, "at_once": MAX_RUNS}}
    raise ValueError("action must be start, status, stop or kinds")
