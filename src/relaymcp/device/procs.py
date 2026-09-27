"""Long-running processes an agent talks to: a game server console, a build watcher, a REPL. Start one, send it lines,
read only what's new, or wait until a line matches, all without screenshots. Output is kept per session (the last
5,000 lines), so reads are cheap and nothing is lost between calls.

Processes run in the signed-in desktop session with no window. Running sessions are listed in procs.json, which
`relaymcp busy` reads so updates don't restart the handheld's servers (and these processes) while one runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

MAX_SESSIONS = 8
KEEP_LINES = 5000
MAX_LINE = 2000
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class Session:
    def __init__(self, name: str, command: str, cwd: str | None, on_exit):
        self.name, self.command, self.cwd = name, command, cwd
        self.started = time.time()
        self.lines: deque[tuple[int, str]] = deque(maxlen=KEEP_LINES)
        self.total = 0          # lines seen so far; line numbers are 1-based and never reused
        self.read_to = 0        # the last line anyone has read
        self.cond = threading.Condition()
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(command, shell=True, cwd=cwd or None, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                     creationflags=flags, start_new_session=os.name != "nt")
        self._on_exit = on_exit
        threading.Thread(target=self._pump, name=f"proc-{name}", daemon=True).start()

    def _pump(self) -> None:
        buf = b""
        stream = self.proc.stdout
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf += chunk
            *complete, buf = re.split(rb"\r?\n|\r", buf)
            if complete:
                self._add(complete)
        if buf:
            self._add([buf])
        self.proc.wait()
        with self.cond:
            self.cond.notify_all()
        self._on_exit()

    def _add(self, raw: list[bytes]) -> None:
        with self.cond:
            for line in raw:
                text = line.decode("utf-8", errors="replace")
                self.total += 1
                self.lines.append((self.total, text[:MAX_LINE] + ("..." if len(text) > MAX_LINE else "")))
            self.cond.notify_all()

    # --- what the tool does ------------------------------------------------------------------------------------------

    def running(self) -> bool:
        return self.proc.poll() is None

    def send(self, text: str) -> None:
        if not self.running():
            raise RuntimeError(f"'{self.name}' has exited (code {self.proc.returncode})")
        data = (text if text.endswith("\n") else text + "\n").replace("\r\n", "\n").replace("\n", os.linesep)
        self.proc.stdin.write(data.encode("utf-8"))
        self.proc.stdin.flush()

    def after(self, cursor: int, max_lines: int) -> tuple[list[str], int, int]:
        """(lines after cursor, the cursor after them, lines skipped because they're gone or over max_lines)."""
        with self.cond:
            fresh = [(n, t) for n, t in self.lines if n > cursor]
            first = fresh[0][0] if fresh else self.total + 1
            gone = max(0, first - cursor - 1) if fresh else max(0, self.total - cursor)
            over = max(0, len(fresh) - max_lines)
            fresh = fresh[over:]  # keep the newest
            return [t for _, t in fresh], self.total, gone + over

    def wait_for(self, pattern: str, cursor: int, timeout: float) -> tuple[str | None, int]:
        """(the first line after cursor matching pattern, or None on timeout/exit; its line number or the cursor)."""
        rx = re.compile(pattern, re.I)
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for n, t in self.lines:
                    if n > cursor and rx.search(t):
                        return t, n
                cursor = max(cursor, self.total)
                left = deadline - time.monotonic()
                if left <= 0 or not self.running():
                    return None, cursor
                self.cond.wait(min(left, 0.5))

    def settle(self, mark: int, first: float, quiet: float = 0.3, limit: float = 5.0) -> None:
        """Wait for output after line `mark` (up to `first` seconds), then until it goes quiet for `quiet` seconds."""
        start = time.monotonic()
        with self.cond:
            while self.total <= mark and self.running() and time.monotonic() - start < first:
                self.cond.wait(0.05)
            last, changed = self.total, time.monotonic()
            while self.running() and time.monotonic() - start < limit:
                self.cond.wait(0.05)
                if self.total != last:
                    last, changed = self.total, time.monotonic()
                elif time.monotonic() - changed >= quiet:
                    break

    def stop(self, command: str = "", grace: float = 10.0) -> int | None:
        """Ask it to exit (by sending `command`, e.g. 'stop'), then end it (and its children) if it doesn't."""
        if self.running() and command:
            try:
                self.send(command)
                self.proc.wait(timeout=grace)
            except (subprocess.TimeoutExpired, OSError, RuntimeError):
                pass
        if self.running():
            _kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return self.proc.poll()

    def info(self) -> dict:
        return {"name": self.name, "command": self.command[:160], "running": self.running(),
                "exit_code": self.proc.poll(), "pid": self.proc.pid, "lines": self.total,
                "started": time.strftime("%H:%M:%S", time.localtime(self.started))}


def _kill_tree(pid: int) -> None:
    """End the process and everything it started (`shell=True` puts a shell between us and the real program)."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
            return
        except psutil.Error:
            return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        else:
            os.killpg(os.getpgid(pid), 9)  # the session's own process group (start_new_session)
    except (OSError, subprocess.TimeoutExpired):
        pass


class Manager:
    def __init__(self, state_file: Path | None = None):
        self.sessions: dict[str, Session] = {}
        self.state_file = state_file
        self._lock = threading.Lock()

    def _save(self) -> None:
        if not self.state_file:
            return
        running = [s.info() for s in list(self.sessions.values()) if s.running()]
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"updated": time.time(), "running": running}), encoding="utf-8")
        except OSError:
            pass

    def get(self, name: str) -> Session:
        s = self.sessions.get(name)
        if not s:
            raise KeyError(f"no process session named {name!r} (running: {', '.join(self.sessions) or 'none'})")
        return s

    def start(self, name: str, command: str, cwd: str | None = None) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name or ""):
            raise ValueError("name: 1-32 letters, digits, '_', '-' or '.'")
        if not command.strip():
            raise ValueError("command is empty")
        with self._lock:
            old = self.sessions.get(name)
            if old and old.running():
                raise RuntimeError(f"'{name}' is already running (stop it first, or pick another name)")
            finished = [n for n, s in self.sessions.items() if not s.running()]
            if len(self.sessions) - len(finished) >= MAX_SESSIONS:
                raise RuntimeError(f"at most {MAX_SESSIONS} process sessions at a time")
            for n in finished:  # forget exited ones once the slots are needed
                if len(self.sessions) >= MAX_SESSIONS or n == name:
                    del self.sessions[n]
            self.sessions[name] = Session(name, command, cwd, self._save)
        self._save()
        return self.sessions[name].info()

    def stop_all(self) -> None:
        for s in list(self.sessions.values()):
            s.stop()
        self._save()


def run(manager: Manager, action: str, name: str = "", command: str = "", cwd: str = "", text: str = "",
        pattern: str = "", timeout: float = 10.0, cursor: int | None = None, max_lines: int = 60) -> dict:
    """The `proc` tool: one entry point so the tool definition stays small."""
    action = (action or "").lower()
    max_lines = max(1, min(int(max_lines), 400))
    if action == "list":
        return {"sessions": [s.info() for s in manager.sessions.values()]}
    if action == "start":
        info = manager.start(name, command, cwd or None)
        s = manager.get(name)
        if pattern:  # e.g. wait until the server says it's ready
            line, n = s.wait_for(pattern, 0, min(float(timeout), 300))
            info["ready"] = line is not None
            info["matched"] = line
        else:
            s.settle(0, first=1.0, limit=2.0)
        lines, end, skipped = s.after(0, max_lines)
        s.read_to = end
        return {**s.info(), **({k: info[k] for k in ("ready", "matched") if k in info}), "output": lines,
                "cursor": end, "skipped": skipped or None}
    s = manager.get(name)
    if action == "send":
        mark = s.total
        s.send(text)
        line = None
        if pattern:
            line, _ = s.wait_for(pattern, mark, min(float(timeout), 300))
        else:  # the reply: its first line within 2 s, then whatever follows until the output goes quiet
            s.settle(mark, first=min(float(timeout), 2.0), limit=min(float(timeout), 5.0))
        lines, end, skipped = s.after(mark, max_lines)
        s.read_to = max(s.read_to, end)
        out = {"sent": text[:200], "output": lines, "cursor": end, "running": s.running(), "skipped": skipped or None}
        if pattern:
            out["matched"] = line
        return out
    if action == "read":
        start = s.read_to if cursor is None else int(cursor)
        lines, end, skipped = s.after(start, max_lines)
        s.read_to = max(s.read_to, end)
        return {"output": lines, "cursor": end, "running": s.running(), "exit_code": s.proc.poll(),
                "skipped": skipped or None}
    if action == "wait":
        if not pattern:
            raise ValueError("wait needs a pattern (a regular expression)")
        start = s.read_to if cursor is None else int(cursor)
        line, n = s.wait_for(pattern, start, min(float(timeout), 300))
        s.read_to = max(s.read_to, n)
        return {"matched": line, "line": n if line else None, "cursor": s.total, "running": s.running(),
                "timed_out": line is None and s.running()}
    if action == "stop":
        code = s.stop(text, min(float(timeout), 60))
        manager._save()
        lines, end, _ = s.after(s.read_to, max_lines)
        return {"stopped": not s.running(), "exit_code": code, "output": lines}
    raise ValueError("action must be start, send, read, wait, stop or list")
