"""CI (Windows): import the installed device runtime and build the hardware MCP server without starting it."""

import asyncio
import importlib

import time

for name in ("paths", "text", "focus", "agent", "speech", "tts", "system", "launcher", "audio", "win_input", "gamepad", "voice",
             "lean", "capture", "ocr", "procs", "pshost", "updates", "behave"):
    t = time.monotonic()
    importlib.import_module(f"relaymcp.device.{name}")
    print(f"import relaymcp.device.{name}: {time.monotonic() - t:.2f}s", flush=True)
print("device modules import OK", flush=True)

from relaymcp.device import server  # noqa: E402

import json  # noqa: E402

tools = asyncio.run(server.build_server(18767).list_tools())
names = sorted(t.name for t in tools)
print(f"{len(names)} tools:", ", ".join(names))
assert len(names) >= 30 and "handheld_status" in names and "voice_assistant" in names
assert {"act", "observe", "screenshot", "proc", "powershell", "focus_window", "behavior"} <= set(names)
definitions = json.dumps([t.model_dump(exclude_none=True) for t in tools])
print(f"tool definitions: {len(definitions)} chars (~{len(definitions) // 4} tokens)")
assert '"title"' not in json.dumps([t.inputSchema for t in tools]), "schema titles should be stripped"
assert len(definitions) <= 17000, "tool definitions grew past the context budget (~4.2k tokens)"
