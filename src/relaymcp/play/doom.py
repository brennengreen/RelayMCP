"""A real game on this Mac for the decision layer: Doom (ViZDoom, Freedoom assets, the deathmatch arena) runs in real
time at 35 tics/s whatever the agent does (async player, no window, no OS input). Every tic, a skill steers the player
(the reflex layer: fight, retreat, collect, explore); a tactics policy picks the skill: hand-written rules, the same
intent compiled from plain English by a local model, a local model scoring the answers (in the background, its latest
choice used), or no tactics at all. Scores come from the game: kills, survival, damage taken.

    pip install "relaymcp[play]"    # vizdoom's wheels carry the game and the free Freedoom assets
    relaymcp play doom              # the planner plays a minute in a window
    relaymcp play doom --policy explore|fight|rules|compiled|scorer|planner --episodes 6 --headless --gif out.gif
                                    # compiled needs Ollama (qwen3.5:9b), scorer needs mlx-lm
"""
import argparse
import json
import math
import os
import random
import signal
import struct
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import vizdoom as vzd

from relaymcp.host import decide

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


@lru_cache(maxsize=1)
def wad():
    """Freedoom 2 (in ViZDoom's wheels): its bytes and its lumps (name, offset, size), in order."""
    b = Path(os.path.dirname(vzd.__file__), "freedoom2.wad").read_bytes()
    n, off = struct.unpack("<ii", b[4:12])
    return b, [(x[2].rstrip(b"\0").decode("latin1"), x[0], x[1])
               for x in (struct.unpack_from("<ii8s", b, off + 16 * i) for i in range(n))]


def map_data(level):
    """A map's lumps by name (THINGS, LINEDEFS, SIDEDEFS, VERTEXES, SECTORS ...)."""
    b, lumps = wad()
    i = [name for name, _, _ in lumps].index(level.upper())
    return {name: b[p:p + s] for name, p, s in lumps[i + 1:i + 11]}


def layout(level):
    """The level's layout as ViZDoom's sectors (floor, ceiling, lines), read from the map: ViZDoom's own sectors
    stream crashes on bigger maps. Heights are as the level starts (doors shut)."""
    d = map_data(level)
    verts = [struct.unpack_from("<hh", d["VERTEXES"], j) for j in range(0, len(d["VERTEXES"]), 4)]
    side = [struct.unpack_from("<h", d["SIDEDEFS"], j + 28)[0] for j in range(0, len(d["SIDEDEFS"]), 30)]
    secs = [SimpleNamespace(floor_height=f, ceiling_height=c, lines=[])
            for f, c in (struct.unpack_from("<hh", d["SECTORS"], j) for j in range(0, len(d["SECTORS"]), 26))]
    for j in range(0, len(d["LINEDEFS"]), 14):
        v1, v2, flags, _, _, front, back = struct.unpack_from("<HHhhhHH", d["LINEDEFS"], j)
        ln = SimpleNamespace(x1=verts[v1][0], y1=verts[v1][1], x2=verts[v2][0], y2=verts[v2][1],
                             is_blocking=bool(flags & 1) or back == 0xFFFF)
        for sd in {front, back} - {0xFFFF}:
            secs[side[sd]].lines.append(ln)
    return secs


class Doom:
    """An action is [attack, speed, forward, back, left, right, turn (degrees), use]."""
    BUTTONS = [vzd.Button.ATTACK, vzd.Button.SPEED, vzd.Button.MOVE_FORWARD, vzd.Button.MOVE_BACKWARD,
               vzd.Button.MOVE_LEFT, vzd.Button.MOVE_RIGHT, vzd.Button.TURN_LEFT_RIGHT_DELTA, vzd.Button.USE]
    VARS = [vzd.GameVariable.HEALTH, vzd.GameVariable.SELECTED_WEAPON_AMMO, vzd.GameVariable.KILLCOUNT,
            vzd.GameVariable.POSITION_X, vzd.GameVariable.POSITION_Y, vzd.GameVariable.ANGLE,
            vzd.GameVariable.DAMAGE_TAKEN, vzd.GameVariable.DEAD, vzd.GameVariable.DAMAGECOUNT,
            vzd.GameVariable.HITCOUNT, vzd.GameVariable.SECRETCOUNT]

    def __init__(self, seconds, seed, show=False, level=None, fair=False):
        """level: a Freedoom 2 map ("MAP01") on Ultra-Violence from a pistol start, else the deathmatch arena.
        fair: objects only while they are on screen (no seeing through walls); the level's layout stays known."""
        g = self.g = vzd.DoomGame()
        self.fair, self.show, self.title = fair, show, None
        if level:
            g.load_config(os.path.join(vzd.scenarios_path, "freedoom2.cfg"))
            g.set_doom_map(level.lower())
            g.set_doom_skill(4)
            g.set_automap_buffer_enabled(False)
            g.set_audio_buffer_enabled(False)
            g.set_render_messages(show)  # pickups, and the agent's goals when watched
            g.set_render_hud(show)
        else:
            g.load_config(os.path.join(vzd.scenarios_path, "deathmatch.cfg"))
        self.static_layout = layout(level) if level else None
        g.set_window_visible(show)  # a window (never fullscreen) to watch it play; hidden otherwise
        g.set_mode(vzd.Mode.ASYNC_PLAYER)
        g.set_objects_info_enabled(True)
        g.set_sectors_info_enabled(not level)  # the level's geometry, for navigation (campaign maps: from the map)
        g.set_labels_buffer_enabled(True)
        g.set_screen_resolution(vzd.ScreenResolution.RES_640X480 if show else vzd.ScreenResolution.RES_320X240)
        if show:
            g.set_render_hud(True)
            g.set_render_crosshair(True)
            g.set_render_messages(True)
            g.add_game_args("+con_scaletext 2")  # messages big enough to read
        g.set_available_buttons(self.BUTTONS)
        g.set_available_game_variables(self.VARS)
        g.set_episode_timeout(int(seconds * 35))
        g.set_seed(seed)
        g.init()
        self.turn_sign = 1.0
        # ViZDoom drops a monster from its objects as it dies, so the state never lists corpses. Monsters the player
        # fired at for 2 s without hurting them (out of reach) are left out for 5 s: id -> tic they show again.
        self.ignored: dict = {}
        self.present: set = set()  # monster ids still in the game (for Memory: forget the ones that died)

    def close(self, timeout=15.0):
        """DoomGame.close() sometimes hangs on macOS, joining a game process that never exits: close in the
        background and, if it hasn't finished in time, kill the game's process."""
        t = threading.Thread(target=self.g.close, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            try:
                ps = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
            except OSError:  # no ps (Windows): leave it to the daemon thread
                ps = ""
            for pid, ppid, cmd in (ln.split(None, 2) for ln in ps.splitlines() if len(ln.split(None, 2)) == 3):
                if int(ppid) == os.getpid() and "vizdoom" in cmd:
                    os.kill(int(pid), signal.SIGKILL)
            t.join(5.0)

    def say(self, text):
        """A line in the game's message area, for whoever is watching."""
        text = "".join(ch for ch in " ".join(text.split()) if ch.isprintable() and ch not in '";\\')
        self.g.send_game_command(f'echo "{text[:46]}"')

    def new_episode(self):
        self.g.new_episode()
        self.ignored.clear()
        self.present = set()
        self.target = None
        self.stuck_t, self.last_pos, self.escape = 0, None, 0

    def calibrate(self):
        a0 = self.g.get_state().game_variables[5]
        self.g.make_action([0, 0, 0, 0, 0, 0, 10.0, 0])
        a1 = self.g.get_state().game_variables[5]
        self.turn_sign = 1.0 if wrap(a1 - a0) > 0 else -1.0  # +delta turns left (counterclockwise) when 1

    def read(self, s):
        hp, ammo, kills, x, y, ang, dmg, dead, dealt, hits, secrets = s.game_variables
        visible = {lb.object_id for lb in s.labels}
        monsters, items = [], []
        self.present = {o.id for o in s.objects if is_monster(o.name)}
        for o in s.objects:
            if self.fair and o.id not in visible:
                continue
            dx, dy = o.position_x - x, o.position_y - y
            dist = math.hypot(dx, dy)
            if dist < 1 or dist > 2500:
                continue
            bearing = wrap(math.degrees(math.atan2(dy, dx)) - ang)  # left +
            entry = {"name": o.name, "distance": round(dist), "bearing": round(bearing, 1), "visible": o.id in visible,
                     "id": o.id, "x": o.position_x, "y": o.position_y}
            if is_monster(o.name):
                if self.ignored.get(o.id, -1) <= s.tic:
                    monsters.append(entry)
            elif o.name in KIND:
                items.append({**entry, "kind": KIND[o.name]})
        monsters.sort(key=lambda t: t["distance"])
        items.sort(key=lambda t: t["distance"])
        return {"health": int(hp), "ammo": int(ammo), "kills": int(kills), "x": x, "y": y, "angle": ang, "dead": bool(dead),
                "damage": float(dmg), "dealt": float(dealt), "secrets": int(secrets), "monsters": monsters[:6], "items": items[:8],
                "screen": s.screen_buffer, "layout": self.static_layout if self.static_layout is not None else s.sectors}

    def turn(self, bearing, gain=0.45, cap=12.0):
        return self.turn_sign * max(-cap, min(cap, bearing * gain))

    def act(self, skill, st, tic):
        """The reflex layer: one tic of a skill -> [attack, speed, fwd, back, left, right, turn]."""
        a = [0, 0, 0, 0, 0, 0, 0.0, 0]
        pos = (st["x"], st["y"])
        moved = self.last_pos is None or math.hypot(pos[0] - self.last_pos[0], pos[1] - self.last_pos[1]) > 3
        self.last_pos = pos
        self.stuck_t = 0 if moved else self.stuck_t + 1
        if self.escape > 0:  # unstick: turn away and push on
            self.escape -= 1
            a[6] = self.turn_sign * 9.0
            a[2] = a[1] = 1
            a[7] = int(self.escape % 6 == 0)  # a door, a switch: open it
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


class Tactics:
    """The policy picks the skill. Rules and compiled policies answer inline (microseconds); a model scoring the
    answers runs in the background on the latest state, and the loop uses its latest choice."""

    def __init__(self, kind, compile_model, cache=None):
        self.kind, self.choice, self.lat, self.n = kind, "explore", [], 0
        self.agree, self.checked = 0, 0  # the choice in use vs the rules, on every tic's real state
        self.pending = None
        if kind == "planner" and compile_model:  # Spinal + a model: the model sets goals, the planner acts
            self.strategist = Strategist(decide.generator(compile_model))
        if kind == "rules":
            self.backend = decide.Rules(oracle)
        elif kind == "fight":
            self.backend = decide.Rules(fight_anything, "fight-anything")
        elif kind == "compiled":
            rng = random.Random(5)
            samples = [sample_state(rng) for _ in range(300)]
            if cache and Path(cache).exists():  # compiled before: load it (checked again, like any policy)
                src = Path(cache).read_text()
                self.backend = decide.Policy(decide.load_policy(src), src)
            else:
                self.backend = decide.compile_policy(QUESTION, samples, decide.generator(compile_model))
                if cache:
                    Path(cache).write_text(self.backend.source)
            self.compile_s = self.backend.compile_ms / 1000
            agree = sum(self.backend.fn(s) == oracle(s) for s in samples) / len(samples)
            print(f"compiled the intent with {compile_model} in {self.compile_s:.1f} s; agrees with the rules on "
                  f"{agree:.0%} of 300 sample states", flush=True)
        elif kind == "llm":  # the model picks every move, as an agent calling a tool per move would
            self.gen = decide.generator(compile_model)
            try:
                self.gen("Answer with one word: ready.")  # load the model first: a cold start is not a decision
            except Exception:  # noqa: BLE001
                pass
            self.latest_state, self.stop = None, threading.Event()
            threading.Thread(target=self._background_llm, daemon=True).start()
        elif kind.startswith("plugin:"):  # your own architecture: pick(state) -> skill, or act(state, tic) -> buttons
            import importlib.util
            path, _, cls = kind[len("plugin:"):].partition(":")
            spec = importlib.util.spec_from_file_location("arena_plugin", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.plugin = getattr(mod, cls or "Agent")()
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

    def _background_llm(self):
        names = QUESTION.names
        while not self.stop.is_set():
            st = self.latest_state
            if st is None:
                time.sleep(0.005)
                continue
            t = time.perf_counter()
            try:
                text = self.gen(f"{INTENT}\n\nState:\n{facts(st)}\n\nWhich skill now? Answer with one word: "
                                f"{', '.join(names)}.").lower()
                self.choice = next((n for n in names if n in text), self.choice)
            except Exception:  # noqa: BLE001
                pass
            self.lat.append((time.perf_counter() - t) * 1000)
            self.n += 1

    def pick(self, st):
        if self.kind in ("explore", "planner"):
            return "explore"
        if self.kind in ("scorer", "llm"):
            self.latest_state = st
        elif self.kind.startswith("plugin:") and hasattr(self.plugin, "pick"):
            t = time.perf_counter()
            self.choice = self.plugin.pick(st)
            self.lat.append((time.perf_counter() - t) * 1000)
            self.n += 1
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


STRATEGIST = """You are the strategist for a Doom player (Freedoom 2, Ultra-Violence, pistol start). An autopilot plays \
at 35 frames a second: it walks real paths on its map of the level, aims and shoots, and keeps doing the goal you pick \
until it is done or something urgent happens. You see a summary every couple of seconds and pick its goal from a \
numbered menu. Your answer arrives 1-2 s late, so pick goals that stay good for several seconds. Use what you know about \
Doom: which monsters are dangerous at which range (hitscanners like shotgun guys and chaingunners hurt from afar; imps' \
fireballs can be dodged; demons only bite up close), when to fight and when to break line of sight or go for health, \
armor, a better weapon or ammo, and when to push on into the level to find more monsters, secrets and the exit. \
Distances are in map units (the player is 56 tall); a bearing is degrees to the left (negative: to the right). \
Reply with JSON only: {"pick": <menu number>, "fight": "engage" or "avoid", "why": "<a few words>"}. "avoid" makes \
the autopilot ignore monsters farther than 250 units unless you pick a fight."""


class Strategist:
    """A model sets the goals, the planner's reflexes act every tic (Spinal's design, with a model at the top): on its own
    thread it sees the latest state and the planner's menu of goals, and picks one (plus engage or avoid); the planner
    boosts that goal while the advice is fresh. The game never waits for it."""

    FRESH = 35 * 5  # advice older than 5 s is ignored

    def __init__(self, generate, every_s=1.5):
        self.generate, self.every_s = generate, every_s
        self.lat, self.calls, self.errors = [], 0, 0
        self.reset(None)
        threading.Thread(target=self._loop, name="strategist", daemon=True).start()

    def reset(self, planner):
        self.planner, self.latest, self.advice, self.history = planner, None, None, []

    def see(self, st, tic):
        self.latest, self.tic = (st, tic), tic

    @staticmethod
    def key(g):
        return g["kind"], g.get("id")

    def adjust(self, out, st):
        a = self.advice
        if not a or self.tic - a["tic"] > self.FRESH:
            return out
        if a["goal"] == ("retreat", None) and not any(g["kind"] == "retreat" for _, g in out) and st["monsters"]:
            out.append((2.0, {"kind": "retreat", "why": "the strategist's call"}))
        adj = []
        for u, g in out:
            if self.key(g) == a["goal"]:
                u, g = u * 2.5 + 0.6, {**g, "why": g["why"] + " (strategist: " + a["why"] + ")"}
            elif g["kind"] == "fight" and a["fight"] == "avoid":
                u *= 0.3
            adj.append((u, g))
        return sorted(adj, key=lambda t: -t[0])

    def avoiding(self):
        a = self.advice
        return bool(a and self.tic - a["tic"] <= self.FRESH and a["fight"] == "avoid")

    def prompt(self, st, tic, menu):
        seen = lambda t: "" if t["visible"] else ", remembered"  # noqa: E731
        lines = [STRATEGIST, "", f"Time {tic / 35:.0f} s. Health {st['health']}, ammo {st['ammo']} (current weapon), kills {st['kills']}, "
                 f"weapons picked up: {', '.join(sorted(self.planner.taken)) or 'none (pistol, fists)'}."]
        lines.append("Monsters: " + ("; ".join(f"{m['name']} {m['distance']} away at bearing {m['bearing']:.0f}{seen(m)}"
                                               for m in st["monsters"]) or "none in sight"))
        lines.append("Items: " + ("; ".join(f"{i['name']} ({i['kind']}) {i['distance']} away{seen(i)}"
                                            for i in st["items"]) or "none known"))
        if self.history:
            lines.append("Your last calls: " + "; ".join(self.history[-3:]))
        lines.append("Menu:")
        lines += [f"{n}. {g['kind'].upper()} {g.get('name', '')} - {g['why']}" for n, g in enumerate(menu, 1)]
        return "\n".join(lines)

    def _loop(self):
        while True:
            snap, planner = self.latest, self.planner
            if snap is None or planner is None:
                time.sleep(0.01)
                continue
            st, tic = snap
            menu = [g for _, g in planner.raw_candidates(st)[:7]]
            if st["monsters"] and not any(g["kind"] == "retreat" for g in menu):
                menu.append({"kind": "retreat", "why": "break line of sight, somewhere safer"})
            t0 = time.perf_counter()
            try:
                text = self.generate(self.prompt(st, tic, menu))
                j = json.loads(text[text.index("{"):text.rindex("}") + 1])
                g = menu[int(j["pick"]) - 1]
                why = str(j.get("why", ""))[:60]
                if self.planner is planner:  # still the same level
                    self.advice = {"tic": tic, "goal": self.key(g), "fight": "avoid" if j.get("fight") == "avoid"
                                   else "engage", "why": why}
                    self.history.append(f"{tic / 35:.0f}s {g['kind']} {g.get('name', '')} ({why}), health "
                                        f"{st['health']}")
            except Exception:  # noqa: BLE001
                self.errors += 1
            self.calls += 1
            self.lat.append((time.perf_counter() - t0) * 1000)
            time.sleep(max(0.0, self.every_s - (time.perf_counter() - t0)))


VALUE = {"SuperShotgun": 1.3, "RocketLauncher": 1.1, "PlasmaRifle": 1.1, "Chaingun": 0.9, "Shotgun": 0.7,
         "Chainsaw": 0.2, "BlueArmor": 0.9, "GreenArmor": 0.6, "ArmorBonus": 0.1, "Medikit": 1.0, "Stimpack": 0.5,
         "HealthBonus": 0.1}


class Planner:
    """Goals, not reflexes: it keeps a map of the level (navigation grid), scores what is worth doing now (an item it
    needs, by value over the real path length; a visible monster worth fighting; a safe spot when hurt; somewhere it
    hasn't been), commits to the best until it is done, fails or something urgent happens, and walks real paths."""

    def __init__(self, doom, seed=0, strategist=None):
        from .nav import NavGrid
        self.doom, self.rng, self.strategist = doom, random.Random(seed), strategist
        if strategist:
            strategist.reset(self)
        self.nav = NavGrid(doom.static_layout or doom.g.get_state().sectors)
        self.goal, self.replans, self.stuck_events, self.got = None, 0, 0, 0
        self.taken: set = set()  # weapons already picked up
        self.banned: dict = {}  # target id -> tic it may be tried again (unreachable: stuck, or no progress)

    def candidates(self, st):
        out = self.raw_candidates(st)
        return self.strategist.adjust(out, st) if self.strategist else out

    def raw_candidates(self, st):
        out = []
        vis = [m for m in st["monsters"] if m["visible"] and m["distance"] < 1000]
        hurt = st["health"] < 40
        if vis and hurt:
            out.append((2.5, {"kind": "retreat", "why": f"hurt ({st['health']}) with {vis[0]['name']} in sight"}))
        if vis and st["ammo"] > 0:
            m = vis[0]
            u = 1.6 * (1 - m["distance"] / 1000) + (0.6 if st["health"] >= 50 else -0.8)
            out.append((u, {"kind": "fight", "id": m["id"], "why": f"{m['name']} at {m['distance']}"}))
        for it in st["items"]:
            if self.banned.get(it["id"], -1) > st.get("tic", 0):
                continue
            v = VALUE.get(it["name"], 0.0)
            if it["kind"] == "health":
                v *= (100 - st["health"]) / 60.0
            elif it["kind"] == "ammo":
                v = 0.8 if st["ammo"] < 20 else 0.1
            elif it["kind"] == "weapon" and it["name"] in self.taken:
                v = 0.05
            if v <= 0.05:
                continue
            out.append((v / (1 + it["distance"] / 500.0),
                        {"kind": "get", "id": it["id"], "name": it["name"], "xy": (it["x"], it["y"]),
                         "why": f"{it['name']} ({it['kind']}), {it['distance']} away"}))
        out.append((0.12, {"kind": "explore", "why": "somewhere not seen lately"}))
        return sorted(out, key=lambda t: -t[0])

    def plan(self, st, tic, urgent=False):
        cands = self.candidates(st)
        if self.goal and not urgent and tic - self.goal["since"] < 70:
            cur = next((u for u, g in cands if g["kind"] == self.goal["kind"] and g.get("id") == self.goal.get("id")), None)
            if cur is not None and cands[0][0] < cur * 1.25:  # commitment: switch only for something clearly better
                return
        for u, g in cands[:4]:
            if g["kind"] in ("get", "explore", "retreat"):
                target = g.get("xy")
                if g["kind"] == "explore":
                    target = self.nav.frontier(st["x"], st["y"], self.rng)
                elif g["kind"] == "retreat":
                    target = self.safe_spot(st)
                found = target and self.nav.path((st["x"], st["y"]), target)
                if not found:
                    continue
                g["path"], g["xy"] = found[0], target
                if g["kind"] == "get":  # value over the real path, not the straight line
                    u = u * (1 + math.hypot(target[0] - st["x"], target[1] - st["y"]) / 500.0) / (1 + found[1] / 500.0)
            g["since"], g["utility"] = tic, round(u, 2)
            self.goal = g
            self.replans += 1
            return

    def safe_spot(self, st):
        threats = [(m["x"], m["y"]) for m in st["monsters"] if m["distance"] < 1500]
        best, score = None, -1e9
        for _ in range(60):
            rc = self.nav.sample(st["x"], st["y"], self.rng)
            if rc is None:
                return None
            x, y = self.nav.point(rc)
            d = min((math.hypot(x - a, y - b) for a, b in threats), default=2000)
            near_health = min((it["distance"] for it in st["items"] if it["kind"] == "health"), default=3000)
            sc = d / 300 - math.hypot(x - st["x"], y - st["y"]) / 900 - near_health / 1500
            if sc > score:
                best, score = (x, y), sc
        return best

    def step(self, st, tic):
        d = self.doom
        st["tic"] = tic
        if tic and tic % 175 == 0 and d.static_layout is None:  # doors it opened, lifts that moved
            s = d.g.get_state()
            if s is not None:
                self.nav.update(s.sectors)
        self.nav.visit(st["x"], st["y"])
        vis = [m for m in st["monsters"] if m["visible"]]
        g = self.goal
        near = 600
        if self.strategist:
            self.strategist.see(st, tic)
            near = 250 if self.strategist.avoiding() else 600
        urgent = (st["health"] < 40 and vis and (not g or g["kind"] != "retreat")) or \
                 (vis and vis[0]["distance"] < near and (not g or g["kind"] in ("explore", "get")) and st["health"] >= 40)
        done = g is None or (g["kind"] in ("get", "explore", "retreat") and (not g.get("path"))) or \
            (g["kind"] == "fight" and not any(m["id"] == g["id"] and m["visible"] for m in st["monsters"])) or \
            (g["kind"] == "get" and not any(i["id"] == g["id"] for i in st["items"]))
        if g and g["kind"] == "get" and not any(i["id"] == g["id"] for i in st["items"]):
            self.got += 1
            if KIND.get(g["name"]) == "weapon":
                self.taken.add(g["name"])
        if done or urgent or tic % 35 == 0:
            self.plan(st, tic, urgent=bool(done or urgent))
        g = self.goal
        a = [0, 0, 0, 0, 0, 0, 0.0, 0]
        pos = (st["x"], st["y"])
        moved = d.last_pos is None or math.hypot(pos[0] - d.last_pos[0], pos[1] - d.last_pos[1]) > 3
        d.last_pos = pos
        d.stuck_t = 0 if moved else d.stuck_t + 1
        if g and g["kind"] == "fight":
            m = next((m for m in vis if m["id"] == g["id"]), vis[0] if vis else None)
            if m:
                d.target = m["id"]
                a[6] = d.turn(m["bearing"])
                a[0] = int(abs(m["bearing"]) < 4)
                a[4 if (tic // 25) % 2 else 5] = a[1] = 1
                a[3 if m["distance"] < 250 else 2] = int(m["distance"] < 250 or m["distance"] > 600)
        elif g and g.get("path"):
            wp = g["path"]
            while wp and math.hypot(wp[0][0] - pos[0], wp[0][1] - pos[1]) < 28:
                wp.pop(0)
            if wp:
                bearing = wrap(math.degrees(math.atan2(wp[0][1] - pos[1], wp[0][0] - pos[0])) - st["angle"])
                a[6] = d.turn(bearing, gain=0.7)
                a[2] = a[1] = int(abs(bearing) < 50)
                a[7] = int(d.stuck_t > 6 and d.stuck_t % 4 == 0)  # blocked: a door in the way? open it
                shot = next((m for m in vis if abs(m["bearing"]) < 4), None)  # a clear shot on the way: take it
                a[0] = int(shot is not None and st["ammo"] > 0)
            if d.stuck_t > 25 or (g["kind"] == "get" and tic - g["since"] > 210):  # stuck, or 6 s without getting it
                self.stuck_events += d.stuck_t > 25
                d.stuck_t = 0
                if g.get("id") is not None:
                    self.banned[g["id"]] = tic + 700  # unreachable for now: don't fixate on it
                self.goal = None
        label = f"{g['kind'].upper()} {g.get('name', '')} - {g['why']}" if g else "thinking"
        return a, label



class Memory:
    """With fair senses the game shows only what is on screen, so Spinal's own agents remember: items where they were
    last seen (until the player has been there), monsters for 3 s after they leave the screen (not once they die)."""

    def __init__(self):
        self.items, self.monsters = {}, {}

    def update(self, st, tic, present):
        x, y, ang = st["x"], st["y"], st["angle"]

        def rel(e):
            dx, dy = e["x"] - x, e["y"] - y
            return {**e, "distance": round(math.hypot(dx, dy)), "visible": False,
                    "bearing": round(wrap(math.degrees(math.atan2(dy, dx)) - ang), 1)}
        seen_i, seen_m = {i["id"] for i in st["items"]}, {m["id"] for m in st["monsters"]}
        self.items.update({i["id"]: i for i in st["items"]})
        self.items = {k: i for k, i in self.items.items() if k in seen_i or math.hypot(i["x"] - x, i["y"] - y) > 48}
        self.monsters.update({m["id"]: (tic, m) for m in st["monsters"]})
        self.monsters = {k: v for k, v in self.monsters.items() if tic - v[0] < 105 and k in present}
        st["items"] = sorted(st["items"] + [rel(i) for k, i in self.items.items() if k not in seen_i],
                             key=lambda t: t["distance"])[:8]
        st["monsters"] = sorted(st["monsters"] + [rel(m) for k, (_, m) in self.monsters.items() if k not in seen_m],
                                key=lambda t: t["distance"])[:6]
        return st


def episode(doom, tactics, gif_frames=None):
    doom.new_episode()
    doom.calibrate()
    planner = Planner(doom, seed=doom.g.get_seed() if hasattr(doom.g, "get_seed") else 0,
                      strategist=getattr(tactics, "strategist", None)) if tactics.kind == "planner" else None
    memory = Memory() if doom.fair else None
    tic, kills, late, skills, last_tic = 0, 0, 0, {}, None
    dealt, dry, said = 0.0, 0, (None, -99)
    t0 = time.perf_counter()
    while not doom.g.is_episode_finished():
        s = doom.g.get_state()
        if s is None:
            break
        if last_tic is not None and s.tic - last_tic > 1:
            late += s.tic - last_tic - 1  # tics the loop missed (it fell behind the game)
        last_tic = s.tic
        st = doom.read(s)
        kills = st["kills"]
        plugin_act = getattr(getattr(tactics, "plugin", None), "act", None)
        if memory and not plugin_act:
            st = memory.update(st, tic, doom.present)
        if plugin_act:
            t = time.perf_counter()
            action, skill = list(plugin_act(st, tic)), "plugin"
            if len(action) > 6:
                action[6] = doom.turn(float(action[6]), gain=1.0, cap=30.0)  # a plugin turns in degrees, left +
            tactics.lat.append((time.perf_counter() - t) * 1000)
            tactics.n += 1
            skills[skill] = skills.get(skill, 0) + 1
        elif planner:
            t = time.perf_counter()
            action, skill = planner.step(st, tic)
            tactics.lat.append((time.perf_counter() - t) * 1000)  # the whole per-tic decision, path finding included
            tactics.n += 1
            kind = skill.split(" ")[0].lower()
            skills[kind] = skills.get(kind, 0) + 1
        else:
            skill = tactics.pick(st)
            skills[skill] = skills.get(skill, 0) + 1
            action = doom.act(skill, st, tic)
        if doom.show:  # say what it is doing, each time its goal changes
            if tic == 0 and doom.title:
                doom.say(doom.title)
            key = (planner.goal.get("kind"), planner.goal.get("id")) if planner and planner.goal else skill
            if key != said[0] and tic - said[1] >= 12:
                doom.say(" ".join(skill.split()).replace(" - ", ": "))
                said = (key, tic)
        if gif_frames is not None and tic % 3 == 0:
            gif_frames.append((s.screen_buffer.copy(), skill, st["health"], kills))
        action = list(action) + [0] * (len(Doom.BUTTONS) - len(action))
        dry = dry + 1 if action[0] and st["dealt"] == dealt else 0
        if dry > 60 and doom.target is not None:  # 2 s of fire without damage: out of reach, for now
            doom.ignored[doom.target] = s.tic + 175
            doom.target, dry = None, 0
        dealt = st["dealt"]
        doom.g.make_action(action)
        tic += 1
    gv = doom.g.get_game_variable
    game_tics = last_tic or 0  # the episode clock resets when the level is exited
    return {"seconds": round(time.perf_counter() - t0, 1), "kills": int(gv(vzd.GameVariable.KILLCOUNT)),
            "died": bool(doom.g.is_player_dead()), "damage": int(gv(vzd.GameVariable.DAMAGE_TAKEN)),
            "dealt": int(gv(vzd.GameVariable.DAMAGECOUNT)), "hits": int(gv(vzd.GameVariable.HITCOUNT)),
            "tics": tic, "missed_tics": late, "secrets": int(gv(vzd.GameVariable.SECRETCOUNT)),
            "game_s": round(game_tics / 35, 1),
            "exited": not doom.g.is_player_dead() and doom.g.get_episode_time() < doom.g.get_episode_timeout() - 2, "skills": {k: round(v / max(1, tic), 2) for k, v in skills.items()},
            **({"got": planner.got, "replans": planner.replans, "stuck": planner.stuck_events} if planner else {})}


def save_gif(frames, path):
    from PIL import Image, ImageDraw
    imgs = []
    for buf, skill, hp, kills in frames:
        im = Image.fromarray(np.transpose(buf, (1, 2, 0))).resize((320, 240))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 320, 13], fill=(0, 0, 0))
        d.text((3, 1), f"hp {hp:3d} k {kills} | {skill}"[:58], fill=(255, 255, 0))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=86, loop=0)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relaymcp play doom")
    ap.add_argument("--policy", choices=["explore", "fight", "rules", "compiled", "scorer", "planner"], required=True)
    ap.add_argument("--episodes", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--gif")
    ap.add_argument("--compile-model", default="qwen3.5:9b")
    ap.add_argument("--policy-cache", help="compiled policy file: loaded if it exists, else written after compiling")
    ap.add_argument("--show", action="store_true", help="watch it play in a window (with the HUD)")
    a = ap.parse_args(argv)
    tactics = Tactics(a.policy, a.compile_model, a.policy_cache)
    doom = Doom(a.seconds, a.seed, a.show)
    results = []
    for ep in range(a.episodes):
        frames = [] if (a.gif and ep == 0) else None
        r = episode(doom, tactics, frames)
        results.append(r)
        print(json.dumps({"policy": a.policy, "episode": ep, **r}), flush=True)
        if frames:
            save_gif(frames, a.gif)
    doom.close()
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


if __name__ == "__main__":
    main()
