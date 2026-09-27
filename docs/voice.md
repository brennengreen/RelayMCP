# Voice prompts

Talk to your AI agent from the handheld: hold two buttons, speak, and hear the answer.

## Triggers

| Trigger | Notes |
|---|---|
| **Hold View + Menu** (~1 s) on the built-in controller | Works in gamepad mode, in games and on the desktop. The virtual gamepad RelayMCP drives is ignored, so an agent can't trigger itself. |
| **Tap *Ask Copilot*** (Desktop / Start menu) | A windowless launcher; pin it to Start or the taskbar. |
| <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>F12</kbd> | Global hotkey. |
| **One button (ROG Ally):** map M1 or M2 to the hotkey | Armoury Crate → *Control Mode* → *Gamepad* → *Key mapping* → M2 → *Combine Keys* → Ctrl + Alt + Shift + F12. |

Armoury Crate's Command Center only offers built-in tiles, so it can't host a custom shortcut. The M1/M2 mapping is
the closest equivalent.

While listening, trigger again (or tap the overlay) to send right away. While it's speaking, trigger again to stop.
Start a request with **"new conversation"** or **"start over"** to begin a fresh agent session; otherwise requests
within 20 minutes continue the same one.

## What happens

1. A chime plays, and a small overlay (top center, never steals focus) shows *Listening* with a level meter.
2. Recording stops after ~1.1 s of silence. It gives up if nothing is said for 8 s, and stops after 30 s regardless.
   The voice detector adapts to background noise and ignores clicks and the chime.
3. **Speech-to-text runs on the handheld:** faster-whisper `small.en` (int8, 8 threads), about 1.2 s for a short
   request on a ROG Ally Z1 Extreme.
4. The text goes through the reverse tunnel to the **voice dispatcher** on your computer. It runs your agent with a
   short preamble: the request was spoken, words may be misheard, and the answer will be read aloud, so it should be
   1–3 plain sentences. Only the handheld's tools are loaded, with their definitions in view (no tool-search step),
   and a fast model answers (`gpt-5.4-mini`, low reasoning).
   - **Warm runtime** (with `relaymcp[voice]`, Python 3.11+): the background service keeps one Copilot runtime
     running, so a prompt starts in milliseconds instead of spending ~4–5 s starting `copilot -p`, and it uses a
     short voice-only system prompt. A one-tool request ("what's my battery?") takes ~3.5 s instead of ~8 s.
     Follow-ups continue the same session. If the runtime isn't available, the dispatcher uses `copilot -p`.
5. The reply appears in the overlay and is **spoken on the handheld** by the Kokoro neural voice, streamed sentence by
   sentence. The first word comes about 0.8 s after the reply arrives.

The speech models load while you talk and unload after 15 idle minutes, freeing roughly 1 GB for games.

## Settings

```sh
relaymcp voice                                  # show settings
relaymcp voice --voice am_michael --speed 1.1   # change the spoken voice
relaymcp voice --permissions full               # see "Permissions" below
relaymcp voice --model gpt-5.4                  # model for voice prompts (default: gpt-5.4-mini)
relaymcp voice --user-name Sam                  # how the agent addresses you
relaymcp voice --disable                        # turn voice prompts off
relaymcp voice --test "What's my battery level?"  # run the full pipeline from your computer
```

**Voices:** Kokoro ids start with an accent letter (`a` American, `b` British, `e` Spanish, `f` French, `h` Hindi,
`i` Italian, `j` Japanese, `p` Brazilian Portuguese, `z` Mandarin) and a gender letter (`f`/`m`). Examples:
`af_heart` (default), `af_bella`, `af_nicole`, `am_michael`, `am_fenrir`, `bf_emma`, `bm_george`. A Windows voice name
(e.g. `Microsoft Zira Desktop`) uses the built-in Windows voices instead. The `list_voices` MCP tool lists them all.

### Permissions

- `handheld` (default): voice requests may only use the handheld's MCP servers.
- `full`: voice requests run with all of your agent's permissions on your computer. See
  [security.md](security.md#voice-prompts-and-permissions).

### Using another agent

`"agent": "custom"` in `~/.relaymcp/config.json` runs any command instead of Copilot CLI. `{prompt}` is replaced with
the preamble plus the transcribed request, and `{session}` with a session id. The command's stdout is the reply.

```json
"voice": {
  "agent": "custom",
  "custom_command": ["claude", "-p", "{prompt}"]
}
```

## Troubleshooting

- **Nothing happens on View + Menu:** the controller must be in *Gamepad* mode, and the hardware server running
  (`relaymcp doctor`). Try *Ask Copilot*; if it says voice prompts aren't running, tap *Repair RelayMCP*.
- **"I didn't hear anything":** check **Settings → Privacy & security → Microphone** (both *Microphone access* and
  *Let desktop apps access your microphone*) and that the mic isn't muted.
- **"Can't reach your Mac":** your computer must be awake, on the same network, with the service running
  (`relaymcp status`).
- **Misheard words:** add names you use often to `"vocabulary"` in `C:\ProgramData\RelayMCP\device.json` on the
  handheld (comma-separated).

## Licensing

The neural voice uses [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (MIT) and the
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) weights (Apache-2.0). For pronunciation it relies on
**phonemizer** and **espeak-ng**, which are **GPL-3.0**. They are installed from PyPI on the handheld as
dependencies. RelayMCP doesn't redistribute them. If that's a concern for your use, remove `kokoro-onnx` from the
`device` extra and the built-in Windows voices are used instead.
