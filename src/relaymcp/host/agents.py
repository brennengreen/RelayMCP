"""Registers RelayMCP's MCP servers with AI agents on this computer.

GitHub Copilot CLI is configured automatically. For other MCP clients RelayMCP prints the exact command or JSON to
use (see `relaymcp mcp --print`), since each keeps its settings in its own place."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import config, devguard
from .voice import find_agent

COPILOT_CONFIG = Path.home() / ".copilot" / "mcp-config.json"
COPILOT_AGENTS = Path.home() / ".copilot" / "agents"
COPILOT_SKILLS = Path.home() / ".copilot" / "skills"
SKILL_NAME = "relaymcp-handheld"
AGENT_NAME = "handheld"
AGENT_MARKER = "<!-- written by relaymcp: `relaymcp agent install` updates it, `relaymcp agent remove` deletes it -->"
DEFAULT_AGENT_MODEL = "claude-haiku-4.5"
HW_ALIAS, SCREEN_ALIAS = "relay-hw", "relay-screen"


def copilot_servers() -> dict:
    try:
        return json.loads(COPILOT_CONFIG.read_text()).get("mcpServers", {})
    except (OSError, ValueError):
        return {}


def register_copilot(cfg: dict) -> list[str]:
    """Add/update the servers in Copilot CLI's user config. Returns what changed."""
    devguard.check("change GitHub Copilot CLI's MCP registrations")
    exe = find_agent("copilot")
    if not exe:
        raise RuntimeError("GitHub Copilot CLI (`copilot`) isn't installed")
    current, changed = copilot_servers(), []
    for name, url, _ in config.mcp_servers(cfg):
        entry = current.get(name)
        if entry and entry.get("url") == url and entry.get("type") == "http":
            continue
        if entry:
            subprocess.run([exe, "mcp", "remove", name], capture_output=True, timeout=30)
        subprocess.run([exe, "mcp", "add", "--transport", "http", "--tools", "*", name, url],
                       check=True, capture_output=True, timeout=30)
        changed.append(name)
    return changed


def unregister_copilot(cfg: dict) -> list[str]:
    devguard.check("change GitHub Copilot CLI's MCP registrations")
    exe = find_agent("copilot")
    current, removed = copilot_servers(), []
    for name, url, _ in config.mcp_servers(cfg):
        if exe and name in current and current[name].get("url") == url:
            subprocess.run([exe, "mcp", "remove", name], capture_output=True, timeout=30)
            removed.append(name)
    return removed


def copilot_registered(cfg: dict) -> bool:
    current = copilot_servers()
    return all(current.get(name, {}).get("url") == url for name, url, _ in config.mcp_servers(cfg))


def snippets(cfg: dict) -> dict[str, str]:
    """Ready-to-use configuration for common MCP clients."""
    servers = config.mcp_servers(cfg)
    generic = {"mcpServers": {name: {"type": "http", "url": url} for name, url, _ in servers}}
    vscode = {"servers": {name: {"type": "http", "url": url} for name, url, _ in servers}}
    return {
        "GitHub Copilot CLI": "\n".join(f"copilot mcp add --transport http --tools '*' {n} {u}" for n, u, _ in servers),
        "Claude Code": "\n".join(f"claude mcp add --transport http --scope user {n} {u}" for n, u, _ in servers),
        "VS Code (.vscode/mcp.json or your user mcp.json)": json.dumps(vscode, indent=2),
        "Other MCP clients (Cursor, Windsurf, Claude Desktop via a proxy, ...)": json.dumps(generic, indent=2),
    }


def detected() -> dict[str, bool]:
    return {"copilot": bool(find_agent("copilot")), "claude": bool(shutil.which("claude")),
            "code": bool(shutil.which("code"))}


def agent_path() -> Path:
    return COPILOT_AGENTS / f"{AGENT_NAME}.agent.md"


def agent_model(cfg: dict) -> str:
    return (cfg.get("agent") or {}).get("model") or DEFAULT_AGENT_MODEL


def handheld_agent(cfg: dict) -> str:
    """A Copilot CLI custom agent that does hands-on work on the handheld with a fast model and only RelayMCP's tools,
    so a main session (often a slow, expensive model) hands device work off instead of driving it step by step."""
    device = cfg["device"]["name"]
    urls = {name: url for name, url, _ in config.mcp_servers(cfg)}
    # The agent's own connections, under names of their own: Copilot defers MCP tool schemas behind a search tool (an
    # extra model round trip per new tool), and deferTools only takes effect on servers the agent itself declares.
    servers = "".join(f"""  {alias}:
    type: http
    url: "{urls[name]}"
    tools: ["*"]
    deferTools: never
    timeout: 90000
""" for alias, name in ((HW_ALIAS, f"{device}-handheld"), (SCREEN_ALIAS, device)))
    hw, screen = HW_ALIAS, SCREEN_ALIAS
    return f"""---
name: {AGENT_NAME}
description: "Operates the {device} handheld (a Windows gaming handheld) hands-on through RelayMCP: plays and navigates games with the virtual gamepad, taps, types, reads the screen and runs apps. Delegate multi-step work on the handheld to it with a concrete goal and what done looks like; it answers with a short report."
model: {agent_model(cfg)}
tools: ["{hw}/*", "{screen}/*"]
mcp-servers:
{servers}---
{AGENT_MARKER}

You operate the handheld "{device}" for the main agent. Be fast: few steps, few screenshots, short answers.

Tools:
- `{hw}`: `observe` (the screen as text with tap points), `act` (several steps in one call), `screenshot` (small
  image), `behavior` (real-time loops: react, track, press_until, navigate a menu by text, watch), `proc` (talk to a
  console such as a game server), `powershell` (a warm session), gamepad (`gamepad_press`, `gamepad_sequence`,
  `gamepad_connect`), touch, `key_press`, `type_text`, `mouse_look`, `focus_window`, audio, display, power.
- `{screen}`: `Snapshot` (UI elements), `Click`, `Type`, `Scroll`, `Shortcut`, `App`, `Wait`, `PowerShell` (runs in
  the desktop session), and a slower full-size `Screenshot`.

How to work:
1. Look with `observe` first: the text on screen with tap points, for a fraction of a screenshot's cost. Use
   `screenshot` to see graphics (a game scene, icons); its `to_screen` maps image coordinates to screen pixels.
2. Do each sub-goal in one `act` call, e.g. [{{"focus": "Minecraft"}}, {{"tap_text": "Play"}}, {{"wait_text": "Servers"}}]
   with observe "text" to see the result in the same reply. Put known gamepad inputs in one `pad` step.
3. Input goes to the foreground window. Results include `foreground`, plus a `warning` when input probably went
   nowhere; if the wrong window is in front, add a {{"focus": ...}} step or call `focus_window` (remember: true).
4. In games use the gamepad; use tap_text, touch or `Click` for desktop UI and `type` steps for text. For anything
   that needs quick reactions or many repeated inputs (reach a menu item, press until something appears, follow a
   target), start a `behavior` and read its events instead of looping yourself.
5. Real-time games: make them turn-based first, `focus_window("<game>", pause={{"button": "start", "text":
   "Game is paused"}})` (it runs only during your calls). Play each sub-goal as one `behavior` program (kind
   "program", params.wait true): Python at frame rate that holds controls, `aim`s while walking and waits `until`
   text or a change, never one small move per turn; while it holds inputs it looks every <=50 ms (`until`, `tap`;
   results' `cadence` shows blind spots); if a game can't pause, queue the next program while one plays
   (params.after = its id). Always add a `guard` (health, a death screen). Calibrate a
   game's camera once (kind "calibrate", max_s 90) to `turn`/`level`/`look_at`/`scan` in degrees. Aim away from
   the HUD and held item; in menus, check the focused tooltip with `sees` before pressing A. Keep a standing
   `guard` (kind "guard") running while programs come and go; `save` programs that worked and check `skills`
   before writing new code.
6. Prefer structured state over pixels: a game server's console (`proc`) or a log beats reading the screen.
7. Coordinates are physical screen pixels.
8. If the same thing blocks you twice (a dialog, a sign-in, a permission), stop and report it instead of guessing.

Finish with 1-5 short lines: what you did, the state you left the handheld in (app and screen), and anything
unexpected. Never paste screenshots or raw tool output.
"""


def skill_path() -> Path:
    return COPILOT_SKILLS / SKILL_NAME / "SKILL.md"


def handheld_skill(cfg: dict) -> str:
    """An on-demand skill for main Copilot sessions: loaded only when a task involves the handheld."""
    device = cfg["device"]["name"]
    return f"""---
name: {SKILL_NAME}
description: How to work with the {device} handheld (a Windows gaming PC controlled through RelayMCP) quickly and cheaply. Use this whenever a task involves the handheld, its games, its screen or its gamepad.
---
{AGENT_MARKER}

# Working with the {device} handheld

The handheld is reached through two MCP servers, `{device}-handheld` (input, fast screen reading, real-time loops,
consoles, PowerShell) and `{device}` (Windows-MCP: UI trees, apps, files), plus SSH (`relaymcp exec`, `ssh {device}`)
for services and elevated work. The model is almost always the slow part: aim for few, big steps.

## Delegate hands-on work
For multi-step work on the device (navigate menus, play, set something up), hand it to the `handheld` custom agent
with a concrete goal and what done looks like. It runs a fast model with a small context and reports back briefly.

## Fast patterns
- Look with `observe` (screen text + tap points, ~150 tokens); `screenshot` only for graphics (~730 tokens).
- Do a sub-goal per call with `act`: `[{{"focus": "Minecraft"}}, {{"tap_text": "Play"}}, {{"wait_text": "Servers"}}]`, with
  `observe: "text"` to see the result in the same reply.
- Reflexes and repetition belong in a `behavior` on the handheld, not in model turns: `navigate` (reach a menu item
  by its text), `press_until`, `react`, `track`, `watch`. Start one, then read `status` events.
- Real-time games: turn-based first (`focus_window` with `pause`, so the game waits while you think), then one
  `program` behavior per sub-goal (`params.wait: true`): Python at frame rate with `pad`, `aim`, `until`, `sees`,
  `guard`. While it holds inputs it must look at least every 50 ms (the 20 fps floor for agentic control): loop on
  `until`/frame reads and `tap` instead of `press`; each result's `cadence` names the blind spots. Calibrate a game's camera once (kind `calibrate`) and programs turn in degrees (`turn`, `level`,
  `look_at`, `scan`). A standing `guard` behavior stops everything on danger; `save` programs that worked and look
  in `skills` first. Games that can't pause: queue the next program (`params.after`) while one plays. See
  `behavior(action="kinds")`.
- Game servers and other consoles: `proc` (start with a ready pattern, `send` commands and read the reply, `wait`
  for a log line). Structured output beats pixels.
- `powershell` on `{device}-handheld` keeps a warm, DPI-aware session (~50 ms per call); errors are plain text.

## Pitfalls
- Input goes to the foreground window: check `foreground`/`warning` in input results, use `focus_window` (not the
  App switch). An invisible ASUS helper window can hold focus; only a real tap on the screen clears it.
- Mark long work so updates wait: `relaymcp busy 90 --note "what you're doing"`, `relaymcp busy off` after.
- SSH commands share one connection; a long-running process started over SSH should use `-o ControlPath=none`.
- If a result says RelayMCP was updated and names new tools, start a new session to use them.
"""


def _ours(path: Path) -> bool:
    try:
        return AGENT_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def agent_installed() -> bool:
    return _ours(agent_path())


def _write_ours(path: Path, text: str) -> bool:
    if path.exists() and not _ours(path):
        raise RuntimeError(f"{path} exists and wasn't written by RelayMCP; leaving it alone")
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def install_agent(cfg: dict) -> bool:
    """Write or refresh the custom agent and the skill. Returns whether anything changed. Never overwrites files the
    user wrote."""
    devguard.check("install a GitHub Copilot CLI custom agent")
    changed = _write_ours(agent_path(), handheld_agent(cfg))
    return _write_ours(skill_path(), handheld_skill(cfg)) or changed


def remove_agent() -> bool:
    devguard.check("remove a GitHub Copilot CLI custom agent")
    removed = False
    for path in (agent_path(), skill_path()):
        if _ours(path):
            path.unlink()
            removed = True
    try:
        skill_path().parent.rmdir()
    except OSError:
        pass
    return removed


def agent_current(cfg: dict) -> bool:
    try:
        return (agent_path().read_text(encoding="utf-8") == handheld_agent(cfg)
                and skill_path().read_text(encoding="utf-8") == handheld_skill(cfg))
    except OSError:
        return False
