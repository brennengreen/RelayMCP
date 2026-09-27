"""A persistent PowerShell session in the signed-in desktop session: variables, functions and modules survive between
calls, and a call costs tens of milliseconds instead of the 1.5-2.5 s of starting powershell.exe. The session is
per-monitor DPI-aware, so Win32 coordinates are physical pixels (the same as screenshots and touch), and output is
plain text: errors and warnings come back as "ERROR: ..." / "WARNING: ..." lines, never CLIXML.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import subprocess
import threading
import time
import uuid

CREATE_NO_WINDOW = 0x08000000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # terminal control codes some PowerShell versions write
MAX_SCRIPT = 64 * 1024
MAX_OUTPUT = 12000

# Run once when the session starts.
STARTUP = (
    "$ProgressPreference = 'SilentlyContinue'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$OutputEncoding = [Text.Encoding]::UTF8; "
    "try { Add-Type -Namespace RelayMCP -Name Dpi -MemberDefinition "
    "'[DllImport(\"user32.dll\")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);' -ErrorAction Stop; "
    "[void][RelayMCP.Dpi]::SetProcessDpiAwarenessContext([IntPtr]-4) } catch { }"
)

# One line per call: the script runs dot-sourced (so what it defines persists), every stream is folded into plain
# text, and a marker line with the call's id ends the output.
CALL = (
    "try {{ . ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))) *>&1 | "
    "ForEach-Object {{ if ($_ -is [System.Management.Automation.ErrorRecord]) {{ 'ERROR: ' + $_.Exception.Message }} "
    "elseif ($_ -is [System.Management.Automation.WarningRecord]) {{ 'WARNING: ' + $_.Message }} else {{ $_ }} }} | "
    "Out-String -Stream -Width 240 }} catch {{ 'ERROR: ' + $_.Exception.Message }}; "
    "[Console]::Out.WriteLine('{marker}'); [Console]::Out.Flush()"
)


def call_line(script: str, marker: str) -> str:
    return CALL.format(b64=base64.b64encode(script.encode("utf-8")).decode(), marker=marker)


def clip(lines: list[str], limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    text = "\n".join(lines).strip("\n")
    if len(text) <= limit:
        return text, False
    return text[: limit // 3] + "\n...\n" + text[-(limit * 2 // 3):], True


class PowerShellHost:
    def __init__(self, exe: str = "powershell.exe"):
        self.exe = exe
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.started_at = 0.0

    def _start(self) -> None:
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen([self.exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                      "-OutputFormat", "Text", "-Command", "-"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=flags, env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"})
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.lines), name="pshost", daemon=True).start()
        self.started_at = time.time()
        self._send_and_wait(STARTUP, 60)

    @staticmethod
    def _pump(proc: subprocess.Popen, q: queue.Queue) -> None:
        for raw in iter(proc.stdout.readline, b""):
            q.put(ANSI.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n")))
        q.put(None)  # the process ended

    def _send_and_wait(self, script: str, timeout: float) -> list[str]:
        marker = f"<<<RELAYMCP-END {uuid.uuid4().hex}>>>"
        self.proc.stdin.write((call_line(script, marker) + "\n").encode("ascii"))
        self.proc.stdin.flush()
        out, deadline = [], time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"no result after {timeout:g} s") from None
            if line is None:
                raise RuntimeError("the PowerShell session ended")
            if marker in line:
                before = line.split(marker, 1)[0]
                return out + [before] if before.strip() else out
            out.append(line)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def run(self, script: str, timeout: float = 60.0, reset: bool = False) -> dict:
        if len(script.encode("utf-8")) > MAX_SCRIPT:
            raise ValueError(f"the script is over {MAX_SCRIPT // 1024} KB")
        timeout = max(1.0, min(float(timeout), 600.0))
        with self.lock:
            fresh = reset or self.proc is None or self.proc.poll() is not None
            if fresh:
                self.stop()
                self._start()
            t0 = time.monotonic()
            try:
                lines = self._send_and_wait(script, timeout)
            except (TimeoutError, RuntimeError, OSError) as e:
                self.stop()  # a stuck or dead session can't be trusted with the next call
                raise RuntimeError(f"{e}; the PowerShell session was reset (variables are gone)") from None
            text, clipped = clip(lines)
            return {"output": text, "ms": round((time.monotonic() - t0) * 1000), "new_session": fresh or None,
                    "clipped": clipped or None}


HOST = PowerShellHost()
