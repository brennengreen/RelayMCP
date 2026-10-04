"""A real game on this Mac for the decision layer: Doom (ViZDoom, Freedoom assets, the deathmatch arena) runs in real
time at 35 tics/s whatever the agent does (async player, no window, no OS input). Every tic, a skill steers the player
(the reflex layer: fight, retreat, collect, explore); a tactics policy picks the skill: hand-written rules, the same
intent compiled from plain English by a local model, a local model scoring the answers (in the background, its latest
choice used), or no tactics at all. Scores come from the game: kills, survival, damage taken.

    pip install vizdoom    # its wheels carry the game and the free Freedoom assets (macOS arm64, Linux, Windows)
    python scripts/decide/doom.py --policy explore|fight|rules|compiled|scorer [--episodes 6] [--seconds 60]
        [--seed 2] [--gif out.gif]    # compiled needs Ollama (qwen3.5:9b), scorer needs mlx-lm
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

import numpy as np
import vizdoom as vzd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from relaymcp.host import decide  # noqa: E402

MONSTERS = {"Zombieman", "ShotgunGuy", "ChaingunGuy", "DoomImp", "Demon", "Spectre", "LostSoul", "Cacodemon",
            "HellKnight", "BaronOfHell", "Arachnotron", "PainElemental", "Revenant", "Fatso", "Archvile",
            "SpiderMastermind", "Cyberdemon", "WolfensteinSS"}
def is_monster(name):
    """Doom's monsters, and ViZDoom's own marines (MarineChainsawVzd ...): missing them made the tactics blind to them."""
    return name in MONSTERS or (name.startswith("Marine") and name.endswith("Vzd"))


KIND = {**{n: "health" for n in ("Medikit", "Stimpack", "HealthBonus")},
        **{n: "ammo" for n in ("ClipBox", "ShellBox", "RocketBox", "CellPack", "Clip", "Shell", "RocketAmmo", "Cell")},
        **{n: "weapon" for n in ("Chainsaw", "Shotgun", "SuperShotgun", "Chaingun", "RocketLauncher", "PlasmaRifle",
                                 "BFG9000")},
        **{n: "armor" for n in ("GreenArmor", "BlueArmor", "ArmorBonus")}}

INTENT = """Distances are in map units (the player is 56 tall). Monsters attack, some from far away; items are picked up by
walking over them.
1. If health is below 40 and a visible monster is within 1000 units, retreat.
2. Otherwise, if health is below 70 and a health item is within 900 units, collect.
3. Otherwise, if a visible monster is within 1000 units and ammo is above 0, fight.
4. Otherwise, if ammo is below 20 and an ammo item or a weapon is within 900 units, collect.
5. Otherwise, if any item is within 400 units, collect.
6. Otherwise, explore."""

QUESTION = decide.Question.make(
    "Which skill should the player run now?",
    {"fight": "turn to the nearest visible monster and shoot", "retreat": "back away from monsters while facing them",
     "collect": "go and pick up the most needed nearby item", "explore": "roam the map looking for monsters and items"},
    INTENT)


def oracle(st):
    near = lambda things, d, f=lambda t: True: any(t["distance"] <= d and f(t) for t in things)  # noqa: E731
    if st["health"] < 40 and near(st["monsters"], 1000, lambda t: t["visible"]):
        return "retreat"
    if st["health"] < 70 and near(st["items"], 900, lambda t: t["kind"] == "health"):
        return "collect"
    if st["ammo"] > 0 and near(st["monsters"], 1000, lambda t: t["visible"]):
        return "fight"
    if st["ammo"] < 20 and near(st["items"], 900, lambda t: t["kind"] in ("ammo", "weapon")):
        return "collect"
    if near(st["items"], 400):
        return "collect"
    return "explore"


def fight_anything(st):
    return "fight" if any(m["visible"] for m in st["monsters"]) else "explore"


def facts(st):
    """The state as short sentences, for a model scoring the answers."""
    vis = [m for m in st["monsters"] if m["visible"]]
    def first(ts):
        return f"{ts[0]['name']} at {ts[0]['distance']}" if ts else "none"
    health = [i for i in st["items"] if i["kind"] == "health"]
    ammo = [i for i in st["items"] if i["kind"] in ("ammo", "weapon")]
    return (f"Health: {st['health']}. Ammo: {st['ammo']}.\nNearest monster: {first(st['monsters'])}.\n"
            f"Nearest visible monster: {first(vis)}.\nNearest health item: {first(health)}.\n"
            f"Nearest ammo or weapon: {first(ammo)}.\nNearest item: {first(st['items'])}.")


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


class Doom:
    BUTTONS = [vzd.Button.ATTACK, vzd.Button.SPEED, vzd.Button.MOVE_FORWARD, vzd.Button.MOVE_BACKWARD,
               vzd.Button.MOVE_LEFT, vzd.Button.MOVE_RIGHT, vzd.Button.TURN_LEFT_RIGHT_DELTA]
    VARS = [vzd.GameVariable.HEALTH, vzd.GameVariable.SELECTED_WEAPON_AMMO, vzd.GameVariable.KILLCOUNT,
            vzd.GameVariable.POSITION_X, vzd.GameVariable.POSITION_Y, vzd.GameVariable.ANGLE,
            vzd.GameVariable.DAMAGE_TAKEN, vzd.GameVariable.DEAD, vzd.GameVariable.DAMAGECOUNT,
            vzd.GameVariable.HITCOUNT]

    def __init__(self, seconds, seed):
        g = self.g = vzd.DoomGame()
        g.load_config(os.path.join(vzd.scenarios_path, "deathmatch.cfg"))
        g.set_window_visible(False)
        g.set_mode(vzd.Mode.ASYNC_PLAYER)
        g.set_objects_info_enabled(True)
        g.set_labels_buffer_enabled(True)
        g.set_screen_resolution(vzd.ScreenResolution.RES_320X240)
        g.set_available_buttons(self.BUTTONS)
        g.set_available_game_variables(self.VARS)
        g.set_episode_timeout(int(seconds * 35))
        g.set_seed(seed)
        g.init()
        self.turn_sign = 1.0
        self.dead_ids: set = set()

    def new_episode(self):
        self.g.new_episode()
        self.dead_ids.clear()
        self.target = None
        self.stuck_t, self.last_pos, self.escape = 0, None, 0

    def calibrate(self):
        a0 = self.g.get_state().game_variables[5]
        self.g.make_action([0, 0, 0, 0, 0, 0, 10.0])
        a1 = self.g.get_state().game_variables[5]
        self.turn_sign = 1.0 if wrap(a1 - a0) > 0 else -1.0  # +delta turns left (counterclockwise) when 1

    def read(self, s):
        hp, ammo, kills, x, y, ang, dmg, dead, dealt, hits = s.game_variables
        visible = {lb.object_id for lb in s.labels}
        monsters, items = [], []
        for o in s.objects:
            dx, dy = o.position_x - x, o.position_y - y
            dist = math.hypot(dx, dy)
            if dist < 1 or dist > 2500:
                continue
            bearing = wrap(math.degrees(math.atan2(dy, dx)) - ang)  # left +
            entry = {"name": o.name, "distance": round(dist), "bearing": round(bearing, 1), "visible": o.id in visible,
                     "id": o.id}
            if is_monster(o.name) and o.id not in self.dead_ids:
                monsters.append(entry)
            elif o.name in KIND:
                items.append({**entry, "kind": KIND[o.name]})
        monsters.sort(key=lambda t: t["distance"])
        items.sort(key=lambda t: t["distance"])
        return {"health": int(hp), "ammo": int(ammo), "kills": int(kills), "x": x, "y": y, "dead": bool(dead),
                "damage": float(dmg), "monsters": monsters[:6], "items": items[:8]}

    def turn(self, bearing, gain=0.45, cap=12.0):
        return self.turn_sign * max(-cap, min(cap, bearing * gain))

    def act(self, skill, st, tic):
        """The reflex layer: one tic of a skill -> [attack, speed, fwd, back, left, right, turn]."""
        a = [0, 0, 0, 0, 0, 0, 0.0]
        pos = (st["x"], st["y"])
        moved = self.last_pos is None or math.hypot(pos[0] - self.last_pos[0], pos[1] - self.last_pos[1]) > 3
        self.last_pos = pos
        self.stuck_t = 0 if moved else self.stuck_t + 1
        if self.escape > 0:  # unstick: turn away and push on
            self.escape -= 1
            a[6] = self.turn_sign * 9.0
            a[2] = a[1] = 1
            return a
        vis = [m for m in st["monsters"] if m["visible"]]
        strafe = 4 if (tic // 25) % 2 else 5
        if skill == "fight" and vis:
            m = vis[0]
            self.target = m["id"]
            a[6] = self.turn(m["bearing"])
            a[0] = int(abs(m["bearing"]) < 4)
            a[strafe] = a[1] = 1  # never a standing target
            if m["distance"] < 250:
                a[3] = 1
            elif m["distance"] > 600:
                a[2] = 1
        elif skill == "retreat" and st["monsters"]:
            m = (vis or st["monsters"])[0]
            a[6] = self.turn(m["bearing"])
            a[3] = a[1] = 1
            a[strafe] = 1
            a[0] = int(m["visible"] and abs(m["bearing"]) < 4 and st["ammo"] > 0)
        elif skill == "collect" and st["items"]:
            want = ("health",) if st["health"] < 60 else (("ammo", "weapon") if st["ammo"] < 15 else ())
            pool = [i for i in st["items"] if i["kind"] in want] or st["items"]
            it = pool[0]
            a[6] = self.turn(it["bearing"], gain=0.6)
            a[2] = a[1] = int(abs(it["bearing"]) < 35)
        else:  # explore
            a[2] = a[1] = 1
            a[6] = self.turn_sign * (2.0 if (tic // 70) % 2 else -2.0)
        if self.stuck_t > 20 and (a[2] or a[3]):
            self.escape, self.stuck_t = 18, 0
        return a

    def credit_kill(self, st):
        """Corpses stay as objects: when the kill count rises, the monster in the crosshair (or the target) died."""
        near = [m for m in st["monsters"] if m["visible"] and abs(m["bearing"]) < 12]
        dead = near[0]["id"] if near else self.target
        if dead is not None:
            self.dead_ids.add(dead)


class Tactics:
    """The policy picks the skill. Rules and compiled policies answer inline (microseconds); a model scoring the
    answers runs in the background on the latest state, and the loop uses its latest choice."""

    def __init__(self, kind, compile_model):
        self.kind, self.choice, self.lat, self.n = kind, "explore", [], 0
        self.agree, self.checked = 0, 0  # the choice in use vs the rules, on every tic's real state
        self.pending = None
        if kind == "rules":
            self.backend = decide.Rules(oracle)
        elif kind == "fight":
            self.backend = decide.Rules(fight_anything, "fight-anything")
        elif kind == "compiled":
            rng = random.Random(5)
            samples = [sample_state(rng) for _ in range(300)]
            self.backend = decide.compile_policy(QUESTION, samples, decide.ollama_generate(compile_model))
            self.compile_s = self.backend.compile_ms / 1000
            agree = sum(self.backend.fn(s) == oracle(s) for s in samples) / len(samples)
            print(f"compiled the intent with {compile_model} in {self.compile_s:.1f} s; agrees with the rules on "
                  f"{agree:.0%} of 300 sample states", flush=True)
        elif kind == "scorer":
            self.backend = decide.MLXDecider("mlx-community/Qwen3.5-2B-MLX-4bit")
            self.backend.decide(QUESTION, facts(sample_state(random.Random(1))))  # prepare and warm
            self.latest_state, self.stop = None, threading.Event()
            threading.Thread(target=self._background, daemon=True).start()

    def _background(self):
        while not self.stop.is_set():
            st = self.latest_state
            if st is None:
                time.sleep(0.005)
                continue
            d = self.backend.decide(QUESTION, facts(st))
            self.choice = d.choice
            self.lat.append(d.ms)
            self.n += 1

    def pick(self, st):
        if self.kind == "explore":
            return "explore"
        if self.kind == "scorer":
            self.latest_state = st
        else:
            t = time.perf_counter()
            self.choice = self.backend.decide(QUESTION, st).choice
            self.lat.append((time.perf_counter() - t) * 1000)
            self.n += 1
        self.checked += 1
        self.agree += self.choice == oracle(st)
        return self.choice


def sample_state(rng):
    mons = [{"name": rng.choice(sorted(MONSTERS)), "distance": rng.randint(50, 2400), "bearing": rng.uniform(-180, 180),
             "visible": rng.random() < 0.6, "id": i} for i in range(rng.randint(0, 4))]
    names = sorted(KIND)
    items = [{"name": n, "kind": KIND[n], "distance": rng.randint(30, 2400), "bearing": rng.uniform(-180, 180),
              "visible": rng.random() < 0.5, "id": 100 + i} for i, n in enumerate(rng.sample(names, rng.randint(0, 5)))]
    return {"health": rng.randint(1, 100), "ammo": rng.randint(0, 60), "kills": 0, "x": 0.0, "y": 0.0, "dead": False,
            "damage": 0.0, "monsters": sorted(mons, key=lambda t: t["distance"]),
            "items": sorted(items, key=lambda t: t["distance"])}


def episode(doom, tactics, gif_frames=None):
    doom.new_episode()
    doom.calibrate()
    tic, kills, late, skills, last_tic = 0, 0, 0, {}, None
    t0 = time.perf_counter()
    while not doom.g.is_episode_finished():
        s = doom.g.get_state()
        if s is None:
            break
        if last_tic is not None and s.tic - last_tic > 1:
            late += s.tic - last_tic - 1  # tics the loop missed (it fell behind the game)
        last_tic = s.tic
        st = doom.read(s)
        if st["kills"] > kills:
            doom.credit_kill(st)
            kills = st["kills"]
        skill = tactics.pick(st)
        skills[skill] = skills.get(skill, 0) + 1
        if gif_frames is not None and tic % 3 == 0:
            gif_frames.append((s.screen_buffer.copy(), skill, st["health"], kills))
        doom.g.make_action(doom.act(skill, st, tic))
        tic += 1
    gv = doom.g.get_game_variable
    return {"seconds": round(time.perf_counter() - t0, 1), "kills": int(gv(vzd.GameVariable.KILLCOUNT)),
            "died": bool(doom.g.is_player_dead()), "damage": int(gv(vzd.GameVariable.DAMAGE_TAKEN)),
            "dealt": int(gv(vzd.GameVariable.DAMAGECOUNT)), "hits": int(gv(vzd.GameVariable.HITCOUNT)),
            "tics": tic, "missed_tics": late, "skills": {k: round(v / max(1, tic), 2) for k, v in skills.items()}}


def save_gif(frames, path):
    from PIL import Image, ImageDraw
    imgs = []
    for buf, skill, hp, kills in frames:
        im = Image.fromarray(np.transpose(buf, (1, 2, 0))).resize((240, 180))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 240, 13], fill=(0, 0, 0))
        d.text((3, 1), f"{skill:8s} hp {hp:3d} kills {kills}", fill=(255, 255, 0))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=86, loop=0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", choices=["explore", "fight", "rules", "compiled", "scorer"], required=True)
    ap.add_argument("--episodes", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--gif")
    ap.add_argument("--compile-model", default="qwen3.5:9b")
    a = ap.parse_args()
    tactics = Tactics(a.policy, a.compile_model)
    doom = Doom(a.seconds, a.seed)
    results = []
    for ep in range(a.episodes):
        frames = [] if (a.gif and ep == 0) else None
        r = episode(doom, tactics, frames)
        results.append(r)
        print(json.dumps({"policy": a.policy, "episode": ep, **r}), flush=True)
        if frames:
            save_gif(frames, a.gif)
    doom.g.close()
    lat = sorted(tactics.lat)
    summary = {"policy": a.policy, "episodes": len(results),
               "kills_mean": round(sum(r["kills"] for r in results) / len(results), 2),
               "deaths": sum(r["died"] for r in results),
               "damage_mean": round(sum(r["damage"] for r in results) / len(results)),
               "dealt_mean": round(sum(r["dealt"] for r in results) / len(results)),
               "hits_mean": round(sum(r["hits"] for r in results) / len(results), 1),
               "missed_tics": sum(r["missed_tics"] for r in results), "decisions": tactics.n,
               "decision_ms_p50": round(lat[len(lat) // 2], 3) if lat else None,
               "decision_ms_p95": round(lat[int(len(lat) * 0.95)], 3) if lat else None,
               "live_agreement_with_rules": round(tactics.agree / tactics.checked, 3) if tactics.checked else None,
               "survived_s_mean": round(sum(r["tics"] for r in results) / len(results) / 35, 1)}
    print("SUMMARY " + json.dumps(summary), flush=True)
