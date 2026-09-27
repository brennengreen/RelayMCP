# Source from the repo root:  . scripts/dev-env.sh
# Puts this checkout's relaymcp first on PATH with its own home (~/.relaymcp-dev) and dev mode on, so nothing run
# here can touch the real installation (background service, Copilot MCP registrations, ~/.ssh/config, the handheld).
_relay_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export RELAYMCP_DEV=1
export RELAYMCP_HOME="${RELAYMCP_DEV_HOME:-$HOME/.relaymcp-dev}"
export PATH="$_relay_root/.venv/bin:$PATH"
echo "RelayMCP dev mode: $("$_relay_root/.venv/bin/relaymcp" --version 2>/dev/null) from $_relay_root, home $RELAYMCP_HOME"
unset _relay_root
