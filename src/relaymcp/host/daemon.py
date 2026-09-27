"""`relaymcp daemon`: the background service on the controlling computer. Keeps the SSH tunnels to the handheld up and
runs the voice dispatcher; writes its status to ~/.relaymcp/state/daemon.json. Installed as a launchd agent (macOS),
a systemd user service (Linux) or a logon task (Windows) by `relaymcp setup` / `relaymcp service install`."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from logging.handlers import RotatingFileHandler

import relaymcp

from . import config, devguard, tunnel, voice

STATUS_FILE = config.STATE_DIR / "daemon.json"
log = logging.getLogger("relaymcp")


def _logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.LOG_DIR / "daemon.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger("relaymcp")
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def read_status() -> dict | None:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run() -> None:
    devguard.check("run the background daemon (tunnels to the handheld)")
    _logging()
    cfg = config.load()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    log.info("daemon %s started (pid %d) for device %r", relaymcp.__version__, os.getpid(), cfg["device"]["name"])
    server = None
    if cfg["voice"].get("enabled", True):
        try:
            server = voice.serve(int(cfg["device"]["ports"]["voice"]))
        except OSError as e:
            log.error("voice dispatcher couldn't start on port %s: %s", cfg["device"]["ports"]["voice"], e)
    forwards = tunnel.start_all(cfg)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        while not stop.is_set():
            status = {"pid": os.getpid(), "version": relaymcp.__version__, "updated": int(time.time()),
                      "device": cfg["device"]["name"], "voice": bool(server),
                      "tunnels": {f.label: f.status() for f in forwards}}
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
            stop.wait(5)
    finally:
        for f in forwards:
            f.stop()
        if server:
            server.shutdown()
        try:
            STATUS_FILE.unlink()
        except OSError:
            pass
        log.info("daemon stopped")
