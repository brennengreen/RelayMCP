# RelayMCP Telemetry (Minecraft Bedrock behavior pack)

Streams the player's exact state for RelayMCP: ground truth for scoring and training, and a sensor for control (like
a drone's telemetry link). Nothing in RelayMCP requires it; with it, programs get `telemetry()` and the camera servo
(`face`, `face_point`, `keep_facing`).

Every tick (20 Hz) it prints one `RELAY {json}` line to the content log: position, eye height (`ey`), yaw (0 =
south/+z, 90 = west), pitch (the game's: +90 = down; RelayMCP turns it to up +), velocity, on ground, sneaking,
hotbar slot, the looked-at block (`look` = [x, y, z, face, block]), health, and every other tick the mobs within 32
blocks (`mobs` = [[type, x, y, z, head_y, id], ...]). The handheld follows the newest content log and shows it to
programs and to the `state` tool (topic `minecraft`).

Commands for benchmarks, typed as chat commands between trials (`/scriptevent relay:<name> {json}`); each answers
with a `RELAY {"reply": name, ...}` line (the `state` tool, topic `minecraft.reply`):

| Command | Arguments | Does |
|---|---|---|
| `relay:tp` | `x`, `y`, `z`, `yaw`, `pitch` | an exact start pose |
| `relay:fill` | `from`, `to`, `block` (air) | resets an area |
| `relay:summon` | `type`, `at` | a target |
| `relay:clear` | `radius` (32) | removes mobs and items |
| `relay:blocks` | `from`, `to` (at most 4096 cells) | the non-air blocks in a region (a build's check) |

## Install (test worlds only)

1. Copy this folder to `%APPDATA%\Minecraft Bedrock\Users\Shared\games\com.mojang\development_behavior_packs\RelayMCP_Telemetry`.
2. With Minecraft closed, add `{"pack_id": "5b7e1f3a-6c1d-4a8e-9a57-2f0c6d1e8b41", "version": [0, 1, 0]}` to the world's
   `world_behavior_packs.json`, and set `content_log_file:1` in `minecraftpe/options.txt` (the log is the channel).
3. Open the world. After editing `scripts/main.js`, `/reload` picks the change up without restarting.

Needs `@minecraft/server` 2.0.0 (Minecraft 1.21.90 or later).
