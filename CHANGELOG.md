# Changelog

## 0.2.0 (unreleased)

Faster, leaner and real-time capable: most of the work moves from model round trips to the
handheld itself.

- **An early-tester feedback loop:** a successful `relaymcp setup` links directly to the public handheld compatibility
  discussion and asks users to help others discover the project. The README, getting-started guide and issue chooser
  route questions and working compatibility reports to Discussions instead of leaving new users without a next step.
- **Voice can use other MCP servers without `full`:** `relaymcp voice --extra-servers pl515` lets handheld-mode voice
  prompts also use named servers from Copilot's MCP config (for example a drone's homestation), with their tool
  schemas kept in view and the prompt telling the agent about them. Nothing else on the computer is opened up.
- **A camera servo in world angles:** with game telemetry, programs `face(yaw, pitch)` or `face_point(x, y, z)`,
  and `keep_facing(...)` a direction, a point or a moving target while they walk and tap: the servo owns the right
  stick, and the program keeps the left stick and buttons. It runs closed-loop at ~120 Hz on a fused estimate: the
  latest telemetry angle plus the turn since then. That turn comes from the visual gyro while it agrees with the
  calibrated stick model, and from the model alone when it doesn't (the gyro can lock onto a repeating texture). It
  asks for the rate that closes the error at the moment the next command takes effect: commands still in flight and
  the target's own motion are fed forward. On the Minecraft-like simulator it faces 90 degrees in 0.75-0.82 s (the
  fastest the plant allows is 0.69 s), lands within 0.2 degrees, and follows a target moving at 40 degrees/s within
  0.2 degrees RMS. On the Ally every turn lands within 0.5 degrees of telemetry's truth. The servo also re-checks the
  stick model against exact telemetry: yaw rates within 2-3% of the calibrated curve, and telemetry's angle ~45 ms
  behind the picture. While telemetry flows the servo skips frame reads and the gyro: on the handheld's 1080p frames
  they had stalled its loop for up to 0.46 s, with the stick held, so a 90 degree turn overshot and took 2 s. Each
  new telemetry sample counts as a look for the 20 fps floor, stamped when it arrived. Games read the stick
  once per frame, so near a still target the servo slows to where one frame turns under half a degree, stops, lets
  telemetry show where the camera really is, and finishes with one pulse of at least 25 ms. Shorter corrections were
  partly dropped while the model counted them, and it dithered for seconds. `face()` returns as soon as the stick
  model says the camera will come to rest on target, and the servo corrects a miss in the background;
  `confirm=True` waits for telemetry. Simulated with 60-120 Hz input reads: 90 degrees in 0.65-0.7 s, 5 degrees in
  0.08 s, final error within 0.3 degrees (it had taken 1.3-2.2 s). `walk_to(x, z)` walks there on telemetry with the left stick pushed relative to where the camera
  faces (so `keep_facing` can look elsewhere meanwhile), predicting position from telemetry's velocity over the
  sample's age, easing in, and hopping a step it's stuck against: in a kinematic Minecraft walker it arrives within
  0.1 s of distance / walking speed at any camera heading, 0.15-0.19 m from the point.
- **Game telemetry as ground truth:** the hardware server follows a game's log for `RELAY {json}` lines and keeps the
  latest sample and 10 s of history. A Minecraft script pack prints the player's exact position, eye height, yaw,
  pitch, velocity, looked-at block, health and nearby mobs 20 times a second. Programs read it with `telemetry()`,
  and a sample under 50 ms old counts as a look. The `state` tool shows it as topic `minecraft`: the inbox's new
  latest-only updates keep a 20 Hz stream from pushing other topics' events out. `handheld_status` reports its rate
  and latency.
- **The 20 fps floor for agentic control, measured on every run:** whatever acts for the agent must act on a look
  under 50 ms old, at every input change and all the time an input is held. Behavior results carry `cadence`
  (`worst_ms`, `p95_ms`, `over_50ms`, `worst_at`: the program line and call that went blind, such as a `wait()` or
  `turn_open` with the stick held). Frame, text (OCR) and state-inbox reads count as looks; idle time with nothing
  held doesn't. Presses no longer block what issues them: the new program call `tap("a", ms=60)` and every
  behavior's pad press go down on top of what is held and let go on their own (a 1 ms release timer), so a 120 Hz
  `react` loop keeps looking while a button is down, and a press no longer drops a stick another behavior holds
  (the old path returned the whole pad to neutral). `guard` and `watch` now look every 50 ms (was 100), and
  `script` and `press_until` check their conditions 20+ times a second while their actions keep their schedule
  (they only looked when acting: 4 and 2.5 times a second).
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
  runs. Its state file is replaced whole, one save at a time (a process exiting while it was being stopped saved
  twice at once, and on Windows the writes interleaved into unreadable JSON).
- **`powershell` tool on the hardware server:** a persistent session (variables and functions carry over, ~50 ms per
  call instead of 1.5–2.5 s), DPI-aware so Win32 coordinates match screenshots, and errors as plain `ERROR:` lines
  instead of CLIXML.
- **Agents hear about updates:** MCP clients only load tools when a session starts, so after an update, results
  briefly carry a `relaymcp_update` note naming the new tools and saying to start a new session (at most 3 notes,
  3 minutes apart; one when only behavior changed; a redeploy doesn't start them over). `handheld_status` reports
  the tool count and a tool-set hash, and any recent update, for the whole 45 minutes.
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
- **Camera control the robotics way (any game, drone feed or robot camera):** `behavior` kind `calibrate` identifies
  the look stick once per app (HUD overlay, latency, coast after release, deadzone and response curve, px/degree by
  loop closure over a full turn, focal length, vertical gain, pitch limit) and saves a profile on the handheld;
  it measures the focal length first and the response curve from slow to fast, stopping below 8% of the picture
  per captured frame (faster, image matching aliases and reads wrong while looking healthy). Programs then get
  `turn(yaw, pitch)`, `level()`, `look_at(x, y)`, `scan(score_fn)` and `look_rate()` in degrees, closed on a visual
  gyro (tile shifts fitted with the exact rotation homography, robust to things moving through the view, its roll
  while yawing giving the camera's pitch), with feedforward through the inverse response curve, coast-aware release,
  tracking through every correction and settling. `aim` defaults to the calibrated deadzone. The gyro samples tiles
  through the predicted local warp when the view rolls (yawing while looking down no longer drifts the pitch), and
  the full turn solves the focal length that makes it exactly 360 degrees (a rough first focal length no longer
  biases pixels per degree by 2-3%), redoing the turn slower if tracking broke or the start view was missed.
  `look_at` corrects only on a match near the middle of the picture (on the Ally, a look-alike dirt block across the
  screen pulled it 20 degrees away), and a `turn` that runs out of time says so (`timed_out`). Calibration also tells
  a radial stick (deadzone and curve on the stick's length, as in Minecraft) from a per-axis one, and turns send the
  stick vector that model needs; the finishing pulses on the vertical axis divided the deflection, not the rate, by
  the vertical gain (on the Ally a 2.7-degree pitch turn ended at 9.7).
- **Armoury Crate's Command Center no longer eats the controller:** on the Ally it opened as the virtual pad
  plugged in, and the game got no input at all (turn mode's resume press and every stick move went to the overlay's
  menu, where a game's presses would change the handheld's settings). It's closed with the pad's B right after
  plugging in and before agent input, and a program stops if it opens mid-run (as if you took over).
- **Turning while looking straight down or up:** Minecraft's pitch limit is exactly -90/+90, where a yaw is pure roll
  in the picture and the small yaw part its direction came from is noise (in the simulator a 30-degree turn spun 810
  degrees). Programs call `set_pitch(deg)` after holding the stick into a limit, the camera keeps its pitch up to date
  through turns, and the gyro takes a yaw's direction from it. Turns also cap their speed by the frame rate of the
  moment (a busy machine captures fewer frames than at calibration, and past ~8% of the picture per frame the gyro
  reads short and the turn overshoots).
- **Turn mode is sturdier:** a dropped resume press is tried again (then the close button) instead of letting a
  program play into the pause menu for its whole run, and a pause profile's `dead` text (e.g. "Respawn") keeps it
  from pressing pause on a death screen, where it only opened a menu that ate the next presses.
- **Turns that can't be fooled into spinning, and turns without the picture:** a `turn` whose error grows well past
  where it started (the picture says it's going the wrong way: a repeating texture locked on wrong, a static overlay)
  stops and says `tracking_lost` instead of spinning for its whole timeout (on the Ally a 27-degree turn went 32
  degrees the other way). A pitch limit only counts when pushing into it (starting up from Minecraft's -90 clamp
  with a sluggish first moment was taken for "at the limit"). `turn_open(yaw, pitch)` turns from the calibrated curve
  alone, one axis at a time, for scenes the picture can't be followed in and from exact references.
- **Open-loop turns measured, not assumed:** calibration now times what `turn_open` does (a deflection held for two
  lengths of time per axis, followed on the picture) and stores each axis' true rate and lag (`look.open_loop`);
  `behavior` kind `calibrate` with `only: "open_loop"` re-times a saved profile in about 10 s. Checked against
  Minecraft's exact references (its ground grid, the flat world's horizon), open-loop yaw had come up 0.042 s x rate
  short (the old lag, coast - ramp, left out that the turn starts late too: now coast - latency - ramp/2) and pitch
  4.5% long (the vertical gain, measured as a steady rate over a short stretch), so a 78-degree aim from the -90
  clamp overshot 2.5 degrees and a 3x3 house's corners missed their blocks. An earlier check had agreed only because
  the two errors cancel near a 0.9 s hold.
- **Programs remember where the camera looks:** the next program's camera starts knowing the pitch the last one
  left it at (turn-based play runs one program per call; yaws seen straight down need it), not after you take over;
  and with the pitch known, a stalled picture only counts as a pitch limit near straight up or down (right after a
  jump's landing it stalled mid-range). A guard started again under its name replaces itself.
- **Instruments read exactly:** programs get `numbers(region)` and `pixel_text(region)`, which read text drawn in a
  game's pixel font glyph by glyph (Windows OCR read Minecraft's "Position: -11, 100, 0" as "-11, 13B," every time),
  and `grid_angle(region)`, a compass for grid worlds: looking straight down, the texture's straight edges give the
  yaw off the world's axes to a fraction of a degree.
- **Programs queue like action chunks:** `params.after = <run id>` starts a program the moment that run finishes
  (plan the next chunk while one plays; it's cancelled if the run ahead fails or is stopped, and a guard firing
  clears the queue), and `params.replace = <run id>` swaps a running program for a new one without letting go of
  what it holds. A turn-based game doesn't pause between chained programs.
- **The controller re-primes after touch, mouse or keyboard input:** tapping the screen switched Minecraft to its
  touch controls, and the next controller press only switched it back. The virtual pad now checks Windows' last-input
  time and nudges the right stick (net zero) first when anything but a controller came in since its last input.
- **Guards, pre-flight checks and a skill library** (from embodied-agent research: runtime monitors kept apart
  from the policy, and Voyager-style libraries of verified skills): `behavior` kind `guard` is a standing safety
  check (setup + a Python condition over the screen) that stops every program when it fires (a turn-based game then
  pauses), can start a reflex program, only judges a running game, and reports through `alerts` in the next tool
  results. Programs with unknown names fail before anything moves, with a "did you mean". `save` / `skills` /
  `forget` keep programs that worked per game, with inputs inferred from the code and run statistics; run them with
  `params.skill` + `params.args` or `skill("name", ...)` inside a program.
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
