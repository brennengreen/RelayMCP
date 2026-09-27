"""Registers RelayMCP's MCP servers with AI agents on this computer.

GitHub Copilot CLI is configured automatically. For other MCP clients RelayMCP prints the exact command or JSON to
use (see `relaymcp mcp --print`), since each keeps its settings in its own place."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import config
from .voice import find_agent

COPILOT_CONFIG = Path.home() / ".copilot" / "mcp-config.json"


def copilot_servers() -> dict:
    try:
        return json.loads(COPILOT_CONFIG.read_text()).get("mcpServers", {})
    except (OSError, ValueError):
        return {}


def register_copilot(cfg: dict) -> list[str]:
    """Add/update the servers in Copilot CLI's user config. Returns what changed."""
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
