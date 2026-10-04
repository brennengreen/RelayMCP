"""A Minecraft-like game on this Mac for the agent: Luanti running Mineclonia, with the relay world mod (telemetry
out, controls in, through files in the world folder; no OS input). This first agent gathers wood and fends off
hostile mobs, so the loop (state -> goal -> skill -> controls) can be watched and measured.

    scripts/luanti/setup.sh     # once: Luanti, Mineclonia, the test world with the relay mod
    python3 scripts/luanti/agent.py [--seconds 90] [--keep-world]
"""
import argparse
import json
import math
import os
import subprocess
import time
from pathlib import Path

TB = Path(os.environ.get("RELAY_LUANTI_HOME", Path(__file__).resolve().parents[2] / ".scratch" / "luanti"))
WORLD = TB / "worlds" / "agent"
LUANTI = "/Applications/Luanti.app/Contents/MacOS/luanti"
HOSTILE = ("zombie", "skeleton", "spider", "creeper", "husk", "stray", "drowned", "witch", "slime", "pillager",
           "vindicator", "enderman", "silverfish", "phantom")
WALK = 4.0


def aim(eye, target):
    dx, dy, dz = target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]
    return math.degrees(math.atan2(-dx, dz)), -math.degrees(math.atan2(dy, math.hypot(dx, dz)))


class Bridge:
    def __init__(self):
        self.tel = WORLD / "relay_telemetry.jsonl"
        self.cmd = WORLD / "relay_cmd.json"
        self.f, self.latest, self.seq = None, None, 0

    def read(self):
        if self.f is None:
            if not self.tel.exists():
                return self.latest
            self.f = open(self.tel)
        for line in self.f.readlines():
            try:
                self.latest = json.loads(line)
            except ValueError:
                pass
        return self.latest

    def send(self, **cmd):
        tmp = self.cmd.with_suffix(".tmp")
        tmp.write_text(json.dumps(cmd))
        os.replace(tmp, self.cmd)

    def one_shot(self, **cmd):
        self.seq += 1
        return {**cmd, "seq": self.seq}


def run(seconds, keep_world=False):
    for f in ("relay_telemetry.jsonl", "relay_cmd.json"):
        (WORLD / f).unlink(missing_ok=True)
    if not keep_world:  # the same fresh world every run: the map regenerates from the fixed seed, the player at spawn
        for f in ("map.sqlite", "players.sqlite", "env_meta.txt", "mod_storage.sqlite", "auth.sqlite"):
            (WORLD / f).unlink(missing_ok=True)
    env = {**os.environ, "MINETEST_USER_PATH": str(TB), "LUANTI_USER_PATH": str(TB)}
    log = open(TB / "luanti.log", "w")
    game = subprocess.Popen([LUANTI, "--config", str(TB / "relay.conf"), "--go", "--world", str(WORLD)],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    b = Bridge()
    t0 = time.time()
    while b.read() is None:
        if time.time() - t0 > 90 or game.poll() is not None:
            raise SystemExit(f"no telemetry from Luanti (see {TB / 'luanti.log'})")
        time.sleep(0.2)
    print("telemetry flowing after", round(time.time() - t0, 1), "s", flush=True)
    stats = {"hits": 0, "hp_min": 20, "goals": {}}
    last_attack, stuck_since, last_dig_target = 0.0, None, None
    banned, target_since = set(), 0.0  # trunk blocks it couldn't get onto: tried for 5 s
    pick_key, pick_since, bad_drops = None, 0.0, set()  # drops it couldn't reach: tried for 4 s
    start = time.time()
    wood0 = None
    while time.time() - start < seconds and game.poll() is None:
        s = b.read()
        now = time.time()
        eye = (s["x"], s["y"] + 1.5, s["z"])
        wood = sum(n for k, n in (s.get("inv") or {}).items() if "tree" in k or "log" in k)
        wood0 = wood if wood0 is None else wood0
        stats["hp_min"] = min(stats["hp_min"], s["hp"])
        mobs = [m for m in (s.get("mobs") or []) if any(h in m[0] for h in HOSTILE)]
        mobs.sort(key=lambda m: math.dist((m[1], m[3]), (s["x"], s["z"])))
        cmd = {"move": [0.0, 0.0]}
        speed = math.hypot(s["vx"], s["vz"])
        if mobs and math.dist((mobs[0][1], mobs[0][3]), (s["x"], s["z"])) < 6:
            goal = "fight " + mobs[0][0].split(":")[-1]
            m = mobs[0]
            yaw, pitch = aim(eye, (m[1], m[2] + 1.0, m[3]))
            cmd.update(yaw=yaw, pitch=pitch)
            d = math.dist((m[1], m[3]), (s["x"], s["z"]))
            if d > 2.2:
                cmd["move"] = [-math.sin(math.radians(yaw)) * WALK, math.cos(math.radians(yaw)) * WALK]
            if now - last_attack > 0.6 and d < 3.5:
                cmd = b.one_shot(**cmd, attack=True)
                last_attack = now
                stats["hits"] += 1
        else:
            trees = [tuple(t) for t in (s.get("trees") or []) if -1 <= t[1] - math.floor(s["y"]) <= 3
                     and tuple(t) not in banned]
            logs = [q for q in (s.get("drops") or []) if ("tree" in q[0] or "log" in q[0])
                    and (round(q[1]), round(q[3])) not in bad_drops]
            logs.sort(key=lambda q: math.dist((q[1], q[3]), (s["x"], s["z"])))
            look = s.get("look")
            on_it = look is not None and ("tree" in look[3] or "log" in look[3])
            if logs and math.dist((logs[0][1], logs[0][3]), (s["x"], s["z"])) < 6 and not on_it:
                goal = "pick up log"  # walk onto it: items are picked up within about a block
                q = logs[0]
                key = (round(q[1]), round(q[3]))
                if key != pick_key:
                    pick_key, pick_since = key, now
                elif now - pick_since > 4.0:
                    bad_drops.add(key)
                yaw, _ = aim(eye, (q[1], eye[1], q[3]))
                cmd.update(yaw=yaw, pitch=20.0, move=[-math.sin(math.radians(yaw)) * WALK, math.cos(math.radians(yaw)) * WALK])
            elif trees:
                if on_it:  # keep the aim on the block in the crosshair until it breaks
                    goal = "chop wood"
                    target_since = now
                    cmd.update(yaw=s["yaw"], pitch=s["pitch"], dig=True)
                    stuck_since = None
                else:
                    if last_dig_target in trees and now - target_since > 5.0:
                        banned.add(last_dig_target)
                        trees.remove(last_dig_target)
                    if not trees:
                        b.send(**cmd)
                        time.sleep(0.05)
                        continue
                    if last_dig_target not in trees:  # the nearest column, its lowest reachable block
                        target_since = now
                        near = min(trees, key=lambda t: math.dist((t[0] + 0.5, t[2] + 0.5), (s["x"], s["z"])))
                        last_dig_target = min((t for t in trees if (t[0], t[2]) == (near[0], near[2])), key=lambda t: t[1])
                    t = last_dig_target
                    center = (t[0] + 0.5, t[1] + 0.5, t[2] + 0.5)
                    yaw, pitch = aim(eye, center)
                    cmd.update(yaw=yaw, pitch=pitch)
                    d = math.dist((center[0], center[2]), (s["x"], s["z"]))
                    goal = "go to tree"
                    if d > 2.2:
                        cmd["move"] = [-math.sin(math.radians(yaw)) * WALK, math.cos(math.radians(yaw)) * WALK]
                        stuck_since = stuck_since or now
                        if speed > 1.0:
                            stuck_since = now
                        cmd["jump"] = now - stuck_since > 0.35
                    elif look is not None and "leaves" in look[3]:
                        cmd["dig"] = True  # clear leaves in the way
            else:
                drops = sorted((s.get("drops") or []), key=lambda q: math.dist((q[1], q[3]), (s["x"], s["z"])))
                if drops and math.dist((drops[0][1], drops[0][3]), (s["x"], s["z"])) < 8:
                    goal = "pick up " + drops[0][0].split(" ")[0].split(":")[-1]
                    yaw, _ = aim(eye, (drops[0][1], eye[1], drops[0][3]))
                else:
                    goal = "explore"
                    if "explore_yaw" not in stats or (stuck_since and now - stuck_since > 2.0):
                        stats["explore_yaw"] = (stats.get("explore_yaw", s["yaw"]) + 100) % 360  # straight lines; turn when blocked
                        stuck_since = now
                    yaw = stats["explore_yaw"]
                cmd.update(yaw=yaw, pitch=0.0, move=[-math.sin(math.radians(yaw)) * WALK, math.cos(math.radians(yaw)) * WALK])
                stuck_since = stuck_since or now
                if speed > 1.0:
                    stuck_since = now
                cmd["jump"] = now - stuck_since > 0.35
        stats["goals"][goal.split(" ")[0]] = stats["goals"].get(goal.split(" ")[0], 0) + 1
        b.send(**cmd)
        time.sleep(0.05)
    b.send(move=[0.0, 0.0])
    s = b.read()
    wood = sum(n for k, n in (s.get("inv") or {}).items() if "tree" in k or "log" in k)
    game.terminate()
    total = sum(stats["goals"].values()) or 1
    print(json.dumps({"seconds": seconds, "wood": wood - (wood0 or 0), "inventory": s.get("inv"), "hp_end": s["hp"],
                      "hp_min": stats["hp_min"], "attacks": stats["hits"], "telemetry_ticks": s["tick"],
                      "time_by_goal": {k: round(v / total, 2) for k, v in stats["goals"].items()}}), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--keep-world", action="store_true", help="carry on in the last run's world")
    a = ap.parse_args()
    run(a.seconds, a.keep_world)
