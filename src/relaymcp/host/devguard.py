"""Dev mode: a checkout used for development (RELAYMCP_DEV=1, set by scripts/dev-env.sh) must never touch the real
installation. It can't change the background service or Copilot's MCP registrations, can't edit ~/.ssh/config, can't
run commands on the handheld, and can't serve an enrollment kit. RELAYMCP_DEV_ALLOW=1 overrides this deliberately."""

from __future__ import annotations

import os


def dev_mode() -> bool:
    return os.environ.get("RELAYMCP_DEV") == "1"


def blocked() -> bool:
    return dev_mode() and os.environ.get("RELAYMCP_DEV_ALLOW") != "1"


def check(action: str) -> None:
    if blocked():
        raise RuntimeError(f"dev mode (RELAYMCP_DEV=1): refusing to {action}. This checkout must not touch the real "
                           "installation; set RELAYMCP_DEV_ALLOW=1 if you really mean it.")
