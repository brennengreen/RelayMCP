# Troubleshooting

Start with:

```sh
relaymcp doctor
```

It checks every link from your computer to the handheld's servers, and prints a fix next to anything that fails.
`relaymcp logs` shows your computer's side; `relaymcp logs --device` shows the handheld's.

## The handheld isn't reachable

`Device reachable: … ✗` means your computer can't open port 22 on the handheld. Usually one of:

- **It's asleep or off.** Wake it. The tunnel reconnects within 30 s. For long unattended work, run
  `relaymcp awake 180` beforehand.
- **It's not on your home network.** Remote access is deliberately off away from home. On the handheld,
  `C:\ProgramData\RelayMCP\state.txt` says `home` or `away`, and `controller.log` says why.
- **Nobody is signed in** (after a reboot, before login). SSH works, but the MCP servers need a signed-in session.
- **Its address changed** (DHCP). Give it a reserved address in your router, or re-run the kit and `relaymcp enroll`.
  `relaymcp trust <new-ip>` also works if you know the address.

## "REMOTE HOST IDENTIFICATION HAS CHANGED" / SSH login fails after a reset

The handheld has a new SSH host key (Windows reset, OpenSSH reinstalled). Run the kit on it again, then:

```sh
relaymcp enroll      # waits for the check-in and trusts the new key
# or, if you're sure it's your handheld:
relaymcp trust
```

## New router

The handheld treats the new network as "away". Either tap **Repair RelayMCP** on the handheld while at home and
answer **Yes** to *trust this network as home*, or re-run `relaymcp setup` on your computer while it's on the new
network (it records the new router), then `relaymcp setup` again to update the handheld.

## MCP servers don't respond, but SSH works

- `relaymcp logs --device` shows `agent.log`, `hardware.out.log` and `windows-mcp.out.log`.
- The agent restarts crashed servers by itself. `Device agent: … restarts N` in `relaymcp doctor` shows how often.
- Tap **Repair RelayMCP** on the handheld, or run `relaymcp setup` (it updates the handheld over SSH).

## Microsoft Defender

RelayMCP starts its processes plainly (`pythonw.exe -m relaymcp.device.agent` from a scheduled task, with the servers
as child `python.exe` processes). Early prototypes launched servers through `conhost --headless cmd /c <script>` with
a restart loop; Defender's machine-learning heuristics (`Trojan:Win32/Commando.A!ml`) flag that pattern, and the
current design avoids it entirely.

`relaymcp doctor` reports any Defender detection involving RelayMCP in the last 24 hours. If you see one:

1. Check the details on the handheld: **Windows Security → Virus & threat protection → Protection history**.
2. Please open an issue with the detection name and the flagged command line.
3. Don't add blanket exclusions. If you must restore a quarantined file, allow that specific item from
   *Protection history*.

## Voice prompts

See [voice.md → Troubleshooting](voice.md#troubleshooting).

## The Armoury Crate "external controller" prompt

The first gamepad tool call plugs in a virtual Xbox controller, which makes Armoury Crate ask whether to disable the
built-in controller. RelayMCP dismisses that prompt automatically (via the `RelayMCP-DismissControllerNotice` task)
without choosing anything. The virtual controller unplugs after 5 idle minutes.

## Where are the logs?

| Side | Log |
|---|---|
| Your computer | `~/.relaymcp/logs/daemon.log` (tunnels, voice prompts), `daemon.out.log` (service stdout/stderr) |
| Handheld, setup | `C:\ProgramData\RelayMCP\setup.log` |
| Handheld, home/away switching | `C:\ProgramData\RelayMCP\controller.log` (changes only) |
| Handheld, agent and servers | `%LOCALAPPDATA%\RelayMCP\agent.log`, `hardware.log`, `hardware.out.log`, `windows-mcp.out.log`, `keepawake.log` |
