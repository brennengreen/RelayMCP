# Security model

RelayMCP gives an AI agent **full control of your handheld**. It can do anything you could do while signed in there:
open apps, read files, run PowerShell, change settings. The design goal is that *only your computer's agent* gets that
power, *only at home*, and that the handheld behaves like a normal handheld everywhere else.

## What's exposed, to whom, and when

| Surface | Exposure |
|---|---|
| **SSH (port 22) on the handheld** | Runs **only on the home network**. Accepts **only** the RelayMCP key from your computer: password and keyboard-interactive auth are disabled. The Windows Firewall rule is limited to **Private** networks and the **local subnet**, and the controller re-applies that scope if anything widens it. |
| **MCP servers on the handheld** (8765, 8767) | Bound to `127.0.0.1`. Reachable only through an SSH tunnel, i.e. only by someone holding your key. Stopped when away. |
| **MCP servers on your computer** (8765, 8767) | The tunnel's local ends, bound to `127.0.0.1`. Any local program (and any local user) on your computer can use them, the same as any local MCP server. |
| **Voice dispatcher on your computer** (8768) | Bound to `127.0.0.1`, reached by the handheld through the reverse tunnel. It accepts only requests with a JSON body, a custom header and a loopback `Host`. A web page can't send those without a CORS preflight, which it never approves, and DNS-rebinding pages fail the `Host` check. |
| **Voice trigger on the handheld** (`/voice/trigger`) | Same header and `Host` checks, loopback only. |
| **Enrollment server on your computer** (8766) | Only while `relaymcp setup`/`enroll` is waiting. Every URL needs the kit's random token. It serves the kit (your *public* key, home router, ports) and accepts one check-in. |

## Trust decisions

- **Host key pinning.** Your computer only talks to the handheld whose SSH host key it pinned
  (`StrictHostKeyChecking yes`, stored under a per-device alias in `~/.relaymcp/known_hosts`). The key is learned
  from the handheld's check-in, which must carry the kit's token, or from `relaymcp trust`, which shows the
  fingerprint and asks.
- **Downloads are verified.** The OpenSSH installer and the Kokoro voice model are checked against pinned SHA-256
  hashes. The ViGEmBus driver installer must carry a valid Authenticode signature from its publisher. Windows-MCP is
  pinned to a tested version.
- **What "home" means.** Your router's MAC address, freshly confirmed with ARP, not a Wi-Fi name. Anyone can name a
  network after yours; spoofing your router's MAC on a network you join is far less likely, and even then SSH still
  requires your key.
- **Setup trusts your home network.** The one-liner downloads the kit over plain HTTP and runs it as administrator,
  and the handheld's check-in is plain HTTP too. The kit's random token keeps other devices from fetching the kit or
  faking a check-in, but it can't stop someone who can intercept traffic on the network. Run setup on your own home
  network. The USB route doesn't download the kit over the network.

## Voice prompts and permissions

A voice prompt runs your agent on your computer with whatever permissions you choose (`relaymcp voice --permissions`):

- **`handheld` (default):** only the handheld's two MCP servers are allowed, and your other MCP servers are disabled
  for that run. A voice request can't run commands or edit files on your computer.
- **`full`:** the agent runs with all permissions (`--allow-all` for Copilot CLI). Anyone holding your handheld at
  home could then do anything on your computer by voice. Only choose this if that's acceptable.

Speech is transcribed **on the handheld**, and the reply is spoken **on the handheld**. Only the transcribed text
travels to your computer, and from there to your agent's model provider (as with any prompt you type).

## Away from home

On any other network the controller stops SSH and the agent right away. Nothing listens, and the handheld never
contacts your computer. Offline, the handheld waits 10 minutes (sleep and Wi-Fi blips), then stops everything. No
settings of other networks are ever changed.

## Antivirus

RelayMCP launches its background processes the plain way (`pythonw.exe -m ...` from a scheduled task, children with
`CREATE_NO_WINDOW`). It doesn't use hidden-console launchers or script restart loops, which endpoint-protection
heuristics associate with malware. `relaymcp doctor` reports any Microsoft Defender detection involving RelayMCP in
the last 24 hours. **Never** add broad Defender exclusions for RelayMCP; if something is flagged, please open an
issue.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Report a vulnerability" on the repo's Security tab) rather
than a public issue. See [SECURITY.md](../SECURITY.md).
