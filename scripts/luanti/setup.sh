#!/bin/bash
# A Minecraft-like test game on macOS: Luanti (Homebrew) running Mineclonia (ContentDB), in a test-bed folder of
# its own (RELAY_LUANTI_HOME, default .scratch/luanti): its own config (windowed, muted, keeps running unfocused),
# and a world with the relay mod (telemetry out, the agent's controls in). Your own Luanti settings aren't touched.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TB="${RELAY_LUANTI_HOME:-$HERE/../../.scratch/luanti}"
W="$TB/worlds/agent"
[ -d /Applications/Luanti.app ] || brew install --cask luanti
mkdir -p "$TB/games" "$W/worldmods"
if [ ! -f "$TB/games/mineclonia/game.conf" ]; then
  url=$(curl -s "https://content.luanti.org/api/packages/ryvnf/mineclonia/releases/" | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['url'])")
  case "$url" in /*) url="https://content.luanti.org$url";; esac
  curl -sL "$url" -o "$TB/mineclonia.zip" && (cd "$TB/games" && unzip -q -o ../mineclonia.zip)
fi
rm -rf "$W/worldmods/relay" && cp -R "$HERE/relay" "$W/worldmods/relay"
cat > "$W/world.mt" <<'CONF'
gameid = mineclonia
world_name = agent
backend = sqlite3
player_backend = sqlite3
auth_backend = sqlite3
mod_storage_backend = sqlite3
creative_mode = false
enable_damage = true
load_mod_relay = true
CONF
cat > "$TB/relay.conf" <<'CONF'
screen_w = 800
screen_h = 600
fullscreen = false
autosave_screensize = false
mute_sound = true
enable_sound = false
pause_on_lost_focus = false
name = agent
fixed_map_seed = 20261003
mg_name = v7
time_speed = 72
dedicated_server_step = 0.05
CONF
echo "ready: python3 $HERE/agent.py --seconds 90"
