# MCP tools

RelayMCP exposes two MCP servers on the controlling computer (names use your device name; `ally` below):

- **`ally-handheld`** - RelayMCP's hardware server (35 tools, listed below)
- **`ally`** - [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) for screen control / computer use (20 tools: `App`, `DisplayInventory`, `PowerShell`, `FileSystem`, `Snapshot`, `Screenshot`, `Click`, `Type`, `Scroll`, `Move`, `Shortcut`, `Wait`, `WaitFor`, `Scrape`, `MultiSelect`, `MultiEdit`, `Clipboard`, `Process`, `Notification`, `Registry`)

_Generated from a live server by `scripts/gen_tools_doc.py`._

## Hardware server (`<device>-handheld`)

| Tool | Parameters | What it does |
|---|---|---|
| `handheld_status` | none | Snapshot of the handheld: screen, battery/charging, brightness, volume, display mode, power mode, connected controllers, virtual gamepad, and keep-awake state. Good first call. |
| `gamepad_press` | `buttons`, `hold_ms=120`, `repeat=1`, `interval_ms=150` | Press virtual Xbox controller buttons together, hold_ms, release; optionally repeat. Buttons: a b x y lb rb lt rt back(view) start(menu) guide ls rs dpad_up dpad_down dpad_left dpad_right. |
| `gamepad_hold` | `duration_ms=500`, `buttons=null`, `left_stick=null`, `right_stick=null`, `left_trigger=0.0`, `right_trigger=0.0` | Hold a controller state for duration_ms, then return to neutral. Sticks are [x, y] from -1 to 1 (y=1 is up/forward); triggers 0-1. Example: walk forward 2s = left_stick [0, 1], duration_ms 2000. |
| `gamepad_sequence` | `steps` | Run timed controller steps in order (max 60s total). Each step replaces the previous state: {"buttons": [...], "left_stick": [x,y], "right_stick": [x,y], "left_trigger": 0-1, "right_trigger": 0-1, "ms": 200}. A step with only "ms" is a neutral pause. Ends in neutral. |
| `gamepad_status` | none | Real and virtual controllers in XInput slots 0-3 (buttons held, sticks, triggers, battery), plus the virtual pad's slot, idle time and the last rumble a game sent to it. |
| `gamepad_watch` | `seconds=5.0`, `slot=null` | Record controller input changes for a few seconds (e.g. what the user presses, or verify the virtual pad). |
| `gamepad_unplug` | none | Unplug the virtual controller now (it also unplugs itself after 5 idle minutes). |
| `controller_rumble` | `left=0.6`, `right=0.6`, `duration_ms=300`, `slot=0` | Vibrate a physical controller (slot 0 = the handheld's built-in controller, in gamepad mode). Left = strong low-frequency motor, right = light high-frequency motor, 0-1. |
| `touch_tap` | `x`, `y`, `count=1`, `hold_ms=60` | Tap the touch screen with one finger at (x, y); count=2 double-taps. |
| `touch_long_press` | `x`, `y`, `duration_ms=900` | Press and hold one finger at (x, y) (context menus, touch-and-hold game actions). |
| `touch_swipe` | `x1`, `y1`, `x2`, `y2`, `duration_ms=300`, `hold_start_ms=0`, `hold_end_ms=0` | Swipe/drag one finger from (x1, y1) to (x2, y2). hold_start_ms > ~500 makes it a drag-and-drop; hold_end_ms keeps the finger down at the end (e.g. hold a virtual joystick). |
| `touch_pinch` | `x`, `y`, `start_spread=400`, `end_spread=150`, `duration_ms=500`, `angle_deg=0` | Two-finger pinch centered on (x, y). spread = pixels between the fingers; end > start zooms in, end < start zooms out. angle_deg rotates the finger axis (0 = horizontal). |
| `touch_gesture` | `fingers`, `duration_ms=500` | Custom multi-touch gesture: up to 10 fingers, each a list of [x, y] points visited evenly over duration_ms. All fingers touch down together and lift together. Example two-finger swipe up: [[[800,700],[800,300]], [[1000,700],[1000,300]]] |
| `key_press` | `keys`, `hold_ms=50`, `repeat=1`, `interval_ms=100` | Press keys together as hardware scan codes (works in games), hold, release. ["ctrl","shift","esc"], ["f11"], ["w"] with hold_ms 2000 = walk forward 2s. Names: letters, digits, f1-f24, space, enter, esc, tab, shift, ctrl, alt, win, up/down/left/right, home, end, pageup, pagedown, insert, delete, backspace, capslock, numpad0-9, volumeup/volumedown/volumemute, playpause, and punctuation like - = [ ] ; ' , . / `. |
| `type_text` | `text`, `interval_ms=5` | Type Unicode text into whatever has focus (search boxes, chat, text fields). Newlines press Enter. |
| `mouse_look` | `dx`, `dy`, `duration_ms=250` | Relative mouse movement, as games use for camera/aim (absolute clicks don't turn a game camera). Positive dx = right, positive dy = down. |
| `mouse_hold` | `button="left"`, `hold_ms=500` | Hold a mouse button at the current cursor position for hold_ms (e.g. mine/attack/charge in a game). |
| `touch_keyboard` | `action="status"` | The Windows on-screen touch keyboard: action = status \| show \| hide \| toggle. |
| `release_all_input` | none | Emergency reset: release any held keys and put the virtual gamepad back to neutral. |
| `audio_devices` | none | Speakers and microphones, the defaults, their volume/mute, and what's currently playing (peak level). |
| `set_volume` | `device="speaker"`, `level=null`, `mute=null` | Set speaker or microphone volume (0-100) and/or mute. Returns before/after so you can restore it. |
| `mic_record` | `seconds=5.0`, `transcribe=false`, `model="base.en"`, `vocabulary=null` | Record the handheld's microphone for N seconds (max 120) to a WAV file. Reports peak/RMS level (is the mic working/muted?) and optionally transcribes it. Needs Windows microphone access turned on (see audio_devices). |
| `speaker_capture` | `seconds=5.0`, `transcribe=false`, `model="base.en"`, `vocabulary=null` | Record what the handheld is playing through its speakers (system audio loopback) for N seconds: verify a game or app actually makes sound, measure loudness, or transcribe spoken audio. Can run while speak/play_sound play. |
| `play_sound` | `wav_path=null`, `tone_hz=880.0`, `seconds=0.5`, `volume=0.3` | Play a WAV file on the handheld's speakers, or a test tone if no path is given. |
| `speak` | `text`, `voice=null`, `rate=0`, `volume=100`, `save_wav=false`, `engine="auto"` | Say text out loud on the handheld's speakers. engine: auto (natural neural voice when installed, else Windows SAPI) \| neural \| sapi. voice: a neural voice id like af_heart (default), am_michael, bf_emma, or a Windows voice name (see list_voices). rate -10..10, volume 0-100. save_wav=True renders a WAV file instead of speaking (e.g. to feed transcribe_audio_file). voice_assistant(action="stop") cuts it off. |
| `list_voices` | none | Text-to-speech voices on the handheld: the neural voices (natural, used by default) and the Windows voices. |
| `listen` | `seconds=5.0`, `model="base.en"`, `language=null`, `vocabulary=null` | Speech-to-text: record the handheld's microphone for N seconds and transcribe it locally with Whisper. model: tiny.en (fastest), base.en (default), small.en (most accurate); non-.en models + language for others. vocabulary: optional names/terms to expect (e.g. "ROG Ally, Minecraft, Creeper"). First use downloads the model. Needs Windows microphone access turned on (see audio_devices). |
| `transcribe_audio_file` | `wav_path`, `model="base.en"`, `language=null`, `vocabulary=null` | Transcribe a WAV file on the handheld (e.g. from mic_record, speaker_capture or speak save_wav). |
| `set_brightness` | `percent` | Set screen brightness 0-100. Returns before/after. |
| `display_modes` | none | Current resolution/refresh rate and all supported modes (e.g. 120 Hz and 60 Hz panels). |
| `set_display_mode` | `refresh_hz=null`, `width=null`, `height=null` | Temporarily change refresh rate and/or resolution (not saved; call again with the 'before' values to restore). |
| `power_mode` | `set_to=null` | Get or set the Windows power mode: best_power_efficiency \| balanced \| best_performance. (Armoury Crate's Silent/Performance/Turbo TDP modes are separate and need Armoury Crate.) |
| `system_load` | none | CPU and memory use, CPU clock, and the top CPU-consuming processes (e.g. while a game runs). |
| `keep_awake` | `minutes=60` | Keep the handheld (and its screen) awake for the next N minutes, e.g. before long unattended work; 0 cancels. Only honored at home. Without a lease it stays awake for 10 minutes after each tool call. |
| `voice_assistant` | `action="status"`, `text=null` | The handheld's push-to-talk voice prompts to the AI agent (hold View + Menu, press Ctrl+Alt+Shift+F12, or tap 'Ask Copilot'). action: status (state, triggers, whether the computer is reachable, last interaction) \| listen (start listening now, as if the button was pressed) \| ask (send `text` as if it had been spoken; for testing the pipeline) \| stop (stop listening or speaking) \| allow_virtual_chord (for 2 minutes, let the virtual gamepad's View + Menu trigger it too; for testing). Don't use ask from inside a voice request: only one runs at a time. |
