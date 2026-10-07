"""Doom as a world for the scientist (H1 on Doom): true objects from ViZDoom, steps of 4 tics, discrete buttons.

Recorded play starts from moments of real play (the base agent playing a practice level, saved every few seconds), then
a random player holds each button 1-8 steps, so the data has fights, not only the start room. Train data comes from
MAP05-MAP12, the unseen test data from MAP13-MAP16; MAP01-MAP04 (the Spinal Score) are never used.

Objects: the player (with its angle, health and level), a "health" marker (x = the player's health), and the monsters,
projectiles and items within RADIUS of the player, at their true positions (also behind walls: the world model learns
the game, not the view). A world model may call walls(level) for the level's blocking lines.
"""

import math
import os
import random
from functools import lru_cache
from pathlib import Path

TICS = 4
RADIUS = 1024
CAP = 64.0
SEGMENT = 60  # steps of random play from each saved moment
PROJECTILES = {"DoomImpBall", "CacodemonBall", "BaronBall", "RevenantTracer", "FatShot", "ArachnotronPlasma",
               "Rocket", "PlasmaBall", "BFGBall", "ArchvileFire", "SpawnShot", "Grenade"}
BUTTONS = {  # [attack, speed, forward, back, left, right, turn (degrees per tic, left +), use]
    "NOOP": [0, 1, 0, 0, 0, 0, 0, 0], "FORWARD": [0, 1, 1, 0, 0, 0, 0, 0], "BACK": [0, 1, 0, 1, 0, 0, 0, 0],
    "STRAFE_LEFT": [0, 1, 0, 0, 1, 0, 0, 0], "STRAFE_RIGHT": [0, 1, 0, 0, 0, 1, 0, 0],
    "TURN_LEFT": [0, 1, 0, 0, 0, 0, 6, 0], "TURN_RIGHT": [0, 1, 0, 0, 0, 0, -6, 0],
    "FIRE": [1, 1, 0, 0, 0, 0, 0, 0], "FORWARD_FIRE": [1, 1, 1, 0, 0, 0, 0, 0], "USE": [0, 1, 0, 0, 0, 0, 0, 1]}
SPLITS = {0: ("MAP05", "MAP06", "MAP07", "MAP08"), 1: ("MAP09", "MAP10", "MAP11", "MAP12"),
          100: ("MAP13", "MAP14"), 101: ("MAP15", "MAP16")}
BASE = os.environ.get("SPINAL_BASE_AGENT", str(Path(__file__).parents[3] / "examples" / "learned_doom_agent.py"))


@lru_cache(maxsize=None)
def walls(level):
    """The level's blocking lines (walls, ledges too high to cross as the level starts) as (x1, y1, x2, y2)."""
    from relaymcp.play.doom import layout
    out = set()
    for sec in layout(level):
        for ln in sec.lines:
            if ln.is_blocking:
                out.add((ln.x1, ln.y1, ln.x2, ln.y2))
    return sorted(out)


class DoomWorld:
    game = "Doom"
    cap = CAP

    def __init__(self, save_dir, steps_per_map=375):
        self.steps_per_map, self.save_dir = steps_per_map, str(save_dir)
        os.makedirs(self.save_dir, exist_ok=True)

    def describe(self, st, segs):
        return f"""GAME: Doom (Freedoom 2 levels, Ultra-Violence). One step = {TICS} tics (35 tics a second). \
Buttons (held for the step; the player always runs): {', '.join(st['actions'])}; TURN_LEFT/RIGHT turn \
{abs(BUTTONS['TURN_LEFT'][6]) * TICS} degrees a step.
Objects are the game's true state each step, in map units (x east, y north; the player is 56 tall, 32 wide): the \
"player" (also angle in degrees, 0 = east, counterclockwise; health; level), a "health" marker (x = the player's \
health, y = 0), and every monster, projectile and item within {RADIUS} units of the player (also behind walls), each \
by its name. w and h are 0. `walls(level)` (provided to your module) lists the level's blocking lines \
(x1, y1, x2, y2). Recorded play: from moments of real play on practice levels, a random player holds each button 1-8 \
steps; the unseen test is on other levels. Segments: {segs}."""

    def actions(self):
        return list(BUTTONS)

    def _game(self, level, seed):
        from relaymcp.play.compiler import Game
        return Game(level, seed, 300)

    @staticmethod
    def objects(g, level):
        from relaymcp.play.doom import KIND, is_monster
        s = g.d.g.get_state()
        if s is None:
            return None
        import vizdoom as vzd
        gv = g.d.g.get_game_variable
        x, y = gv(vzd.GameVariable.POSITION_X), gv(vzd.GameVariable.POSITION_Y)
        hp = gv(vzd.GameVariable.HEALTH)
        out = [{"cat": "player", "x": round(x, 1), "y": round(y, 1), "w": 0, "h": 0,
                "angle": round(gv(vzd.GameVariable.ANGLE), 1), "health": int(hp), "level": level},
               {"cat": "health", "x": int(hp), "y": 0, "w": 0, "h": 0}]
        for o in s.objects:
            if o.name == "DoomPlayer":
                continue
            if not (is_monster(o.name) or o.name in PROJECTILES or o.name in KIND):
                continue
            if math.hypot(o.position_x - x, o.position_y - y) > RADIUS:
                continue
            out.append({"cat": o.name, "x": round(o.position_x, 1), "y": round(o.position_y, 1), "w": 0, "h": 0})
        return out

    def _press(self, g, name):
        a = list(BUTTONS[name])
        a[6] = g.d.turn(float(a[6]), gain=1.0, cap=30.0)
        for _ in range(TICS):
            if g.done():
                break
            g.d.g.make_action(a)
            g.tic += 1

    def record(self, seed, steps=None):
        """For each level of the split: the base agent plays, and every ~6 s its game is saved; from each saved
        moment a random player plays SEGMENT steps."""
        from relaymcp.play.compiler import Compiled, load_base
        rng = random.Random(seed)
        segs, tmp = [], self.save_dir
        for level in SPLITS[seed]:
            g = self._game(level, seed + 1)
            agent = Compiled(load_base(BASE))
            saves, k = [], 0
            while not g.done() and len(saves) * SEGMENT < self.steps_per_map:
                if g.tic and g.tic % 210 == 0:
                    p = os.path.join(tmp, f"{level}-{seed}-{k}.sav")
                    g.d.g.save(p)
                    saves.append(p)
                    k += 1
                g.step(agent)
            g.d.close()
            for p in saves:  # each in a fresh game, as an experiment from it will be (loads agree only then)
                g = self._game(level, 1)
                g.load(p, 0)
                seg = {"level": level, "save": p, "actions": [], "objs": [self.objects(g, level)]}
                a, hold = "NOOP", 0
                for _ in range(SEGMENT):
                    if hold <= 0:
                        a, hold = rng.choice(list(BUTTONS)), rng.randint(1, 8)
                    hold -= 1
                    self._press(g, a)
                    o = self.objects(g, level)
                    if o is None or g.done():
                        break
                    seg["actions"].append(a)
                    seg["objs"].append(o)
                if seg["actions"]:
                    segs.append(seg)
                g.d.close()
        return segs

    def experiment(self, seg, step, actions):
        g = self._game(seg["level"], 1)
        try:
            g.load(seg["save"], 0)
            for a in seg["actions"][:step]:
                self._press(g, a)
            out = [self.objects(g, seg["level"])]
            for a in actions:
                self._press(g, a)
                o = self.objects(g, seg["level"])
                if o is None or g.done():
                    break
                out.append(o)
            return out
        finally:
            g.d.close()
