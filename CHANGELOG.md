# Changelog

## 0.1.1 (2026-09-26)

Input reliability, from real use driving Minecraft Bedrock on a ROG Ally.

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
- **`handheld_status` ~2 s faster:** brightness is read and set through WMI over COM instead of starting PowerShell.
- **Leaner tools:** hardware tool definitions are 21% smaller (no schema titles or null defaults, shorter
  descriptions), and results are minified JSON without empty fields.
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
