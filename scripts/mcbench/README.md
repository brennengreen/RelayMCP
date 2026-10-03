# Minecraft real-time benchmark

Scores how well RelayMCP plays in real time, against ground truth: the [RelayMCP Telemetry
pack](../../packs/minecraft-telemetry/README.md) reports the player's exact pose, the looked-at block and nearby mobs
20 times a second, and its commands reset each trial (`relay:tp`, `clear`, `fill`, `summon`) and read a build back
block by block (`relay:blocks`). Every task is a behavior `program` (`tasks/*.py`) that runs on the handheld, with no
model in the loop, and every result carries its cadence: the 20 fps floor (a fresh look at least every 50 ms while
acting).

| Task | What it does | Scored by |
|---|---|---|
| `step` | `face()` yaw steps of 10-180 degrees and pitch targets | time until the stick model says on target, time until telemetry confirms, final error |
| `pursuit` | `keep_facing` a direction sweeping at 20, 40 and 80 degrees/s | RMS error (bias + jitter) per telemetry sample, at that sample's own tick |
| `reaction` | a pig summoned at a random bearing, 5 times | time from the first sample listing it to facing it; error holding it while it wanders |
| `course` | `walk_path` through 10 waypoints (turns up to 120 degrees) facing the way | time vs distance at walking speed, distance off the route, stops |
| `house` | a 5x5 oak house with glass windows and a door (`tasks/house.py`) | blocks right, missing, wrong and extra; seconds |

## Running it

```sh
python scripts/mcbench/mcgym.py step         # one task against the local simulator (no handheld): step | pursuit | reaction | course
python scripts/mcbench/bench.py suite        # every task on the handheld, one scorecard
python scripts/mcbench/bench.py house --x 110 --y -60 --z 40   # one task, at a chosen spot
```

On the handheld it needs: Minecraft in front on a Creative test world with cheats on and the pack applied (flat and
Peaceful is easiest), the RelayMCP Telemetry stream flowing (`state` topic `minecraft`), and for the house, oak
planks in hotbar slot 1, glass in slot 3 and an oak door in slot 4. The bench connects the virtual controller,
resumes the game if a focus steal paused it, and clears each task's ground before it starts (`suite` puts the tasks
40 blocks apart). Every run is saved to `scripts/mcbench/results/` (not committed).

The local simulator (`mcgym.py`) is the camera plant the servo is tested against, a walker on the left stick
relative to the camera, telemetry with the handheld's measured timing (a tick's angles ~45 ms behind the picture,
delivered ~54 ms after the tick, mobs every other tick) and wandering mobs. Programs run there exactly as on the
handheld, so a task's bugs show up before it uses the device.

## Results so far

| Where | step 90 degrees | pursuit 40/s | reaction | course | house | cadence p95 |
|---|---|---|---|---|---|---|
| simulator (2026-10-03) | 0.64 s, confirmed 1.1 s, error <= 0.2 | 0.26 degrees RMS | 0.9 s | 1-2% over ideal, <= 0.13 m off | - | 52-54 ms |
| handheld (2026-10-03) | - | - | - | - | 73/73 right, 1 extra, 59.4 s | 55 ms (worst 341 ms) |

Telemetry is the only look while it flows, and it comes at the game's 20 Hz tick, so every task sits at the floor
(p95 just over 50 ms). The margin has to come from the picture: a visual gyro fused at frame rate.
