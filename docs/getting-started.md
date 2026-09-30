# Getting started

This walks through a complete setup: your computer first, then the handheld. Plan on about 10 minutes, most of it
waiting for downloads on the handheld.

## What you need

| | Requirements |
|---|---|
| **Handheld** | Windows 11 (Home is fine), an administrator account, on your home Wi-Fi. Tested on an original ROG Ally; other Windows handhelds (Legion Go, MSI Claw, ROG Ally X, ...) should work. |
| **Your computer** | macOS, Linux or Windows on the same network, with OpenSSH (built into all three) and [uv](https://docs.astral.sh/uv/getting-started/installation/). |
| **An AI agent** | Any MCP client. Voice prompts run [GitHub Copilot CLI](https://github.com/github/copilot-cli) by default (`copilot` on your PATH). |

## 1. Install RelayMCP on your computer

```sh
uv tool install git+https://github.com/brennengreen/RelayMCP
relaymcp --version
```

This installs one command, `relaymcp`, with no dependencies beyond Python's standard library. `pipx install git+...`
works too. Don't run it through `uvx`: the background service needs a permanent install.

## 2. Run setup

```sh
relaymcp setup
```

Setup asks for:

1. **A name for your handheld** (default `ally`). It becomes the SSH alias (`ssh ally`) and the MCP server names
   (`ally` for screen control, `ally-handheld` for hardware).
2. **Whether this computer is on your home network right now.** If yes, your router's MAC address becomes "home".
   The handheld only turns remote access on when it's behind that router.

Then it:

- creates a dedicated SSH key in `~/.relaymcp/id_ed25519` and an `ssh_config` there (your `~/.ssh/config` gains one
  `Include` line);
- builds the handheld's setup kit in `~/.relaymcp/kit/<name>/`;
- installs the background service (tunnels + voice dispatcher);
- registers the MCP servers with GitHub Copilot CLI if it's installed;
- serves the kit on your network and waits for the handheld to check in.

Useful flags: `--name`, `--usb /Volumes/MYSTICK` (also copies the kit to a drive), `--no-wait`, `--yes`.

> **macOS firewall:** if it's on, macOS may ask whether Python may accept incoming connections while setup is
> waiting. Allow it for the one-liner route; the USB route doesn't need it.

## 3. Run the kit on the handheld

Sign in on the handheld and connect it to your home Wi-Fi. Then use either route.

**USB drive or microSD card:** copy the kit folder over, open it in File Explorer, and double-tap
**`Setup RelayMCP.cmd`**.

**One-liner (no drive):** press <kbd>Win</kbd>+<kbd>R</kbd> (or open PowerShell) and enter the command setup printed:

```powershell
powershell -c "irm http://192.168.1.10:8766/Ab3xYz | iex"
```

The one-liner fetches the kit over plain HTTP, so use it only on your own home network
([why](security.md#trust-decisions)).

Choose **Yes** when Windows asks for administrator rights. The first run takes 2–5 minutes. It installs:

- OpenSSH Server (key-only, reachable only from your local network; the installer's SHA-256 is verified)
- [uv](https://docs.astral.sh/uv/) and a private Python 3.12
- [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) (pinned version) and the RelayMCP device runtime
- the Whisper speech models and the Kokoro voice (~200 MB, hash-checked)
- the ViGEmBus virtual-controller driver (signature-checked; x64 handhelds)
- two scheduled tasks (`RelayMCP-Controller`, `RelayMCP-Agent`) plus the *Repair RelayMCP* and *Ask Copilot*
  shortcuts

When it finishes, the handheld shows a summary and checks in with your computer. Setup there trusts the handheld's
SSH host key (verified with the kit's token), starts the tunnels and runs `relaymcp doctor`.

## 4. Try it

```sh
copilot -p "Take a screenshot of my handheld and describe what's on screen"
copilot -p "On my handheld, open the Xbox app and tell me what's installed"
relaymcp say "Hello from RelayMCP"
relaymcp voice --test "What's my battery level?"   # the whole voice pipeline, without speaking
```

On the handheld, hold **View + Menu** for a second (or tap **Ask Copilot**) and ask something out loud.

If setup works on your device, add the handheld model, host OS and MCP client to the
[early tester roll call](https://github.com/brennengreen/RelayMCP/discussions/5). Working reports help establish
compatibility just as much as bug reports.

## Day to day

- **Nothing to start or stop.** The handheld enables remote access by itself at home and disables it everywhere
  else. The tunnel on your computer reconnects on its own.
- **Long unattended work:** `relaymcp awake 180` keeps the handheld awake for 3 hours (at home only).
  `relaymcp awake off` cancels.
- **Health:** `relaymcp status` for a quick check, `relaymcp doctor` to include the handheld.
- **Something broke on the handheld** (a Windows update, a reset): tap **Repair RelayMCP** on its Desktop. It
  re-runs setup and fixes what's missing.

## Updating

```sh
uv tool upgrade relaymcp
relaymcp setup        # rebuilds the kit and, if the handheld is reachable, updates it over SSH
```

## New router, reset handheld, new computer

- **New router:** tap *Repair RelayMCP* on the handheld at home and answer **Yes** when it asks to trust the network,
  or re-run `relaymcp setup` from your computer while on the new network.
- **Reset or reinstalled handheld:** run the kit again (USB or one-liner), then `relaymcp enroll` on your computer
  (it waits for the check-in and trusts the new host key).
- **New computer:** install RelayMCP there and run `relaymcp setup`, then run the new kit on the handheld.

## Uninstalling

```sh
relaymcp uninstall --device   # removes it from the handheld over SSH, then from this computer
uv tool uninstall relaymcp
```

On the handheld without your computer:

```powershell
powershell -ExecutionPolicy Bypass -File C:\ProgramData\RelayMCP\Relay-Setup.ps1 -Uninstall
```

This removes the tasks, shortcuts, runtime, Windows-MCP, logs and your computer's SSH key, and stops and disables SSH.
OpenSSH Server and the ViGEmBus driver stay installed; remove them in **Settings → Apps** if you like.
