"""Push-to-talk voice prompts to your AI agent (GitHub Copilot CLI by default).

Hold View + Menu on the handheld's controller (about a second), press Ctrl+Alt+Shift+F12, or tap "Ask Copilot": a chime
plays and a small overlay says "Listening". Speak; recording stops when you stop talking. The handheld transcribes the
request locally (Whisper) and sends it through the SSH tunnel to the RelayMCP voice dispatcher on your computer (its
127.0.0.1:<voice port> is reverse-forwarded here), which runs the agent. The reply is shown in the overlay and spoken.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes

import numpy as np

from . import audio, gamepad, speech, tts
from .paths import VOICE_HEADER, device_settings
from .text import speakable

log = logging.getLogger("relaymcp")

_SETTINGS = device_settings()
HOST_URL = f"http://127.0.0.1:{_SETTINGS['ports']['voice']}"  # reverse-forwarded to the computer's voice dispatcher
HOST_LABEL = _SETTINGS["host_label"]                          # "your Mac", "your PC"...
VOICE_RATE = 16000
FRAME = 480                          # 30 ms
CHORD_BITS = gamepad.BUTTON_BITS["back"] | gamepad.BUTTON_BITS["start"]  # View + Menu
CHORD_HOLD_S = 0.7
HOTKEY_ID = 0xA11E
HOTKEY_MODS = 0x0001 | 0x0002 | 0x0004 | 0x4000  # MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
HOTKEY_VK = 0x7B                                 # F12
HOTKEY_NAME = "Ctrl+Alt+Shift+F12"
MAX_RECORD_S = 30
NO_SPEECH_TIMEOUT_S = 8
END_SILENCE_S = 1.1
VAD_MARGIN_DB = 12.0     # speech = this much above the noise floor...
VAD_MIN_DB = -50.0       # ...and at least this loud
VAD_IGNORE_FRAMES = 8    # first 240 ms: chime tail, button click
VAD_SPEECH_FRAMES = 8    # ~240 ms of voiced audio before it counts as speech
STT_MODEL = "small.en"       # ~1.2 s for a short request on the test handheld; base.en (~0.4 s) misheard real speech
STT_FALLBACK = None          # (transcribe() can redo low-confidence results with a bigger model)
MODEL_IDLE_UNLOAD_S = 15 * 60  # free speech models' memory (for games) after this long unused
VOCABULARY = _SETTINGS.get("vocabulary") or "Copilot, handheld, Armoury Crate, Minecraft, Steam, Game Pass"

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Private, fully-typed user32 (64-bit HWNDs; doesn't touch the argtypes other modules see on ctypes.windll.user32).
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
_u32.SetWindowPos.restype = wintypes.BOOL
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.ShowWindow.restype = wintypes.BOOL
_u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.GetWindowLongW.restype = ctypes.c_long
_u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_u32.SetWindowLongW.restype = ctypes.c_long
HWND_TOPMOST = wintypes.HWND(-1)
SWP_KEEP_TOPMOST = 0x0010 | 0x0002 | 0x0001 | 0x0040  # NOACTIVATE | NOMOVE | NOSIZE | SHOWWINDOW


def _com_init() -> None:
    ctypes.windll.ole32.CoInitializeEx(None, 0)


def _tone_sequence(notes: list[tuple[float, float]], volume: float = 0.22) -> np.ndarray:
    out = []
    for freq, secs in notes:
        n = int(audio.RATE * secs)
        t = np.arange(n) / audio.RATE
        x = np.sin(2 * np.pi * freq * t) * volume if freq else np.zeros(n)
        fade = min(n // 4, int(audio.RATE * 0.008))
        if fade:
            ramp = np.linspace(0, 1, fade)
            x[:fade] *= ramp
            x[-fade:] *= ramp[::-1]
        out.append(x)
    return np.concatenate(out).astype(np.float32).reshape(-1, 1)


CHIMES = {
    "start": _tone_sequence([(659, 0.07), (0, 0.02), (988, 0.09)]),
    "stop": _tone_sequence([(988, 0.06), (0, 0.02), (659, 0.08)]),
    "reply": _tone_sequence([(784, 0.06), (0, 0.015), (1047, 0.08)], 0.16),
    "error": _tone_sequence([(311, 0.12), (0, 0.04), (233, 0.16)]),
}


def chime(kind: str) -> None:
    try:
        audio._sc().default_speaker().play(CHIMES[kind], samplerate=audio.RATE)
    except Exception as e:
        log.warning("chime failed: %s", e)


# ---------------------------------------------------------------------------------------------------- overlay

class Overlay:
    """Small always-on-top status panel (top center). Never takes focus from the game or app in front."""

    KINDS = {  # icon glyph (Segoe Fluent Icons / MDL2), accent color
        "listening": ("\uE720", "#4cc2ff"),
        "working": ("\uE895", "#c9a0ff"),
        "thinking": ("\uE895", "#ffc83d"),
        "reply": ("\uE8F2", "#6ccb5f"),
        "error": ("\uE7BA", "#ff6b6b"),
    }
    WIDTH = 900

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._failed = False
        self.on_tap = None

    def _ensure(self) -> bool:
        if self._failed:
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._main, daemon=True, name="voice-overlay")
            self._thread.start()
        return True

    def show(self, kind: str, title: str, body: str = "", hint: str = "", autohide_s: float | None = None,
             timer: bool = False) -> None:
        if self._ensure():
            self._q.put(("show", kind, title, body, hint, autohide_s, timer))

    def level(self, db: float) -> None:
        if self._thread is not None and not self._failed:
            self._q.put(("level", db))

    def hide(self, after_s: float = 0) -> None:
        if self._thread is not None and not self._failed:
            self._q.put(("hide", after_s))

    def _main(self) -> None:
        try:
            import tkinter as tk
            from tkinter import font as tkfont

            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            bg = "#1c1c21"
            root.configure(bg=bg)
            families = set(tkfont.families(root))
            icon_family = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
            outer = tk.Frame(root, bg=bg, padx=26, pady=18)
            outer.pack(fill="both", expand=True)
            icon = tk.Label(outer, bg=bg, font=(icon_family, -40))
            icon.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 20), pady=(4, 0))
            title = tk.Label(outer, bg=bg, fg="#ffffff", font=("Segoe UI Semibold", -28), anchor="w", justify="left")
            title.grid(row=0, column=1, sticky="w")
            body = tk.Label(outer, bg=bg, fg="#dcdcdc", font=("Segoe UI", -23), anchor="w", justify="left",
                            wraplength=self.WIDTH - 140)
            body.grid(row=1, column=1, sticky="w", pady=(4, 0))
            meter = tk.Canvas(outer, bg="#2c2c33", height=8, width=self.WIDTH - 140, highlightthickness=0)
            hint = tk.Label(outer, bg=bg, fg="#8a8a93", font=("Segoe UI", -17), anchor="w", justify="left",
                            wraplength=self.WIDTH - 140)
            hint.grid(row=3, column=1, sticky="w", pady=(8, 0))
            bar = meter.create_rectangle(0, 0, 0, 8, width=0, fill="#4cc2ff")
            state = {"kind": None, "token": 0, "shown": False, "t0": 0.0, "timer": False, "title": ""}

            def tap(_event=None):
                if self.on_tap:
                    try:
                        self.on_tap()
                    except Exception as e:
                        log.warning("overlay tap handler failed: %s", e)

            for w in (root, outer, icon, title, body, meter, hint):
                w.bind("<Button-1>", tap)

            def place() -> None:
                root.update_idletasks()
                h = outer.winfo_reqheight()
                sw = root.winfo_screenwidth()
                root.geometry(f"{self.WIDTH}x{h}+{(sw - self.WIDTH) // 2}+26")

            def apply_styles() -> int:
                hwnd = int(root.wm_frame(), 16)
                GWL_EXSTYLE = -20
                ex = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                _u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | 0x08000000 | 0x00000080 | 0x00000008)  # NOACTIVATE|TOOLWINDOW|TOPMOST
                try:
                    pref = ctypes.c_int(2)  # DWMWCP_ROUND
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
                except Exception:
                    pass
                return hwnd

            root.update_idletasks()
            hwnd = apply_styles()

            def show_window() -> None:
                if not state["shown"]:
                    root.deiconify()
                    state["shown"] = True
                _u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                _u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_KEEP_TOPMOST)

            def hide_now(token: int) -> None:
                if token == state["token"] and state["shown"]:
                    root.withdraw()
                    state["shown"] = False

            def tick() -> None:
                if state["shown"]:
                    if state["timer"]:
                        title.configure(text=f"{state['title']}  {int(time.monotonic() - state['t0'])}s")
                    # Games that go borderless-fullscreen can push themselves above other topmost windows.
                    _u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_KEEP_TOPMOST)
                root.after(500, tick)

            def poll() -> None:
                try:
                    while True:
                        item = self._q.get_nowait()
                        if item[0] == "show":
                            _, kind, t, b, h, autohide_s, timer = item
                            glyph, color = self.KINDS.get(kind, self.KINDS["working"])
                            state.update(kind=kind, title=t, timer=timer, t0=time.monotonic())
                            state["token"] += 1
                            icon.configure(text=glyph, fg=color)
                            title.configure(text=t)
                            body.configure(text=b)
                            body.grid() if b else body.grid_remove()
                            hint.configure(text=h)
                            hint.grid() if h else hint.grid_remove()
                            if kind == "listening":
                                meter.grid(row=2, column=1, sticky="w", pady=(10, 0))
                                meter.coords(bar, 0, 0, 0, 8)
                            else:
                                meter.grid_remove()
                            place()
                            show_window()
                            if autohide_s:
                                tok = state["token"]
                                root.after(int(autohide_s * 1000), lambda tok=tok: hide_now(tok))
                        elif item[0] == "level":
                            if state["kind"] == "listening":
                                frac = min(1.0, max(0.0, (item[1] + 60) / 45))
                                meter.coords(bar, 0, 0, int(frac * (self.WIDTH - 140)), 8)
                        elif item[0] == "hide":
                            tok = state["token"]
                            if item[1]:
                                root.after(int(item[1] * 1000), lambda tok=tok: hide_now(tok))
                            else:
                                hide_now(tok)
                except queue.Empty:
                    pass
                root.after(30, poll)

            root.after(30, poll)
            root.after(500, tick)
            root.mainloop()
        except Exception as e:
            self._failed = True
            log.warning("voice overlay unavailable (sounds and speech still work): %s", e)


# ---------------------------------------------------------------------------------------------------- assistant

class VoiceAssistant:
    def __init__(self) -> None:
        self.state = "idle"  # idle | listening | transcribing | thinking | speaking
        self._lock = threading.Lock()
        self._stop_recording = threading.Event()
        self._stop_speaking = threading.Event()
        self.overlay = Overlay()
        self.overlay.on_tap = self._on_overlay_tap
        self.last: dict = {}
        self.hotkey_ok: bool | None = None
        self.allow_virtual_chord_until = 0.0
        self.pads: dict = {}
        self._started = False

    # ---- triggers
    def start_background(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._chord_loop, daemon=True, name="voice-chord").start()
        threading.Thread(target=self._hotkey_loop, daemon=True, name="voice-hotkey").start()
        threading.Thread(target=self._janitor, daemon=True, name="voice-janitor").start()

    def _janitor(self) -> None:
        while True:
            time.sleep(60)
            if self.state != "idle":
                continue
            try:
                freed = speech.unload_idle(MODEL_IDLE_UNLOAD_S) + (["kokoro"] if tts.KOKORO.unload_if_idle(MODEL_IDLE_UNLOAD_S) else [])
                if freed:
                    import gc
                    gc.collect()
                    log.info("voice: unloaded idle speech models %s", freed)
            except Exception as e:
                log.warning("voice: unloading models failed: %s", e)

    def trigger(self, source: str, text: str | None = None) -> dict:
        """Start listening (or, with text, skip the microphone). While listening: send now. While speaking: stop."""
        with self._lock:
            state = self.state
            if state == "idle":
                self.state = "listening" if text is None else "thinking"
                self._stop_recording.clear()
                self._stop_speaking.clear()
                threading.Thread(target=self._session, args=(source, text), daemon=True, name="voice-session").start()
                return {"started": True, "source": source, "mode": "typed text" if text else "listening"}
            if state == "listening":
                self._stop_recording.set()
                return {"action": "stopped listening; sending what was said"}
            if state == "speaking":
                self._stop_speaking.set()
                return {"action": "stopped speaking"}
            return {"busy": state}

    def _on_overlay_tap(self) -> None:
        if self.state == "listening":
            self._stop_recording.set()
        elif self.state == "speaking":
            self._stop_speaking.set()
            self.overlay.hide()
        elif self.state == "idle":
            self.overlay.hide()

    def _chord_loop(self) -> None:
        held_since = None
        fired = False
        connected: list[int] = []
        next_scan = 0.0
        while True:
            try:
                now = time.monotonic()
                if now >= next_scan:  # polling disconnected XInput slots is slow, so rescan every 2 s
                    connected = [i for i in range(4)
                                 if gamepad._xinput.XInputGetState(i, ctypes.byref(gamepad.XINPUT_STATE())) == 0]
                    next_scan = now + 2.0
                virtual = gamepad.PAD.index()
                self.pads = {"xinput_slots": connected, "virtual_slot": virtual}
                allow_virtual = now < self.allow_virtual_chord_until
                pressed = False
                for i in connected:
                    if i == virtual and not allow_virtual:
                        continue
                    st = gamepad.XINPUT_STATE()
                    if gamepad._xinput.XInputGetState(i, ctypes.byref(st)) == 0 and \
                            (st.Gamepad.wButtons & CHORD_BITS) == CHORD_BITS:
                        pressed = True
                        break
                if pressed:
                    if held_since is None:
                        held_since = now
                    elif not fired and now - held_since >= CHORD_HOLD_S:
                        fired = True
                        log.info("voice: View + Menu chord")
                        self.trigger("controller")
                else:
                    held_since = None
                    fired = False
                time.sleep(0.04)
            except Exception as e:
                log.warning("voice chord loop error: %s", e)
                time.sleep(2)

    def _hotkey_loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.RegisterHotKey(None, HOTKEY_ID, HOTKEY_MODS, HOTKEY_VK):
            self.hotkey_ok = False
            log.warning("voice: couldn't register %s (error %s)", HOTKEY_NAME, ctypes.get_last_error())
            return
        self.hotkey_ok = True
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:  # WM_HOTKEY
                log.info("voice: %s", HOTKEY_NAME)
                self.trigger("hotkey")

    # ---- one voice interaction
    def _set(self, state: str) -> None:
        with self._lock:
            self.state = state

    def _session(self, source: str, typed: str | None) -> None:
        _com_init()
        t0 = time.monotonic()
        record = {"source": source, "started": time.strftime("%H:%M:%S")}
        try:
            health = self._host_health()
            if not health.get("ok"):
                self._fail(f"Can't reach {HOST_LABEL}",
                           f"{HOST_LABEL[:1].upper() + HOST_LABEL[1:]} needs to be awake and on your home network.",
                           record, "computer unreachable")
                return
            tts.preload()  # the neural voice loads while you talk and Copilot works
            if typed is None:
                threading.Thread(target=self._warm_model, daemon=True).start()
                self.overlay.show("listening", "Listening\u2026", "",
                                  "Stops when you stop talking \u00b7 hold View + Menu again or tap here to send now")
                chime("start")
                samples, heard, manual, vad = self._record()
                record["vad"] = vad
                if not heard and not manual:
                    chime("error")
                    self.overlay.show("error", "I didn't hear anything", "", "", autohide_s=3)
                    record["result"] = "no speech"
                    return
                chime("stop")
                self._set("transcribing")
                self.overlay.show("working", "Transcribing\u2026")
                tr = speech.transcribe(samples, VOICE_RATE, STT_MODEL, None, VOCABULARY, fallback=STT_FALLBACK)
                text = tr["text"].strip()
                record["audio_s"] = tr["audio_seconds"]
                record["stt"] = {k: tr.get(k) for k in ("model", "avg_logprob", "transcribe_seconds", "retried_from")
                                 if tr.get(k) is not None}
                if not text or not re.search(r"[A-Za-z0-9]", text):
                    if manual and not heard:  # pressed again before saying anything
                        self.overlay.show("working", "Cancelled", "", "", autohide_s=1.5)
                        record["result"] = "cancelled"
                        return
                    self._fail("Sorry, I didn't catch that", "", record, "empty transcript", speak=False)
                    return
            else:
                text = typed.strip()
            record["prompt"] = text
            self._set("thinking")
            self.overlay.show("thinking", "Copilot is working", f"\u201c{text}\u201d", "", timer=True)
            result = self._ask_mac(text)
            record["seconds"] = round(time.monotonic() - t0, 1)
            if not result.get("ok"):
                err = result.get("error", "unknown error")
                msg = {"busy": "Copilot is still working on your last request.",
                       "unreachable": f"I lost the connection to {HOST_LABEL}."}.get(err, f"Something went wrong: {err}")
                self._fail("Copilot couldn't answer", msg, record, err)
                return
            reply = (result.get("reply") or "").strip() or "Done."
            record["reply"] = reply
            self._set("speaking")
            self.overlay.show("reply", "Copilot", reply if len(reply) <= 700 else reply[:700] + "\u2026",
                              "Tap to dismiss")
            chime("reply")
            record["tts"] = self._speak(speakable(reply), result.get("tts") or {})
            self.overlay.hide(after_s=6)
            record["result"] = "ok"
        except Exception as e:
            log.exception("voice session failed")
            self._fail("Voice prompt failed", str(e)[:200], record, str(e))
        finally:
            record["total_s"] = round(time.monotonic() - t0, 1)
            self.last = record
            log.info("voice: %s", json.dumps(record)[:600])
            self._set("idle")

    def _fail(self, title: str, detail: str, record: dict, reason: str, speak: bool = True) -> None:
        record["result"] = f"error: {reason}"
        chime("error")
        self.overlay.show("error", title, detail, "", autohide_s=6)
        if speak:
            self._set("speaking")
            self._speak(f"{title}. {detail}" if detail else title)

    def _warm_model(self) -> None:
        try:
            speech._get_model(STT_MODEL)
        except Exception as e:
            log.warning("voice: model warm-up failed: %s", e)

    def _record(self) -> tuple[np.ndarray, bool, bool, dict]:
        """Record until the speaker stops. Returns (samples, heard speech, stopped manually, VAD stats).

        Speech only counts once there's about a quarter second of voiced audio, so a click (like releasing View + Menu
        right next to the mic), a bump or the chime's tail can't end the recording before you start talking; such
        blips are forgotten after a pause and listening continues."""
        mic = audio._sc().default_microphone()
        frames: list[np.ndarray] = []
        levels: list[float] = []
        voiced = silent = 0
        speaking = False
        peak = -120.0
        floor = threshold = 0.0
        manual = False
        with mic.recorder(samplerate=VOICE_RATE, channels=1, blocksize=FRAME) as rec:
            while True:
                if self._stop_recording.is_set():
                    manual = True
                    break
                block = rec.record(numframes=FRAME)
                x = (block[:, 0] if block.ndim > 1 else block).astype(np.float32)
                frames.append(x)
                db = 20 * math.log10(float(np.sqrt(np.mean(np.square(x)))) + 1e-9)
                self.overlay.level(db)
                levels.append(db)
                if not speaking:  # noise floor: a low percentile of the last ~3 s; frozen once you're talking
                    floor = min(max(float(np.percentile(levels[-100:], 20)), -72.0), -42.0)
                    threshold = max(floor + VAD_MARGIN_DB, VAD_MIN_DB)
                elapsed = len(frames) * FRAME / VOICE_RATE
                if len(frames) > VAD_IGNORE_FRAMES:
                    peak = max(peak, db)
                    if db > threshold:
                        voiced += 1
                        silent = 0
                        if voiced >= VAD_SPEECH_FRAMES:
                            speaking = True
                    else:
                        silent += 1
                        if silent * FRAME / VOICE_RATE >= END_SILENCE_S:
                            if speaking:
                                break
                            voiced = 0  # just a blip: keep waiting for real speech
                if not speaking and elapsed >= NO_SPEECH_TIMEOUT_S:
                    break
                if elapsed >= MAX_RECORD_S:
                    break
        samples = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        stats = {"floor_db": round(floor, 1), "threshold_db": round(threshold, 1),
                 "peak_db": round(peak, 1) if peak > -120 else None, "voiced_frames": voiced}
        return samples, speaking, manual, stats

    def _host_health(self) -> dict:
        try:
            with _NO_PROXY.open(HOST_URL + "/health", timeout=3) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _ask_mac(self, text: str) -> dict:
        body = json.dumps({"text": text, "source": "relaymcp-voice", "device": _SETTINGS["name"]}).encode()
        req = urllib.request.Request(HOST_URL + "/prompt", body, {"Content-Type": "application/json",
                                                                  VOICE_HEADER: "1"})
        try:
            with _NO_PROXY.open(req, timeout=20 * 60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError) as e:
            log.warning("voice: request to the computer failed: %s", e)
            return {"ok": False, "error": "unreachable"}

    def _speak(self, text: str, settings: dict | None = None) -> dict:
        """Speak with the natural voice (or the Windows voice); stops when View + Menu is held or the overlay is tapped.
        settings (from the computer's voice config): voice, speed, engine."""
        settings = settings or {}
        try:
            info = tts.speak(text, self._stop_speaking, voice=settings.get("voice"), speed=float(settings.get("speed") or 1.0),
                             engine=settings.get("engine") or "auto")
            return {k: info[k] for k in ("engine", "voice", "first_audio_s", "stopped") if k in info}
        except Exception as e:
            log.warning("voice: speaking failed: %s", e)
            return {"error": str(e)[:200]}

    def status(self) -> dict:
        return {
            "state": self.state,
            "triggers": {
                "controller": "hold View + Menu for about a second (Gamepad mode)",
                "hotkey": f"{HOTKEY_NAME} ({'registered' if self.hotkey_ok else 'not available'})",
                "shortcut": "'Ask Copilot' on the Desktop and in Start",
            },
            "controllers": self.pads,
            "computer": self._host_health(),
            "last": self.last,
        }


ASSISTANT = VoiceAssistant()
