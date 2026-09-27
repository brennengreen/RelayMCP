# Configuration

Everything RelayMCP keeps on your computer lives in `~/.relaymcp/` (override with the `RELAYMCP_HOME` environment
variable). `relaymcp setup` writes `config.json`, and most settings have a command. Edit the file by hand for the rest,
then run `relaymcp setup` (for device/port changes) or `relaymcp service restart`.

## `~/.relaymcp/config.json`

```jsonc
{
  "version": 1,
  "host_label": "your Mac",            // how the handheld refers to this computer in messages
  "device": {
    "name": "ally",                    // ssh alias; MCP servers "<name>" and "<name>-handheld"
    "host": "192.168.1.23",              // learned at check-in (or `relaymcp trust <ip>`)
    "user": "Sam",                     // Windows account on the handheld (learned at check-in)
    "computer_name": "ROG-ALLY",
    "home_networks": ["MyWiFi"],       // cosmetic: shown in messages on the handheld
    "home_gateways": ["AA-BB-CC-11-22-33"],  // router MAC address(es) = "home"
    "ports": {"screen": 8765, "hardware": 8767, "voice": 8768}
  },
  "voice": {
    "enabled": true,
    "agent": "copilot",                // copilot | custom
    "custom_command": null,            // for agent=custom, e.g. ["claude", "-p", "{prompt}"]
    "permissions": "handheld",         // handheld | full
    "model": null,                     // model for voice prompts (agent default if null)
    "reasoning_effort": null,
    "timeout_minutes": 10,
    "new_conversation_after_minutes": 20,
    "notify": true,                    // desktop notification when a voice prompt arrives
    "workdir": "~/.relaymcp/voice-workspace",
    "voice": "af_heart",               // spoken voice (Kokoro id or Windows voice name)
    "speech_speed": 1.0,
    "user_name": "Sam"                 // how the agent addresses you
  },
  "enroll": {"port": 8766, "token": "…"},  // the kit's check-in token (random)
  "windows_mcp": "windows-mcp==0.8.6"      // pinned Windows-MCP version installed on the handheld
}
```

**Ports** are the same on both ends of the tunnel. If something else on your computer uses 8765/8767/8768, change
them here and re-run `relaymcp setup`: that rebuilds the kit and updates the handheld, the tunnel and your MCP client
registrations.

**Several home networks** (e.g. a mesh with two routers, or a second home): add each router's MAC address to
`home_gateways`, then re-run `relaymcp setup`.

## On the handheld: `C:\ProgramData\RelayMCP\device.json`

Written by setup; read by the device runtime.

```json
{
  "name": "ally",
  "host_label": "your Mac",
  "version": "0.1.0",
  "ports": {"screen": 8765, "hardware": 8767, "voice": 8768},
  "gamepad_idle_minutes": 30,
  "vocabulary": "Copilot, ROG Ally, Minecraft, Steam"
}
```

`gamepad_idle_minutes` (optional) is how long the virtual controller stays plugged in without use (default 30,
0 = until `gamepad_unplug`). It never unplugs while a fullscreen game or the `focus_window` target is in front.
`vocabulary` (optional, add it by hand) biases speech recognition toward names you use. Setup updates the managed keys
and keeps any you added.

## Environment variables

| Variable | Effect |
|---|---|
| `RELAYMCP_HOME` | Use a different folder instead of `~/.relaymcp` |
| `NO_COLOR` | Plain terminal output |
