"""Audio: device status, volume/mute, microphone recording, speaker (loopback) capture, and test tones.

All functions here must run on a COM-initialized (MTA) worker thread; server.py takes care of that.
"""

from __future__ import annotations

import math
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .paths import USER_DIR

DATA_DIR = USER_DIR / "recordings"
RATE = 48000
MAX_SECONDS = 120


def _sc():
    import soundcard as sc  # imported lazily on the COM worker thread
    return sc


def _db(v: float) -> float:
    return round(20 * math.log10(v), 1) if v > 1e-9 else -120.0


def levels(data: np.ndarray) -> dict:
    x = data.astype(np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if not x.size:
        return {"peak_dbfs": -120.0, "rms_dbfs": -120.0, "clipped_percent": 0.0, "silent": True}
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(np.square(x))))
    return {
        "peak_dbfs": _db(peak),
        "rms_dbfs": _db(rms),
        "clipped_percent": round(float(np.mean(np.abs(x) >= 0.999)) * 100, 3),
        "silent": rms < 10 ** (-60 / 20),
    }


def save_wav(path: Path, data: np.ndarray, rate: int) -> Path:
    x = data if data.ndim == 1 else data.mean(axis=1)
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def _new_path(prefix: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = sorted(DATA_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    for p in old[:-50]:  # keep the 50 most recent recordings
        try:
            p.unlink()
        except OSError:
            pass
    return DATA_DIR / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.wav"


# ---------------------------------------------------------------------------------------------------- devices / volume

def _endpoint_volume(device):
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    raw = getattr(device, "_dev", device)
    # QueryInterface (reference counted) rather than ctypes.cast (also used by pycaw's EndpointVolume property),
    # which double-releases the COM object and crashes the process during garbage collection.
    return raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)


def _meter(device):
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioMeterInformation

    raw = getattr(device, "_dev", device)
    return raw.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None).QueryInterface(IAudioMeterInformation)


def _default_device(kind: str):
    from pycaw.pycaw import AudioUtilities

    if kind == "speaker":
        return AudioUtilities.GetSpeakers()
    if kind == "microphone":
        return AudioUtilities.GetMicrophone()
    raise ValueError("device must be 'speaker' or 'microphone'")


def volume_state(kind: str) -> dict:
    dev = _default_device(kind)
    if dev is None:
        return {"device": kind, "present": False}
    ev = _endpoint_volume(dev)
    out = {"device": kind, "present": True, "volume_percent": round(ev.GetMasterVolumeLevelScalar() * 100),
           "muted": bool(ev.GetMute())}
    try:
        out["current_peak"] = round(float(_meter(dev).GetPeakValue()), 4)
    except Exception:
        pass
    return out


def set_volume(kind: str, level: float | None = None, mute: bool | None = None) -> dict:
    dev = _default_device(kind)
    if dev is None:
        raise RuntimeError(f"no default {kind}")
    ev = _endpoint_volume(dev)
    before = {"volume_percent": round(ev.GetMasterVolumeLevelScalar() * 100), "muted": bool(ev.GetMute())}
    if level is not None:
        ev.SetMasterVolumeLevelScalar(max(0.0, min(100.0, float(level))) / 100, None)
    if mute is not None:
        ev.SetMute(1 if mute else 0, None)
    return {"device": kind, "before": before, "after": {k: v for k, v in volume_state(kind).items() if k in before}}


def devices() -> dict:
    sc = _sc()
    return {
        "default_speaker": sc.default_speaker().name,
        "default_microphone": sc.default_microphone().name,
        "speakers": [s.name for s in sc.all_speakers()],
        "microphones": [m.name for m in sc.all_microphones()],
        "speaker_volume": volume_state("speaker"),
        "microphone_volume": volume_state("microphone"),
        "microphone_privacy": microphone_privacy(),
    }


# ---------------------------------------------------------------------------------------------------- record / play

def microphone_privacy() -> dict:
    """Windows privacy switches for the microphone (device-wide HKLM and per-user HKCU)."""
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    out = {}
    for label, hive, sub in (("device", winreg.HKEY_LOCAL_MACHINE, key), ("user", winreg.HKEY_CURRENT_USER, key),
                             ("desktop_apps", winreg.HKEY_CURRENT_USER, key + r"\NonPackaged")):
        try:
            with winreg.OpenKey(hive, sub) as k:
                out[label] = winreg.QueryValueEx(k, "Value")[0]
        except OSError:
            out[label] = "unknown"
    out["recording_allowed"] = all(v != "Deny" for v in out.values())
    if not out["recording_allowed"]:
        out["how_to_enable"] = "On the handheld: Settings > Privacy & security > Microphone > turn on Microphone access and 'Let desktop apps access your microphone'."
    return out


def record_microphone(seconds: float, save: bool = True) -> tuple[dict, np.ndarray]:
    seconds = max(0.2, min(float(seconds), MAX_SECONDS))
    mic = _sc().default_microphone()
    try:
        data = mic.record(samplerate=RATE, numframes=int(RATE * seconds), channels=1)
    except RuntimeError as e:
        if "0x80070005" in str(e):
            p = microphone_privacy()
            raise RuntimeError("Windows is blocking microphone access (privacy settings: "
                               f"device={p['device']}, user={p['user']}, desktop apps={p['desktop_apps']}). "
                               + p.get("how_to_enable", "")) from e
        raise
    result = {"source": mic.name, "seconds": round(seconds, 2), **levels(data)}
    if save:
        result["wav"] = str(save_wav(_new_path("mic"), data, RATE))
    return result, (data[:, 0] if data.ndim > 1 else data)


def record_speakers(seconds: float, save: bool = True) -> tuple[dict, np.ndarray]:
    """Capture what the speakers are playing (WASAPI loopback)."""
    seconds = max(0.2, min(float(seconds), MAX_SECONDS))
    sc = _sc()
    speaker = sc.default_speaker()
    loop = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    # Loopback only delivers audio while the output is active, so keep a silent stream playing meanwhile.
    stop = threading.Event()

    def silence():
        try:
            with speaker.player(samplerate=RATE, channels=1) as p:
                block = np.zeros((RATE // 10, 1), dtype=np.float32)
                while not stop.is_set():
                    p.play(block)
        except Exception:
            pass

    t = threading.Thread(target=_com_wrapped(silence), daemon=True)
    t.start()
    try:
        time.sleep(0.1)
        data = loop.record(samplerate=RATE, numframes=int(RATE * seconds), channels=1)
    finally:
        stop.set()
        t.join(timeout=2)
    result = {"source": f"loopback of {speaker.name}", "seconds": round(seconds, 2), **levels(data)}
    if save:
        result["wav"] = str(save_wav(_new_path("speakers"), data, RATE))
    return result, (data[:, 0] if data.ndim > 1 else data)


def _com_wrapped(fn):
    def run():
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            fn()
        finally:
            ctypes.windll.ole32.CoUninitialize()
    return run


def play_tone(frequency: float = 440.0, seconds: float = 1.0, volume: float = 0.3) -> dict:
    seconds = max(0.05, min(float(seconds), 30.0))
    t = np.arange(int(RATE * seconds)) / RATE
    tone = (np.sin(2 * np.pi * float(frequency) * t) * max(0.0, min(float(volume), 1.0))).astype(np.float32)
    fade = min(len(tone) // 10, int(RATE * 0.01))
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
    speaker = _sc().default_speaker()
    speaker.play(tone.reshape(-1, 1), samplerate=RATE)
    return {"played": f"{frequency} Hz for {seconds}s", "speaker": speaker.name}


def play_wav(path: str) -> dict:
    data, rate = load_wav(path)
    speaker = _sc().default_speaker()
    speaker.play(data.reshape(-1, 1), samplerate=rate)
    return {"played": path, "seconds": round(len(data) / rate, 2), "speaker": speaker.name}
