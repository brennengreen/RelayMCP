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
  image), gamepad (`gamepad_press`, `gamepad_sequence`, `gamepad_hold`, `gamepad_connect`), touch, `key_press`,
  `type_text`, `mouse_look`, `focus_window`, audio, speech, display, power, `handheld_status`.
- `{screen}`: `Snapshot` (UI elements), `Click`, `Type`, `Scroll`, `Shortcut`, `App`, `Wait`, `PowerShell` (runs in
  the desktop session), and a slower full-size `Screenshot`.

How to work:
1. Look with `observe` first: the text on screen with tap points, for a fraction of a screenshot's cost. Use
   `screenshot` to see graphics (a game scene, icons); its `to_screen` maps image coordinates to screen pixels.
2. Do each sub-goal in one `act` call, e.g. [{{"focus": "Minecraft"}}, {{"tap_text": "Play"}}, {{"wait_text": "Servers"}}]
   with observe "text" to see the result in the same reply. Put known gamepad inputs in one `pad` step.
3. Input goes to the foreground window. Results include `foreground`, plus a `warning` when input probably went
   nowhere; if the wrong window is in front, add a {{"focus": ...}} step or call `focus_window` (remember: true).
4. In games use the gamepad; use tap_text, touch or `Click` for desktop UI and `type` steps for text.
5. Coordinates are physical screen pixels.
6. If the same thing blocks you twice (a dialog, a sign-in, a permission), stop and report it instead of guessing.

Finish with 1-5 short lines: what you did, the state you left the handheld in (app and screen), and anything
unexpected. Never paste screenshots or raw tool output.
"""


def agent_installed() -> bool:
    try:
        return AGENT_MARKER in agent_path().read_text(encoding="utf-8")
    except OSError:
        return False


def install_agent(cfg: dict) -> bool:
    """Write or refresh the custom agent. Returns whether the file changed. Never overwrites an agent the user wrote."""
    devguard.check("install a GitHub Copilot CLI custom agent")
    path, text = agent_path(), handheld_agent(cfg)
    if path.exists() and not agent_installed():
        raise RuntimeError(f"{path} exists and wasn't written by RelayMCP; leaving it alone")
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def remove_agent() -> bool:
    devguard.check("remove a GitHub Copilot CLI custom agent")
    if not agent_installed():
        return False
    agent_path().unlink()
    return True


def agent_current(cfg: dict) -> bool:
    try:
        return agent_path().read_text(encoding="utf-8") == handheld_agent(cfg)
    except OSError:
        return False
