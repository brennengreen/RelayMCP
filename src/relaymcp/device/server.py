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
import ctypes
import functools
import gc
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, audio, focus, gamepad, speech, system, tts, voice, win_input
from .lean import lean_result, lean_schema
from .paths import USER_DIR, VOICE_HEADER, device_settings

LOG_DIR = USER_DIR
log = logging.getLogger("relaymcp")


def _com_init() -> None:
    ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED


INPUT = ThreadPoolExecutor(max_workers=1, thread_name_prefix="input")          # touch/keys/mouse/gamepad, in order
COM = ThreadPoolExecutor(max_workers=1, thread_name_prefix="com", initializer=_com_init)  # capture, volume, keyboard UI
PLAY = ThreadPoolExecutor(max_workers=1, thread_name_prefix="play", initializer=_com_init)  # speech/tones (overlaps capture)
STT = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt", initializer=_com_init)  # whisper (CPU heavy)


def _in_worker(pool: ThreadPoolExecutor, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    finally:
        if pool is not INPUT:
            # Collect COM wrappers here, on their own thread, never later in the event loop thread.
            gc.collect()


def _focused(fn):
    """Input goes to whatever is in front: refocus the remembered input target first, then report what had focus."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        pre = focus.before_input()
        return focus.annotate(fn(*args, **kwargs), pre)
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
- Coordinates are physical pixels, the same as `{screen}` screenshots.
- Input only reaches the window in front. After launching a game, call focus_window("<game>") once: input tools keep it
  in front, and each result's `foreground` says where input went (plus a warning if it was probably lost).
- The gamepad is a VIRTUAL Xbox controller (games see a second controller). gamepad_connect before playing; it stays
  plugged while a game is in front. Armoury Crate's "external controller" notice is closed for you (nothing disabled).
- key_* send scan codes (work in games); mouse_look turns game cameras; touch_* inject real multi-touch.
- Restore what you change (volume, brightness, display/power mode). Use keep_awake for long unattended work.
- Voice prompts from the handheld (hold View + Menu) arrive as separate agent sessions."""


def build_server(port: int) -> FastMCP:
    name = device_settings()["name"]
    mcp = FastMCP(f"{name}-handheld", instructions=INSTRUCTIONS.format(screen=name), host="127.0.0.1", port=port,
                  stateless_http=True)

    def tool():
        return lambda fn: mcp.tool(structured_output=False)(lean_result(fn))

    # ------------------------------------------------------------------------------------------ overview
    @tool()
    async def handheld_status() -> dict:
        """Snapshot of the handheld: screen, battery/charging, brightness, volume, display mode, power mode,
        connected controllers, virtual gamepad, and keep-awake state. Good first call."""
        out: dict[str, Any] = {"server_version": __version__}
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

    # ------------------------------------------------------------------------------------------ gamepad
    @tool()
    async def gamepad_press(buttons: list[str], hold_ms: int = 120, repeat: int = 1, interval_ms: int = 150) -> dict:
        """Press virtual Xbox controller buttons together, hold_ms, release; optionally repeat.
        Buttons: a b x y lb rb lt rt back(view) start(menu) guide ls rs dpad_up dpad_down dpad_left dpad_right."""
        steps = []
        for i in range(max(1, min(int(repeat), 50))):
            steps.append({"buttons": buttons, "ms": hold_ms})
            if i < repeat - 1:
                steps.append({"ms": interval_ms})
        return await _run(INPUT, _focused(gamepad.PAD.run_steps), steps)

    @tool()
    async def gamepad_hold(duration_ms: int = 500, buttons: list[str] | None = None, left_stick: list[float] | None = None,
                           right_stick: list[float] | None = None, left_trigger: float = 0.0, right_trigger: float = 0.0) -> dict:
        """Hold a controller state for duration_ms, then return to neutral. Sticks are [x, y] from -1 to 1
        (y=1 is up/forward); triggers 0-1. Example: walk forward 2s = left_stick [0, 1], duration_ms 2000."""
        step = {"buttons": buttons or [], "left_stick": left_stick, "right_stick": right_stick,
                "left_trigger": left_trigger, "right_trigger": right_trigger, "ms": duration_ms}
        return await _run(INPUT, _focused(gamepad.PAD.run_steps), [step])

    @tool()
    async def gamepad_sequence(steps: list[dict]) -> dict:
        """Run timed controller steps in order (max 60s total). Each step replaces the previous state:
        {"buttons": [...], "left_stick": [x,y], "right_stick": [x,y], "left_trigger": 0-1, "right_trigger": 0-1, "ms": 200}.
        A step with only "ms" is a neutral pause. Ends in neutral."""
        return await _run(INPUT, _focused(gamepad.PAD.run_steps), steps)

    @tool()
    async def gamepad_status() -> dict:
        """Real and virtual controllers in XInput slots 0-3 (buttons held, sticks, triggers, battery), plus the virtual
        pad's slot, idle time and the last rumble a game sent to it."""
        return {"slots": gamepad.xinput_states(),
                "virtual_gamepad": {"connected": gamepad.PAD.connected(), "xinput_slot": gamepad.PAD.index(),
                                    "idle_seconds": gamepad.PAD.idle_seconds(), "last_rumble_from_game": gamepad.PAD.last_rumble,
                                    "armoury_crate_notice": gamepad.PAD.last_notice,
                                    "kept_plugged": gamepad.PAD.pinned,
                                    "auto_unplug_after_idle_seconds": gamepad.idle_limit_s()},
                "foreground": focus.short(focus.foreground())}

    @tool()
    async def gamepad_watch(seconds: float = 5.0, slot: int | None = None) -> dict:
        """Record controller input changes for a few seconds (e.g. what the user presses, or verify the virtual pad)."""
        return await _run(STT, gamepad.xinput_watch, seconds, slot)

    @tool()
    async def gamepad_connect(keep_plugged: bool = True) -> dict:
        """Plug the virtual controller in ahead of time (e.g. right after launching a game) so the first press lands:
        waits until Windows sees it, closes Armoury Crate's notice and gives focus back. keep_plugged=True keeps it
        connected until gamepad_unplug (it otherwise unplugs after idle time, never while a fullscreen game or the
        focus_window target is in front)."""
        return await _run(INPUT, _focused(gamepad.PAD.connect), keep_plugged)

    @tool()
    async def gamepad_unplug() -> dict:
        """Unplug the virtual controller now."""
        return {"unplugged": await _run(INPUT, gamepad.PAD.disconnect)}

    @tool()
    async def focus_window(target: str = "", remember: bool = True) -> dict:
        """Bring a window to the front and verify it really is (gets around Windows' foreground lock, unlike app
        switching that only reports success). target: window title or process name, e.g. "Minecraft". remember=True
        makes it the input target: input tools then refocus it if something steals focus, and their results say
        whether it was in front. Empty target = what has focus now; target="none" forgets the input target."""
        def run() -> dict:
            if not target:
                t = focus.target()
                return {"foreground": focus.short(focus.foreground(), t), "input_target": t and {k: t[k] for k in ("query", "title", "app")},
                        "windows": [focus.short(w) for w in focus.windows(8)]}
            if target.strip().lower() == "none":
                focus.set_target(None)
                return {"input_target": None, "foreground": focus.short(focus.foreground())}
            result = focus.focus_window(target)
            if remember and result.get("ok"):
                focus.set_target(target)
                result["input_target"] = target
            return result
        return await _run(INPUT, run)

    @tool()
    async def controller_rumble(left: float = 0.6, right: float = 0.6, duration_ms: int = 300, slot: int = 0) -> dict:
        """Vibrate a physical controller (slot 0 = the handheld's built-in controller, in gamepad mode). Left = strong
        low-frequency motor, right = light high-frequency motor, 0-1."""
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
        """Swipe/drag one finger from (x1, y1) to (x2, y2). hold_start_ms > ~500 makes it a drag-and-drop;
        hold_end_ms keeps the finger down at the end (e.g. hold a virtual joystick)."""
        return await _run(INPUT, _focused(win_input.touch_swipe), x1, y1, x2, y2, duration_ms, hold_start_ms, hold_end_ms)

    @tool()
    async def touch_pinch(x: int, y: int, start_spread: int = 400, end_spread: int = 150, duration_ms: int = 500,
                          angle_deg: float = 0) -> dict:
        """Two-finger pinch centered on (x, y). spread = pixels between the fingers; end > start zooms in,
        end < start zooms out. angle_deg rotates the finger axis (0 = horizontal)."""
        return await _run(INPUT, _focused(win_input.touch_pinch), x, y, start_spread, end_spread, duration_ms, angle_deg)

    @tool()
    async def touch_gesture(fingers: list[list[list[int]]], duration_ms: int = 500) -> dict:
        """Custom multi-touch: up to 10 fingers, each a list of [x, y] points visited evenly over duration_ms,
        all down and up together. Two-finger swipe up: [[[800,700],[800,300]], [[1000,700],[1000,300]]]."""
        return await _run(INPUT, _focused(win_input.touch_path_gesture), fingers, duration_ms)

    # ------------------------------------------------------------------------------------------ keyboard / mouse
    @tool()
    async def key_press(keys: list[str], hold_ms: int = 50, repeat: int = 1, interval_ms: int = 100) -> dict:
        """Press keys together as hardware scan codes (works in games), hold, release: ["ctrl","shift","esc"],
        ["w"] + hold_ms 2000 = walk 2 s. Names: letters, digits, f1-f24, space, enter, esc, tab, shift, ctrl, alt, win,
        up/down/left/right, home, end, pageup, pagedown, insert, delete, backspace, numpad0-9, volume keys, punctuation."""
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
        """Record the mic for N seconds (max 120) to a WAV: peak/RMS level (is it working or muted?), optionally
        transcribed. Needs Windows microphone access (see audio_devices)."""
        result, samples = await _run(COM, audio.record_microphone, seconds)
        if transcribe:
            result["transcript"] = await _run(STT, speech.transcribe, samples, audio.RATE, model, None, vocabulary)
        return result

    @tool()
    async def speaker_capture(seconds: float = 5.0, transcribe: bool = False, model: str = "base.en",
                              vocabulary: str | None = None) -> dict:
        """Record what the handheld plays (system audio loopback) for N seconds: check that a game makes sound,
        measure loudness, or transcribe speech."""
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
        """Speak text on the handheld (natural neural voice). engine: auto | neural | sapi. voice: af_heart
        (default), am_michael, bf_emma... or a Windows voice (list_voices). rate -10..10, volume 0-100. save_wav=True
        renders a WAV instead. voice_assistant(action="stop") cuts it off."""
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
        """Record the mic for N seconds and transcribe it locally (Whisper). model: tiny.en | base.en (default) |
        small.en (most accurate); others with language. vocabulary: expected names, e.g. "Minecraft, Creeper"."""
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
        """Get or set the Windows power mode: best_power_efficiency | balanced | best_performance.
        (Armoury Crate's Silent/Performance/Turbo TDP modes are separate and need Armoury Crate.)"""
        if set_to:
            return await _run(INPUT, system.set_power_mode, set_to)
        return await _run(INPUT, system.power_mode)

    @tool()
    async def system_load() -> dict:
        """CPU and memory use, CPU clock, and the top CPU-consuming processes (e.g. while a game runs)."""
        return await _run(STT, system.system_load)

    @tool()
    async def keep_awake(minutes: float = 60) -> dict:
        """Keep the handheld and its screen awake for N minutes (0 cancels), e.g. before long unattended work.
        Home only; it also stays awake 10 minutes after any tool call."""
        return await _run(INPUT, system.keep_awake, minutes)

    # ------------------------------------------------------------------------------------------ voice prompts
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
        """Push-to-talk voice prompts (hold View + Menu, Ctrl+Alt+Shift+F12, or 'Ask Copilot'). action: status |
        listen (start now) | ask (send `text` as if spoken; for testing) | stop (listening or speaking) |
        allow_virtual_chord (2 min, testing). Don't call from inside a voice request."""
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

    for t in mcp._tool_manager.list_tools():  # leaner definitions in every tools/list (see _lean_schema)
        t.parameters = lean_schema(t.parameters)
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
    log.info("RelayMCP hardware server %s starting on 127.0.0.1:%d", __version__, args.port)
    if args.parent_pid:
        _exit_with_parent(args.parent_pid)
    try:
        voice.ASSISTANT.start_background()
    except Exception as e:
        log.warning("voice triggers unavailable: %s", e)
    try:
        build_server(args.port).run(transport="streamable-http")
    finally:
        try:
            gamepad.PAD.disconnect()
            win_input.release_all_keys()
        except Exception:
            pass
        log.info("RelayMCP hardware server stopped")


if __name__ == "__main__":
    main()
