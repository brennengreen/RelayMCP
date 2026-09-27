"""Home-network detection on the controlling computer: the router's MAC address (how the handheld recognizes home),
this computer's LAN address (for the setup kit's check-in) and, when the OS allows it, the Wi-Fi name."""

from __future__ import annotations

import platform
import re
import socket
import subprocess

SYSTEM = platform.system()
_MAC = re.compile(r"(?<![0-9a-f:-])((?:[0-9a-f]{1,2}[:-]){5}[0-9a-f]{1,2})(?![0-9a-f:-])", re.I)


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=0x08000000 if SYSTEM == "Windows" else 0).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def normalize_mac(mac: str) -> str | None:
    """'aa:bb:cc:11:22:33' / '0:1b:2:a:b:c' / 'AA-BB-...' -> 'AA-BB-CC-11-22-33' (None if not a unicast MAC)."""
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return None
    out = "-".join(p.zfill(2).upper() for p in parts)
    return None if out in ("00-00-00-00-00-00", "FF-FF-FF-FF-FF-FF") else out


def parse_gateway_ip(text: str) -> str | None:
    """From `route -n get default` (macOS), `ip route show default` (Linux) or a bare address (Windows)."""
    m = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", text) or re.search(r"default via (\d+\.\d+\.\d+\.\d+)", text) \
        or re.search(r"^\s*(\d+\.\d+\.\d+\.\d+)\s*$", text, re.M)
    return m.group(1) if m else None


def parse_neighbor_mac(text: str) -> str | None:
    """The first MAC address in `arp -n <ip>` (macOS), `ip neigh show <ip>` (Linux) or Get-NetNeighbor output."""
    for m in _MAC.finditer(text):
        mac = normalize_mac(m.group(1))
        if mac:
            return mac
    return None


def gateway_ip() -> str | None:
    if SYSTEM == "Darwin":
        return parse_gateway_ip(_run(["route", "-n", "get", "default"]))
    if SYSTEM == "Linux":
        return parse_gateway_ip(_run(["ip", "-4", "route", "show", "default"]))
    if SYSTEM == "Windows":
        return parse_gateway_ip(_run(["powershell", "-NoProfile", "-Command",
                                      "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | "
                                      "Select-Object -First 1).NextHop"]))
    return None


def gateway_mac(ip: str | None = None) -> str | None:
    ip = ip or gateway_ip()
    if not ip:
        return None
    try:  # make sure the router is in the ARP/neighbor cache
        with socket.create_connection((ip, 80), timeout=1):
            pass
    except OSError:
        pass
    if SYSTEM == "Darwin":
        return parse_neighbor_mac(_run(["arp", "-n", ip]))
    if SYSTEM == "Linux":
        return parse_neighbor_mac(_run(["ip", "neigh", "show", ip]))
    if SYSTEM == "Windows":
        return parse_neighbor_mac(_run(["powershell", "-NoProfile", "-Command",
                                        f"(Get-NetNeighbor -IPAddress {ip} | Select-Object -First 1).LinkLayerAddress"]))
    return None


def lan_ip() -> str | None:
    """This computer's address on the network that has the default route (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((gateway_ip() or "192.0.2.1", 9))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None


def hostname() -> str:
    """A name the handheld can resolve on the LAN (mDNS .local on macOS)."""
    if SYSTEM == "Darwin":
        name = _run(["scutil", "--get", "LocalHostName"]).strip()
        if name:
            return name + ".local"
    return socket.gethostname()


def wifi_name() -> str | None:
    """Best effort; newer macOS versions hide the Wi-Fi name from command-line tools without Location access."""
    out = None
    if SYSTEM == "Darwin":
        for iface in ("en0", "en1"):
            m = re.search(r"^\s+SSID : (.+)$", _run(["ipconfig", "getsummary", iface]), re.M)
            if m:
                out = m.group(1).strip()
                break
    elif SYSTEM == "Linux":
        out = _run(["iwgetid", "-r"]).strip() or None
        if not out:
            for line in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
                if line.startswith("yes:"):
                    out = line[4:]
                    break
    elif SYSTEM == "Windows":
        m = re.search(r"^\s+SSID\s+:\s(.+)$", _run(["netsh", "wlan", "show", "interfaces"]), re.M)
        out = m.group(1).strip() if m else None
    if out and ("redacted" in out.lower() or out.startswith("<")):
        return None
    return out or None
