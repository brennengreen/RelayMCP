# Changelog

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
