"""Is someone using the handheld right now? Rollouts and deploys restart the handheld's servers and this computer's
tunnels, so they wait until nobody is: no recent MCP tool calls, no `relaymcp busy` mark, no keep-awake lease, and no
command running over SSH (a long-running server, a build, a script). Tunnels run no command, so they don't count.

Any agent or script can mark the handheld busy for a while with `relaymcp busy <minutes>`.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import sshconf

# One JSON line: seconds since the last MCP tool call, the busy and keep-awake marks (device-local times, compared
# with the device's own clock), and the processes SSH commands started (except this check's own).
PROBE_SCRIPT = r"""
$u = Join-Path $env:LOCALAPPDATA 'RelayMCP'; $now = Get-Date
$o = [ordered]@{ now = $now.ToString('s') }
$t = @('hardware.log', 'windows-mcp.out.log') | ForEach-Object { $p = Join-Path $u $_; if (Test-Path $p) { (Get-Item $p).LastWriteTime } } | Sort-Object -Descending | Select-Object -First 1
$o.idle_s = if ($t) { [int]($now - $t).TotalSeconds } else { 99999 }
foreach ($n in 'busy', 'awake') {
    $f = Join-Path $u "$n-until.txt"
    $o["$($n)_until"] = if (Test-Path $f) { [string](Get-Content $f -TotalCount 1) } else { '' }
}
$nf = Join-Path $u 'busy-note.txt'
$o.busy_note = if (Test-Path $nf) { [string](Get-Content $nf -TotalCount 1) } else { '' }
$all = @(Get-CimInstance Win32_Process)
$sshd = @($all | Where-Object { $_.Name -eq 'sshd-session.exe' } | ForEach-Object { $_.ProcessId })
$mine = @(); $p = $PID
for ($i = 0; $i -lt 16 -and $p; $i++) { $mine += $p; $p = ($all | Where-Object { $_.ProcessId -eq $p } | Select-Object -First 1).ParentProcessId }
$pf = Join-Path $u 'procs.json'
$o.proc_sessions = @(if (Test-Path $pf) { try { (Get-Content $pf -Raw | ConvertFrom-Json).running | Where-Object {
    $p = Get-Process -Id $_.pid -ErrorAction SilentlyContinue
    $p -and $_.start_epoch -and [math]::Abs(([DateTimeOffset]$p.StartTime).ToUnixTimeSeconds() - [double]$_.start_epoch) -lt 10
} | ForEach-Object { $_.name } } catch { } })
$o.ssh_commands = @($all | Where-Object { ($sshd -contains $_.ParentProcessId) -and ($_.Name -notin @('sshd-session.exe', 'conhost.exe')) -and ($mine -notcontains $_.ProcessId) } | ForEach-Object { '{0} since {1:HH:mm}' -f $_.Name, $_.CreationDate })
$o | ConvertTo-Json -Compress
"""

MARK_SCRIPT = r"""
$d = Join-Path $env:LOCALAPPDATA 'RelayMCP'; New-Item -ItemType Directory -Force -Path $d | Out-Null
$until = (Get-Date).AddMinutes([int]'__MINUTES__')
Set-Content -Path (Join-Path $d 'busy-until.txt') -Value $until.ToString('s') -Encoding ascii
Set-Content -Path (Join-Path $d 'busy-note.txt') -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__NOTE__'))) -Encoding utf8
'marked busy until ' + $until.ToString('ddd HH:mm')
"""

CLEAR_SCRIPT = r"""
$d = Join-Path $env:LOCALAPPDATA 'RelayMCP'
Remove-Item (Join-Path $d 'busy-until.txt'), (Join-Path $d 'busy-note.txt') -ErrorAction SilentlyContinue
'busy mark cleared'
"""


def _when(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).strip()) if text and str(text).strip() else None
    except ValueError:
        return None


def reasons(probe: dict, quiet_s: int = 60, ignore_lease: bool = False) -> list[str]:
    """Why the handheld counts as busy (empty = quiet)."""
    out = []
    idle = probe.get("idle_s")
    if isinstance(idle, (int, float)) and idle < quiet_s:
        out.append(f"an MCP tool call {int(idle)} s ago")
    now = _when(probe.get("now")) or datetime.now()
    busy_until = _when(probe.get("busy_until"))
    if busy_until and busy_until > now:
        note = (probe.get("busy_note") or "").strip()
        out.append(f"marked busy until {busy_until:%H:%M}" + (f" ({note})" if note else ""))
    awake_until = _when(probe.get("awake_until"))
    if awake_until and awake_until > now and not ignore_lease:
        out.append(f"a keep-awake lease until {awake_until:%H:%M}")
    sessions = probe.get("proc_sessions") or []
    if isinstance(sessions, str):
        sessions = [sessions]
    if sessions:
        out.append("process sessions running: " + ", ".join(sessions[:4]))
    commands = probe.get("ssh_commands") or []
    if isinstance(commands, str):
        commands = [commands]
    if commands:
        out.append("running over SSH: " + ", ".join(commands[:4]) + (" ..." if len(commands) > 4 else ""))
    return out


def probe(cfg: dict) -> dict:
    out = sshconf.powershell(cfg, PROBE_SCRIPT, timeout=45)
    for line in reversed(out.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"couldn't check whether the handheld is busy: {(out.stdout + out.stderr).strip()[-200:]}")


def mark(cfg: dict, minutes: int, note: str = "") -> str:
    import base64
    script = (MARK_SCRIPT.replace("__MINUTES__", str(max(1, min(int(minutes), 24 * 60))))
              .replace("__NOTE__", base64.b64encode(note[:200].encode()).decode()))
    return sshconf.powershell(cfg, script, timeout=30).stdout.strip()


def clear(cfg: dict) -> str:
    return sshconf.powershell(cfg, CLEAR_SCRIPT, timeout=30).stdout.strip()
