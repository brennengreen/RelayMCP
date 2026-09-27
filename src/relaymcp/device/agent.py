"""Background agent: runs RelayMCP's MCP servers hidden in the signed-in user's session and keeps the handheld awake
while an AI agent is using it.

The RelayMCP-Agent scheduled task starts this with pythonw.exe (no window, no console) only while the handheld is at
home, and stops it when the handheld leaves. There are no shell scripts or console tricks involved:

- Each server runs as `python.exe -m ...` with CREATE_NO_WINDOW, so it has a hidden console that console programs it
  starts (PowerShell etc.) inherit - no window ever flashes.
- Each server sits in its own kill-on-close job object, so the servers end whenever the agent ends (however it ends).
  The jobs allow silent breakaway, so apps a server launches for the user (games, Notepad...) are never killed.
- A server that exits is restarted with backoff; its output goes to a size-capped log.
- Keep-awake: holds the display/system awake while there was a Copilot tool call or SSH command in the last 10 minutes,
  or while a keep_awake lease (%LOCALAPPDATA%\\RelayMCP\\awake-until.txt) is active - and only at home.

Logs (%LOCALAPPDATA%\\RelayMCP): agent.log, <service>.out.log, keepawake.log; status in agent-status.json.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import APP, LEASE_FILE, STATE_FILE, USER_DIR, device_settings

STATUS_FILE = USER_DIR / "agent-status.json"

KEEP_AWAKE_GRACE_S = 10 * 60
CREATE_NO_WINDOW = 0x08000000
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002

log = logging.getLogger("agent")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------------------------------------------- job objects

class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMITS), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]


def _kill_on_close_job() -> int | None:
    """A job whose processes are killed when its last handle (ours) closes. Silent breakaway: only the process we
    assign is in it, never the programs that process starts."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _EXTENDED_LIMITS()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x1000  # KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        kernel32.CloseHandle(job)
        return None
    return job


# ---------------------------------------------------------------------------------------------------- services

class Service:
    """One MCP server process, restarted with backoff when it exits."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], port: int, signature: list[str]):
        self.name, self.argv, self.env, self.port = name, argv, env, port
        self.signature = [s.lower() for s in signature]  # command-line fragments that identify this server
        self.proc: subprocess.Popen | None = None
        self.job: int | None = None
        self.started = 0.0
        self.next_start = 0.0
        self.failures = 0
        self.restarts = 0
        self.last_exit: str | None = None
        self.tail: collections.deque[str] = collections.deque(maxlen=15)
        self.out = logging.getLogger(f"svc.{name}")
        self.out.propagate = False
        if not self.out.handlers:
            h = RotatingFileHandler(USER_DIR / f"{name}.out.log", maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            self.out.addHandler(h)
            self.out.setLevel(logging.INFO)

    def available(self) -> bool:
        return Path(self.argv[0]).exists()

    def start(self) -> None:
        kill_matching(self.signature, "stale " + self.name)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", **self.env)
        self.out.info("--- %s starting: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), " ".join(self.argv))
        self.proc = subprocess.Popen(self.argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW, cwd=str(USER_DIR))
        self.job = _kill_on_close_job()
        if self.job and not kernel32.AssignProcessToJobObject(self.job, int(self.proc._handle)):
            log.warning("%s: couldn't assign to a job (%d)", self.name, ctypes.get_last_error())
        self.started = time.monotonic()
        threading.Thread(target=self._pump, args=(self.proc,), name=f"pump-{self.name}", daemon=True).start()
        log.info("%s started (pid %d)", self.name, self.proc.pid)

    def _pump(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            self.tail.append(line)
            self.out.info("%s", line)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            if self.job:
                kernel32.TerminateJobObject(self.job, 1)
            else:
                self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.job:
            kernel32.CloseHandle(self.job)
            self.job = None
        kill_matching(self.signature, "leftover " + self.name)
        self.proc = None

    def check(self, now: float) -> None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                if self.failures and now - self.started > 300:
                    self.failures = 0  # healthy for 5 minutes: forget earlier crashes
                return
            ran = now - self.started
            self.last_exit = f"{datetime.now():%H:%M:%S} exit {code} after {ran:.0f}s"
            self.stop()
            self.failures += 1
            delay = min(60.0, 2.0 ** min(self.failures, 6))
            self.next_start = now + delay
            log.warning("%s exited with code %s after %.0fs; restarting in %.0fs. Last output:\n    %s",
                        self.name, code, ran, delay, "\n    ".join(list(self.tail)[-6:]))
            return
        if now >= self.next_start:
            try:
                self.start()
                if self.last_exit:
                    self.restarts += 1
            except Exception as e:
                self.failures += 1
                self.next_start = now + min(60.0, 2.0 ** min(self.failures, 6))
                log.error("%s failed to start: %s", self.name, e)

    def status(self) -> dict:
        running = self.proc is not None and self.proc.poll() is None
        return {"running": running, "pid": self.proc.pid if running else None, "port": self.port,
                "up_s": round(time.monotonic() - self.started) if running else None,
                "restarts": self.restarts, "last_exit": self.last_exit}


def kill_matching(signature: list[str], why: str) -> None:
    """Kill processes whose command line contains every fragment (e.g. a server left over from an older launcher)."""
    import psutil
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.info["pid"] == me or not (p.info["name"] or "").lower().startswith("python"):
                continue
            cmd = " ".join(" ".join(p.info["cmdline"] or []).split()).lower()
            if cmd and all(s in cmd for s in signature):
                p.kill()
                log.info("killed %s (pid %d)", why, p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


# ---------------------------------------------------------------------------------------------------- keep-awake

class KeepAwake:
    def __init__(self, ports: list[int]):
        self.ports = set(ports)
        self.holding = False
        self.last_activity = 0.0
        self.last_reason = ""
        self.last_ssh_check = 0.0
        self.reason = ""
        self.log = logging.getLogger("keepawake")
        self.log.propagate = False
        h = RotatingFileHandler(USER_DIR / "keepawake.log", maxBytes=128 * 1024, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
        self.log.addHandler(h)
        self.log.setLevel(logging.INFO)

    def _copilot_connected(self) -> bool:
        import psutil
        try:
            return any(c.status == psutil.CONN_ESTABLISHED and c.laddr and c.laddr.port in self.ports
                       for c in psutil.net_connections(kind="tcp4"))
        except Exception:
            return False

    def _ssh_command(self) -> bool:
        """A shell under an SSH session means a command from the controlling computer (the tunnel itself runs no shell)."""
        import psutil
        try:
            procs = [(p.info["pid"], p.info["ppid"], (p.info["name"] or "").lower())
                     for p in psutil.process_iter(["pid", "ppid", "name"])]
        except Exception:
            return False
        sessions = {pid for pid, _, name in procs if name == "sshd-session.exe"}
        return bool(sessions) and any(ppid in sessions and name in ("powershell.exe", "pwsh.exe", "cmd.exe")
                                      for _, ppid, name in procs)

    def tick(self, now: float) -> None:
        at_home = _read(STATE_FILE) == "home"
        reason = ""
        if at_home:
            if self._copilot_connected():
                self.last_activity, self.last_reason = now, "Copilot tool call"
            if now - self.last_ssh_check >= 30:
                self.last_ssh_check = now
                if self._ssh_command():
                    self.last_activity, self.last_reason = now, "SSH command"
            lease = None
            try:
                lease = datetime.fromisoformat(_read(LEASE_FILE))
            except ValueError:
                pass
            if lease and lease > datetime.now():
                reason = f"lease until {lease:%a %H:%M}"
            elif self.last_activity and now - self.last_activity < KEEP_AWAKE_GRACE_S:
                reason = f"recent {self.last_reason}"
        self.reason = reason
        want = bool(reason)
        if want != self.holding:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if want else ES_CONTINUOUS
            kernel32.SetThreadExecutionState(flags)
            self.holding = want
            self.log.info("keeping awake (%s)" % reason if want else "released; the handheld can sleep normally")

    def release(self) -> None:
        if self.holding:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.holding = False
            self.log.info("released (agent stopping)")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------------------------------- main

def build_services(screen_port: int, hardware_port: int, windows_mcp_python: str | None) -> list[Service]:
    scripts = Path(sys.executable).parent
    own_python = scripts / "python.exe"
    wm_python = Path(windows_mcp_python) if windows_mcp_python else Path(sys.prefix).parent / "windows-mcp" / "Scripts" / "python.exe"
    return [
        Service("windows-mcp",
                [str(wm_python), "-m", "windows_mcp", "serve", "--transport", "streamable-http",
                 "--host", "127.0.0.1", "--port", str(screen_port)],
                {"ANONYMIZED_TELEMETRY": "false", "WINDOWS_MCP_STATELESS_HTTP": "true"},
                screen_port, ["windows_mcp", "serve", f"--port {screen_port}"]),
        Service("hardware",
                [str(own_python), "-m", "relaymcp.device.server", "--port", str(hardware_port),
                 "--parent-pid", str(os.getpid())],
                {"HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
                 "ANONYMIZED_TELEMETRY": "false"},
                hardware_port, ["relaymcp.device.server", f"--port {hardware_port}"]),
    ]


def main() -> None:
    ports = device_settings()["ports"]
    parser = argparse.ArgumentParser(description="Runs RelayMCP's MCP servers hidden and keeps the handheld awake while in use")
    parser.add_argument("--screen-port", type=int, default=ports["screen"])
    parser.add_argument("--hardware-port", type=int, default=ports["hardware"])
    parser.add_argument("--windows-mcp-python", help="python.exe of the windows-mcp tool (found automatically)")
    args = parser.parse_args()

    USER_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # pythonw: give stray prints somewhere harmless to go
        sys.stdout = sys.stderr = open(USER_DIR / "agent.stray.log", "a", encoding="utf-8", buffering=1)
    handler = RotatingFileHandler(USER_DIR / "agent.log", maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP}-Agent")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log.info("another agent is already running; exiting")
        return

    from . import __version__
    services = build_services(args.screen_port, args.hardware_port, args.windows_mcp_python)
    keep_awake = KeepAwake([args.screen_port, args.hardware_port])
    log.info("agent %s started (pid %d)", __version__, os.getpid())
    for s in services:
        if not s.available():
            log.warning("%s isn't installed (%s); skipping it", s.name, s.argv[0])
    services = [s for s in services if s.available()]

    last_status = 0.0
    try:
        while True:
            now = time.monotonic()
            for s in services:
                s.check(now)
            try:
                keep_awake.tick(now)
            except Exception as e:
                log.warning("keep-awake check failed: %s", e)
            if now - last_status >= 5:
                last_status = now
                status = {"agent_pid": os.getpid(), "updated": datetime.now().isoformat(timespec="seconds"),
                          "keep_awake": keep_awake.reason or None, "services": {s.name: s.status() for s in services}}
                try:
                    tmp = STATUS_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
                    os.replace(tmp, STATUS_FILE)
                except OSError:
                    pass
            time.sleep(1)
    finally:
        keep_awake.release()
        for s in services:
            s.stop()
        log.info("agent stopped")
        del mutex


if __name__ == "__main__":
    main()
