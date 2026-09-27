"""Telemetry from a game, followed live. A script pack (or mod) in the game prints one line per tick,
`RELAY {json}`, into a log file the game writes; the handheld follows the newest log and keeps the latest sample and a
short history. For Minecraft Bedrock (the RelayMCP Telemetry pack) that is the player's exact position, eye height, yaw,
pitch, velocity, the looked-at block and nearby mobs, 20 times a second.

It is ground truth for scoring and training, and one more sensor for control, like a drone's telemetry link; nothing
requires it. Angles follow RelayMCP's camera convention: yaw grows turning right (Minecraft's own yaw does: 0 = south
+z, 90 = west), pitch is up + (Minecraft's is down +).
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

PREFIX = "RELAY {"
KEEP_S = 10.0


def minecraft_log_folders() -> list[Path]:
    """Where Minecraft Bedrock writes its content logs (the GDK build, then the older UWP one)."""
    out = []
    appdata, local = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
    if appdata:
        out.append(Path(appdata) / "Minecraft Bedrock" / "logs")
    if local:
        out.append(Path(local) / "Packages" / "Microsoft.MinecraftUWP_8wekyb3d8bbwe" / "LocalState" / "logs")
    return out


def normalize(data: dict, recv_perf: float, recv_wall: float) -> dict:
    """A sample in RelayMCP's conventions, with when it arrived and how late (game clock to our read)."""
    s = dict(data)
    if "pitch" in s:
        s["pitch"] = -float(s["pitch"])  # up +
    s["recv"] = recv_perf
    if isinstance(data.get("t"), (int, float)):
        s["latency_ms"] = round(recv_wall * 1000 - float(data["t"]), 1)
    return s


class Telemetry:
    """The latest sample and a short history, fed line by line."""

    def __init__(self, keep_s: float = KEEP_S, on_sample: Callable[[dict], None] | None = None):
        self._lock = threading.Lock()
        self.latest: dict | None = None
        self.history: collections.deque = collections.deque()
        self.keep_s = keep_s
        self.samples = 0
        self.bad = 0
        self.on_sample = on_sample
        self.source: str | None = None

    def feed(self, line: str, recv_perf: float | None = None, recv_wall: float | None = None) -> dict | None:
        i = line.find(PREFIX)
        if i < 0:
            return None
        try:
            data = json.loads(line[i + len(PREFIX) - 1:])
        except ValueError:
            self.bad += 1
            return None
        if not isinstance(data, dict):
            return None
        s = normalize(data, time.perf_counter() if recv_perf is None else recv_perf,
                      time.time() if recv_wall is None else recv_wall)
        with self._lock:
            self.latest = s
            self.history.append(s)
            while self.history and s["recv"] - self.history[0]["recv"] > self.keep_s:
                self.history.popleft()
            self.samples += 1
        if self.on_sample:
            try:
                self.on_sample(s)
            except Exception:
                pass
        return s

    def get(self) -> dict | None:
        """The latest sample, with age_ms (since it arrived)."""
        with self._lock:
            s = self.latest
        if s is None:
            return None
        return {**s, "age_ms": round((time.perf_counter() - s["recv"]) * 1000, 1)}

    def since(self, seconds: float) -> list[dict]:
        cut = time.perf_counter() - seconds
        with self._lock:
            return [s for s in self.history if s["recv"] >= cut]

    def status(self) -> dict:
        s = self.get()
        with self._lock:
            lat = sorted(x["latency_ms"] for x in self.history if "latency_ms" in x)
            rate = 0.0
            if len(self.history) > 1:
                span = self.history[-1]["recv"] - self.history[0]["recv"]
                rate = (len(self.history) - 1) / span if span > 0 else 0.0
        out = {"samples": self.samples, "source": self.source, "hz": round(rate, 1),
               "age_ms": s["age_ms"] if s else None}
        if lat:
            out["latency_ms_p50"] = lat[len(lat) // 2]
            out["latency_ms_p95"] = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        if self.bad:
            out["unreadable_lines"] = self.bad
        return out


class Follower:
    """Follow the newest file matching `pattern` in `folders` (a game starts a new log each session), handing each
    complete new line to `on_line`. Existing lines of the file found at start are skipped; a file that appears later
    is read from its start."""

    def __init__(self, folders: list[Path], pattern: str, on_line: Callable[[str], None], poll_s: float = 0.005,
                 rescan_s: float = 2.0, on_switch: Callable[[str], None] | None = None):
        self.folders, self.pattern, self.on_line = folders, pattern, on_line
        self.poll_s, self.rescan_s, self.on_switch = poll_s, rescan_s, on_switch
        self.path: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def newest(self) -> Path | None:
        best, best_t = None, -1.0
        for folder in self.folders:
            try:
                for p in folder.glob(self.pattern):
                    t = p.stat().st_mtime
                    if t > best_t:
                        best, best_t = p, t
            except OSError:
                continue
        return best

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="telemetry-follow", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        f, pending, first, next_scan = None, b"", True, 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    next_scan = now + self.rescan_s
                    newest = self.newest()
                    if newest is not None and newest != self.path:
                        if f:
                            f.close()
                        try:
                            f = open(newest, "rb")
                        except OSError:
                            f = None
                        else:
                            if first:
                                f.seek(0, os.SEEK_END)  # what was logged before we started is history
                            self.path, pending = newest, b""
                            if self.on_switch:
                                self.on_switch(str(newest))
                        first = False
                    elif newest is None:
                        first = False
                if f is None:
                    self._stop.wait(self.rescan_s)
                    continue
                chunk = f.read(1 << 16)
                if not chunk:
                    self._stop.wait(self.poll_s)
                    continue
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    try:
                        self.on_line(raw.decode("utf-8", "replace").rstrip("\r"))
                    except Exception:
                        pass
        finally:
            if f:
                f.close()
