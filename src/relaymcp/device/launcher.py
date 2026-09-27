"""'Ask Copilot' launcher: a windowless program for Desktop/Start shortcuts that tells the RelayMCP hardware server to
start listening for a voice prompt."""

from __future__ import annotations

import ctypes
import urllib.request

from .paths import VOICE_HEADER, device_settings


def main() -> None:
    port = device_settings()["ports"]["hardware"]
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/voice/trigger", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", VOICE_HEADER: "1"})
        opener.open(req, timeout=5).read()
    except Exception:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Voice prompts aren't running right now.\n\n"
            "They work while this handheld is signed in and on your home network. If they should be working, "
            "tap 'Repair RelayMCP' on the Desktop.",
            "Ask Copilot",
            0x00000040 | 0x00040000,  # MB_ICONINFORMATION | MB_TOPMOST
        )


if __name__ == "__main__":
    main()
