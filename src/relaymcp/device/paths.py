"""Where RelayMCP keeps things on the handheld, and the device settings written by setup (device.json)."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP = "RelayMCP"
USER_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP       # logs, recordings, models, lease
MACHINE_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP   # setup, controller, state, device.json
STATE_FILE = MACHINE_DIR / "state.txt"      # "home" or "away", written by the RelayMCP-Controller task
DEVICE_FILE = MACHINE_DIR / "device.json"   # written by setup (see device_settings)
LEASE_FILE = USER_DIR / "awake-until.txt"   # keep-awake lease (keep_awake tool, `relaymcp awake`)
DISMISS_TASK = f"{APP}-DismissControllerNotice"
VOICE_HEADER = "X-Relay-Voice"              # required on local voice requests (trigger and prompt)

DEFAULTS = {
    "name": "handheld",            # the device's name on the controlling computer (MCP servers: <name>, <name>-handheld)
    "host_label": "your computer",  # how messages refer to the controlling computer ("your Mac")
    "ports": {"screen": 8765, "hardware": 8767, "voice": 8768},
    "gamepad_idle_minutes": 30,  # virtual pad unplugs after this long unused (0 = never); never while a game is in front
}


def device_settings() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(DEVICE_FILE.read_text(encoding="utf-8-sig"))
        cfg.update({k: v for k, v in data.items() if k != "ports"})
        cfg["ports"].update(data.get("ports") or {})
    except (OSError, ValueError):
        pass
    return cfg
