# Changelog

## 0.1.1 (2026-09-26)

Input reliability, from real use driving Minecraft Bedrock on a ROG Ally.

- **Focus stealers are caught and undone:** input results name invisible windows that hold focus (like ASUS'
  AsHotplugCtrl) instead of reporting success. The hardware server watches the foreground and, when a pop-up or an
  invisible helper takes focus, gives it back to the last app window before sending input, even with no input
  target set. `focus_window()` with no target explains why input may not register: what has focus, recent focus
  changes, and seconds since the last input.
- **Input lands in the game:** when the virtual gamepad plugs in, Armoury Crate's notice took the foreground and input
  was silently dropped. RelayMCP now puts the game back in front after closing the notice.
- **`focus_window`** brings a window to the front past Windows' foreground lock and reports what *really* has focus.
  With `remember`, input tools refocus that window if something steals focus.
- **Every input result** (gamepad, touch, keys, mouse) carries a compact `foreground` report, plus a warning when input
  probably went nowhere.
- **`gamepad_connect`** plugs the pad in ahead of time and waits until Windows sees it. The pad no longer unplugs
  mid-game: the idle timeout is 30 min by default (`gamepad_idle_minutes` in device.json, 0 = never), and it's never
  applied while a fullscreen game or the input target is in front.
- **`relaymcp doctor`** checks that the handheld runs the same device build as this computer.
- **`relaymcp exec --file script.ps1`** (or `-` for stdin) runs a whole script with arguments, with no quoting
  trouble.
- **Faster voice prompts:** voice runs load only the handheld's tools (no built-in MCP servers or custom
  instructions) and default to a fast model (`gpt-5.4-mini`, low reasoning). A one-tool prompt went from ~13 s to
  ~8 s. Models that reject a setting are retried without it.
- **Warm voice runtime** (`pip install 'relaymcp[voice]'`, Python 3.11+): the background service keeps one Copilot
  runtime running (github-copilot-sdk), so voice prompts skip `copilot -p`'s ~4–5 s start-up and use a short
  voice-only system prompt. One-tool requests take ~3.5 s instead of ~8 s. Falls back to `copilot -p` if the runtime
  isn't available; a prompt the model already received is never run twice.
- **`handheld_status` ~2 s faster:** brightness is read and set through WMI over COM instead of starting PowerShell.
- **Leaner tools:** hardware tool definitions are 21% smaller (no schema titles or null defaults, shorter
  descriptions), and results are minified JSON without empty fields.
- **`relaymcp agent install`** adds a `handheld` custom agent to GitHub Copilot CLI: a fast model (Claude Haiku
  4.5 by default, `--model` to change it) with only RelayMCP's tools and a short playbook. Main sessions hand
  device work to it instead of driving the handheld step by step with a slow model.
- **No tool-search round trip:** Copilot hides MCP tool schemas behind a search tool, which cost every voice prompt
  an extra model call. Voice runs and the `handheld` agent keep the handheld's tool schemas in view; other Copilot
  sessions are unchanged.
- **`proc` tool: long-running consoles over MCP** (e.g. a Bedrock Dedicated Server): start one, send it a line and
  get its reply, read only new output, or wait until a line matches, all without screenshots. Updates wait while one
  runs.
- **`powershell` tool on the hardware server:** a persistent session (variables and functions carry over, ~50 ms per
  call instead of 1.5–2.5 s), DPI-aware so Win32 coordinates match screenshots, and errors as plain `ERROR:` lines
  instead of CLIXML.
- **Agents hear about updates:** MCP clients only load tools when a session starts, so after an update, results
  briefly carry a `relaymcp_update` note naming the new tools and saying to start a new session. `handheld_status`
  reports the tool count and a tool-set hash.
- **Updates wait for the handheld to be free:** `relaymcp deploy` and `scripts/rollout.sh` hold off while a tool call
  happened in the last minute, a keep-awake lease or `relaymcp busy` mark is active, or a command runs over SSH
  (`deploy --force` overrides). New `relaymcp busy [minutes|off]` marks the handheld in use.
- **SSH sessions no longer drop each other:** the shared SSH connection is owned by the background service and runs
  detached from it; commands reuse it but never become it (a connection born in a short-lived shell used to take
  every session riding on it down when that shell ended). `relaymcp trust` only stops it taking new sessions.
- **`relaymcp bench`** measures latency (ping, status, screenshot, SSH) and tokens per result, and compares runs.
  `scripts/analyze_session.py` splits a Copilot session's time into model and device time.
- **Dev mode** (`scripts/dev-env.sh`): a development checkout can't touch the real installation.

## 0.1.0 (2026-09-26)

First release.

- **`relaymcp` CLI** for macOS, Linux and Windows (standard library only): `setup`, `status`/`doctor`, `say`, `awake`,
  `voice`, `mcp`, `logs`, `enroll`, `kit`, `trust`, `exec`, `ssh`, `deploy`, `uninstall`.
- **Background service** (launchd / systemd user service / logon task): self-healing SSH tunnels and the voice
  dispatcher.
- **Zero-typing handheld setup kit**: USB or one-liner install, token-authenticated check-in, host-key pinning.
- **Handheld runtime**: a windowless agent that supervises Windows-MCP and the hardware server (job objects, crash
  restarts, keep-awake), with no console-hiding launchers for antivirus heuristics to flag.
- **Hardware MCP server** with 35 tools: virtual Xbox controller, XInput reads and rumble, multi-touch, scan-code
  keys, mouse-look, audio and loopback capture, speech-to-text, text-to-speech, brightness, display mode, power mode,
  system load, keep-awake and voice prompts.
- **Voice prompts**: View + Menu / *Ask Copilot* / hotkey; local Whisper `small.en` transcription; Kokoro neural voice
  streamed by sentence with instant stop; Windows SAPI fallback.
- **Home-only operation**: on at home (router MAC), off immediately on other networks, off after 10 minutes offline.
