# Changelog

## 0.2.0 (unreleased)

Faster, leaner and real-time capable: most of the work moves from model round trips to the
handheld itself.

- **Focus stealers are caught and undone:** input results name invisible windows that hold focus (like ASUS'
  AsHotplugCtrl) instead of reporting success. The hardware server watches the foreground and, when a pop-up or an
  invisible helper takes focus, gives it back to the last app window before sending input, even with no input
  target set. `focus_window()` with no target explains why input may not register: what has focus, recent focus
  changes, and seconds since the last input.
- **Teach by showing:** `gamepad_watch(as_steps=true)` records what you do on the controller and returns it as
  `gamepad_sequence` steps, so an agent can replay a move it was shown.
- **Gamepad timing:** sequence steps are scheduled against deadlines with a 1 ms timer, so they don't drift and each
  lands within about a millisecond (Windows' default timer made each step up to 15 ms late). New `ramp_ms` eases
  sticks and triggers into a step.
- **`behavior` tool: real-time loops on the handheld** that react in tens of milliseconds with no model round
  trips: `react` (press when a region changes or shows a color), `track` (steer the mouse or a stick onto a colored
  target), `press_until` (repeat an input until text or a color appears), `watch` (report changes) and `navigate`
  (move through a menu with the d-pad or arrow keys to an item by its text, reading the highlight, then select it),
  and `script`: a small state machine the model writes once ("press right until *Iron Golem* shows, then attack
  until the server reports it angry") that runs on the handheld with text, color, change and state-inbox conditions. Each has a
  time limit, releases all input when it ends, and stops as soon as a real controller moves.
- **`state` inbox:** programs on the handheld (a game server script, an add-on) POST JSON to
  `127.0.0.1:<port>/state/<topic>`; agents read the latest state or wait for a matching event with the `state` tool,
  exact and a few tokens instead of reading pixels.
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
- **Real-time games, played in batches:** a model thinks for seconds between steps, and in Minecraft night fell and a
  zombie killed the player during a few pauses for thought. Now:
  - **Turn-based play:** `focus_window(..., pause={"button": "start", "text": "Game is paused"})` keeps the game
    paused whenever none of the agent's input is running: every input call and behavior resumes it first and pauses
    it after (checking the pause text, so a game that paused itself or a menu that ate the press are handled), and
    screenshots/observe taken while paused show the frame from just before the pause. Optional `region` (where the
    pause text shows: faster checks) and `close` (a button that closes a menu in which the pause button does nothing,
    like Minecraft's crafting screen). Resuming waits until the pause text is gone on two reads in a row and the
    screen has settled (a fading menu's text stops being readable before it's gone, and input sent then lands in
    the menu).
  - **`program` behaviors:** a short Python program runs on the handheld at frame rate with a controller and
    perception API (`pad` holds a whole controller state, `press`, `seq`, `wait`, `until`, `aim` steers the camera
    onto a screen point while walking, `track` follows one for custom loops, `shift` measures how far the view moved
    (calibrate camera turns), `sees` text in a region, `frame`/`diff`/`color`, `guard` stops on danger, `log`,
    `result`). `params.wait: true` returns when it ends, so one call plays one sub-goal. Loops that never wait
    are interrupted at the time limit.
- **Found by playing Minecraft:** template matching (aim, track at) no longer locks onto open sky: its running sums
  are float64 and flat windows can't match (float32 rounding had bright, nearly flat sky scoring in the thousands).
  `aim` and `track at` learn a game's stick deadzone (Minecraft ignores deflections under ~0.4) and report it, and
  stop with a clear error when the "target" doesn't move even at full deflection (it was the held pickaxe, drawn
  over the same corner of every frame).
  Text matching accepts a label cut off partway ("Wooden Picl" for "Wooden Pickaxe", a clipped tooltip) and tolerates stylized game fonts (Minecraft's "Resume" reads as
  "fiesume" to OCR, "Quit" as "auit"); `track` can aim at whatever is at a screen point (template matching, e.g. a
  tree seen in a screenshot) and turns the camera until it's under the crosshair; behaviors can keep a controller
  state held (`{"hold": {"right_trigger": 1}}`) until something happens, e.g. mine until the block breaks. A newly
  plugged virtual controller nudges the right stick out and back, because Minecraft spent the first real press
  switching to controller mode (`gamepad_prime` in device.json turns it off).
- **`relaymcp-handheld` skill:** `relaymcp agent install` also adds an on-demand Copilot skill with the playbook
  (fast patterns and pitfalls), loaded only when a task involves the handheld.
- **Hardened by review:** two independent review rounds before release: menu highlights are never guessed,
  PowerShell timeouts hold under streaming output, stray SSH connections are cleaned up, key holds survive
  failed sends, and the test suite can't reach a real handheld.

## 0.1.1 (2026-09-26)

Input reliability, from real use driving Minecraft Bedrock on a ROG Ally.

- **Input lands in the game:** when the virtual gamepad plugs in, Armoury Crate's notice took the foreground and input
  was silently dropped. RelayMCP now puts the game back in front after closing the notice.
- **Fast screen tools:** `screenshot` (DXGI capture, a small JPEG in ~70 ms, ~730 tokens instead of ~1,900),
  `observe` (the screen as OCR text with tap points, ~150 tokens) and `act` (several steps in one call, e.g.
  focus a window, tap a button by its text, wait for a line of text).
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
