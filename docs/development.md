# Development

## Layout

```text
src/relaymcp/
  host/                 the controlling computer: standard library only, Python 3.9+
    cli.py              `relaymcp` commands
    config.py           ~/.relaymcp paths and config.json
    sshconf.py          key, ssh_config, known_hosts, running commands on the handheld
    netinfo.py          router MAC / LAN IP / Wi-Fi detection (macOS, Linux, Windows)
    kit.py              builds the handheld's setup kit (template + device zip + OpenSSH MSI)
    enroll.py           token-guarded LAN kit server and check-in
    daemon.py           the background service: tunnels + voice dispatcher
    tunnel.py           SSH tunnel supervisor
    voice.py            voice dispatcher (runs the agent for push-to-talk prompts)
    services.py         launchd / systemd / scheduled-task installers
    agents.py           MCP client registration (Copilot CLI) and config snippets
    doctor.py           health checks
  device/               the handheld: Windows, Python 3.12 (installed there as "relaymcp-device")
    agent.py            windowless supervisor: servers in job objects, restarts, keep-awake
    server.py           the hardware MCP server (FastMCP, streamable HTTP on 127.0.0.1)
    voice.py            push-to-talk triggers, recording/VAD, overlay, round trip to the dispatcher
    speech.py tts.py    Whisper speech-to-text; Kokoro/SAPI text-to-speech
    text.py             reply → speech text shaping (pure, tested anywhere)
    gamepad.py win_input.py audio.py system.py   hardware access
    paths.py            app folders + device.json
    setup/Relay-Setup.ps1   the handheld's setup/repair/uninstall script (a template the kit fills in)
tests/                  pytest suite (runs on macOS, Linux and Windows)
scripts/gen_tools_doc.py    regenerates docs/tools.md from a live device
```

One Python distribution (`relaymcp`) holds both halves. The handheld's dependencies are the `device` extra. You
don't install that yourself: `relaymcp kit` zips the package, generates a small `pyproject.toml` for
`relaymcp-device` from the extra, and the handheld installs it with uv. Because the zip is deterministic, re-running
setup skips the reinstall when nothing changed.

## Environment

```sh
git clone https://github.com/brennengreen/RelayMCP && cd RelayMCP
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # unit tests
.venv/bin/ruff check src tests
```

To use your checkout as the real `relaymcp` command (for the background service too):

```sh
uv tool install -e .        # ~/.local/bin/relaymcp now runs this checkout
relaymcp setup
```

## Working on the device side

With a handheld set up and reachable:

```sh
relaymcp deploy             # zip this checkout's code, reinstall it on the handheld, restart the agent (~30 s)
relaymcp deploy --full      # rebuild the kit and re-run the whole setup unattended (for Relay-Setup.ps1 changes)
relaymcp logs --device      # agent/server logs
relaymcp exec -- Get-Content $env:LOCALAPPDATA\RelayMCP\agent-status.json
```

SSH sessions of Windows administrators are elevated, so `deploy --full` can run the setup script unattended.
Changes to the hardware server's tools show up in your MCP client after it reconnects. Regenerate the tool reference
with `python scripts/gen_tools_doc.py`.

Things worth knowing:

- **COM:** `server.py` sets `sys.coinit_flags = 0` and imports `soundcard` before anything touches `comtypes`.
  Audio, volume and speech calls run on dedicated COM worker threads (`_run(COM|PLAY|STT, ...)`).
- **DPI:** the server is per-monitor-DPI aware, so touch and click coordinates are physical pixels (the same as
  Windows-MCP screenshots).
- **SSH sessions can't see the desktop.** To inspect windows, use the Windows-MCP `PowerShell` tool (which runs in
  the signed-in session) rather than `relaymcp exec`.
- **ViGEmBus before vgamepad:** vgamepad ships only as source, and its build opens the ViGEmBus driver's interactive
  installer if the driver isn't registered, which would hang an unattended setup. So the kit carries the driver's MSI
  (the exact one vgamepad bundles, from its pinned PyPI release) and `Relay-Setup.ps1` installs it silently, after a
  signature check, before building the runtime. CI does the same on a clean Windows runner.
- **Don't hide consoles with `conhost --headless` or restart loops in `.cmd` files.** Defender flags that pattern;
  see [how-it-works.md](how-it-works.md#the-agent-and-its-servers-process-lifecycle).

## Tests and CI

`tests/` covers the host side end to end (config, kit rendering, deterministic device zip, SSH config, netinfo
parsing, service files, voice dispatcher security checks, enrollment tokens) plus the pure device text shaping. CI
(`.github/workflows/ci.yml`) runs ruff and pytest on macOS, Linux and Windows (Python 3.9 and 3.12 across them). On Windows it
also builds a real kit, parses the rendered `Relay-Setup.ps1` with Windows PowerShell 5.1, installs the device
runtime and imports its driver-free modules.

## Releasing

1. Bump `__version__` in `src/relaymcp/__init__.py` and add a `CHANGELOG.md` entry.
2. Make sure CI is green, then tag: `git tag v0.x.y && git push --tags`.
3. Users update with `uv tool upgrade relaymcp && relaymcp setup`.
