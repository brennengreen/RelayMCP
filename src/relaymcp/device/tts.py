"""Text-to-speech for the handheld: a natural neural voice (Kokoro, runs locally) with Windows SAPI voices as the fallback.

Replies are spoken sentence by sentence: the next sentence is synthesized while the current one plays, so speech starts
about a second after a reply arrives and stops within ~0.1 s when asked (View + Menu, overlay tap, or stop).

Kokoro needs the `kokoro-onnx` package and two model files (~200 MB), downloaded once to
%LOCALAPPDATA%\\RelayMCP\\models\\kokoro and checked against pinned SHA-256 hashes:
    python -m relaymcp.device.tts --download
Without them everything still works with the built-in Windows voices.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from . import audio
from .paths import USER_DIR
from .text import chunks

log = logging.getLogger("relaymcp")

KOKORO_DIR = USER_DIR / "models" / "kokoro"
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODEL = "kokoro-v1.0.fp16.onnx"  # fastest on the test handheld's CPU (int8 is slower than real time there)
KOKORO_VOICES = "voices-v1.0.bin"
KOKORO_FILES = {
    KOKORO_MODEL: ("c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204", 177464787),
    KOKORO_VOICES: ("bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d", 28214398),
}
KOKORO_RATE = 24000
DEFAULT_VOICE = "af_heart"
LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "j": "ja", "p": "pt-br", "z": "cmn"}
BLOCK = 2400  # 100 ms at 24 kHz: how quickly a stop takes effect

TOOL_STOP = threading.Event()  # stops a `speak` tool call (set by voice_assistant stop)


# ---------------------------------------------------------------------------------------------------- Kokoro

class _Kokoro:
    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()
        self.last_used = 0.0

    @staticmethod
    def installed() -> bool:
        try:
            import kokoro_onnx  # noqa: F401
        except Exception:
            return False
        return all((KOKORO_DIR / name).is_file() and (KOKORO_DIR / name).stat().st_size == size
                   for name, (_, size) in KOKORO_FILES.items())

    def get(self):
        with self._lock:
            if self._engine is None:
                from kokoro_onnx import Kokoro
                start = time.monotonic()
                self._engine = Kokoro(str(KOKORO_DIR / KOKORO_MODEL), str(KOKORO_DIR / KOKORO_VOICES))
                log.info("tts: Kokoro loaded in %.1fs", time.monotonic() - start)
            self.last_used = time.monotonic()
            return self._engine

    def unload_if_idle(self, max_idle_s: float) -> bool:
        with self._lock:
            if self._engine is not None and time.monotonic() - self.last_used > max_idle_s:
                self._engine = None
                return True
        return False


KOKORO = _Kokoro()


def preload() -> None:
    """Load the neural voice in the background (a voice request takes long enough to hide the ~2 s load)."""
    if KOKORO.installed():
        threading.Thread(target=_safe_load, name="tts-preload", daemon=True).start()


def _safe_load() -> None:
    try:
        KOKORO.get()
    except Exception as e:
        log.warning("tts: Kokoro unavailable: %s", e)


def download_models(verbose: bool = False) -> dict:
    """Fetch the Kokoro model files (skips files already present), verifying their SHA-256 hashes."""
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    fetched = []
    for name, (sha256, size) in KOKORO_FILES.items():
        dst = KOKORO_DIR / name
        if dst.is_file() and dst.stat().st_size == size:
            continue
        part = dst.with_name(name + ".part")
        digest = hashlib.sha256()
        if verbose:
            print(f"downloading {name} ({size / 1e6:.0f} MB)...", flush=True)
        with urllib.request.urlopen(KOKORO_URL + name, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{name} failed its SHA-256 check")
        os.replace(part, dst)
        fetched.append(name)
    return {"dir": str(KOKORO_DIR), "downloaded": fetched, "ready": KOKORO.installed()}


# ---------------------------------------------------------------------------------------------------- speaking

def speak(text: str, stop: threading.Event | None = None, voice: str | None = None, speed: float = 1.0,
          volume: float = 1.0, engine: str = "auto") -> dict:
    """Speak text on the default speakers; blocks until done or `stop` is set. engine: auto | neural | sapi."""
    text = text.strip()
    if not text:
        raise ValueError("nothing to say")
    stop = stop or threading.Event()
    engine = (engine or "auto").lower()
    if engine not in ("auto", "neural", "sapi"):
        raise ValueError("engine must be auto, neural or sapi")
    if engine != "sapi" and not _is_sapi_voice(voice):
        if KOKORO.installed():
            try:
                return _speak_kokoro(text, stop, voice, speed, volume)
            except Exception as e:
                if engine == "neural" or stop.is_set():
                    raise
                log.warning("tts: neural voice failed (%s); using the Windows voice", e)
        elif engine == "neural":
            raise RuntimeError("the neural voice isn't installed (python -m relaymcp.device.tts --download)")
    return _speak_sapi(text, stop, voice if _is_sapi_voice(voice) else None, speed, volume)


def _is_sapi_voice(voice: str | None) -> bool:
    return bool(voice) and (" " in voice or voice.lower().startswith("microsoft"))


def _kokoro_voice(k, voice: str | None) -> str:
    names = k.get_voices()
    if voice and voice in names:
        return voice
    if voice:
        log.warning("tts: unknown voice %r; using %s", voice, DEFAULT_VOICE)
    return DEFAULT_VOICE


def _synthesize(k, text: str, voice: str, speed: float) -> np.ndarray:
    samples, rate = k.create(text, voice=voice, speed=max(0.5, min(float(speed), 2.0)), lang=LANGS.get(voice[0], "en-us"))
    if rate != KOKORO_RATE:
        raise RuntimeError(f"unexpected sample rate {rate}")
    return np.asarray(samples, dtype=np.float32)


def _speak_kokoro(text: str, stop: threading.Event, voice: str | None, speed: float, volume: float) -> dict:
    k = KOKORO.get()
    name = _kokoro_voice(k, voice)
    pieces: queue.Queue = queue.Queue(maxsize=2)

    def produce() -> None:
        try:
            for piece in chunks(text):
                if stop.is_set():
                    break
                pieces.put(_synthesize(k, piece, name, speed))
        except Exception as e:  # handed to the player thread
            pieces.put(e)
        finally:
            pieces.put(None)

    start = time.monotonic()
    threading.Thread(target=produce, name="tts-synth", daemon=True).start()
    first_audio = None
    played = 0.0
    gain = max(0.0, min(float(volume), 1.0))
    with audio._sc().default_speaker().player(samplerate=KOKORO_RATE, channels=1, blocksize=BLOCK // 2) as player:
        while not stop.is_set():
            try:
                item = pieces.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            if first_audio is None:
                first_audio = time.monotonic() - start
            x = item * gain
            for i in range(0, len(x), BLOCK):
                if stop.is_set():
                    break
                player.play(x[i:i + BLOCK].reshape(-1, 1))
            played += len(x) / KOKORO_RATE
        if not stop.is_set():
            player.play(np.zeros((BLOCK * 2, 1), dtype=np.float32))  # let the last word finish before closing
    KOKORO.last_used = time.monotonic()
    return {"engine": "neural (Kokoro)", "voice": name, "first_audio_s": round(first_audio or 0.0, 2),
            "audio_s": round(played, 2), "seconds": round(time.monotonic() - start, 2), "stopped": stop.is_set()}


def _speak_sapi(text: str, stop: threading.Event, voice: str | None, speed: float, volume: float) -> dict:
    """In-process SAPI: starts instantly and can be cut off."""
    import comtypes.client
    start = time.monotonic()
    sp = comtypes.client.CreateObject("SAPI.SpVoice")
    if voice:
        tokens = sp.GetVoices()
        for i in range(tokens.Count):
            if voice.lower() in tokens.Item(i).GetDescription().lower():
                sp.Voice = tokens.Item(i)
                break
    sp.Rate = max(-10, min(10, round((float(speed) - 1.0) * 10)))
    sp.Volume = max(0, min(100, round(float(volume) * 100)))
    sp.Speak(text, 1)  # SVSFlagsAsync
    deadline = time.monotonic() + 30 + len(text) / 5
    while time.monotonic() < deadline:
        if sp.WaitUntilDone(100):
            break
        if stop.is_set():
            break
    else:
        stop.set()
    if stop.is_set():
        sp.Speak("", 3)  # SVSFlagsAsync | SVSFPurgeBeforeSpeak
    return {"engine": "Windows SAPI", "voice": sp.Voice.GetDescription(), "seconds": round(time.monotonic() - start, 2),
            "stopped": stop.is_set()}


def render(text: str, path: str, voice: str | None = None, speed: float = 1.0) -> dict:
    """Render speech to a 24 kHz WAV file with the neural voice (short pauses between sentences)."""
    k = KOKORO.get()
    name = _kokoro_voice(k, voice)
    gap = np.zeros(int(KOKORO_RATE * 0.15), dtype=np.float32)
    parts = []
    for piece in chunks(text):
        parts += [_synthesize(k, piece, name, speed), gap]
    audio.save_wav(Path(path), np.concatenate(parts), KOKORO_RATE)
    return {"engine": "neural (Kokoro)", "voice": name, "wav": path,
            "audio_s": round(sum(len(p) for p in parts) / KOKORO_RATE, 2)}


def voices() -> dict:
    out: dict = {"default": DEFAULT_VOICE if KOKORO.installed() else "Windows default"}
    if KOKORO.installed():
        out["neural"] = {"note": "first letter = accent (a American, b British, e Spanish, f French, h Hindi, i Italian, "
                                 "j Japanese, p Brazilian Portuguese, z Mandarin); second = f female / m male",
                         "voices": sorted(KOKORO.get().get_voices())}
    else:
        out["neural"] = "not installed (python -m relaymcp.device.tts --download)"
    try:
        import comtypes.client
        tokens = comtypes.client.CreateObject("SAPI.SpVoice").GetVoices()
        out["windows"] = [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
    except Exception as e:
        out["windows"] = f"unavailable: {e}"
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="RelayMCP text-to-speech")
    parser.add_argument("--download", action="store_true", help="download the neural voice model files (~200 MB)")
    parser.add_argument("--say", help="speak this text (for testing)")
    parser.add_argument("--voice")
    args = parser.parse_args()
    if args.download:
        print(download_models(verbose=True))
    if args.say:
        sys.coinit_flags = 0
        print(speak(args.say, voice=args.voice))


if __name__ == "__main__":
    main()
