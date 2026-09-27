"""Installs `relaymcp daemon` as a background service: a launchd agent (macOS), a systemd user service (Linux) or a
logon task (Windows). It runs as you, starts at login, and restarts if it stops."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import config, devguard

LABEL = "dev.relaymcp.daemon"
SYSTEMD_UNIT = "relaymcp.service"
WINDOWS_TASK = "RelayMCP-Daemon"


def _python() -> str:
    exe = Path(sys.executable)
    if os.name == "nt":
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)
    return str(exe)


def service_path() -> str:
    """PATH for the service: where ssh and your AI agent CLI live (services don't read your shell profile)."""
    dirs: list[str] = []
    for tool in ("ssh", "copilot"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
             "/usr/sbin", "/sbin"]
    if os.name == "nt":
        dirs = [d for d in dirs if not d.startswith("/")] + os.environ.get("PATH", "").split(os.pathsep)
    seen: list[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def ephemeral_python() -> bool:
    """True when running from a throwaway environment (uvx, pipx run): a service can't point there."""
    p = sys.executable.replace("\\", "/")
    return "/archive-v0/" in p or "/.cache/uv/" in p or "/pipx/.cache/" in p


# ---------------------------------------------------------------------------------------------------- macOS

def launchd_plist() -> str:
    args = "".join(f"\n    <string>{escape(a)}</string>" for a in (_python(), "-m", "relaymcp", "daemon"))
    log = escape(str(config.LOG_DIR / "daemon.out.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- RelayMCP: SSH tunnels to your handheld + voice dispatcher. Manage with `relaymcp service ...`. -->
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{escape(service_path())}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _launchd_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


# ---------------------------------------------------------------------------------------------------- Linux

def systemd_unit() -> str:
    return f"""# RelayMCP: SSH tunnels to your handheld + voice dispatcher. Manage with `relaymcp service ...`.
[Unit]
Description=RelayMCP daemon (tunnels to your handheld, voice prompts)
After=network-online.target

[Service]
ExecStart="{_python()}" -m relaymcp daemon
Environment="PATH={service_path()}"
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _systemd_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / SYSTEMD_UNIT


# ---------------------------------------------------------------------------------------------------- API

def backend() -> str:
    return {"Darwin": "launchd", "Linux": "systemd", "Windows": "task"}.get(platform.system(), "none")


def install() -> str:
    """Install (or update) and start the service. Returns a short description."""
    devguard.check("install or update the background service")
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    kind = backend()
    if kind == "launchd":
        path = _launchd_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = launchd_plist()
        if not path.exists() or path.read_text() != text:
            subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
            path.write_text(text)
        loaded = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True).returncode == 0
        if not loaded:
            subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], check=True, capture_output=True)
        else:
            subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], capture_output=True)
        return f"launchd agent {LABEL}"
    if kind == "systemd":
        path = _systemd_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit())
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", SYSTEMD_UNIT], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "restart", SYSTEMD_UNIT], check=True)
        return f"systemd user service {SYSTEMD_UNIT} (tip: `loginctl enable-linger` keeps it running when logged out)"
    if kind == "task":
        command = f'"{_python()}" -m relaymcp daemon'
        subprocess.run(["schtasks", "/Create", "/F", "/TN", WINDOWS_TASK, "/SC", "ONLOGON", "/RL", "LIMITED",
                        "/TR", command], check=True, capture_output=True)
        subprocess.run(["schtasks", "/End", "/TN", WINDOWS_TASK], capture_output=True)
        subprocess.run(["schtasks", "/Run", "/TN", WINDOWS_TASK], check=True, capture_output=True)
        return f"scheduled task {WINDOWS_TASK} (at logon)"
    raise RuntimeError(f"no background-service support for {platform.system()}; run `relaymcp daemon` yourself")


def uninstall() -> bool:
    devguard.check("remove the background service")
    kind = backend()
    if kind == "launchd":
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
        path = _launchd_file()
    elif kind == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], capture_output=True)
        path = _systemd_file()
    elif kind == "task":
        subprocess.run(["schtasks", "/End", "/TN", WINDOWS_TASK], capture_output=True)
        return subprocess.run(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK], capture_output=True).returncode == 0
    else:
        return False
    if path.exists():
        path.unlink()
        return True
    return False


def restart() -> None:
    devguard.check("restart the background service")
    kind = backend()
    if kind == "launchd":
        subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], capture_output=True)
    elif kind == "systemd":
        subprocess.run(["systemctl", "--user", "restart", SYSTEMD_UNIT], capture_output=True)
    elif kind == "task":
        subprocess.run(["schtasks", "/End", "/TN", WINDOWS_TASK], capture_output=True)
        subprocess.run(["schtasks", "/Run", "/TN", WINDOWS_TASK], capture_output=True)


def status() -> tuple[bool, bool, str]:
    """(installed, running, description)."""
    kind = backend()
    if kind == "launchd":
        installed = _launchd_file().exists()
        out = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True).stdout
        return installed, "state = running" in out, f"launchd agent {LABEL}"
    if kind == "systemd":
        installed = _systemd_file().exists()
        active = subprocess.run(["systemctl", "--user", "is-active", SYSTEMD_UNIT], capture_output=True,
                                text=True).stdout.strip() == "active"
        return installed, active, f"systemd user service {SYSTEMD_UNIT}"
    if kind == "task":
        out = subprocess.run(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/FO", "LIST"], capture_output=True, text=True)
        return out.returncode == 0, "Running" in out.stdout, f"scheduled task {WINDOWS_TASK}"
    return False, False, "unsupported"
