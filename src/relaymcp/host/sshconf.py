"""SSH plumbing: RelayMCP's own key, ssh_config and known_hosts (all in ~/.relaymcp), plus running commands on the
handheld. Your ~/.ssh/config only gains one `Include` line, so `ssh <device>` works in any terminal too."""

from __future__ import annotations

import base64
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from . import config, devguard

MARK = "# Added by RelayMCP (https://github.com/brennengreen/RelayMCP); remove with `relaymcp uninstall`"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ssh_exe() -> str:
    return shutil.which("ssh") or "ssh"


def host_key_alias(cfg: dict) -> str:
    return f"relaymcp-{cfg['device']['name']}"


def ensure_key() -> bool:
    """Create RelayMCP's SSH key if needed. Returns True if it was created."""
    if config.KEY_FILE.exists():
        return False
    config.HOME.mkdir(parents=True, exist_ok=True)
    comment = f"relaymcp@{socket.gethostname().split('.')[0]}"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(config.KEY_FILE)],
                   check=True, creationflags=NO_WINDOW)
    return True


def public_key() -> str:
    return Path(str(config.KEY_FILE) + ".pub").read_text(encoding="utf-8").strip()


def _p(path: Path) -> str:
    """A path for ssh_config: forward slashes, quoted if it has spaces."""
    s = str(path).replace("\\", "/")
    return f'"{s}"' if " " in s else s


def render_ssh_config(cfg: dict) -> str:
    d = cfg["device"]
    lines = [
        "# Managed by RelayMCP (`relaymcp setup` rewrites this file; don't edit it by hand).",
        f"Host {d['name']}",
    ]
    if d.get("host"):
        lines.append(f"  HostName {d['host']}")
    if d.get("user"):
        lines.append(f"  User {d['user']}")
    lines += [
        f"  HostKeyAlias {host_key_alias(cfg)}",
        f"  IdentityFile {_p(config.KEY_FILE)}",
        "  IdentitiesOnly yes",
        f"  UserKnownHostsFile {_p(config.KNOWN_HOSTS)}",
        "  StrictHostKeyChecking yes",
        "  ConnectTimeout 10",
        "  ServerAliveInterval 30",
    ]
    if os.name != "nt":  # Windows' OpenSSH client can't multiplex connections
        lines += ["  ControlMaster auto", f"  ControlPath {_p(config.STATE_DIR / 'cm-%C')}", "  ControlPersist 10m"]
    return "\n".join(lines) + "\n"


def write_ssh_config(cfg: dict) -> bool:
    """Write ~/.relaymcp/ssh_config and make sure ~/.ssh/config includes it. Returns True if anything changed."""
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    text = render_ssh_config(cfg)
    changed = not config.SSH_CONFIG.exists() or config.SSH_CONFIG.read_text(encoding="utf-8") != text
    if changed:
        config.SSH_CONFIG.write_text(text, encoding="utf-8")
    return ensure_include() or changed


def user_ssh_config() -> Path:
    return Path.home() / ".ssh" / "config"


def ensure_include() -> bool:
    """Put `Include <~/.relaymcp/ssh_config>` at the top of ~/.ssh/config (Include only applies globally there)."""
    devguard.check("edit ~/.ssh/config")
    path = user_ssh_config()
    line = f"Include {_p(config.SSH_CONFIG)}"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if line in current.splitlines():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    path.write_text(f"{MARK}\n{line}\n\n{current}", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return True


def remove_include() -> bool:
    devguard.check("edit ~/.ssh/config")
    path = user_ssh_config()
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(True)
    keep = [ln for ln in lines if ln.strip() != MARK and ln.strip() != f"Include {_p(config.SSH_CONFIG)}"]
    if len(keep) == len(lines):
        return False
    text = "".join(keep).lstrip("\n")
    path.write_text(text, encoding="utf-8")
    return True


def trust_host_key(cfg: dict, key: str) -> None:
    """Trust the handheld's SSH host key ("ssh-ed25519 AAAA...") under its HostKeyAlias."""
    parts = key.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-")):
        raise ValueError(f"not an SSH public key: {key[:40]!r}")
    alias = host_key_alias(cfg)
    lines = []
    if config.KNOWN_HOSTS.exists():
        lines = [ln for ln in config.KNOWN_HOSTS.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and ln.split()[0] != alias]
    lines.append(f"{alias} {parts[0]} {parts[1]}")
    config.KNOWN_HOSTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    close_master(cfg)


def fingerprint(key: str) -> str:
    try:
        out = subprocess.run(["ssh-keygen", "-lf", "-"], input=key, capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.split()
        return out[1] if len(out) > 1 else "?"
    except Exception:
        return "?"


def scan_host_key(host: str) -> str | None:
    devguard.check("contact the handheld")
    try:
        out = subprocess.run(["ssh-keyscan", "-T", "5", "-t", "ed25519", host], capture_output=True, text=True,
                             timeout=20, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            return f"{parts[1]} {parts[2]}"
    return None


def close_master(cfg: dict) -> None:
    devguard.check("close the real SSH control connection")
    if os.name != "nt":
        subprocess.run([ssh_exe(), "-O", "exit", cfg["device"]["name"]], capture_output=True, timeout=10)


def resolved_host(cfg: dict) -> str | None:
    """The HostName ssh would connect to for the device alias."""
    try:
        out = subprocess.run([ssh_exe(), "-G", cfg["device"]["name"]], capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"^hostname (\S+)$", out, re.M)
    return m.group(1) if m else None


def reachable(host: str | None, port: int = 22, timeout: float = 3.0) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(cfg: dict, command: str, timeout: float = 60, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command in the device's default shell (Windows PowerShell)."""
    devguard.check("run commands on the handheld")
    return subprocess.run([ssh_exe(), "-o", "BatchMode=yes", cfg["device"]["name"], command], capture_output=True,
                          text=True, timeout=timeout, check=check, creationflags=NO_WINDOW)


def powershell(cfg: dict, script: str, timeout: float = 600) -> subprocess.CompletedProcess:
    """Run a multi-line PowerShell script on the device (encoded, so quoting can't break it). Errors come back as
    'ERROR: ...' lines on stdout."""
    wrapped = ("$ProgressPreference = 'SilentlyContinue'\n$ErrorActionPreference = 'Stop'\n"
               "trap { Write-Output ('ERROR: ' + $_.Exception.Message); exit 1 }\n" + script)
    encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode()
    result = run(cfg, f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}",
                 timeout=timeout)
    result.stderr = clixml_to_text(result.stderr)
    return result


def clixml_to_text(text: str) -> str:
    """PowerShell sends errors over a redirected stderr as CLIXML; turn that back into readable lines."""
    if not text or "#< CLIXML" not in text:
        return text
    import html
    parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
    out = "".join(html.unescape(p).replace("_x000D__x000A_", "\n") for p in parts)
    return out.strip() + "\n" if out.strip() else ""


def copy_to(cfg: dict, local: list[Path], remote_dir: str, timeout: float = 600) -> None:
    """scp files to a folder (relative to the device user's home)."""
    devguard.check("copy files to the handheld")
    subprocess.run([shutil.which("scp") or "scp", "-q", "-o", "BatchMode=yes", *[str(p) for p in local],
                    f"{cfg['device']['name']}:{remote_dir}/"], check=True, timeout=timeout, creationflags=NO_WINDOW)
