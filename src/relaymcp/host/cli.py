"""relaymcp: set up and run RelayMCP on the computer that controls your handheld.

  relaymcp setup      one-time setup (then run the kit it builds on the handheld)
  relaymcp status     is everything up?          relaymcp doctor   ...with fixes
  relaymcp say TEXT   speak on the handheld      relaymcp awake    keep it awake for a while
  relaymcp voice      voice-prompt settings      relaymcp mcp      MCP client configuration
  relaymcp logs       host (or --device) logs    relaymcp deploy   push local changes to the handheld
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import relaymcp

from . import agents, config, daemon, devguard, doctor, enroll, kit, netinfo, services, sshconf, ui

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
DEVICE_LOGS = ["agent.log", "hardware.log", "hardware.out.log", "windows-mcp.out.log", "keepawake.log"]


# ---------------------------------------------------------------------------------------------------- helpers

def _need_config() -> dict:
    if not config.exists():
        ui.fail("RelayMCP isn't set up on this computer yet. Run: relaymcp setup")
        sys.exit(1)
    return config.load()


def _need_device(cfg: dict) -> None:
    if not sshconf.reachable(sshconf.resolved_host(cfg)):
        ui.fail(f"'{cfg['device']['name']}' isn't reachable (asleep, off, or away from home?). Try: relaymcp doctor")
        sys.exit(1)


def mcp_call(cfg: dict, tool: str, arguments: dict, timeout: float = 180) -> dict:
    """Call a tool on the handheld's hardware server through the tunnel."""
    url = f"http://127.0.0.1:{cfg['device']['ports']['hardware']}/mcp"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                            "Accept": "application/json, text/event-stream"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = opener.open(req, timeout=timeout).read().decode()
    msg = next((json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")), None) or json.loads(raw)
    res = msg.get("result", msg)
    text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise RuntimeError(text or "tool error")
    sc = res.get("structuredContent")
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else {"result": sc}
    try:
        return json.loads(text)
    except ValueError:
        return {"result": text}


def apply_checkin(cfg: dict, report: dict) -> None:
    """Trust what the handheld reported at the end of its setup (host key, address, account, router)."""
    if report.get("status") != "ok":
        raise RuntimeError(f"the handheld reported a problem: {report.get('error') or report}")
    key = report.get("hostkey", "")
    d = cfg["device"]
    d["host"] = report.get("ip") or report.get("from") or d.get("host")
    d["user"] = report.get("user") or d.get("user")
    d["computer_name"] = report.get("host") or d.get("computer_name")
    for mac in (report.get("gateway") or "").split(","):
        mac = netinfo.normalize_mac(mac) or ""
        if mac and mac not in d["home_gateways"]:
            d["home_gateways"].append(mac)
    config.save(cfg)
    sshconf.write_ssh_config(cfg)
    sshconf.trust_host_key(cfg, key)
    ui.ok(f"{d['computer_name'] or 'the handheld'} checked in from {d['host']}; trusted its SSH host key "
          f"{sshconf.fingerprint(key)}")


def wait_for_checkin(cfg: dict, kit_dir: Path, host_ip: str | None) -> bool:
    token, port = config.ensure_token(cfg), int(cfg["enroll"]["port"])
    e = enroll.Enrollment(kit_dir, token, host_ip or "0.0.0.0", port)
    try:
        e.start()
    except OSError as err:
        ui.warn(f"couldn't serve the kit on port {port} ({err}); use the USB route")
    label = cfg.get("host_label") or config.default_host_label()
    print()
    ui.step("Now, on the handheld (signed in, on your home network)")
    ui.info(f"Either copy {ui.bold(str(kit_dir))} to a USB drive or microSD card and double-tap")
    ui.info(f"{ui.bold('Setup RelayMCP.cmd')} on it, or press Win+R and run:")
    ui.info("")
    ui.info(ui.bold(f'  powershell -c "{e.one_liner}"'))
    ui.info("")
    ui.info("Choose Yes when Windows asks. It takes 2-5 minutes the first time (it downloads Python, speech models")
    ui.info(f"and the natural voice), then checks in with {label}.")
    ui.info(ui.dim("Waiting for the check-in... (Ctrl+C to stop; `relaymcp enroll` resumes waiting)"))
    try:
        report = e.wait()
    except KeyboardInterrupt:
        print()
        ui.warn("stopped waiting. After the handheld is set up, run `relaymcp enroll` (or `relaymcp trust <ip>`).")
        return False
    finally:
        e.stop()
    apply_checkin(cfg, report or {})
    return True


# ---------------------------------------------------------------------------------------------------- commands

def cmd_setup(args: argparse.Namespace) -> None:
    first = not config.exists()
    cfg = config.load()
    ui.step(f"RelayMCP {relaymcp.__version__} setup" + ("" if first else " (updating your existing setup)"))
    if services.ephemeral_python():
        ui.warn("you're running from a temporary environment (uvx); install RelayMCP first so its background service"
                " keeps working:  uv tool install relaymcp  (see README)")
    d = cfg["device"]
    name = args.name or (ui.ask("A short name for your handheld (its ssh alias and MCP server name)", d["name"])
                         if first else d["name"])
    if not NAME_RE.match(name):
        ui.fail("use lowercase letters, digits and dashes (e.g. ally, legion-go)")
        sys.exit(2)
    d["name"] = name
    cfg["host_label"] = args.host_label or cfg.get("host_label") or config.default_host_label()
    cfg["voice"]["user_name"] = cfg["voice"].get("user_name") or config.default_user_name()

    ui.step("Home network")
    gw, ssid = netinfo.gateway_mac(), netinfo.wifi_name()
    if gw and gw not in d["home_gateways"]:
        where = f"router {gw}" + (f" on '{ssid}'" if ssid else "")
        if args.yes or ui.confirm(f"Is this computer on your home network right now ({where})?", True):
            d["home_gateways"].append(gw)
    if ssid and ssid not in d["home_networks"]:
        d["home_networks"].append(ssid)
    if not d["home_networks"]:
        d["home_networks"] = [ui.ask("Your home Wi-Fi name (only used in messages on the handheld)", "home")]
    if d["home_gateways"]:
        ui.ok(f"home = router {', '.join(d['home_gateways'])} ({', '.join(d['home_networks'])})")
    else:
        ui.warn("no home router recorded: the handheld will ask to trust your Wi-Fi the first time setup runs there")

    ui.step("SSH")
    if sshconf.ensure_key():
        ui.ok(f"created RelayMCP's key {config.KEY_FILE}")
    config.ensure_token(cfg)
    config.save(cfg)
    sshconf.write_ssh_config(cfg)
    ui.ok(f"`ssh {name}` is configured ({config.SSH_CONFIG}, included from ~/.ssh/config)")

    ui.step("Setup kit for the handheld")
    host_ip, host_name = netinfo.lan_ip(), netinfo.hostname()
    kit_dir = kit.build(cfg, host_ip=host_ip, host_name=host_name)
    ui.ok(f"{kit_dir}")
    if args.usb:
        target = Path(args.usb) / "RelayMCP"
        shutil.copytree(kit_dir, target, dirs_exist_ok=True)
        ui.ok(f"copied to {target}")

    ui.step("Background service (tunnels + voice prompts)")
    try:
        ui.ok(services.install())
    except Exception as e:
        ui.fail(f"couldn't install the background service: {e}")

    ui.step("AI agents")
    found = agents.detected()
    if found["copilot"]:
        changed = agents.register_copilot(cfg)
        servers = ", ".join(n for n, _, _ in config.mcp_servers(cfg))
        ui.ok(f"GitHub Copilot CLI: {servers}" + (" (updated)" if changed else " (already registered)"))
    else:
        ui.warn("GitHub Copilot CLI not found; voice prompts need it (https://github.com/github/copilot-cli)")
    others = [k for k in ("claude", "code") if found[k]]
    ui.info("Other MCP clients: `relaymcp mcp --print` shows what to add" +
            (f" (found: {', '.join(others)})" if others else ""))

    reachable = bool(d.get("host")) and sshconf.reachable(sshconf.resolved_host(cfg))
    if reachable and sshconf.run(cfg, "hostname", timeout=20).returncode == 0:
        ui.step(f"'{name}' is already set up and reachable")
        if args.yes or ui.confirm("Update it now over SSH (re-runs its setup unattended)?", True):
            deploy_full(cfg, kit_dir)
    elif not args.no_wait:
        wait_for_checkin(cfg, kit_dir, host_ip)
        services.restart()
        time.sleep(8)
    print()
    ui.step("Checking everything")
    healthy = doctor.print_checks(doctor.run_checks(config.load()))
    print()
    if healthy:
        ui.step("Done! Try it:")
        ui.info('copilot -p "Take a screenshot of my handheld and tell me what\'s on screen"')
        ui.info('relaymcp say "Hello from RelayMCP"')
        ui.info("On the handheld: hold View + Menu (or tap 'Ask Copilot') and speak.")
    else:
        ui.info("Some checks failed; the arrows show what to do. `relaymcp doctor` re-checks.")


def cmd_enroll(args: argparse.Namespace) -> None:
    cfg = _need_config()
    host_ip, host_name = netinfo.lan_ip(), netinfo.hostname()
    kit_dir = kit.build(cfg, host_ip=host_ip, host_name=host_name)
    if wait_for_checkin(cfg, kit_dir, host_ip):
        services.restart()
        time.sleep(8)
        doctor.print_checks(doctor.run_checks(config.load()))


def cmd_kit(args: argparse.Namespace) -> None:
    cfg = _need_config()
    out = kit.build(cfg, Path(args.out) if args.out else None, netinfo.lan_ip(), netinfo.hostname())
    ui.ok(f"kit: {out}")
    if args.usb:
        target = Path(args.usb) / "RelayMCP"
        shutil.copytree(out, target, dirs_exist_ok=True)
        ui.ok(f"copied to {target}")
    ui.info("On the handheld: double-tap 'Setup RelayMCP.cmd' in that folder.")


def cmd_trust(args: argparse.Namespace) -> None:
    cfg = _need_config()
    host = args.host or cfg["device"].get("host")
    if not host:
        ui.fail("give the handheld's IP address: relaymcp trust 192.168.1.23")
        sys.exit(2)
    key = sshconf.scan_host_key(host)
    if not key:
        ui.fail(f"no SSH host key from {host} (is the handheld awake, at home and set up?)")
        sys.exit(1)
    fp = sshconf.fingerprint(key)
    if not (args.yes or ui.confirm(f"Trust {host}'s SSH host key {fp}?", True)):
        return
    cfg["device"]["host"] = host
    config.save(cfg)
    sshconf.write_ssh_config(cfg)
    sshconf.trust_host_key(cfg, key)
    services.restart()
    ui.ok(f"trusted {fp}; tunnels restarting")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = _need_config()
    ok = doctor.print_checks(doctor.run_checks(cfg, deep=args.command == "doctor"))
    sys.exit(0 if ok else 1)


def cmd_say(args: argparse.Namespace) -> None:
    cfg = _need_config()
    params: dict = {"text": " ".join(args.text)}
    if args.voice:
        params["voice"] = args.voice
    print(json.dumps(mcp_call(cfg, "speak", params)))


def cmd_awake(args: argparse.Namespace) -> None:
    cfg = _need_config()
    _need_device(cfg)
    value = args.minutes or "120"
    if value in ("off", "0"):
        script = "Remove-Item (Join-Path $env:LOCALAPPDATA 'RelayMCP\\awake-until.txt') -ErrorAction SilentlyContinue; 'keep-awake lease cancelled'"
    elif value.isdigit():
        script = (f"$d = Join-Path $env:LOCALAPPDATA 'RelayMCP'; New-Item -ItemType Directory -Force -Path $d | Out-Null; "
                  f"$u = (Get-Date).AddMinutes({int(value)}); Set-Content -Path (Join-Path $d 'awake-until.txt') "
                  f"-Value $u.ToString('s') -Encoding ascii; 'keeping the handheld awake until ' + $u.ToString('ddd HH:mm')")
    else:
        ui.fail("usage: relaymcp awake [minutes|off]")
        sys.exit(2)
    out = sshconf.powershell(cfg, script, timeout=30)
    print(out.stdout.strip() or out.stderr.strip())


def cmd_voice(args: argparse.Namespace) -> None:
    cfg = _need_config()
    v, changed, restart = cfg["voice"], False, False
    for key, value in (("voice", args.voice), ("speech_speed", args.speed), ("permissions", args.permissions),
                       ("agent", args.agent), ("model", args.model), ("user_name", args.user_name)):
        if value is not None:
            v[key] = value
            changed = True
    if args.extra_servers is not None:
        v["extra_servers"] = [s.strip() for s in args.extra_servers.split(",") if s.strip()]
        changed = True
        unknown = [s for s in v["extra_servers"] if s not in config.copilot_mcp_servers()]
        if unknown:
            ui.warn(f"not in Copilot's MCP config (~/.copilot/mcp-config.json) yet: {', '.join(unknown)}")
    if args.enable or args.disable:
        v["enabled"] = bool(args.enable)
        changed = restart = True
    if changed:
        config.save(cfg)
        if restart:
            services.restart()
        ui.ok("saved (takes effect with the next voice prompt)")
    if args.test:
        ui.info("asking through the handheld's voice pipeline (the reply is spoken there)...")
        print(json.dumps(mcp_call(cfg, "voice_assistant", {"action": "ask", "text": args.test})))
        for _ in range(240):
            time.sleep(1)
            st = mcp_call(cfg, "voice_assistant", {"action": "status"})
            if st.get("state") == "idle":
                print(json.dumps(st.get("last"), indent=1))
                break
        return
    print(json.dumps({k: v.get(k) for k in ("enabled", "agent", "permissions", "extra_servers", "voice", "speech_speed", "model",
                                           "user_name", "timeout_minutes", "new_conversation_after_minutes")},
                     indent=2))


def cmd_mcp(args: argparse.Namespace) -> None:
    cfg = _need_config()
    if args.action == "add":
        changed = agents.register_copilot(cfg)
        ui.ok("registered with GitHub Copilot CLI" + (f": {', '.join(changed)}" if changed else " (no changes)"))
    elif args.action == "remove":
        removed = agents.unregister_copilot(cfg)
        ui.ok(f"removed from GitHub Copilot CLI: {', '.join(removed) or 'nothing to remove'}")
    else:
        for name, url, desc in config.mcp_servers(cfg):
            ui.info(f"{ui.bold(name):<24} {url}  {ui.dim(desc)}")
        print()
        for client, snippet in agents.snippets(cfg).items():
            ui.step(client)
            print(snippet)
            print()


def cmd_service(args: argparse.Namespace) -> None:
    if args.action == "install":
        ui.ok(services.install())
    elif args.action == "uninstall":
        ui.ok("removed" if services.uninstall() else "not installed")
    elif args.action == "restart":
        services.restart()
        ui.ok("restarted")
    else:
        installed, running, desc = services.status()
        st = daemon.read_status() or {}
        print(json.dumps({"service": desc, "installed": installed, "running": running,
                          "tunnels": st.get("tunnels"), "voice": st.get("voice")}, indent=2))


def cmd_logs(args: argparse.Namespace) -> None:
    if args.device:
        cfg = _need_config()
        _need_device(cfg)
        names = ", ".join(f"'{n}'" for n in DEVICE_LOGS)
        script = (f"foreach ($n in @({names})) {{ $p = Join-Path (Join-Path $env:LOCALAPPDATA 'RelayMCP') $n; "
                  f"if (Test-Path $p) {{ \"==> $n\"; Get-Content $p -Tail {args.lines} }} }}; "
                  f"$c = Join-Path $env:ProgramData 'RelayMCP\\controller.log'; "
                  f"if (Test-Path $c) {{ '==> controller.log'; Get-Content $c -Tail {args.lines} }}")
        print(sshconf.powershell(cfg, script, timeout=60).stdout.rstrip())
        return
    path = config.LOG_DIR / "daemon.log"
    if not path.exists():
        ui.warn(f"no log yet ({path})")
        return
    if args.follow and shutil.which("tail"):
        os.execvp("tail", ["tail", "-n", str(args.lines), "-f", str(path)])
    print("".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-args.lines:]).rstrip())


DEPLOY_SCRIPT = r"""
$kit = Join-Path $env:ProgramData 'RelayMCP'
$zip = Join-Path $env:USERPROFILE 'relaymcp-deploy\relaymcp-device.zip'
Stop-ScheduledTask -TaskName 'RelayMCP-Agent' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object { ([string]$_.CommandLine) -match 'relaymcp\.device\.(agent|server)|windows_mcp serve' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Copy-Item $zip (Join-Path $kit 'relaymcp-device.zip') -Force
Remove-Item -Recurse -Force (Join-Path $kit 'device') -ErrorAction SilentlyContinue
Expand-Archive -Path $zip -DestinationPath (Join-Path $kit 'device') -Force
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
try {
    $out = cmd /c "uv tool install --force --reinstall-package relaymcp-device --python 3.12 `"$kit\device`" 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "uv install failed: $(($out | Select-Object -Last 3) -join ' ')" }
    Set-Content -Path (Join-Path $kit 'relaymcp-device.sha256') -Value (Get-FileHash $zip -Algorithm SHA256).Hash -Encoding ascii
} finally {
    Start-ScheduledTask -TaskName 'RelayMCP-Agent'  # whatever happened, bring the handheld's servers back
}
$port = (Get-Content (Join-Path $kit 'device.json') -Raw | ConvertFrom-Json).ports.hardware
for ($i = 0; $i -lt 60 -and -not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { 'deployed; hardware server listening' } else { 'deployed, but the hardware server is not listening yet:'; Get-Content (Join-Path $env:LOCALAPPDATA 'RelayMCP\hardware.out.log') -Tail 15 -ErrorAction SilentlyContinue }
"""

RUN_SETUP_SCRIPT = r"""
$f = Join-Path $env:USERPROFILE 'relaymcp-deploy\Relay-Setup.ps1'
$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$errs)
if ($errs.Count) { $errs | ForEach-Object { 'PARSE ERROR line {0}: {1}' -f $_.Extent.StartLineNumber, $_.Message }; exit 1 }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $f -Unattended 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '\S' }
"""


def deploy_full(cfg: dict, kit_dir: Path) -> None:
    ui.info("copying the kit and running its setup on the handheld (1-5 minutes)...")
    sshconf.powershell(cfg, "New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE 'relaymcp-deploy') | Out-Null")
    sshconf.copy_to(cfg, [kit_dir / f for f in kit.KIT_FILES], "relaymcp-deploy")
    out = sshconf.powershell(cfg, RUN_SETUP_SCRIPT, timeout=1800)
    for line in out.stdout.splitlines():
        ui.info(line.rstrip())
    if "Setup failed" in out.stdout or "PARSE ERROR" in out.stdout or "ERROR:" in out.stdout:
        raise RuntimeError("setup on the handheld failed (see above)")


def cmd_deploy(args: argparse.Namespace) -> None:
    cfg = _need_config()
    _need_device(cfg)
    if not args.force:
        from . import busy
        why = busy.reasons(busy.probe(cfg))
        if why:
            ui.fail("the handheld is in use (" + "; ".join(why) + "); deploying restarts its servers. Try again later, "
                    "or pass --force.")
            sys.exit(1)
    if args.full:
        kit_dir = kit.build(cfg, host_ip=netinfo.lan_ip(), host_name=netinfo.hostname())
        deploy_full(cfg, kit_dir)
        return
    tmp = config.STATE_DIR / "relaymcp-device.zip"
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(kit.build_device_zip())
    sshconf.powershell(cfg, "New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE 'relaymcp-deploy') | Out-Null")
    sshconf.copy_to(cfg, [tmp], "relaymcp-deploy")
    out = sshconf.powershell(cfg, DEPLOY_SCRIPT, timeout=900)
    print(out.stdout.strip())
    if "ERROR:" in out.stdout:
        sys.exit(1)


INLINE_SCRIPT_LIMIT = 6000  # bytes; bigger scripts are copied over and run with -File (command lines max out at 32k)


def script_command(text: str, args: list[str]) -> str:
    """PowerShell that runs `text` as a script block with `args`. The script travels base64-encoded, so no shell or
    PowerShell quoting can mangle it."""
    b64 = base64.b64encode(text.encode("utf-8")).decode()
    return (f"$__relay_src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))\n"
            f"$__relay_args = @({kit.ps_list(args)})\n"
            "& ([scriptblock]::Create($__relay_src)) @__relay_args")


def run_script(cfg: dict, text: str, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    if len(text.encode("utf-8")) <= INLINE_SCRIPT_LIMIT:
        return sshconf.powershell(cfg, script_command(text, args), timeout=timeout)
    name = f"relaymcp-exec-{secrets.token_hex(4)}.ps1"
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    local = config.STATE_DIR / name
    local.write_text(text, encoding="utf-8-sig")  # BOM: Windows PowerShell 5.1 reads BOM-less files as ANSI
    try:
        sshconf.powershell(cfg, "New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE 'relaymcp-deploy') | Out-Null")
        sshconf.copy_to(cfg, [local], "relaymcp-deploy")
    finally:
        local.unlink(missing_ok=True)
    remote = (f"$__f = Join-Path $env:USERPROFILE 'relaymcp-deploy\\{name}'; $__a = @({kit.ps_list(args)}); "
              "try { & $__f @__a } finally { Remove-Item $__f -ErrorAction SilentlyContinue }")
    return sshconf.powershell(cfg, remote, timeout=timeout)


def cmd_exec(args: argparse.Namespace) -> None:
    cfg = _need_config()
    parts = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.file:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
        out = run_script(cfg, text, parts, args.timeout)
    elif parts:
        out = sshconf.powershell(cfg, " ".join(parts), timeout=args.timeout)
    else:
        ui.fail("usage: relaymcp exec -- <PowerShell command>   or   relaymcp exec --file script.ps1 [-- args]")
        sys.exit(2)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr)
    sys.exit(out.returncode)


def cmd_agent(args: argparse.Namespace) -> None:
    cfg = _need_config()
    if args.model:
        cfg.setdefault("agent", {})["model"] = args.model
        config.save(cfg)
    if args.action == "install":
        changed = agents.install_agent(cfg)
        ui.ok(f"{'installed' if changed else 'already up to date'}: {agents.agent_path()} (model {agents.agent_model(cfg)})")
        ui.ok(f"and the skill {agents.skill_path()}")
        ui.info("Copilot sessions started from now on can hand handheld work to the agent (or pick it with /agent "
                "handheld), and load the skill's playbook when a task involves the handheld.")
    elif args.action == "remove":
        ui.ok("removed" if agents.remove_agent() else "not installed")
    else:
        print(agents.handheld_agent(cfg))


def cmd_busy(args: argparse.Namespace) -> None:
    from . import busy
    cfg = _need_config()
    _need_device(cfg)
    value = (args.minutes or "").strip().lower()
    if value in ("off", "0"):
        print(busy.clear(cfg))
        return
    if value:
        if not value.isdigit():
            ui.fail("usage: relaymcp busy [minutes|off] [--note text]   or   relaymcp busy --check")
            sys.exit(2)
        print(busy.mark(cfg, int(value), args.note or ""))
        return
    why = busy.reasons(busy.probe(cfg), args.quiet_seconds, args.ignore_lease)
    if args.check:
        print("busy: " + "; ".join(why) if why else "quiet")
        sys.exit(1 if why else 0)
    if why:
        ui.warn("the handheld is in use: " + "; ".join(why))
    else:
        ui.ok("the handheld is quiet: no recent tool calls, busy mark, keep-awake lease or SSH commands")


def cmd_bench(args: argparse.Namespace) -> None:
    from . import bench
    cfg = _need_config()
    if args.input:
        devguard.check("send input to the handheld")
    bench.main(cfg, args.rounds, args.input, not args.no_screen, args.json)


def cmd_ssh(args: argparse.Namespace) -> None:
    cfg = _need_config()
    argv = [sshconf.ssh_exe(), cfg["device"]["name"], *args.rest]
    if os.name == "nt":
        sys.exit(subprocess.call(argv))
    os.execvp(argv[0], argv)


def cmd_uninstall(args: argparse.Namespace) -> None:
    cfg = config.load()
    if args.device and config.exists():
        if sshconf.reachable(sshconf.resolved_host(cfg)):
            ui.step(f"Removing RelayMCP from '{cfg['device']['name']}'")
            out = sshconf.powershell(cfg, "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                                          "(Join-Path $env:ProgramData 'RelayMCP\\Relay-Setup.ps1') -Uninstall -Unattended",
                                     timeout=600)
            for line in out.stdout.splitlines():
                ui.info(line.rstrip())
        else:
            ui.warn("the handheld isn't reachable; on it, run: powershell -ExecutionPolicy Bypass -File "
                    "C:\\ProgramData\\RelayMCP\\Relay-Setup.ps1 -Uninstall")
    ui.step("Removing RelayMCP from this computer")
    if services.uninstall():
        ui.ok("background service removed")
    if config.exists():
        removed = agents.unregister_copilot(cfg)
        if removed:
            ui.ok(f"removed from GitHub Copilot CLI: {', '.join(removed)}")
    if sshconf.remove_include():
        ui.ok("removed the Include line from ~/.ssh/config")
    if config.HOME.exists() and (args.yes or ui.confirm(f"Delete {config.HOME} (settings, key, kit, logs)?", False)):
        shutil.rmtree(config.HOME)
        ui.ok(f"deleted {config.HOME}")
    ui.info("To remove the command itself: uv tool uninstall relaymcp")


def cmd_daemon(args: argparse.Namespace) -> None:
    daemon.run()


# ---------------------------------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relaymcp", description="RelayMCP: remote agentic control for handheld PCs.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Docs: https://github.com/brennengreen/RelayMCP")
    p.add_argument("--version", action="version", version=f"relaymcp {relaymcp.__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("setup", help="set up this computer and build the handheld's setup kit")
    s.add_argument("--name", help="device name (default: ally)")
    s.add_argument("--host-label", help='how the handheld refers to this computer (default: "your Mac"/"your PC")')
    s.add_argument("--usb", metavar="PATH", help="also copy the kit to this drive")
    s.add_argument("--no-wait", action="store_true", help="don't wait for the handheld to check in")
    s.add_argument("-y", "--yes", action="store_true", help="accept the defaults without asking")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("enroll", help="serve the kit on your network and wait for the handheld to check in")
    s.set_defaults(func=cmd_enroll)

    s = sub.add_parser("kit", help="(re)build the handheld's setup kit")
    s.add_argument("--out", help="folder to write it to (default ~/.relaymcp/kit/<device>)")
    s.add_argument("--usb", metavar="PATH", help="also copy it to this drive")
    s.set_defaults(func=cmd_kit)

    s = sub.add_parser("trust", help="trust the handheld's current SSH host key (e.g. after a reset)")
    s.add_argument("host", nargs="?", help="its IP address (default: the last known one)")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_trust)

    for name, text in (("status", "quick health check"), ("doctor", "full health check, including the handheld")):
        s = sub.add_parser(name, help=text)
        s.set_defaults(func=cmd_status)

    s = sub.add_parser("say", help="speak text on the handheld")
    s.add_argument("text", nargs="+")
    s.add_argument("--voice", help="e.g. af_heart, am_michael, bf_emma, or a Windows voice name")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("awake", help="keep the handheld awake for N minutes (default 120), or 'off'")
    s.add_argument("minutes", nargs="?")
    s.set_defaults(func=cmd_awake)

    s = sub.add_parser("voice", help="show or change voice-prompt settings")
    s.add_argument("--voice", help="spoken voice (af_heart, am_michael, bf_emma, ...)")
    s.add_argument("--speed", type=float, help="speech speed 0.5-2.0")
    s.add_argument("--permissions", choices=["handheld", "full"])
    s.add_argument("--extra-servers", metavar="NAMES",
                   help="comma-separated Copilot MCP servers voice may also use in handheld mode ('' clears)")
    s.add_argument("--agent", choices=["copilot", "custom"])
    s.add_argument("--model", help="model for voice prompts (agent default if unset)")
    s.add_argument("--user-name", help="your name, as the agent should hear it")
    s.add_argument("--enable", action="store_true")
    s.add_argument("--disable", action="store_true")
    s.add_argument("--test", metavar="TEXT", help="send TEXT through the handheld's voice pipeline")
    s.set_defaults(func=cmd_voice)

    s = sub.add_parser("mcp", help="MCP client configuration (add/remove for Copilot CLI; --print for others)")
    s.add_argument("action", nargs="?", choices=["add", "remove", "show"], default="show")
    s.add_argument("--print", dest="action", action="store_const", const="show")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("service", help="manage the background service")
    s.add_argument("action", nargs="?", choices=["install", "uninstall", "restart", "status"], default="status")
    s.set_defaults(func=cmd_service)

    s = sub.add_parser("logs", help="show logs (this computer, or --device)")
    s.add_argument("--device", action="store_true", help="the handheld's logs")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("deploy", help="(developers) push this checkout's device code to the handheld")
    s.add_argument("--full", action="store_true", help="re-run the whole setup on the handheld instead")
    s.add_argument("--force", action="store_true", help="deploy even while the handheld is in use")
    s.set_defaults(func=cmd_deploy)

    s = sub.add_parser("busy", help="mark the handheld busy (rollouts and deploys wait), or check whether it's in use")
    s.add_argument("minutes", nargs="?", help="mark busy for this many minutes, or 'off'")
    s.add_argument("--note", help="who or what is using it (shown to whoever checks)")
    s.add_argument("--check", action="store_true", help="exit 0 if quiet, 1 if busy (for scripts)")
    s.add_argument("--quiet-seconds", type=int, default=60, help="how recent a tool call counts as use (default 60)")
    s.add_argument("--ignore-lease", action="store_true", help="don't count a keep-awake lease as use")
    s.set_defaults(func=cmd_busy)

    s = sub.add_parser("exec", help="run PowerShell on the handheld: a command, or a script with --file")
    s.add_argument("-f", "--file", help="a .ps1 script to run ('-' reads it from stdin); words after -- are its arguments")
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("agent", help="a fast Copilot CLI custom agent (and skill) for hands-on handheld work")
    s.add_argument("action", nargs="?", choices=["install", "remove", "print"], default="print")
    s.add_argument("--model", help=f"model it runs on (default {agents.DEFAULT_AGENT_MODEL})")
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser("bench", help="measure tool latency and context cost through the tunnel")
    s.add_argument("--rounds", type=int, default=15, help="pings per server")
    s.add_argument("--input", action="store_true", help="also time the virtual gamepad plugging in (sends input)")
    s.add_argument("--no-screen", action="store_true", help="skip the screenshot timing")
    s.add_argument("--json", action="store_true", help="print the full result as JSON")
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("ssh", help="open an SSH session to the handheld")
    s.add_argument("rest", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_ssh)

    s = sub.add_parser("uninstall", help="remove RelayMCP from this computer (and --device from the handheld)")
    s.add_argument("--device", action="store_true", help="also remove it from the handheld (over SSH)")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("daemon")  # the background service entry point (no help: hidden from the list)
    s.set_defaults(func=cmd_daemon)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        ui.fail(str(e))
        sys.exit(1)
