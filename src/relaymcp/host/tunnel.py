"""Keeps the SSH tunnels to the handheld open whenever it's reachable, using the system `ssh`:

  tools: this computer's 127.0.0.1:<screen>/<hardware> -> the handheld's MCP servers
  voice: the handheld's 127.0.0.1:<voice> -> this computer's voice dispatcher (push-to-talk prompts)

They're separate connections so a stale voice port on the handheld can never take the tools down. While the handheld
is away, asleep or offline each one just checks every 30 seconds; only state changes are logged."""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from . import sshconf

log = logging.getLogger("relaymcp.tunnel")
SSH_OPTIONS = ["-N", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "BatchMode=yes",
               "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3"]


def forwards(cfg: dict) -> dict[str, list[str]]:
    p = cfg["device"]["ports"]
    out = {"tools": ["-L", f"127.0.0.1:{p['screen']}:127.0.0.1:{p['screen']}",
                     "-L", f"127.0.0.1:{p['hardware']}:127.0.0.1:{p['hardware']}"]}
    if cfg["voice"].get("enabled", True):
        out["voice"] = ["-R", f"127.0.0.1:{p['voice']}:127.0.0.1:{p['voice']}"]
    return out


def command(cfg: dict, args: list[str]) -> list[str]:
    return [sshconf.ssh_exe(), *SSH_OPTIONS, *args, cfg["device"]["name"]]


class Forward(threading.Thread):
    def __init__(self, name: str, cfg: dict, args: list[str]):
        super().__init__(name=f"tunnel-{name}", daemon=True)
        self.label, self.cfg, self.args = name, cfg, args
        self.state = "starting"
        self.detail = ""
        self.since = time.time()
        self._halt = threading.Event()
        self._proc: subprocess.Popen | None = None

    def _note(self, state: str, detail: str = "") -> None:
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail, self.since = state, detail, time.time()
            log.info("[%s] %s%s", self.label, state, f": {detail}" if detail else "")

    def run(self) -> None:
        while not self._halt.is_set():
            host = sshconf.resolved_host(self.cfg)
            if not sshconf.reachable(host):
                self._note("waiting", "device not reachable (away, asleep, or offline); checking every 30s")
                self._halt.wait(30)
                continue
            started = time.time()
            try:
                self._proc = subprocess.Popen(command(self.cfg, self.args), stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                              creationflags=sshconf.NO_WINDOW)
            except OSError as e:
                self._note("failed", f"can't run ssh: {e}")
                self._halt.wait(30)
                continue
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._note("up", host or "")
            err = self._proc.communicate()[1] or ""
            lasted = time.time() - started
            last = err.strip().splitlines()[-1] if err.strip() else ""
            if self._halt.is_set():
                break
            if lasted >= 30:
                self._note("reconnecting", f"closed after {lasted:.0f}s{': ' + last if last else ''}")
                self._halt.wait(2)
            else:
                self._note("failed", last or "ssh exited")
                self._halt.wait(30)
        self._note("stopped")

    def stop(self) -> None:
        self._halt.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail, "since": int(self.since)}


def start_all(cfg: dict) -> list[Forward]:
    if not cfg["device"].get("host"):
        log.info("no device address yet (run `relaymcp setup`); tunnels not started")
        return []
    threads = [Forward(name, cfg, args) for name, args in forwards(cfg).items()]
    for t in threads:
        t.start()
    return threads

