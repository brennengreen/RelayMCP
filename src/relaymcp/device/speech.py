"""Speech: text-to-speech (Windows SAPI voices via System.Speech) and speech-to-text (local faster-whisper)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import audio
from .paths import USER_DIR

CREATE_NO_WINDOW = 0x08000000
MODELS_DIR = USER_DIR / "models"
WHISPER_MODELS = ("tiny.en", "base.en", "small.en", "tiny", "base", "small")


def _powershell(script: str, timeout: float = 120) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[-800:] or f"powershell exited {proc.returncode}")
    return proc.stdout.strip()


def _ps_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def voices() -> list[dict]:
    out = _powershell(
        "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | ForEach-Object { $i = $_.VoiceInfo; [pscustomobject]@{ name = $i.Name; "
        "culture = $i.Culture.Name; gender = [string]$i.Gender; enabled = $_.Enabled } } | ConvertTo-Json -Compress"
    )
    data = json.loads(out) if out else []
    return data if isinstance(data, list) else [data]


def speak(text: str, voice: str | None = None, rate: int = 0, volume: int = 100, to_file: str | None = None) -> dict:
    """Speak through the default speakers, or render to a WAV file when to_file is given."""
    if not text.strip():
        raise ValueError("nothing to say")
    rate = max(-10, min(int(rate), 10))
    volume = max(0, min(int(volume), 100))
    lines = [
        "Add-Type -AssemblyName System.Speech",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        f"$s.Rate = {rate}; $s.Volume = {volume}",
    ]
    if voice:
        lines.append(f"$s.SelectVoice({_ps_str(voice)})")
    if to_file:
        Path(to_file).parent.mkdir(parents=True, exist_ok=True)
        fmt = "New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)"
        lines.append(f"$s.SetOutputToWaveFile({_ps_str(to_file)}, ({fmt}))")
    else:
        lines.append("$s.SetOutputToDefaultAudioDevice()")
    lines += [f"$s.Speak({_ps_str(text)})", "$s.Dispose()"]
    start = time.monotonic()
    _powershell("; ".join(lines) + "; 'ok'", timeout=max(60, len(text) / 5))
    result = {"spoke": text if len(text) <= 200 else text[:200] + "...", "seconds": round(time.monotonic() - start, 2),
              "voice": voice or "default"}
    if to_file:
        result["wav"] = to_file
    return result


# ---------------------------------------------------------------------------------------------------- speech to text

_models: dict = {}
_last_used: dict[str, float] = {}
_model_lock = threading.Lock()
CPU_THREADS = max(4, min(8, (os.cpu_count() or 8) // 2))  # physical cores; faster-whisper defaults to 4
LOW_LOGPROB = -0.6  # below this average log-probability a transcript is unreliable; retry with the fallback model
HALLUCINATIONS = {"you", "thank you", "thanks for watching", "bye", "okay", "so"}  # Whisper's usual noise guesses


def _get_model(name: str):
    if name not in WHISPER_MODELS:
        raise ValueError(f"model must be one of {', '.join(WHISPER_MODELS)}")
    with _model_lock:
        model = _models.get(name)
        if model is None:
            from faster_whisper import WhisperModel
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS,
                                 download_root=str(MODELS_DIR))
            _models[name] = model
        _last_used[name] = time.monotonic()
        return model


def unload_idle(max_idle_s: float) -> list[str]:
    """Free the memory of speech models not used for a while (they reload in about a second)."""
    with _model_lock:
        idle = [n for n in _models if time.monotonic() - _last_used.get(n, 0.0) > max_idle_s]
        for n in idle:
            del _models[n]
            _last_used.pop(n, None)
    return idle


def _to_16k(x: np.ndarray, rate: int) -> np.ndarray:
    x = x.astype(np.float32)
    if rate == 16000:
        return x
    n = int(round(len(x) * 16000 / rate))
    if n <= 1:
        return np.zeros(1, dtype=np.float32)
    # Average over each output sample's input window (cheap low-pass), then interpolate.
    if rate % 16000 == 0:
        k = rate // 16000
        trimmed = x[: len(x) // k * k]
        return trimmed.reshape(-1, k).mean(axis=1).astype(np.float32)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def _run(x: np.ndarray, model: str, language: str | None, vocabulary: str | None) -> dict:
    start = time.monotonic()
    m = _get_model(model)
    load_s = time.monotonic() - start
    lang = language or ("en" if model.endswith(".en") else None)
    segments, info = m.transcribe(x, language=lang, beam_size=1, vad_filter=True, condition_on_previous_text=False,
                                  without_timestamps=True, initial_prompt=vocabulary or None)
    segs = list(segments)
    text = " ".join(s.text.strip() for s in segs).strip()
    weight = sum(max(len(s.text), 1) for s in segs)
    logprob = sum(s.avg_logprob * max(len(s.text), 1) for s in segs) / weight if segs else None
    if logprob is not None and text.lower().strip(" .!?,") in HALLUCINATIONS and logprob < -0.4:
        text = ""  # a lone "you"/"thank you" from background noise
    return {"text": text, "language": info.language, "model": model, "avg_logprob": round(logprob, 3) if logprob is not None else None,
            "model_load_seconds": round(load_s, 2), "transcribe_seconds": round(time.monotonic() - start - load_s, 2)}


def transcribe(samples: np.ndarray, rate: int, model: str = "base.en", language: str | None = None,
               vocabulary: str | None = None, fallback: str | None = None) -> dict:
    """Transcribe locally. With `fallback` (e.g. small.en), an empty or low-confidence result is redone with that
    more accurate (slower) model."""
    x = _to_16k(samples, rate)
    result = _run(x, model, language, vocabulary)
    if fallback and fallback != model and (not result["text"] or (result["avg_logprob"] or 0) < LOW_LOGPROB):
        first = result
        result = _run(x, fallback, language, vocabulary)
        result["retried_from"] = {k: first[k] for k in ("model", "text", "avg_logprob", "transcribe_seconds")}
    result["audio_seconds"] = round(len(samples) / rate, 2)
    return result


def transcribe_file(path: str, model: str = "base.en", language: str | None = None, vocabulary: str | None = None) -> dict:
    data, rate = audio.load_wav(path)
    return {"wav": path, **transcribe(data, rate, model, language, vocabulary)}
