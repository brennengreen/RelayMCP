"""Let a model play: Luanti + Mineclonia on this Mac. A thinking model (local, Ollama) gets a situation report every few
seconds, picks the next skill and says why in the game chat; skills carry it out at 20 Hz; reflexes (fight or flee a
mob that's close) never wait for the model. The goal it's given: survive the day and the night.

    python3 scripts/luanti/play.py [--minutes 6] [--model qwen3.5:9b] [--keep-world]
"""
import argparse
import json
import math
import os
import subprocess
import threading
import time
import urllib.request

from agent import HOSTILE, LUANTI, TB, WALK, WORLD, Bridge, aim

DAY_S = 86400 / 288  # time_speed 288 (scripts/luanti/setup.sh): a day every 5 minutes
SKILLS = {
    "chop_wood": "walk to the nearest tree, chop a log and pick it up",
    "explore": "walk in a straight line for a while (find trees, get away from danger)",
    "fight": "attack the nearest hostile mob",
    "flee": "run away from the nearest hostile mob",
    "dig_in": "dig a 2-deep hole where you stand and seal the top with a block: a shelter for the night",
    "wait": "stay still for a few seconds (e.g. inside a shelter until morning)",
}
SYSTEM = ("You are playing Mineclonia, a Minecraft clone, through a few skills. Each time, pick the one skill to run next "
          "and say why in a few words, as JSON: {\"skill\": ..., \"say\": ...}. Skills: " +
          "; ".join(f"{k}: {v}" for k, v in SKILLS.items()) + ". Hostile mobs come out at night and in the dark.")


def fwd(yaw, speed=WALK):
    return [-math.sin(math.radians(yaw)) * speed, math.cos(math.radians(yaw)) * speed]


class Player:
    def __init__(self, b):
        self.b, self.skill, self.started, self.mem, self.result = b, "explore", time.time(), {}, "just spawned"
        self.sheltered, self.banned, self.bad_drops = False, set(), set()

    def hostiles(self, s):
        out = [m for m in (s.get("mobs") or []) if any(h in m[0] for h in HOSTILE)]
        return sorted(out, key=lambda m: math.dist((m[1], m[3]), (s["x"], s["z"])))

    def wood(self, s):
        return sum(n for k, n in (s.get("inv") or {}).items() if "tree" in k or "log" in k)

    def start(self, skill, s):
        self.skill, self.started, self.mem = skill, time.time(), {"wood0": self.wood(s), "y0": s["y"], "phase": 0}
        if skill != "wait" and skill != "dig_in":
            self.sheltered = False

    def step(self, s, now):
        """One tic of the current skill -> (controls, finished?)."""
        k, m, t = self.skill, self.mem, now - self.started
        eye = (s["x"], s["y"] + 1.5, s["z"])
        speed = math.hypot(s["vx"], s["vz"])
        mobs = self.hostiles(s)
        if k == "fight":
            if not mobs or math.dist((mobs[0][1], mobs[0][3]), (s["x"], s["z"])) > 10 or t > 12:
                return {}, "no mob left in reach" if not mobs else "gave up"
            mb = mobs[0]
            yaw, pitch = aim(eye, (mb[1], mb[2] + 1.0, mb[3]))
            d = math.dist((mb[1], mb[3]), (s["x"], s["z"]))
            cmd = {"yaw": yaw, "pitch": pitch, "move": fwd(yaw) if d > 2.2 else [0, 0]}
            if now - m.get("hit", 0) > 0.6 and d < 3.5:
                m["hit"] = now
                cmd = self.b.one_shot(**cmd, attack=True)
            return cmd, None
        if k == "flee":
            if not mobs or t > 6:
                return {}, "got away" if not mobs else "ran for 6 s"
            yaw, _ = aim(eye, (mobs[0][1], eye[1], mobs[0][3]))
            return {"yaw": (yaw + 180) % 360, "pitch": 0, "move": fwd((yaw + 180) % 360), "jump": speed < 1.5}, None
        if k == "explore":
            if "yaw" not in m or now - m.get("moving", now) > 2.0:
                m["yaw"] = (m.get("yaw", s["yaw"]) + 100) % 360
                m["moving"] = now
            if speed > 1.0:
                m["moving"] = now
            if t > 8:
                return {}, f"walked {t:.0f} s"
            return {"yaw": m["yaw"], "pitch": 0, "move": fwd(m["yaw"]), "jump": now - m["moving"] > 0.35}, None
        if k == "wait":
            return ({"move": [0, 0]}, "waited") if t > 6 else ({"move": [0, 0]}, None)
        if k == "dig_in":
            if m["phase"] < 2:  # dig the block under the feet, twice
                if s["y"] < m["y0"] - 0.9 * (m["phase"] + 1):
                    m["phase"] += 1
                if t > 20:
                    return {}, "couldn't dig down"
                return {"pitch": 89.0, "yaw": s["yaw"], "move": [0, 0], "dig": True}, None
            blocks = [h for h in (s.get("hotbar") or []) if any(w in h[1] for w in ("dirt", "tree", "log", "stone", "planks", "sand"))]
            if not blocks:
                return {}, "dug in, but no block to seal the top"
            if m["phase"] == 2:
                m["phase"] = 3
                return self.b.one_shot(move=[0, 0], slot=blocks[0][0]), None
            self.sheltered = True
            return self.b.one_shot(move=[0, 0], seal=True), "dug in and sealed the top"
        # chop_wood
        if self.wood(s) > m["wood0"]:
            return {}, "got a log"
        if t > 30:
            return {}, "found no reachable tree in 30 s"
        trees = [tuple(x) for x in (s.get("trees") or []) if -1 <= x[1] - math.floor(s["y"]) <= 3 and tuple(x) not in self.banned]
        logs = sorted((q for q in (s.get("drops") or []) if ("tree" in q[0] or "log" in q[0])
                       and (round(q[1]), round(q[3])) not in self.bad_drops),
                      key=lambda q: math.dist((q[1], q[3]), (s["x"], s["z"])))
        look = s.get("look")
        on_it = look is not None and ("tree" in look[3] or "log" in look[3])
        if logs and math.dist((logs[0][1], logs[0][3]), (s["x"], s["z"])) < 6 and not on_it:
            key = (round(logs[0][1]), round(logs[0][3]))
            if m.get("pick") != key:
                m["pick"], m["pick_t"] = key, now
            elif now - m["pick_t"] > 4:
                self.bad_drops.add(key)
            yaw, _ = aim(eye, (logs[0][1], eye[1], logs[0][3]))
            return {"yaw": yaw, "pitch": 20.0, "move": fwd(yaw)}, None
        if on_it:
            m["target_t"] = now
            return {"yaw": s["yaw"], "pitch": s["pitch"], "dig": True, "move": [0, 0]}, None
        if not trees:
            return {}, "no tree nearby"
        tgt = m.get("target")
        if tgt in trees and now - m.get("target_t", now) > 5:
            self.banned.add(tgt)
            trees.remove(tgt)
            tgt = None
        if tgt not in trees:
            if not trees:
                return {}, "no reachable tree"
            near = min(trees, key=lambda x: math.dist((x[0] + 0.5, x[2] + 0.5), (s["x"], s["z"])))
            tgt = min((x for x in trees if (x[0], x[2]) == (near[0], near[2])), key=lambda x: x[1])
            m["target"], m["target_t"] = tgt, now
        c = (tgt[0] + 0.5, tgt[1] + 0.5, tgt[2] + 0.5)
        yaw, pitch = aim(eye, c)
        cmd = {"yaw": yaw, "pitch": pitch, "move": [0, 0]}
        if math.dist((c[0], c[2]), (s["x"], s["z"])) > 2.2:
            m.setdefault("moving", now)
            if speed > 1.0:
                m["moving"] = now
            cmd.update(move=fwd(yaw), jump=now - m["moving"] > 0.35)
        elif look is not None and "leaves" in look[3]:
            cmd["dig"] = True
        return cmd, None


def report(p, s):
    tod = s["tod"]
    night = tod < 0.21 or tod > 0.79
    nxt = ((0.79 - tod) % 1.0) if not night else ((0.21 - tod) % 1.0)
    mobs = p.hostiles(s)
    trees = s.get("trees") or []
    inv = ", ".join(f"{n} {k.split(':')[-1]}" for k, n in (s.get("inv") or {}).items()) or "nothing"
    return (f"Time: {'NIGHT' if night else 'day'}, {'morning' if night else 'night'} in about {nxt * DAY_S:.0f} s.\n"
            f"Health: {s['hp']}/20. Deaths so far: {s.get('deaths', 0)}. Sheltered: {'yes' if p.sheltered else 'no'}.\n"
            f"Inventory: {inv}.\n"
            f"Hostile mobs near: " + (", ".join(f"{m[0].split(':')[-1]} {math.dist((m[1], m[3]), (s['x'], s['z'])):.0f} m"
                                            for m in mobs[:4]) or "none") + ".\n"
            f"Trees within 32 m: {len(trees)}.\n"
            f"Last skill: {p.skill} -> {p.result}.\n"
            "Goal: survive this day and the night without dying. What next?")


def think(model, text):
    body = {"model": model, "stream": False, "think": False, "options": {"temperature": 0.2},
            "format": {"type": "object", "properties": {"skill": {"type": "string", "enum": list(SKILLS)},
                                                         "say": {"type": "string"}}, "required": ["skill", "say"]},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]}
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(json.loads(r.read())["message"]["content"])


def main(minutes, model, keep_world):
    for f in ("relay_telemetry.jsonl", "relay_cmd.json"):
        (WORLD / f).unlink(missing_ok=True)
    if not keep_world:
        for f in ("map.sqlite", "players.sqlite", "env_meta.txt", "mod_storage.sqlite", "auth.sqlite"):
            (WORLD / f).unlink(missing_ok=True)
    env = {**os.environ, "MINETEST_USER_PATH": str(TB), "LUANTI_USER_PATH": str(TB)}
    game = subprocess.Popen([LUANTI, "--config", str(TB / "relay.conf"), "--go", "--world", str(WORLD)],
                            env=env, stdout=open(TB / "luanti.log", "w"), stderr=subprocess.STDOUT)
    b = Bridge()
    while b.read() is None:
        time.sleep(0.2)
    p = Player(b)
    log = open(TB / "play_log.jsonl", "w")
    want = {"skill": None, "say": None, "busy": False, "lat": []}

    def ask(text):
        t0 = time.time()
        try:
            d = think(model, text)
            want.update(skill=d.get("skill"), say=str(d.get("say", ""))[:80])
        except Exception as e:  # noqa: BLE001
            want.update(skill="wait", say=f"(model error: {type(e).__name__})")
        want["lat"].append(time.time() - t0)
        want["busy"] = False

    start, last_ask, reflex, pending_say = time.time(), 0.0, None, None
    nights, was_night, deaths0 = 0, False, None
    last_tick, tick_at = None, time.time()
    while time.time() - start < minutes * 60 and game.poll() is None:
        s = b.read()
        now = time.time()
        if s["tick"] != last_tick:
            last_tick, tick_at = s["tick"], now
        elif now - tick_at > 5:
            print(f"the game stopped sending telemetry at {now - start:.0f} s (see {TB / 'luanti.log'})", flush=True)
            break
        deaths0 = s.get("deaths", 0) if deaths0 is None else deaths0
        night = s["tod"] < 0.21 or s["tod"] > 0.79
        if night and not was_night:
            nights += 1
        was_night = night
        mobs = p.hostiles(s)
        close = mobs and math.dist((mobs[0][1], mobs[0][3]), (s["x"], s["z"])) < 3.0 and not p.sheltered
        if close and p.skill not in ("fight", "flee"):  # reflex: never wait for the model with a mob at arm's length
            reflex = "fight" if s["hp"] > 8 else "flee"
            p.start(reflex, s)
        if want["skill"] and not want["busy"]:
            if want["say"]:
                pending_say = f"{want['skill']}: {want['say']}"  # sent with this tic's controls
                log.write(json.dumps({"t": round(now - start, 1), "skill": want["skill"], "say": want["say"],
                                      "hp": s["hp"], "tod": round(s["tod"], 3)}) + "\n")
                log.flush()
                print(f"{now - start:5.0f}s  {want['skill']:9s} {want['say']}", flush=True)
            if p.skill not in ("fight", "flee") or not close:
                p.start(want["skill"], s)
            want.update(skill=None, say=None)
        cmd, done = p.step(s, now)
        if done:
            p.result = done
        if (done or now - last_ask > 6) and not want["busy"]:
            want["busy"], last_ask = True, now
            threading.Thread(target=ask, args=(report(p, s),), daemon=True).start()
            if done:
                p.start("wait", s)  # until the model answers
        if pending_say:
            cmd, pending_say = b.one_shot(**cmd, say=pending_say), None
        b.send(**{"move": [0, 0], **cmd})
        time.sleep(0.05)
    s = b.read()
    game.terminate()
    lat = sorted(want["lat"])
    print(json.dumps({"minutes": minutes, "deaths": s.get("deaths", 0) - (deaths0 or 0), "hp_end": s["hp"],
                      "nights_seen": nights, "decisions": len(lat),
                      "model_s_p50": round(lat[len(lat) // 2], 1) if lat else None,
                      "inventory": s.get("inv")}), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=6)
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--keep-world", action="store_true")
    a = ap.parse_args()
    main(a.minutes, a.model, a.keep_world)
