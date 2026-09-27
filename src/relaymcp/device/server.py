"""RelayMCP hardware server: handheld hardware tools (gamepad, touch, keys, audio, speech, display, power) over MCP.

Runs hidden in the signed-in user's session on 127.0.0.1 only (reached from the controlling computer through the SSH
tunnel), and only while the handheld is on the home network (the RelayMCP agent starts it; the controller stops it).
"""

from __future__ import annotations

import sys

sys.coinit_flags = 0  # comtypes: initialize COM as multithreaded (MTA) on import

try:
    # soundcard initializes COM (MTA) itself and rejects a thread where COM is already initialized, so it must be
    # imported before comtypes/pycaw touch COM. Worker threads then join the same process-wide MTA.
    import soundcard  # noqa: F401
except Exception:  # audio tools will report the error when used
    pass

import argparse
import asyncio
import base64
import ctypes
import functools
import gc
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import (__version__, audio, behave, capture, control, focus, gamepad, inbox, lean, ocr, procs, pshost, skills,
               speech, system, tts, turns, updates, voice, win_input)
from .lean import compact, lean_result, lean_schema
from .paths import USER_DIR, VOICE_HEADER, device_settings

LOG_DIR = USER_DIR
log = logging.getLogger("relaymcp")


def _com_init() -> None:
    ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED


INPUT = ThreadPoolExecutor(max_workers=1, thread_name_prefix="input")          # touch/keys/mouse/gamepad, in order
COM = ThreadPoolExecutor(max_workers=1, thread_name_prefix="com", initializer=_com_init)  # capture, volume, keyboard UI
PLAY = ThreadPoolExecutor(max_workers=1, thread_name_prefix="play", initializer=_com_init)  # speech/tones (overlaps capture)
STT = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt", initializer=_com_init)  # whisper (CPU heavy)
SCREEN = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen", initializer=_com_init)  # capture + OCR (owns DXGI)
GRABBER = capture.Grabber()
PROC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="proc")  # process sessions (reads and waits block)
PROCS = procs.Manager(USER_DIR / "procs.json")
PS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ps")  # the persistent PowerShell session, one call at a time


class _BehaviorOutputs:
    """What behaviors may do: short presses, mouse moves, clicks at the cursor, stick positions; and release it all."""

    def key(self, keys):
        win_input.key_press(keys, 40, 1, 0)

    def pad(self, buttons):
        gamepad.PAD.run_steps([{"buttons": buttons, "ms": 60}])

    def click(self):
        x, y = capture.cursor_pos() or (None, None)
        if x is not None:
            win_input.mouse_click(x, y)

    def mouse(self, dx, dy):
        win_input.mouse_move_relative(dx, dy, 0)

    def stick(self, side, x, y):
        gamepad.PAD.stick(side, x, y)

    def hold(self, state):
        gamepad.PAD.hold(state)

    def seq(self, steps):
        gamepad.PAD.run_steps(steps)

    def release(self, used=()):
        # Key taps, pad presses, clicks and mouse moves are momentary; a steered stick or a held state stays put.
        if "stick" in used or "hold" in used:
            gamepad.PAD.neutral()


def _on_screen(text: str, region=None) -> bool:
    frame = GRABBER.grab()
    box = capture.clamp_region(region, frame.left, frame.top, frame.width, frame.height) if region else None
    return ocr.find(ocr.recognize(frame, box), text) is not None


def _screen_settled(timeout: float = 1.0, threshold: float = 2.0) -> bool:
    """Wait until two grabs ~50 ms apart barely differ (an animation, like a menu fading out, finished)."""
    import numpy as np
    end, prev = time.monotonic() + timeout, None
    while time.monotonic() < end:
        arr, _ = GRABBER.grab_array(None)
        small = arr[::8, ::8, :3].astype(np.int16)
        if prev is not None and float(np.abs(small - prev).mean()) < threshold:
            return True
        prev = small
        time.sleep(0.05)
    return False


TURNS = turns.Turns(  # turn-based play (focus_window pause): the game runs only while the agent's input runs
    press=lambda buttons: gamepad.PAD.run_steps([{"buttons": buttons, "ms": 100}]),
    grab=lambda: SCREEN.submit(GRABBER.grab).result(timeout=5),
    sees=lambda text, region=None: SCREEN.submit(_on_screen, text, region).result(timeout=10),
    in_use=lambda: gamepad.physical_active(),
    settle=lambda: SCREEN.submit(_screen_settled).result(timeout=5),
)


BEHAVIORS = behave.Runtime(
    grab=lambda region: SCREEN.submit(GRABBER.grab_array, region).result(timeout=5),
    outputs=_BehaviorOutputs(),
    takeover=lambda: gamepad.physical_active(),
    read_text=lambda region: SCREEN.submit(lambda: ocr.recognize(GRABBER.grab(), capture.clamp_region(
        region, 0, 0, *win_input.screen_size()) if region else None)).result(timeout=10),
    cursor=lambda: capture.cursor_pos(),
    state_events=lambda topic, since: (lambda r: (r["events"], r["cursor"]))(inbox.INBOX.read(topic, since, 200)),
    # A paused game must be in front to resume; guards only watch, so they don't keep a turn-based game running.
    on_start=lambda run: None if run.kind == "guard" else (focus.before_input(), TURNS.begin()),
    on_end=lambda run: None if run.kind == "guard" else TURNS.end(),
    profiles=control.ProfileStore(USER_DIR / "profiles"),
    app=lambda: (focus.foreground() or {}).get("process") or "unknown",
    skills=skills.SkillStore(USER_DIR / "skills"),
    active=TURNS.running,
)
lean.ALERT_HOOK = BEHAVIORS.take_alerts


def _in_worker(pool: ThreadPoolExecutor, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    finally:
        if pool is not INPUT:
            # Collect COM wrappers here, on their own thread, never later in the event loop thread.
            gc.collect()


def _focused(fn, turn: bool = True):
    """Input goes to whatever is in front: refocus the remembered input target first, then report what had focus.
    In turn-based play the game is resumed first and paused again after (turn=False: not for this call)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        pre = focus.before_input()
        if turn:
            TURNS.begin()
        try:
            result = focus.annotate(fn(*args, **kwargs), pre)
        finally:
            if turn:
                TURNS.end()
        if turn and TURNS.profile and isinstance(result, dict):
            result["game"] = "paused" if TURNS.paused else (TURNS.note or "running")
        return result
    return wrapper


async def _run(pool: ThreadPoolExecutor, fn, *args, **kwargs) -> Any:
    name = getattr(fn, "__name__", None) or getattr(getattr(fn, "func", None), "__name__", "call")
    start = time.monotonic()
    try:
        result = await asyncio.get_running_loop().run_in_executor(pool, functools.partial(_in_worker, pool, fn, *args, **kwargs))
        log.info("%s ok (%.2fs)", name, time.monotonic() - start)
        return result
    except Exception as e:
        log.warning("%s failed (%.2fs): %s", name, time.monotonic() - start, e)
        raise


INSTRUCTIONS = """Hardware tools for the user's Windows gaming handheld (ROG Ally-class: touch screen, built-in Xbox-style
controller, speakers, mic). Pair with the `{screen}` server (screen control): look there, act here.
- Fastest loop: observe (screen text + tap points) -> act (several steps in one call, e.g. tap_text + wait_text).
  screenshot here is faster and ~3x cheaper than `{screen}`'s; its to_screen maps image to screen pixels.
- Coordinates are physical screen pixels, the same as `{screen}` screenshots.
- powershell here keeps its session and is ~30x faster than `{screen}`'s; proc runs consoles (e.g. a game server).
- Input only reaches the window in front. After launching a game, call focus_window("<game>") once (not `{screen}`'s
  App switch, which can claim success when it failed): input tools keep it in front, and each result's `foreground`
  says where input went (plus a warning if it was probably lost).
- The gamepad is a VIRTUAL Xbox controller (games see a second controller). gamepad_connect before playing; it stays
  plugged while a game is in front. Armoury Crate's "external controller" notice is closed for you (nothing disabled).
- key_* send scan codes (work in games); mouse_look turns game cameras; touch_* inject real multi-touch.
- Real-time games: focus_window(pause={{"button": "start", "text": "<its pause text>"}}) makes them turn-based (they
  run only during your calls), then play each sub-goal as one behavior program (Python at frame rate), never one
  small move per call.
- Restore what you change (volume, brightness, display/power mode). Use keep_awake for long unattended work.
- Voice prompts from the handheld (hold View + Menu) arrive as separate agent sessions."""


def build_server(port: int, record_tools: bool = False, upgraded: bool = False) -> FastMCP:
    name = device_settings()["name"]
    mcp = FastMCP(f"{name}-handheld", instructions=INSTRUCTIONS.format(screen=name), host="127.0.0.1", port=port,
                  stateless_http=True)

    def tool():
        return lambda fn: mcp.tool(structured_output=False)(lean_result(fn))

    # ------------------------------------------------------------------------------------------ overview
    @tool()
    async def handheld_status() -> dict:
        """Battery, screen, brightness, volume, display/power mode, controllers, virtual pad, keep-awake. Good first
        call."""
        out: dict[str, Any] = {"server_version": __version__, "tools": updates.NOTICE.summary()}
        out.update(system.overview_status())
        for key, pool, fn in (("brightness", INPUT, system.brightness), ("display", INPUT, system.display_mode),
                              ("power_mode", INPUT, system.power_mode), ("speaker", COM, functools.partial(audio.volume_state, "speaker")),
                              ("microphone", COM, functools.partial(audio.volume_state, "microphone")),
                              ("microphone_privacy", COM, audio.microphone_privacy),
                              ("keep_awake", INPUT, system.keep_awake_status)):
            try:
                val = await _run(pool, fn)
                if key == "display":
                    val = val["current"]
                out[key] = val
            except Exception as e:
                out[key] = f"unavailable: {e}"
        out["controllers"] = [s for s in gamepad.xinput_states() if s["connected"]]
        out["virtual_gamepad"] = {"connected": gamepad.PAD.connected(), "xinput_slot": gamepad.PAD.index(),
                                  "idle_seconds": gamepad.PAD.idle_seconds()}
        out["foreground"] = focus.short(focus.foreground(), focus.target())
        return out

    # ------------------------------------------------------------------------------------------ screen
    @tool()
    async def screenshot(region: list[int] | None = None, window: str = "", max_side: int = 960, quality: int = 70,
                         only_if_changed: bool = False) -> Any:
        """Fast screenshot: small JPEG (~700 tokens), foreground window, cursor. region = [l, t, r, b] in screen px
        zooms in; window crops to one; to_screen maps image to screen px. only_if_changed: no image if unchanged."""
        if window:
            info = await _run(SCREEN, focus.find_window, window)
            if not info:
                raise ValueError(f"no window matches {window!r}")
            region = info["rect"]
        frozen = TURNS.frozen()
        shot = await _run(SCREEN, GRABBER.screenshot, region, max_side, quality, only_if_changed, frozen)
        meta = {**shot["meta"], "foreground": focus.short(focus.foreground()), "cursor": capture.cursor_pos()}
        if frozen is not None:
            meta["paused"] = "the game is paused; this is the frame from just before"

        text = TextContent(type="text", text=json.dumps(compact(meta), separators=(",", ":")))
        if shot["jpeg"] is None:
            return [text]
        return [ImageContent(type="image", data=base64.b64encode(shot["jpeg"]).decode(), mimeType="image/jpeg"), text]

    def read_text(region=None) -> dict:
        """Grab and OCR (on SCREEN): {"lines", "frame", "box", "ms"}."""
        t0 = time.monotonic()
        frame = TURNS.frozen() or GRABBER.grab()  # paused (turn-based play): the world as it was, not the pause menu
        box = capture.clamp_region(region, frame.left, frame.top, frame.width, frame.height)
        return {"lines": ocr.recognize(frame, box), "frame": frame, "box": box, "ms": round((time.monotonic() - t0) * 1000)}

    async def observe_impl(find: str = "", region=None, image: bool = False, max_lines: int = 60) -> tuple[dict, Any]:
        """(compact result, image content or None)."""
        seen = await _run(SCREEN, read_text, region)
        lines = seen["lines"]
        if find:
            best = ocr.find(lines, find)
            lines = [best] + [ln for ln in lines if ln is not best and find.lower() in ln["text"].lower()] if best else []
        out: dict[str, Any] = {"foreground": focus.short(focus.foreground()),
                               "text": [[ln["text"], *ocr.center(ln["box"])] for ln in lines[:max_lines]], "ms": seen["ms"]}
        if len(lines) > max_lines:
            out["more_lines"] = len(lines) - max_lines
        if not lines:
            out["note"] = f"no text matching {find!r}" if find else "no text found (a game scene?); use screenshot to look"
        if not image:
            return out, None
        jpeg, meta = await _run(SCREEN, capture.encode, seen["frame"], seen["box"])
        out["to_screen"] = meta["to_screen"]
        return out, ImageContent(type="image", data=base64.b64encode(jpeg).decode(), mimeType="image/jpeg")

    def with_image(result: dict, img) -> Any:
        if img is None:
            return result
        return [img, TextContent(type="text", text=json.dumps(compact(result), separators=(",", ":"), ensure_ascii=False))]

    @tool()
    async def observe(find: str = "", region: list[int] | None = None, image: bool = False, max_lines: int = 60) -> Any:
        """The screen as text (cheaper than a screenshot): foreground + OCR lines [text, x, y], x,y = the line's
        center in screen px (tap there). find = matching lines only; image adds a small screenshot."""
        return with_image(*await observe_impl(find, region, image, max_lines))

    @tool()
    async def act(steps: list[dict], observe: str = "") -> Any:
        """Several steps in one call, in order; stops at the first failure. Steps (screen px): {"press": ["a"],
        "ms": 120}, {"pad": [gamepad_sequence steps]}, {"tap": [x,y]}, {"tap_text": "Play"}, {"click": [x,y]},
        {"click_text": "OK"}, {"swipe": [x1,y1,x2,y2]}, {"key": ["enter"]}, {"type": "text"}, {"focus":
        "Minecraft"}, {"wait": 500}, {"wait_text": "Connected", "timeout": 10} (or "gone": true). observe =
        "text" | "image" returns the screen after."""
        if len(steps) > 40:
            raise ValueError("at most 40 steps per call")
        t_start, notes, failed = time.monotonic(), [], None
        pre = await _run(INPUT, focus.before_input)
        await _run(INPUT, TURNS.begin)
        ended = False
        try:
            out, img = await act_steps(steps, observe, t_start, notes, failed, pre)
            await _run(INPUT, TURNS.end)
            ended = True
            return with_image(with_game_state(out), img)
        finally:
            if not ended:
                await _run(INPUT, TURNS.end)

    def with_game_state(out: Any) -> Any:
        """Turn-based play: say whether the game is paused now (or why not)."""
        if TURNS.profile and isinstance(out, dict):
            out["game"] = "paused" if TURNS.paused else (TURNS.note or "running")
        return out

    async def act_steps(steps, observe, t_start, notes, failed, pre) -> tuple[dict, Any]:
        for i, step in enumerate(steps):
            if time.monotonic() - t_start > 90:
                failed = {"step": i, "error": "act ran out of time (90 s)"}
                break
            try:
                note = await act_step(step)
                if note:
                    notes.append(f"{i}: {note}")
            except Exception as e:
                failed = {"step": i, "error": str(e)}
                break
        done = len(steps) if failed is None else failed["step"]
        out: dict[str, Any] = {"done": f"{done}/{len(steps)}", "notes": notes or None, "failed": failed,
                               "ms": round((time.monotonic() - t_start) * 1000)}
        out = await _run(INPUT, focus.annotate, out, pre)
        img = None
        if observe in ("text", "image"):
            out["observe"], img = await observe_impl(image=observe == "image")
        return out, img

    async def act_step(step: dict) -> str | None:
        """Run one act step; returns a short note worth reporting (or None)."""
        kind = next((k for k in step if k not in ("ms", "timeout", "gone", "hold_ms", "button", "count")), None)
        arg, ms = step.get(kind), step.get("ms")
        if kind == "press":
            await _run(INPUT, gamepad.PAD.run_steps, [{"buttons": arg if isinstance(arg, list) else [arg], "ms": ms or 120}])
        elif kind == "pad":
            await _run(INPUT, gamepad.PAD.run_steps, arg)
        elif kind == "tap":
            await _run(INPUT, win_input.touch_tap, int(arg[0]), int(arg[1]), 1, step.get("hold_ms", 60))
        elif kind == "click":
            await _run(INPUT, win_input.mouse_click, int(arg[0]), int(arg[1]), step.get("button", "left"), step.get("count", 1))
        elif kind in ("tap_text", "click_text"):
            seen = await _run(SCREEN, read_text)
            hit = ocr.find(seen["lines"], str(arg))
            if not hit:
                raise LookupError(f"no text matching {arg!r} on screen")
            x, y = ocr.center(hit["box"])
            if kind == "tap_text":
                await _run(INPUT, win_input.touch_tap, x, y, 1, step.get("hold_ms", 60))
            else:
                await _run(INPUT, win_input.mouse_click, x, y, step.get("button", "left"), step.get("count", 1))
            return f"{'tapped' if kind == 'tap_text' else 'clicked'} {hit['text']!r} at [{x},{y}]"
        elif kind == "swipe":
            x1, y1, x2, y2 = (int(v) for v in arg)
            await _run(INPUT, win_input.touch_swipe, x1, y1, x2, y2, ms or 300, 0, 0)
        elif kind == "key":
            await _run(INPUT, win_input.key_press, arg if isinstance(arg, list) else [arg], ms or 50, 1, 100)
        elif kind == "type":
            await _run(INPUT, win_input.type_text, str(arg), 5)
        elif kind == "focus":
            def bring() -> dict:
                result = focus.focus_window(str(arg))
                if result.get("ok"):
                    focus.set_target(str(arg))
                return result
            result = await _run(INPUT, bring)
            if not result.get("ok"):
                raise RuntimeError(f"couldn't bring {arg!r} to the front ({result.get('foreground') or 'unknown'} is)")
        elif kind == "wait":
            await asyncio.sleep(min(float(arg), 30000) / 1000)
        elif kind == "wait_text":
            timeout, gone = min(float(step.get("timeout", 10)), 60), bool(step.get("gone"))
            deadline = time.monotonic() + timeout
            while True:
                seen = await _run(SCREEN, read_text)
                hit = ocr.find(seen["lines"], str(arg))
                if bool(hit) != gone:
                    return f"{arg!r} {'gone' if gone else 'visible'}" + (f" at {ocr.center(hit['box'])}" if hit else "")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{arg!r} {'still visible' if gone else 'not seen'} after {timeout:g} s")
                await asyncio.sleep(0.25)
        else:
            raise ValueError(f"unknown step {step!r}")
        return None

    # ------------------------------------------------------------------------------------------ real-time behaviors
    @tool()
    async def behavior(action: str, kind: str = "", params: dict | None = None, id: str = "", since: int = 0,
                       max_s: float = 30.0) -> dict:
        """Real-time loops on the handheld, no model round trips. action: start (kind, params, max_s; params.wait =
        return when done, params.after = id: queue it, params.replace = id: take over) | status (id, since) | stop
        (id or all) | kinds (docs) | save / skills / forget (programs that worked, per game). Kinds: program (Python at frame rate: real-time play), calibrate (a game's
        camera, once), guard (standing safety check), react, track, press_until, navigate, watch, script. A real
        controller moving stops them."""
        out = await _run(PROC, behave.run_tool, BEHAVIORS, action, kind, params, id, since, max_s)
        return with_game_state(out) if action == "start" and (params or {}).get("wait") else out

    # ------------------------------------------------------------------------------------------ processes
    @tool()
    async def powershell(script: str, timeout: float = 60.0, reset: bool = False) -> dict:
        """PowerShell in a persistent desktop-session runspace: state persists, ~50 ms per call, DPI-aware, errors
        as plain "ERROR:" lines. reset = start fresh."""
        return await _run(PS, pshost.HOST.run, script, timeout, reset)

    @tool()
    async def proc(action: str, name: str = "", command: str = "", cwd: str = "", text: str = "", pattern: str = "",
                   timeout: float = 10.0, cursor: int | None = None, max_lines: int = 60) -> dict:
        """Consoles you talk to (e.g. a game server), no window. action: start (name, command, cwd; pattern = wait
        for ready) | send (text = a stdin line; returns the reply, or waits for pattern) | read (new output) |
        wait (pattern, timeout) | stop (text = graceful command) | list. Updates wait while one runs."""
        return await _run(PROC, procs.run, PROCS, action, name, command, cwd, text, pattern, timeout, cursor, max_lines)

    # ------------------------------------------------------------------------------------------ gamepad
    @tool()
    async def gamepad_press(buttons: list[str], hold_ms: int = 120, repeat: int = 1, interval_ms: int = 150) -> dict:
        """Press virtual controller buttons together for hold_ms, optionally repeat. Buttons: a b x y lb rb lt rt
        back(view) start(menu) guide ls rs dpad_up dpad_down dpad_left dpad_right."""
        steps = []
        for i in range(max(1, min(int(repeat), 50))):
            steps.append({"buttons": buttons, "ms": hold_ms})
            if i < repeat - 1:
                steps.append({"ms": interval_ms})
        return await _run(INPUT, _focused(gamepad.PAD.run_steps), steps)

    @tool()
    async def gamepad_hold(duration_ms: int = 500, buttons: list[str] | None = None, left_stick: list[float] | None = None,
                           right_stick: list[float] | None = None, left_trigger: float = 0.0, right_trigger: float = 0.0) -> dict:
        """Hold a controller state for duration_ms, then neutral. Sticks [x, y] in -1..1 (y=1 up/forward), triggers
        0-1. Walk forward 2 s: left_stick [0, 1], duration_ms 2000."""
        step = {"buttons": buttons or [], "left_stick": left_stick, "right_stick": right_stick,
                "left_trigger": left_trigger, "right_trigger": right_trigger, "ms": duration_ms}
        return await _run(INPUT, _focused(gamepad.PAD.run_steps), [step])

    @tool()
    async def gamepad_sequence(steps: list[dict]) -> dict:
        """Timed controller steps (max 60 s), each replacing the last: {"buttons": [...], "left_stick": [x,y],
        "right_stick": [x,y], "left_trigger": 0-1, "right_trigger": 0-1, "ms": 200, "ramp_ms": 100 (ease in)};
        only "ms" = neutral pause. ~1 ms accurate; ends neutral."""
        return await _run(INPUT, _focused(gamepad.PAD.run_steps), steps)

    @tool()
    async def gamepad_status() -> dict:
        """Controllers in XInput slots 0-3 (buttons, sticks, triggers, battery), and the virtual pad's slot, idle
        time and last rumble from the game."""
        return {"slots": gamepad.xinput_states(),
                "virtual_gamepad": {"connected": gamepad.PAD.connected(), "xinput_slot": gamepad.PAD.index(),
                                    "idle_seconds": gamepad.PAD.idle_seconds(), "last_rumble_from_game": gamepad.PAD.last_rumble,
                                    "armoury_crate_notice": gamepad.PAD.last_notice,
                                    "kept_plugged": gamepad.PAD.pinned,
                                    "auto_unplug_after_idle_seconds": gamepad.idle_limit_s()},
                "foreground": focus.short(focus.foreground())}

    @tool()
    async def gamepad_watch(seconds: float = 5.0, slot: int | None = None, as_steps: bool = False) -> dict:
        """Record controller input for a few seconds (what the user presses). as_steps = as gamepad_sequence steps
        (slot 0 unless given), to replay a move you were shown."""
        out = await _run(STT, gamepad.xinput_watch, seconds, slot)
        if as_steps:
            steps = gamepad.to_steps(out["events"], slot=slot if slot is not None else 0)
            return {"seconds": out["seconds"], "steps": steps, "total_ms": sum(st["ms"] for st in steps)}
        return out

    @tool()
    async def gamepad_connect(keep_plugged: bool = True) -> dict:
        """Plug the virtual controller in ahead of time so the first press lands (waits for Windows, closes Armoury
        Crate's notice, restores focus, wakes the game's controller mode). keep_plugged: stays until gamepad_unplug;
        else unplugs when idle, never while a game is in front."""
        return await _run(INPUT, _focused(gamepad.PAD.connect, turn=False), keep_plugged)

    @tool()
    async def gamepad_unplug() -> dict:
        """Unplug the virtual controller now."""
        return {"unplugged": await _run(INPUT, gamepad.PAD.disconnect)}

    @tool()
    async def focus_window(target: str = "", remember: bool = True, pause: dict | None = None) -> dict:
        """Bring a window to the front and verify it (past Windows' foreground lock). target = title or process
        ("Minecraft"); remember = input tools refocus it. No target = what has focus and why input may not
        register; "none" clears the target. pause = {"button": "start", "text": "Game is paused"}: turn-based, the
        game runs only during your input calls; {} = off."""
        def run() -> dict:
            if not target:
                return {**focus.diagnose(), "windows": [focus.short(w) for w in focus.windows(8)],
                        "turns": TURNS.status()}
            if target.strip().lower() == "none":
                focus.set_target(None)
                TURNS.configure(None)
                return {"input_target": None, "foreground": focus.short(focus.foreground())}
            result = focus.focus_window(target)
            if remember and result.get("ok"):
                focus.set_target(target)
                result["input_target"] = target
            if pause is not None:
                result["turns"] = TURNS.configure(pause or None)
            return result
        return await _run(INPUT, run)

    @tool()
    async def controller_rumble(left: float = 0.6, right: float = 0.6, duration_ms: int = 300, slot: int = 0) -> dict:
        """Vibrate a physical controller (slot 0 = built-in, in gamepad mode): left = strong low motor, right =
        light high motor, 0-1."""
        return await _run(INPUT, gamepad.rumble, left, right, duration_ms, slot)

    # ------------------------------------------------------------------------------------------ touch
    @tool()
    async def touch_tap(x: int, y: int, count: int = 1, hold_ms: int = 60) -> dict:
        """Tap the touch screen with one finger at (x, y); count=2 double-taps."""
        return await _run(INPUT, _focused(win_input.touch_tap), x, y, count, hold_ms)

    @tool()
    async def touch_long_press(x: int, y: int, duration_ms: int = 900) -> dict:
        """Press and hold one finger at (x, y) (context menus, touch-and-hold game actions)."""
        return await _run(INPUT, _focused(win_input.touch_tap), x, y, 1, duration_ms)

    @tool()
    async def touch_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, hold_start_ms: int = 0,
                          hold_end_ms: int = 0) -> dict:
        """Swipe/drag one finger from (x1, y1) to (x2, y2). hold_start_ms > ~500 = drag-and-drop; hold_end_ms keeps
        the finger down at the end."""
        return await _run(INPUT, _focused(win_input.touch_swipe), x1, y1, x2, y2, duration_ms, hold_start_ms, hold_end_ms)

    @tool()
    async def touch_pinch(x: int, y: int, start_spread: int = 400, end_spread: int = 150, duration_ms: int = 500,
                          angle_deg: float = 0) -> dict:
        """Two-finger pinch at (x, y): spread = px between fingers, end > start zooms in; angle_deg rotates the
        finger axis."""
        return await _run(INPUT, _focused(win_input.touch_pinch), x, y, start_spread, end_spread, duration_ms, angle_deg)

    @tool()
    async def touch_gesture(fingers: list[list[list[int]]], duration_ms: int = 500) -> dict:
        """Custom multi-touch: up to 10 fingers, each a list of [x, y] points visited evenly over duration_ms.
        Two-finger swipe up: [[[800,700],[800,300]],[[1000,700],[1000,300]]]."""
        return await _run(INPUT, _focused(win_input.touch_path_gesture), fingers, duration_ms)

    # ------------------------------------------------------------------------------------------ keyboard / mouse
    @tool()
    async def key_press(keys: list[str], hold_ms: int = 50, repeat: int = 1, interval_ms: int = 100) -> dict:
        """Press keys together as game-safe scan codes, hold, release: ["ctrl","shift","esc"]; ["w"] + hold_ms 2000
        = walk 2 s. Keys: letters, digits, f1-f24, arrows, space, enter, esc, tab, shift, ctrl, alt, win, home,
        end, pageup, pagedown, insert, delete, backspace, numpad0-9, volume, punctuation."""
        return await _run(INPUT, _focused(win_input.key_press), keys, hold_ms, repeat, interval_ms)

    @tool()
    async def type_text(text: str, interval_ms: int = 5) -> dict:
        """Type Unicode text into whatever has focus (search boxes, chat, text fields). Newlines press Enter."""
        return await _run(INPUT, _focused(win_input.type_text), text, interval_ms)

    @tool()
    async def mouse_look(dx: int, dy: int, duration_ms: int = 250) -> dict:
        """Relative mouse movement, as games use for camera/aim (absolute clicks don't turn a game camera).
        Positive dx = right, positive dy = down."""
        return await _run(INPUT, _focused(win_input.mouse_move_relative), dx, dy, duration_ms)

    @tool()
    async def mouse_hold(button: str = "left", hold_ms: int = 500) -> dict:
        """Hold a mouse button at the current cursor position for hold_ms (e.g. mine/attack/charge in a game)."""
        return await _run(INPUT, _focused(win_input.mouse_hold), button, hold_ms)

    @tool()
    async def touch_keyboard(action: str = "status") -> dict:
        """The Windows on-screen touch keyboard: action = status | show | hide | toggle."""
        return await _run(COM, win_input.touch_keyboard, action)

    @tool()
    async def release_all_input() -> dict:
        """Emergency reset: release any held keys and put the virtual gamepad back to neutral."""
        released = await _run(INPUT, win_input.release_all_keys)
        if gamepad.PAD.connected():
            await _run(INPUT, _focused(gamepad.PAD.run_steps), [{"ms": 0}])
        return {"released_keys": released, "gamepad_neutral": gamepad.PAD.connected()}

    # ------------------------------------------------------------------------------------------ audio
    @tool()
    async def audio_devices() -> dict:
        """Speakers and microphones, the defaults, their volume/mute, and what's currently playing (peak level)."""
        return await _run(COM, audio.devices)

    @tool()
    async def set_volume(device: str = "speaker", level: int | None = None, mute: bool | None = None) -> dict:
        """Set speaker or microphone volume (0-100) and/or mute. Returns before/after so you can restore it."""
        return await _run(COM, audio.set_volume, device, level, mute)

    @tool()
    async def mic_record(seconds: float = 5.0, transcribe: bool = False, model: str = "base.en",
                         vocabulary: str | None = None) -> dict:
        """Record the mic N seconds (max 120) to a WAV: peak/RMS level (working or muted?), optionally transcribed."""
        result, samples = await _run(COM, audio.record_microphone, seconds)
        if transcribe:
            result["transcript"] = await _run(STT, speech.transcribe, samples, audio.RATE, model, None, vocabulary)
        return result

    @tool()
    async def speaker_capture(seconds: float = 5.0, transcribe: bool = False, model: str = "base.en",
                              vocabulary: str | None = None) -> dict:
        """Record what the handheld plays (loopback) for N seconds: loudness, or transcribe speech."""
        result, samples = await _run(COM, audio.record_speakers, seconds)
        if transcribe:
            result["transcript"] = await _run(STT, speech.transcribe, samples, audio.RATE, model, None, vocabulary)
        return result

    @tool()
    async def play_sound(wav_path: str | None = None, tone_hz: float = 880.0, seconds: float = 0.5, volume: float = 0.3) -> dict:
        """Play a WAV file on the handheld's speakers, or a test tone if no path is given."""
        if wav_path:
            return await _run(PLAY, audio.play_wav, wav_path)
        return await _run(PLAY, audio.play_tone, tone_hz, seconds, volume)

    # ------------------------------------------------------------------------------------------ speech
    @tool()
    async def speak(text: str, voice: str | None = None, rate: int = 0, volume: int = 100, save_wav: bool = False,
                    engine: str = "auto") -> dict:
        """Speak aloud (neural voice): voice af_heart (default), am_michael, bf_emma... or a Windows voice
        (list_voices); engine auto|neural|sapi; rate -10..10; volume 0-100; save_wav renders a WAV."""
        speed = 1.0 + max(-10, min(int(rate), 10)) * 0.05
        if save_wav:
            path = str(audio._new_path("tts"))
            if engine != "sapi" and not tts._is_sapi_voice(voice) and tts.KOKORO.installed():
                return await _run(PLAY, tts.render, text, path, voice, speed)
            return await _run(PLAY, speech.speak, text, voice, rate, volume, path)
        tts.TOOL_STOP.clear()
        return await _run(PLAY, tts.speak, text, tts.TOOL_STOP, voice, speed, max(0, min(int(volume), 100)) / 100, engine)

    @tool()
    async def list_voices() -> dict:
        """Text-to-speech voices on the handheld: the neural voices (natural, used by default) and the Windows voices."""
        return await _run(PLAY, tts.voices)

    @tool()
    async def listen(seconds: float = 5.0, model: str = "base.en", language: str | None = None,
                     vocabulary: str | None = None) -> dict:
        """Record the mic N seconds and transcribe locally (Whisper). model: tiny.en | base.en (default) | small.en;
        vocabulary: expected words, e.g. "Minecraft, Creeper"."""
        result, samples = await _run(COM, audio.record_microphone, seconds)
        result["transcript"] = await _run(STT, speech.transcribe, samples, audio.RATE, model, language, vocabulary)
        return result

    @tool()
    async def transcribe_audio_file(wav_path: str, model: str = "base.en", language: str | None = None,
                                    vocabulary: str | None = None) -> dict:
        """Transcribe a WAV file on the handheld (e.g. from mic_record, speaker_capture or speak save_wav)."""
        return await _run(STT, speech.transcribe_file, wav_path, model, language, vocabulary)

    # ------------------------------------------------------------------------------------------ display / power
    @tool()
    async def set_brightness(percent: int) -> dict:
        """Set screen brightness 0-100. Returns before/after."""
        return await _run(INPUT, system.set_brightness, percent)

    @tool()
    async def display_modes() -> dict:
        """Current resolution/refresh rate and all supported modes (e.g. 120 Hz and 60 Hz panels)."""
        return await _run(INPUT, system.display_mode)

    @tool()
    async def set_display_mode(refresh_hz: int | None = None, width: int | None = None, height: int | None = None) -> dict:
        """Temporarily change refresh rate and/or resolution (not saved; call again with the 'before' values to restore)."""
        return await _run(INPUT, system.set_display_mode, width, height, refresh_hz)

    @tool()
    async def power_mode(set_to: str | None = None) -> dict:
        """Get or set the Windows power mode: best_power_efficiency | balanced | best_performance (not Armoury
        Crate's TDP modes)."""
        if set_to:
            return await _run(INPUT, system.set_power_mode, set_to)
        return await _run(INPUT, system.power_mode)

    @tool()
    async def system_load() -> dict:
        """CPU and memory use, CPU clock, and the top CPU-consuming processes (e.g. while a game runs)."""
        return await _run(STT, system.system_load)

    @tool()
    async def keep_awake(minutes: float = 60) -> dict:
        """Keep the handheld awake N minutes (0 cancels) for long unattended work. Home only; tool calls also keep
        it awake 10 min."""
        return await _run(INPUT, system.keep_awake, minutes)

    # ------------------------------------------------------------------------------------------ voice prompts
    @mcp.custom_route("/state/{topic}", methods=["POST"])
    async def state_post(request: Request) -> JSONResponse:
        if not inbox.allowed(request.headers):
            return JSONResponse({"error": f"forbidden (send the header {inbox.STATE_HEADER}: 1 to 127.0.0.1)"},
                                status_code=403)
        body = await request.body()
        if len(body) > inbox.MAX_BODY:
            return JSONResponse({"error": "body over 64 KB"}, status_code=413)
        try:
            n = inbox.INBOX.post(request.path_params["topic"], json.loads(body or b"null"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "n": n})

    @tool()
    async def state(action: str = "read", topic: str = "", since: int = 0, match: str = "", timeout: float = 10.0,
                    max_items: int = 20) -> dict:
        """State programs on the handheld (e.g. a game server script) POST to 127.0.0.1:<port>/state/<topic> (header
        X-Relay-State: 1). action: read (latest + events since `since`) | wait (next event on topic matching text
        or key=value) | topics."""
        return await _run(PROC, inbox.run_tool, inbox.INBOX, action, topic, since, match, timeout, max_items)

    @mcp.custom_route("/voice/trigger", methods=["POST"])
    async def voice_trigger(request: Request) -> JSONResponse:
        # Only local programs may press the button: the custom header can't be sent cross-site without a CORS
        # preflight (which this server never approves), and the Host check stops DNS-rebinding pages.
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
        if request.headers.get(VOICE_HEADER) != "1" or host not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(voice.ASSISTANT.trigger("shortcut"))

    @tool()
    async def voice_assistant(action: str = "status", text: str | None = None) -> dict:
        """Push-to-talk voice prompts (hold View + Menu). action: status | listen (start now) | ask (send `text` as
        if spoken, for testing) | stop | allow_virtual_chord (2 min, testing). Don't call from inside a voice
        request."""
        a = action.lower()
        if a == "status":
            return await _run(STT, voice.ASSISTANT.status)
        if a == "listen":
            return voice.ASSISTANT.trigger("mcp")
        if a == "ask":
            if not text:
                raise ValueError("ask needs text")
            return voice.ASSISTANT.trigger("mcp", text)
        if a == "stop":
            tts.TOOL_STOP.set()
            if voice.ASSISTANT.state in ("listening", "speaking"):
                return voice.ASSISTANT.trigger("mcp")
            return {"state": voice.ASSISTANT.state, "action": "nothing to stop"}
        if a == "allow_virtual_chord":
            voice.ASSISTANT.allow_virtual_chord_until = time.monotonic() + 120
            return {"virtual_chord_allowed_for_s": 120}
        raise ValueError("action must be status, listen, ask, stop or allow_virtual_chord")

    for t in mcp._tool_manager.list_tools():  # leaner definitions in every tools/list (see lean.py)
        t.parameters = lean_schema(t.parameters)
        t.description = " ".join((t.description or "").split())  # docstrings carry their indentation otherwise
    if record_tools:
        names = [t.name for t in mcp._tool_manager.list_tools()]
        seen = USER_DIR / "tools-seen.json"
        updates.NOTICE = updates.Notice(updates.record(names, __version__, seen, upgraded=upgraded), path=seen)
        lean.NOTE_HOOK = updates.NOTICE.take
    return mcp


def _exit_with_parent(pid: int) -> None:
    """Stop when the agent that started us ends (its job object normally does this already)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        log.warning("parent process %d not found; not watching it", pid)
        return

    def wait() -> None:
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)
        log.info("agent (pid %d) ended; stopping", pid)
        try:
            gamepad.PAD.disconnect()
            win_input.release_all_keys()
        finally:
            os._exit(0)

    import threading
    threading.Thread(target=wait, name="parent-watch", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="RelayMCP hardware tools (MCP server)")
    parser.add_argument("--port", type=int, default=device_settings()["ports"]["hardware"])
    parser.add_argument("--parent-pid", type=int, help="exit when this process (the agent) ends")
    args = parser.parse_args()

    # An older runtime ran here before (it logged) but never recorded its tools: its clients know 0.1.0's. Decided
    # before this run's own log file exists.
    upgraded = not (USER_DIR / "tools-seen.json").exists() and (LOG_DIR / "hardware.log").exists()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    import faulthandler
    _fault_file = open(LOG_DIR / "hardware.crash.log", "a", encoding="utf-8")
    _fault_file.write(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    _fault_file.flush()
    faulthandler.enable(file=_fault_file, all_threads=True)
    handler = RotatingFileHandler(LOG_DIR / "hardware.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    win_input.set_dpi_awareness()
    focus.TRACKER.start()
    PROCS.reset_state()
    log.info("RelayMCP hardware server %s starting on 127.0.0.1:%d", __version__, args.port)
    if args.parent_pid:
        _exit_with_parent(args.parent_pid)
    try:
        voice.ASSISTANT.start_background()
    except Exception as e:
        log.warning("voice triggers unavailable: %s", e)
    try:
        build_server(args.port, record_tools=True, upgraded=upgraded).run(transport="streamable-http")
    finally:
        try:
            gamepad.PAD.disconnect()
            win_input.release_all_keys()
        except Exception:
            pass
        try:
            PROCS.stop_all()  # don't leave orphaned consoles behind
            pshost.HOST.stop()
            BEHAVIORS.stop()
        except Exception:
            pass
        log.info("RelayMCP hardware server stopped")


if __name__ == "__main__":
    main()
