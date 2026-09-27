"""Device side of RelayMCP (Windows handhelds): the background agent, the hardware MCP server, and voice prompts.

Runs on the handheld only; installed there by the setup kit that `relaymcp kit` builds on your computer.
"""

from relaymcp import __version__

__all__ = ["__version__"]
