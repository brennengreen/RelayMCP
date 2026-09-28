"""RelayMCP settings and file locations on the controlling computer (everything lives in ~/.relaymcp)."""

from __future__ import annotations

import copy
import getpass
import json
import os
import platform
import secrets
import subprocess
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("RELAYMCP_HOME") or Path.home() / ".relaymcp").expanduser()
CONFIG_FILE = HOME / "config.json"
KEY_FILE = HOME / "id_ed25519"
SSH_CONFIG = HOME / "ssh_config"
KNOWN_HOSTS = HOME / "known_hosts"
KIT_DIR = HOME / "kit"
CACHE_DIR = HOME / "cache"
LOG_DIR = HOME / "logs"
STATE_DIR = HOME / "state"
VOICE_WORKDIR = HOME / "voice-workspace"

WINDOWS_MCP = "windows-mcp==0.8.6"  # pinned: the version RelayMCP is tested with

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "host_label": None,  # how the handheld refers to this computer ("your Mac"); set by setup
    "device": {
        "name": "ally",             # ssh alias; MCP servers are "<name>" (screen) and "<name>-handheld" (hardware)
        "host": None,               # IP address or hostname (learned when the device checks in)
        "user": None,               # Windows account name on the device
        "computer_name": None,
        "home_networks": [],        # Wi-Fi names (cosmetic; home is recognized by the router's MAC address)
        "home_gateways": [],        # router MAC addresses, e.g. "AA-BB-CC-11-22-33"
        "ports": {"screen": 8765, "hardware": 8767, "voice": 8768},
    },
    "voice": {
        "enabled": True,
        "agent": "copilot",          # copilot | custom (see docs/voice.md)
        "custom_command": None,      # for agent=custom: ["my-agent", "--prompt", "{prompt}"]
        "permissions": "handheld",   # handheld = only the device's tools; full = everything on this computer too
        "extra_servers": [],         # handheld mode: other Copilot MCP servers voice may use too, e.g. ["pl515"]
        "model": "gpt-5.4-mini",     # fast; falls back to the agent's default if unavailable
        "reasoning_effort": "low",   # dropped automatically for models that don't support it
        "timeout_minutes": 10,
        "new_conversation_after_minutes": 20,
        "notify": True,
        "workdir": str(VOICE_WORKDIR),
        "voice": "af_heart",         # neural voice id, or a Windows voice name ("Microsoft Zira Desktop")
        "speech_speed": 1.0,
        "user_name": None,           # used in the prompt preamble ("Voice request from <user_name>")
    },
    "enroll": {"port": 8766, "token": None},
    "windows_mcp": WINDOWS_MCP,
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    data: dict = {}
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return _merge(defaults(), data)


def defaults() -> dict:
    """DEFAULTS, with ports moved out of the way (+10000) in dev mode so a dev daemon can never collide with the real
    one."""
    out = copy.deepcopy(DEFAULTS)
    if os.environ.get("RELAYMCP_DEV") == "1":
        out["device"]["ports"] = {k: v + 10000 for k, v in out["device"]["ports"].items()}
        out["enroll"]["port"] += 10000
    return out


def exists() -> bool:
    return CONFIG_FILE.exists()


def save(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    _private(HOME)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    _private(CONFIG_FILE)


def _private(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass


def ensure_token(cfg: dict) -> str:
    """A random token that authenticates the handheld's check-in (and guards the LAN kit download)."""
    if not cfg["enroll"].get("token"):
        cfg["enroll"]["token"] = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
    return cfg["enroll"]["token"]


def default_host_label() -> str:
    system = platform.system()
    return {"Darwin": "your Mac", "Windows": "your PC", "Linux": "your computer"}.get(system, "your computer")


def default_user_name() -> str:
    """The person's display name when available (for the voice preamble), else the account name."""
    try:
        if platform.system() == "Darwin":
            name = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=5).stdout.strip()
            if name:
                return name.split()[0]
        elif os.name != "nt":
            import pwd
            gecos = pwd.getpwnam(getpass.getuser()).pw_gecos.split(",")[0].strip()
            if gecos:
                return gecos.split()[0]
    except Exception:
        pass
    return getpass.getuser()


def copilot_mcp_servers() -> dict:
    """Copilot CLI's user-level MCP servers (~/.copilot/mcp-config.json)."""
    try:
        return json.loads((Path.home() / ".copilot" / "mcp-config.json").read_text()).get("mcpServers", {})
    except (OSError, ValueError):
        return {}


def voice_servers(cfg: dict) -> tuple[str, ...]:
    """The MCP servers a handheld-mode voice prompt may use: the device's two, then voice.extra_servers."""
    name = cfg["device"]["name"]
    own = (name, f"{name}-handheld")
    extra = [s for s in cfg["voice"].get("extra_servers") or [] if s and s not in own]
    return own + tuple(dict.fromkeys(extra))


def voice_extra_server_configs(cfg: dict) -> dict:
    """Copilot's own entries for voice.extra_servers (the ones it knows), with every tool schema kept in view."""
    known = copilot_mcp_servers()
    return {s: {**known[s], "tools": ["*"], "deferTools": "never"} for s in voice_servers(cfg)[2:] if s in known}


def mcp_servers(cfg: dict) -> list[tuple[str, str, str]]:
    """(name, url, description) of the MCP servers RelayMCP exposes on this computer."""
    name, ports = cfg["device"]["name"], cfg["device"]["ports"]
    return [
        (name, f"http://127.0.0.1:{ports['screen']}/mcp", "screen control / computer use (Windows-MCP)"),
        (f"{name}-handheld", f"http://127.0.0.1:{ports['hardware']}/mcp", "handheld hardware: gamepad, touch, audio, voice"),
    ]
