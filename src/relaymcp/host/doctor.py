"""`relaymcp doctor` / `relaymcp status`: checks every link in the chain and says how to fix what's broken."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from dataclasses import dataclass

import relaymcp

from . import agents, config, daemon, kit, services, sshconf, ui

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    optional: bool = False


def mcp_ok(url: str, timeout: float = 15) -> tuple[bool, str]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                            "Accept": "application/json, text/event-stream"})
    try:
        raw = _NO_PROXY.open(req, timeout=timeout).read().decode()
        data = next((json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")), None) \
            or json.loads(raw)
        tools = data.get("result", {}).get("tools", [])
        return bool(tools), f"{len(tools)} tools"
    except Exception as e:
        return False, str(e)[:80]


def voice_health(port: int) -> dict | None:
    try:
        return json.loads(_NO_PROXY.open(f"http://127.0.0.1:{port}/health", timeout=3).read().decode())
    except Exception:
        return None


DEVICE_PROBE = r"""
$u = Join-Path $env:LOCALAPPDATA 'RelayMCP'; $m = Join-Path $env:ProgramData 'RelayMCP'
$o = [ordered]@{ computer = $env:COMPUTERNAME; state = [string](Get-Content (Join-Path $m 'state.txt') -ErrorAction SilentlyContinue | Select-Object -First 1) }
try { $o.agent = Get-Content (Join-Path $u 'agent-status.json') -Raw | ConvertFrom-Json } catch { $o.agent = $null }
$t = Get-ScheduledTask -TaskName 'RelayMCP-Agent' -ErrorAction SilentlyContinue; $o.agent_task = if ($t) { [string]$t.State } else { 'missing' }
$dj = $null; try { $dj = Get-Content (Join-Path $m 'device.json') -Raw -ErrorAction Stop | ConvertFrom-Json } catch { }
$o.runtime = [ordered]@{ hash = [string](Get-Content (Join-Path $m 'relaymcp-device.sha256') -ErrorAction SilentlyContinue | Select-Object -First 1); version = [string]$dj.version }
try { $o.defender_detections_24h = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -gt (Get-Date).AddDays(-1) -and (($_.Resources -join ' ') -match 'relaymcp|windows_mcp') }).Count } catch { }
$o | ConvertTo-Json -Depth 5 -Compress
"""


def device_probe(cfg: dict) -> dict | None:
    try:
        out = sshconf.powershell(cfg, DEVICE_PROBE, timeout=40)
    except Exception:
        return None
    for line in reversed(out.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def runtime_check(local_hash: str, local_version: str, remote: dict | None) -> Check:
    """Is the handheld running the device runtime this computer would deploy? (The kit's zip is deterministic, so
    equal hashes mean identical code.)"""
    remote = remote or {}
    rh, rv = (remote.get("hash") or "").strip().lower(), remote.get("version") or "?"
    if not rh:
        return Check("Device runtime", False, "unknown (no deployed build recorded on the handheld)", "run `relaymcp setup`")
    if rh == local_hash.lower():  # identical code, so the same version (device.json's is only rewritten by setup)
        return Check("Device runtime", True, f"up to date ({local_version}, build {rh[:8]})")
    return Check("Device runtime", False, f"outdated: the handheld runs {rv} (build {rh[:8]}), this computer has "
                 f"{local_version} (build {local_hash[:8]})", "run `relaymcp setup` (or `relaymcp deploy`)")


def run_checks(cfg: dict, deep: bool = True) -> list[Check]:
    d = cfg["device"]
    name = d["name"]
    checks: list[Check] = []
    checks.append(Check("Configuration", config.exists(), str(config.CONFIG_FILE), "run `relaymcp setup`"))
    checks.append(Check("SSH key", config.KEY_FILE.exists(), str(config.KEY_FILE), "run `relaymcp setup`"))
    include = sshconf.user_ssh_config()
    inc_ok = include.exists() and str(config.SSH_CONFIG).replace("\\", "/") in include.read_text(encoding="utf-8")
    checks.append(Check("ssh config", inc_ok and config.SSH_CONFIG.exists(), f"`ssh {name}` via {config.SSH_CONFIG}",
                        "run `relaymcp setup`"))
    checks.append(Check("Device address", bool(d.get("host")), d.get("host") or "unknown",
                        "run `relaymcp setup` and set up the handheld (or `relaymcp trust <ip>`)"))
    installed, running, desc = services.status()
    st = daemon.read_status()
    fresh = bool(st) and time.time() - st.get("updated", 0) < 30
    checks.append(Check("Background service", installed and running and fresh, desc,
                        "run `relaymcp service install` (logs: `relaymcp logs`)"))
    host = sshconf.resolved_host(cfg)
    reach = sshconf.reachable(host)
    checks.append(Check("Device reachable", reach, f"{host}:22" if host else "no address",
                        "is the handheld awake, signed in and on your home network? (SSH is off away from home)"))
    probe = None
    if reach:
        r = sshconf.run(cfg, "hostname", timeout=20)
        ssh_ok = r.returncode == 0
        checks.append(Check("SSH login", ssh_ok, r.stdout.strip() or (r.stderr.strip().splitlines() or ["failed"])[-1],
                            "tap 'Repair RelayMCP' on the handheld; if its host key changed: `relaymcp trust`"))
        if ssh_ok and deep:
            probe = device_probe(cfg)
    if st:
        for label, t in (st.get("tunnels") or {}).items():
            checks.append(Check(f"Tunnel ({label})", t.get("state") == "up", f"{t.get('state')} {t.get('detail', '')}".strip(),
                                "it reconnects by itself once the handheld is reachable"))
    for server, url, _desc in config.mcp_servers(cfg):
        good, detail = mcp_ok(url) if reach else (False, "device not reachable")
        checks.append(Check(f"MCP '{server}'", good, f"{url} ({detail})",
                            "wait ~20 s after the handheld wakes; else `relaymcp logs --device`"))
    if cfg["voice"].get("enabled", True):
        h = voice_health(int(d["ports"]["voice"]))
        good = bool(h and h.get("ok") and h.get("agent_found"))
        detail = f"agent={h.get('agent')} permissions={h.get('permissions')}" if h else "not responding"
        if h and h.get("runtime"):
            detail += {"warm": ", warm runtime (fast)", "cold": ", warm runtime starting",
                       "failed": ", warm runtime failed: using copilot -p",
                       "cli": ""}.get(h["runtime"], "")
            if h["runtime"] == "cli" and h.get("agent") == "copilot" and h.get("permissions") != "full":
                detail += " (install relaymcp[voice] for ~2x faster replies)"
        checks.append(Check("Voice dispatcher", good, detail, "`relaymcp service restart`; is Copilot CLI installed?",
                            optional=True))
    if agents.detected()["copilot"]:
        checks.append(Check("Copilot CLI registration", agents.copilot_registered(cfg),
                            ", ".join(n for n, _, _ in config.mcp_servers(cfg)), "run `relaymcp mcp add`"))
    if probe:
        at_home = probe.get("state") == "home"
        checks.append(Check("Device location", at_home, probe.get("state") or "unknown",
                            "remote access only runs on the home network"))
        agent = probe.get("agent") or {}
        svcs = agent.get("services") or {}
        good = probe.get("agent_task") == "Running" and all(s.get("running") for s in svcs.values()) and bool(svcs)
        detail = ", ".join(f"{k} {'up' if v.get('running') else 'down'} (restarts {v.get('restarts', 0)})"
                           for k, v in svcs.items()) or f"task {probe.get('agent_task')}"
        checks.append(Check("Device agent", good, detail, "tap 'Repair RelayMCP' on the handheld"))
        try:
            local = hashlib.sha256(kit.build_device_zip()).hexdigest()
            checks.append(runtime_check(local, relaymcp.__version__, probe.get("runtime")))
        except Exception as e:
            checks.append(Check("Device runtime", False, f"couldn't compare builds: {e}", optional=True))
        if agent.get("keep_awake"):
            checks.append(Check("Keep-awake", True, agent["keep_awake"], optional=True))
        det = probe.get("defender_detections_24h")
        if det:
            checks.append(Check("Microsoft Defender", False, f"{det} detection(s) involving RelayMCP in 24 h",
                                "see docs/troubleshooting.md#microsoft-defender"))
    return checks


def print_checks(checks: list[Check]) -> bool:
    all_ok = True
    for c in checks:
        if c.ok:
            ui.ok(f"{c.name}: {c.detail}")
        elif c.optional:
            ui.warn(f"{c.name}: {c.detail}" + (f"  \u2192 {c.fix}" if c.fix else ""))
        else:
            all_ok = False
            ui.fail(f"{c.name}: {c.detail}" + (f"  \u2192 {c.fix}" if c.fix else ""))
    return all_ok
