"""CI (Windows): import the installed device runtime and build the hardware MCP server without starting it."""

import asyncio
import importlib

for name in ("paths", "text", "agent", "speech", "tts", "system", "launcher", "audio", "win_input", "gamepad", "voice"):
    importlib.import_module(f"relaymcp.device.{name}")
print("device modules import OK")

from relaymcp.device import server  # noqa: E402

tools = asyncio.run(server.build_server(18767).list_tools())
names = sorted(t.name for t in tools)
print(f"{len(names)} tools:", ", ".join(names))
assert len(names) >= 30 and "handheld_status" in names and "voice_assistant" in names
