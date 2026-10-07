"""spinal learn: a model learns a game by practising it, and writes its own reflexes.

    spinal learn doom --model copilot:claude-opus-5.5 --out learned/doom
    spinal learn breakout --model copilot:claude-opus-5.5 --out learned/breakout

Models are too slow to play a real-time game move by move, so here the model plays it the way a programmer would: it
reads the game's manual (rules, controls and what the program sees, nothing about how to play well), writes an agent,
watches it play, and improves it. Built to keep improving with more practice, not to stall:

- Experience grows. Every death, heavy hit, lost ball or stall in practice is saved as a game state a few seconds
  before it happened: a drill. The drill bank grows with every round of play, and every program is tested on all of it.
- Gains are kept. The model edits the best program (search/replace), it doesn't rewrite it, and a change is accepted
  only if, paired level by level and drill by drill against the best, it is better beyond chance (a bootstrap). A
  change that fixes one thing and breaks another doesn't get in.
- Search is parallel. Each round, several candidates work on different failures at once: more compute, more tries.
- Lessons are kept. The model keeps a notebook of what it has found out about the game, carried into every prompt.
- The curve is measured. After each round the best program plays monitor levels it never practised on; learn.json
  records that score against model calls and practice time.

Nothing in the loop knows about any one game: a game is a manual, a starting skeleton, practice and monitor levels,
and a way to play a level or a drill. The result is a plain plugin that plays in real time with no model. Practice
never touches the levels it is judged on: Doom practises on MAP05-MAP12, is monitored on MAP13-MAP16 and is scored by
the Spinal Score (`spinal arena run`, MAP01-MAP04); Breakout practises, is monitored and is scored on different seeds.

The model's code runs on this computer, in child processes with time limits, like any plugin you download: read it.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import pickle
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SYSTEM = """You write programs that play games, and improve them from practice. You get the game's manual (its rules, \
its controls and exactly what your program sees), a notebook of what has been learned about the game so far, the best \
program so far and how it did: on whole practice levels, and on drills (short situations saved from moments where a \
program failed: a drill starts a new Agent in the middle of a level, a few seconds before the failure). Fix what you \
are asked to focus on without breaking what works: a change is kept only if, compared level by level and drill by \
drill, the program does better overall. Write general play that works on levels you have never seen, not a route \
through one level. Use log(...) to report what your program sees and decides, to check your assumptions (only a few \
lines per level are shown).

Reply in this format:
NOTE: what you changed and why, at most 40 words.
```notebook
The whole notebook, updated: facts about the game and about the program that you have checked (at most 50 short
lines). Keep what is still true, fix what was wrong.
```
Then the change, as one or more edits to the best program:
<<<<<<< SEARCH
exact lines from the best program (enough to be unique)
=======
the new lines
>>>>>>> REPLACE
or, only if most of it changes, the complete new program in one ```python block. Python standard library and numpy \
only. act() is called once per frame and must return within a few milliseconds."""

LOG_LINES = 25
EVENTS = 40
DRILL_CAP = 160


def load_agent(path, log=None):
    """The program's Agent, with log() bound (a no-op unless given)."""
    spec = importlib.util.spec_from_file_location("learned_agent", path)
    mod = importlib.util.module_from_spec(spec)
    mod.log = log or (lambda *a, **k: None)
    spec.loader.exec_module(mod)
    return mod.Agent()


class Log:
    def __init__(self, limit=LOG_LINES):
        self.lines, self.limit, self.dropped, self.t = [], limit, 0, 0.0

    def __call__(self, *args, **_):
        if len(self.lines) < self.limit:
            self.lines.append(f"[{self.t:.1f}s] " + " ".join(str(a) for a in args)[:200])
        else:
            self.dropped += 1

    def all(self):
        return self.lines + ([f"... {self.dropped} more log lines"] if self.dropped else [])


def cut(events, n=EVENTS):
    """The first and last events of a long story."""
    return events if len(events) <= n else events[:n // 2] + [f"... {len(events) - n} more ..."] + events[-n // 2:]


def p95(lat):
    lat = sorted(lat)
    return round(lat[int(len(lat) * 0.95)], 2) if lat else None


class Moments:
    """Saves the game every second (a ring of the last few), and keeps the save from a few seconds before a failure:
    a drill. At most `cap` per level, at least `gap` seconds apart."""

    def __init__(self, bank, tag, save, ext, every, before=4, ring=6, cap=5, gap=15.0):
        self.bank, self.tag, self.save, self.ext, self.every = bank, tag, save, ext, every
        self.before, self.ring, self.cap, self.gap = before, ring, cap, gap
        self.found, self.last = [], -1e9
        if bank:
            Path(bank, "tmp").mkdir(parents=True, exist_ok=True)

    def _ring(self, k):
        return Path(self.bank, "tmp", f"{self.tag}-{os.getpid()}-{threading.get_ident()}-{k % self.ring}{self.ext}")

    def tick(self, frame):
        if self.bank and frame % self.every == 0:
            self.save(str(self._ring(frame // self.every)))

    def keep(self, frame, kind, why, force=False, **start):
        t = frame / self.every
        if not self.bank or len(self.found) >= self.cap or (t - self.last < self.gap and not force):
            return
        k = frame // self.every - self.before
        src = next((self._ring(j) for j in range(k, k + self.before) if j >= 0 and self._ring(j).exists()), None)
        if src is None:
            return
        self.last = t
        did = f"{self.tag}-{kind}-{frame}"
        shutil.copy(src, Path(self.bank, did + self.ext))
        self.found.append({"id": did, "kind": kind, "t": round(t, 1), "why": why, "state": did + self.ext, **start})

    def close(self):
        for k in range(self.ring if self.bank else 0):
            self._ring(k).unlink(missing_ok=True)


# -- games ------------------------------------------------------------------------------------------------------------

DOOM_MANUAL = """Doom (Freedoom 2: the free Doom II), Ultra-Violence (the hardest normal skill), pistol start: each level \
starts you with 100 health, a pistol with 50 bullets, and fists.

SCORE of a level (Doom's own end-of-level tally), the mean of three parts, each 0-1: kills (monsters killed / monsters \
in the level), secrets (secret areas entered / secret areas in the level) and exit (par time / your time, capped at 1, \
or 0 if you never reach the exit). Dying ends the level. A level also ends after 3x its par time (at most 300 s in \
practice).

TIME: the game runs at 35 tics per second. act() is called once per tic. Judged runs are in real time: an answer later \
than about 28 ms misses tics. (Practice waits for you.)

RULES (as the game's manual tells a player): monsters attack when they see you or hear shots. Zombiemen, shotgun guys \
and chaingunners shoot instantly (hitscan); imps, cacodemons, barons and others throw projectiles that can be dodged; \
demons, spectres and lost souls bite or charge. Doors, switches and lifts work with use, pressed facing them from \
close by. Some doors need a key card or skull key of the matching colour, picked up by walking over it. A level ends \
when you press use on its exit switch (on some levels: walk through an exit doorway or teleporter). A secret area \
counts when you step into it; secrets are often behind walls that open with use. Items are picked up by walking over \
them: health (stimpack, medikit, small bonuses), armor, ammo and weapons (shotgun, super shotgun, chaingun, rocket \
launcher, plasma rifle, BFG, chainsaw). A new weapon is selected when you pick it up.

YOUR PROGRAM: a file with a class Agent (no arguments) with act(state, tic) -> action. One Agent plays all the levels \
of a run in turn (the layout object changes when a new level starts; tic restarts at 0).
state (a dict), everything as on screen, nothing seen through walls:
  health, ammo (of the current weapon), kills, secrets (counts so far), x, y (map units), angle (degrees, 0 = east, \
counterclockwise), damage (total taken so far), dealt (total dealt so far), dead (bool)
  monsters: those on screen, nearest first (at most 6): dicts with name, distance, bearing (degrees, left of where you \
look is positive), x, y, id (stable while the level lasts)
  items: those on screen, nearest first (at most 8): same fields plus kind ("health", "ammo", "weapon" or "armor"); \
keys are not listed (they are visible on the screen)
  screen: the picture, a numpy uint8 array of shape (3, 240, 320) (RGB planes)
  layout: the level's map, as a list of sectors (objects with attributes floor_height, ceiling_height and lines); each \
line has attributes x1, y1, x2, y2 and is_blocking (a solid wall or a one-sided line). Heights are as the level \
starts: a closed door is a sector whose floor and ceiling are equal. Exits, switches and secrets are not marked.
action: a list [attack, speed, forward, back, left, right, turn, use]: 0/1 buttons (left/right strafe; speed runs), \
turn in degrees this tic (left positive, at most 30)."""

DOOM_STARTER = '''"""A starting point: stands still."""


class Agent:
    def act(self, state, tic):
        return [0, 0, 0, 0, 0, 0, 0.0, 0]
'''


class Doom:
    """Freedoom 2 on Ultra-Violence, as in the Spinal Score; practice and monitor on maps the score never uses."""
    name = "doom"
    manual = DOOM_MANUAL
    starter = DOOM_STARTER
    practice = ("MAP05", "MAP06", "MAP07", "MAP08", "MAP09", "MAP10", "MAP11", "MAP12")
    monitor = ("MAP13", "MAP14", "MAP15", "MAP16")
    held_out = ()  # MAP01-MAP04: `spinal arena run --agent plugin:<agent.py>:Agent`
    seeds = (1, 2)  # for the monitor

    def units(self, rnd):
        """Round rnd's practice: every practice map, on two seeds no round has used before."""
        return [(m, 100 + 2 * rnd + i) for m in self.practice for i in (0, 1)]
    drill_s = 10
    timeout_s = 900

    def _check(self, level):
        from . import arena
        if level in arena.MAPS:
            raise ValueError(f"{level} is a scored level: practice uses other maps")
        return arena.levels((level,))[level]

    def _run(self, game, agent, log, events, on_tic, load=None, tics=None):
        from . import doom
        lat = []
        tactics = doom.Tactics("planner", None)  # the per-tic loop of a plugin, given the agent below
        tactics.kind = "plugin:learned"

        class Watched:
            def act(self, st, tic):
                log.t = tic / 35
                on_tic(st, tic)
                t0 = time.perf_counter()
                out = agent.act(st, tic)
                lat.append((time.perf_counter() - t0) * 1000)
                return out
        tactics.plugin = Watched()
        return doom.episode(game, tactics, load=load, tics=tics), lat

    def play(self, path, level, seed=1, bank=None):
        from . import arena, doom
        monsters, secrets, par = self._check(level)
        log, events = Log(), []
        game = doom.Doom(min(arena.CAP * par, 300), seed, level=level, fair=True, realtime=False)
        mom = Moments(bank, f"{level}-s{seed}", game.g.save, ".sav", 35)
        last, stay, hurt = {}, [None, 0], []
        r, error = None, None

        def note(st, tic):
            t = tic / 35
            mom.tick(tic)
            if last:
                if st["health"] < last["health"]:
                    seen = ", ".join(f"{m['name']} {m['distance']} away" for m in st["monsters"][:3]) or "nothing"
                    events.append(f"[{t:.1f}s] lost {last['health'] - st['health']} health (now {st['health']}); "
                                  f"on screen: {seen}")
                    hurt.append((tic, last["health"] - st["health"]))
                    recent = sum(h for k, h in hurt if tic - k < 105)
                    if recent >= 40 and st["health"] > 0:
                        mom.keep(tic, "hurt", f"lost {recent} health in 3 s", health=last["health"])
                if st["kills"] > last["kills"]:
                    events.append(f"[{t:.1f}s] kill ({st['kills']}/{monsters})")
                if st["secrets"] > last["secrets"]:
                    events.append(f"[{t:.1f}s] found a secret ({st['secrets']}/{secrets})")
            pos = (round(st["x"] / 32), round(st["y"] / 32))
            if pos == stay[0]:
                stay[1] += 1
                if stay[1] == 35 * 5:
                    events.append(f"[{t:.1f}s] has not moved for 5 s at x={st['x']:.0f} y={st['y']:.0f}")
                    mom.keep(tic, "stuck", f"did not move for 5 s at x={st['x']:.0f} y={st['y']:.0f}",
                             health=st["health"])
            else:
                stay[0], stay[1] = pos, 0
            if tic % (35 * 15) == 0:
                events.append(f"[{t:.1f}s] at x={st['x']:.0f} y={st['y']:.0f}, health {st['health']}, ammo "
                              f"{st['ammo']}, kills {st['kills']}")
            last.update(health=st["health"], kills=st["kills"], secrets=st["secrets"], tic=tic)

        try:
            r, lat = self._run(game, load_agent(path, log), log, events, note)
            if r["died"]:
                mom.keep(last.get("tic", 0), "died", "died: " + "; ".join(e for e in events[-4:]), force=True,
                         health=None)
        except Exception:  # noqa: BLE001
            lat, error = [], traceback.format_exc(limit=6)[-2500:]
        finally:
            game.close()
            mom.close()
        for m in mom.found:
            m["why"] = m["why"] + " | before it: " + " / ".join(
                e for e in events if _t(e) is not None and m["t"] - 6 <= _t(e) <= m["t"] + 4)[-600:]
            m.update(level=level, seed=seed)
        if r is None:
            return {"level": level, "seed": seed, "score": 0.0, "tally": "crashed", "events": cut(events),
                    "log": log.all(), "error": error, "ms_p95": None, "moments": mom.found}
        k = min(1.0, r["kills"] / monsters)
        s = min(1.0, r["secrets"] / secrets) if secrets else 1.0
        e = min(1.0, par / max(r["game_s"], 1.0)) if r["exited"] else 0.0
        end = "exited" if r["exited"] else ("died" if r["died"] else "time ran out")
        events.append(f"[{r['game_s']:.1f}s] {end}")
        return {"level": level, "seed": seed, "score": round((k + s + e) / 3, 4),
                "tally": f"kills {r['kills']}/{monsters}, secrets {r['secrets']}/{secrets}, {end} at {r['game_s']:.0f} s "
                         f"(par {par} s); damage taken {r['damage']}, dealt {r['dealt']}",
                "events": cut(events), "log": log.all(), "error": None, "ms_p95": p95(lat), "moments": mom.found}

    def drills(self, path, level, drills, bank):
        """Drills on one level, one game: each starts a new Agent from its saved state for drill_s seconds."""
        from . import doom
        self._check(level)
        game = doom.Doom(320 + self.drill_s, 1, level=level, fair=True, realtime=False)
        out = []
        try:
            for d in drills:
                log, events, first, far = Log(10), [], {}, [0.0]

                def note(st, tic, first=first, events=events, far=far):
                    if not first:
                        first.update(x=st["x"], y=st["y"], health=st["health"], kills=st["kills"])
                    far[0] = max(far[0], ((st["x"] - first["x"]) ** 2 + (st["y"] - first["y"]) ** 2) ** 0.5)
                    if st["health"] < first.get("hp", first["health"]):
                        events.append(f"[{tic / 35:.1f}s] health {st['health']}")
                    first["hp"], first["end"] = st["health"], st
                try:
                    r, _ = self._run(game, load_agent(path, log), log, events, note,
                                     load=str(Path(bank, d["state"])), tics=35 * self.drill_s)
                    end = first.get("end", {})
                    alive = not r["died"]
                    if d["kind"] == "stuck":
                        score = min(1.0, far[0] / 384)
                        tally = f"moved {far[0]:.0f} units away (384 to pass)"
                    else:
                        hp0 = max(1, first.get("health", 1))
                        score = (0.6 + 0.4 * min(1.0, end.get("health", 0) / hp0)) if alive else 0.0
                        tally = (f"{'survived' if alive else 'died'}, health {first.get('health')} -> "
                                 f"{end.get('health', 0)}, kills +{end.get('kills', 0) - first.get('kills', 0)}")
                    out.append({"id": d["id"], "score": round(score, 4), "tally": tally, "events": cut(events, 12),
                                "log": log.all(), "error": None})
                except Exception:  # noqa: BLE001
                    out.append({"id": d["id"], "score": 0.0, "tally": "crashed", "events": events[-6:],
                                "log": log.all(), "error": traceback.format_exc(limit=4)[-1500:]})
        finally:
            game.close()
        return out


def _t(event):
    m = re.match(r"\[(\d+(?:\.\d+)?)s\]", event)
    return float(m.group(1)) if m else None


BREAKOUT_MANUAL = """Breakout (Atari 2600), the standard game (game 1, one player), five balls (lives).

SCORE: the points you make, divided by 864 (both walls cleared, the most a game can make). Bricks are worth 1 (the \
bottom two rows, blue and green), 4 (yellow and orange) or 7 (the top two rows, red). The game ends when the last ball \
is lost, or after 18,000 frames (5 minutes).

TIME: 60 frames per second; act() is called once per frame. The console sometimes repeats your previous action \
instead of the new one (a quarter of the time, like a sticky joystick).

RULES (as the game's manual tells a player): you move a paddle left and right along the bottom of the screen and \
press FIRE to serve a ball (at the start and after each ball is lost). The ball bounces off the walls, the ceiling and \
your paddle; the part of the paddle it hits sets its angle. It breaks the bricks it hits and gets faster as the game \
goes on; when it reaches the top rows the paddle shrinks to half its width. A ball that falls past the paddle is lost. \
When a wall is cleared a second wall appears.

YOUR PROGRAM: a file with a class Agent (no arguments) with act(frame, t) -> action. One Agent plays one game; a new \
Agent is made for each game.
frame: the screen as a numpy uint8 array of shape (210, 160, 3) (rows, columns, RGB). The score and the balls left \
are drawn at the top. Nothing else is given: find the paddle, the ball and the bricks in the picture.
t: the frame number (0 at the start).
action: one of the strings "NOOP", "FIRE", "RIGHT", "LEFT"."""

BREAKOUT_STARTER = '''"""A starting point: serves, then does nothing."""


class Agent:
    def act(self, frame, t):
        return "FIRE" if t % 60 == 0 else "NOOP"
'''


class Atari:
    """An Atari 2600 game through the Arcade Learning Environment (`pip install ale-py`): pixels in, joystick out."""
    timeout_s = 900
    frames = 18_000
    drill_s = 6
    seeds = (1,)  # the level is the seed

    def __init__(self, rom, manual, starter, top):
        self.name, self.rom, self.manual, self.starter, self.top = rom, rom, manual, starter, top
        self.practice = ("seed 1000", "...")  # new seeds every round: see units()
        self.monitor = tuple(f"seed {i}" for i in range(51, 63))
        self.held_out = tuple(f"seed {i}" for i in range(101, 111))

    def units(self, rnd):
        """Round rnd's practice: 24 games on seeds no round has used before."""
        return [(f"seed {1000 + 24 * rnd + i}", 1) for i in range(24)]

    def _ale(self, seed):
        from ale_py import ALEInterface, LoggerMode, roms
        ALEInterface.setLoggerMode(LoggerMode.Error)
        ale = ALEInterface()
        ale.setInt("random_seed", int(seed))
        ale.setFloat("repeat_action_probability", 0.25)
        ale.setInt("max_num_frames_per_episode", self.frames)
        ale.loadROM(roms.get_rom_path(self.rom))
        return ale, {str(a).rsplit(".", 1)[-1]: a for a in ale.getMinimalActionSet()}

    def _step(self, ale, names, agent, frame, t, lat):
        t0 = time.perf_counter()
        a = agent.act(frame, t)
        lat.append((time.perf_counter() - t0) * 1000)
        if a not in names:
            raise ValueError(f"act() returned {a!r}: expected one of {sorted(names)}")
        return int(ale.act(names[a]))

    def play(self, path, level, seed=1, bank=None):
        ale, names = self._ale(level.split()[-1])
        mom = Moments(bank, f"{self.name}-{level.replace(' ', '')}",
                      lambda p: Path(p).write_bytes(pickle.dumps(ale.cloneState())), ".state", 60, before=3, cap=2,
                      gap=5.0)
        log, events, lat, points, t, last_point = Log(), [], [], 0, 0, 0
        lives, error = ale.lives(), None
        try:
            agent = load_agent(path, log)
            while not ale.game_over():
                log.t = t / 60
                mom.tick(t)
                got = self._step(ale, names, agent, ale.getScreenRGB(), t, lat)
                if got:
                    points, last_point = points + got, t
                if t - last_point == 60 * 20:
                    events.append(f"[{t / 60:.1f}s] no points for 20 s")
                    mom.keep(t + 3 * 60, "stalled", "no points for 20 s", force=True)
                if ale.lives() < lives:
                    lives = ale.lives()
                    events.append(f"[{t / 60:.1f}s] lost a ball ({lives} left), {points} points so far")
                    mom.keep(t, "lost ball", f"lost a ball at {t / 60:.1f}s with {points} points")
                if t % (60 * 30) == 0 and t:
                    events.append(f"[{t / 60:.1f}s] {points} points")
                t += 1
        except Exception:  # noqa: BLE001
            error = traceback.format_exc(limit=6)[-2500:]
        finally:
            mom.close()
        end = "crashed" if error else ("game over" if lives == 0 else "time ran out")
        events.append(f"[{t / 60:.1f}s] {end}: {points} points")
        for m in mom.found:
            m.update(level=level, seed=seed)
        return {"level": level, "seed": seed, "score": round(min(1.0, points / self.top), 4),
                "tally": f"{points} points, {end}", "events": cut(events), "log": log.all(), "error": error,
                "ms_p95": p95(lat), "moments": mom.found}

    def drills(self, path, level, drills, bank):
        out = []
        for d in drills:
            ale, names = self._ale(level.split()[-1])
            log, lat, t, points = Log(10), [], 0, 0
            try:
                ale.restoreState(pickle.loads(Path(bank, d["state"]).read_bytes()))
                agent, lives = load_agent(path, log), ale.lives()
                while not ale.game_over() and t < 60 * self.drill_s * (2 if d["kind"] == "stalled" else 1):
                    log.t = t / 60
                    points += self._step(ale, names, agent, ale.getScreenRGB(), t, lat)
                    if ale.lives() < lives:
                        break
                    t += 1
                kept = ale.lives() >= lives
                score = float(points > 0) if d["kind"] == "stalled" else float(kept)
                out.append({"id": d["id"], "score": score, "error": None, "log": log.all(), "events": [],
                            "tally": f"{'kept the ball' if kept else f'lost the ball at {t / 60:.1f}s'}, "
                                     f"+{points} points"})
            except Exception:  # noqa: BLE001
                out.append({"id": d["id"], "score": 0.0, "tally": "crashed", "events": [], "log": log.all(),
                            "error": traceback.format_exc(limit=4)[-1500:]})
        return out


GAMES = {"doom": Doom, "breakout": lambda: Atari("breakout", BREAKOUT_MANUAL, BREAKOUT_STARTER, 864)}


# -- running programs -------------------------------------------------------------------------------------------------

def run_isolated(game, method, *args):
    """A level (or a level's drills) in a child process: the model's code can crash, hang or leak without taking the
    learner with it."""
    cmd = [sys.executable, "-m", "relaymcp.play.learn", "_run", game.name, method, json.dumps(args)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=game.timeout_s)
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith(("{", "["))), None)
        if line:
            return json.loads(line)
        err = (p.stderr or p.stdout)[-2500:]
    except subprocess.TimeoutExpired:
        err = f"took longer than {game.timeout_s} s of wall time: stopped (is act() too slow, or stuck?)"
    return {"error": err}


class Runner:
    def __init__(self, game, bank, workers=4, isolate=True):
        self.game, self.bank, self.workers, self.isolate = game, Path(bank), workers, isolate
        self.pool = ThreadPoolExecutor(max(1, workers))

    def _call(self, method, *args):
        return run_isolated(self.game, method, *args) if self.isolate else getattr(self.game, method)(*args)

    def levels(self, path, levels, record=None):
        """Every level on each of the game's seeds; failures saved as drills into the folder `record`."""
        return self.units(path, [(lv, s) for lv in levels for s in getattr(self.game, "seeds", (1,))], record)

    def units(self, path, units, record=None):
        """(level, seed) pairs."""
        if record:
            Path(record).mkdir(parents=True, exist_ok=True)
        def one(item):
            lv, seed = item
            r = self._call("play", str(path), lv, seed, str(record) if record else None)
            if "level" not in r:
                return {"level": lv, "seed": seed, "score": 0.0, "tally": "crashed", "events": [], "log": [],
                        "error": r["error"], "ms_p95": None, "moments": []}
            return r
        return list(self.pool.map(one, units))

    def drills(self, path, drills):
        by = {}
        for d in drills:
            by.setdefault(d["level"], []).append(d)

        def one(item):
            lv, ds = item
            r = self._call("drills", str(path), lv, ds, str(self.bank))
            if isinstance(r, dict):
                return [{"id": d["id"], "score": 0.0, "tally": "crashed", "events": [], "log": [],
                         "error": r["error"]} for d in ds]
            return r
        return {r["id"]: r for rs in self.pool.map(one, by.items()) for r in rs}


# -- the loop ---------------------------------------------------------------------------------------------------------

def parse(reply):
    """-> note, notebook, program (a whole file) or edits [(search, replace)]."""
    note = re.search(r"NOTE:\s*(.+)", reply)
    book = re.search(r"```notebook\s*\n(.*?)```", reply, re.S)
    edits = re.findall(r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n?>>>>>>> REPLACE", reply, re.S)
    rest = reply if not book else reply.replace(book.group(0), "")
    blocks = [b for b in re.findall(r"```(?:python|py)?\s*\n(.*?)```", rest, re.S) if "<<<<<<< SEARCH" not in b]
    program = max(blocks, key=len) if blocks and not edits else None
    return (note.group(1).strip()[:300] if note else "(no note)"), (book.group(1).strip() if book else None), \
        program, edits


def apply_edits(code, edits):
    for i, (old, new) in enumerate(edits, 1):
        n = code.count(old)
        if n != 1:
            raise ValueError(f"edit {i}: its SEARCH text is found {n} times in the program (it must be exactly once)"
                             f":\n{old[:300]}")
        code = code.replace(old, new)
    return code


def diff_edits(old, new, context=2):
    """The changes from old to new as search/replace edits (lines, with a little context to anchor them)."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            lo, hi = max(0, i1 - context), min(len(a), i2 + context)
            out.append(("".join(a[lo:hi]), "".join(a[lo:i1] + b[j1:j2] + a[i2:hi])))
    return out


def merge(base, programs):
    """base with every program's changes to it applied, in order; a change that no longer applies (it overlaps one
    already in) is left out. -> (code, how many programs went in whole)."""
    code, whole = base, 0
    for prog in programs:
        ok = True
        for old, new in diff_edits(base, prog):
            if code.count(old) == 1:
                code = code.replace(old, new)
            else:
                ok = False
        whole += ok
    return code, whole


def spread(xs):
    m = sum(xs) / len(xs) if xs else 0.0
    return max(0.01, (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5) if xs else 1.0


def compare(new, old, drill_weight=0.5, n=2000, rng=None):
    """Paired: the new program against the best on the same levels and drills. -> (gain, chance it's a gain). Each
    part is measured in its own spread (the best's scores' standard deviation), so levels that score 5% and drills
    that score 90% count alike; the gain is reported in level points."""
    rng = rng or random.Random(0)
    sl, sd = spread(list(old["levels"].values())), spread(list(old["drills"].values()))
    lv = [new["levels"][k] - old["levels"][k] for k in old["levels"] if k in new["levels"]]
    dr = [(new["drills"][k] - old["drills"][k]) * sl / sd for k in old["drills"] if k in new["drills"]]

    def gain(a, b):
        return (sum(a) / len(a) if a else 0.0) + drill_weight * (sum(b) / len(b) if b else 0.0)
    mean = gain(lv, dr)
    if not any(lv) and not any(dr):
        return 0.0, 0.0
    wins = sum(gain([rng.choice(lv) for _ in lv], [rng.choice(dr) for _ in dr]) > 0 for _ in range(n))
    return mean, wins / n


def report_levels(results, detail=True, events=14, logs=10):
    """Every level's tally; what happened and the log only on the first seed of each (the prompt stays short)."""
    out, seen = [], set()
    for r in results:
        seed = f" seed {r['seed']}" if r.get("seed", 1) != 1 or len({x.get("seed") for x in results}) > 1 else ""
        out.append(f"\n## {r['level']}{seed}: {100 * r['score']:.1f}% ({r['tally']}; act() p95 {r['ms_p95']} ms)")
        if r.get("error"):
            out.append("ERROR:\n" + r["error"])
        if detail and r["level"] not in seen:
            seen.add(r["level"])
            if r["events"]:
                out.append("What happened:\n" + "\n".join(cut(r["events"], events)))
            if r["log"]:
                out.append("Its log:\n" + "\n".join(r["log"][:logs]))
    return "\n".join(out)


class Learner:
    def __init__(self, game, generate, out, width=3, workers=4, isolate=True, say=print, label=None, accept=0.9):
        self.game, self.generate, self.out, self.width, self.say = game, generate, Path(out), width, say
        self.accept = accept
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "drills").mkdir(exist_ok=True)
        self.runner = Runner(game, self.out / "drills", workers, isolate)
        self.state_f = self.out / "learn.json"
        self.st = json.loads(self.state_f.read_text()) if self.state_f.exists() else {
            "game": game.name, "model": label, "practice": list(game.practice), "monitor": list(game.monitor),
            "tries": [], "drills": [], "curve": [], "notebook": "", "best": None, "round": 0, "model_calls": 0,
            "model_s": 0.0, "practice_s": 0.0, "started": time.strftime("%Y-%m-%d %H:%M")}
        self.results = {}  # try -> {"levels": [...], "drills": {id: result}}

    # programs and their results
    def path(self, n):
        return self.out / f"try{n:03d}.py"

    def scores(self, n):
        r = self.results[n]
        return {"levels": {(x["level"], x.get("seed", 1)): x["score"] for x in r["levels"]},
                "drills": {k: v["score"] for k, v in r["drills"].items()}}

    def units(self, rnd):
        """Practice is new every round (fresh seeds) when the game can: a lucky score on one set of games is not
        carried forward, and there is no fixed set to overfit."""
        if hasattr(self.game, "units"):
            return self.game.units(rnd)
        return [(lv, s) for lv in self.game.practice for s in getattr(self.game, "seeds", (1,))]

    def evaluate(self, n, units, drills=None):
        """Play try n on these practice units, and on the drills (None: keep its drill results so far)."""
        t0 = time.time()
        levels = self.runner.units(self.path(n), units, self.new(n))
        old = self.results.get(n, {}).get("drills", {})
        dr = (self.runner.drills(self.path(n), drills) if drills else {}) if drills is not None else old
        self.st["practice_s"] += time.time() - t0
        self.results[n] = {"levels": levels, "drills": dr}
        return self.results[n]

    def mean(self, n):
        res = self.results[n]["levels"]
        return round(sum(r["score"] for r in res) / len(res), 4)

    def new(self, n):
        """Where a try's failures are saved while it plays (the bank itself only changes between rounds)."""
        return self.out / "drills" / "new" / f"try{n:03d}"

    def add_drills(self, n):
        """A try's failures become drills; the bank keeps the newest, dropping first those the best already passes."""
        have, new = {d["id"] for d in self.st["drills"]}, []
        for m in (m for r in self.results[n]["levels"] for m in r.get("moments", [])):
            src = self.new(n) / m["state"]
            if m["id"] not in have and src.exists():
                shutil.move(src, self.out / "drills" / m["state"])
                have.add(m["id"])
                new.append(m)
        shutil.rmtree(self.new(n), ignore_errors=True)
        self.st["drills"] += [{**m, "round": self.st["round"]} for m in new]
        best = self.results.get(self.st["best"], {}).get("drills", {})
        while len(self.st["drills"]) > DRILL_CAP:
            passed = [d for d in self.st["drills"] if best.get(d["id"], {}).get("score", 0) >= 0.9]
            drop = (passed or self.st["drills"])[0]
            self.st["drills"].remove(drop)
            (self.out / "drills" / drop["state"]).unlink(missing_ok=True)
        return new

    def best_try(self):
        return next(t for t in self.st["tries"] if t["try"] == self.st["best"])

    # the model
    def focus(self, k):
        """The k-th largest group of drills the best fails (by kind and level), else its weakest levels."""
        res = self.results[self.st["best"]]
        failing = [d for d in self.st["drills"] if res["drills"].get(d["id"], {}).get("score", 1) < 0.7]
        groups = {}
        for d in failing:
            groups.setdefault((d["kind"], d["level"]), []).append(d)
        order = sorted(groups.values(), key=len, reverse=True)
        if not order or k >= len(order) + 1:
            worst = sorted(res["levels"], key=lambda r: r["score"])[:2]
            return "Focus: the practice levels where it scores lowest: " + \
                ", ".join(f"{r['level']} (seed {r.get('seed', 1)})" for r in worst) + \
                ". Find out why from what happened there, and fix the biggest cause.", []
        if k == len(order):
            return "Focus: whatever costs the most score on the practice levels (look at what happened there).", []
        group = order[k][:4]
        lines = [f"Focus: drills the best program fails, of kind '{group[0]['kind']}' on {group[0]['level']} "
                 f"({len(order[k])} of them). Find the cause and fix it in general, not for this spot only."]
        for d in group:
            r = res["drills"][d["id"]]
            lines.append(f"\n### drill {d['id']} (saved {d['t']}s into {d['level']}, a few seconds before: {d['why']})"
                         f"\nThe best program in this drill: {100 * r['score']:.0f}% - {r['tally']}")
            if r.get("error"):
                lines.append("ERROR:\n" + r["error"])
            if r["events"]:
                lines.append("What happened: " + " / ".join(r["events"]))
            if r["log"]:
                lines.append("Its log:\n" + "\n".join(r["log"]))
        return "\n".join(lines), group

    def prompt(self, focus):
        best = self.best_try()
        res = self.results[best["try"]]
        drills = res["drills"]
        kinds = {}
        for d in self.st["drills"]:
            s = drills.get(d["id"], {}).get("score", 0)
            k = kinds.setdefault(d["kind"], [0, 0])
            k[0], k[1] = k[0] + (s >= 0.7), k[1] + 1
        hist = "\n".join(f"- try {t['try']} (round {t['round']}): {t['verdict']} - {t['note']}"
                         for t in self.st["tries"][-30:])
        return "\n\n".join([
            f"# The game's manual\n\n{self.game.manual}",
            "# Notebook (what has been learned so far)\n\n" + (self.st["notebook"] or "(empty)"),
            f"# Tries so far (latest 30)\n\n{hist}",
            f"# The best program (try {best['try']}: {100 * self.mean(best['try']):.1f}% on the latest practice games)\n\n"
            f"```python\n{self.path(best['try']).read_text()}\n```",
            f"# How it did on the practice levels\n{report_levels(res['levels'])}",
            "# Drills: " + (", ".join(f"{k}: passes {a} of {b}" for k, (a, b) in kinds.items()) or "none yet"),
            "# Your task\n\n" + focus + "\n\nOther candidates are working on other problems at the same time.",
        ])

    def candidate(self, k):
        text, _ = self.focus(k)
        t0 = time.time()
        try:
            reply = self.generate(self.prompt(text))
            err = None
        except Exception as e:  # noqa: BLE001
            reply, err = "", f"the model failed: {e}"
        self.st["model_calls"] += 1
        self.st["model_s"] += time.time() - t0
        if err:
            return None, err, None, "(no note)"
        note, book, program, edits = parse(reply)
        try:
            code = program or (apply_edits(self.path(self.st["best"]).read_text(), edits) if edits else None)
            if not code:
                return None, "the reply had neither edits nor a program", book, note
            compile(code, "agent.py", "exec")
        except (ValueError, SyntaxError) as e:
            return None, str(e), book, note
        return code, None, book, note

    # a round
    def round(self):
        st = self.st
        st["round"] += 1
        rnd = st["round"]
        best = st["best"]
        with ThreadPoolExecutor(self.width) as ex:
            made = list(ex.map(self.candidate, range(self.width)))
        tried = []
        for code, err, book, note in made:
            n = len(st["tries"])
            entry = {"try": n, "round": rnd, "note": note, "parent": best}
            st["tries"].append(entry)
            if err:
                self.path(n).write_text(f"# not run: {err}\n")
                entry.update(verdict=f"not run ({err[:120]})", practice=None)
                self.say(f"round {rnd} try {n}: not run - {err[:100]}")
                continue
            self.path(n).write_text(code)
            tried.append((entry, book))
        drills, units = list(st["drills"]), self.units(rnd)
        jobs = [(best, None)] + [(entry["try"], drills) for entry, _ in tried]  # the best plays the new games too
        with ThreadPoolExecutor(len(jobs)) as ex:
            list(ex.map(lambda j: self.evaluate(j[0], units, j[1]), jobs))
        self.best_try().setdefault("rounds", {})[str(rnd)] = self.mean(best)
        old = self.scores(best)
        winner = None
        for entry, book in tried:
            n = entry["try"]
            gain, chance = compare(self.scores(n), old)
            res = self.results[n]
            entry.update(practice=self.mean(n), best_practice=self.mean(best),
                         drills=round(sum(v["score"] for v in res["drills"].values()) / max(1, len(res["drills"])), 4),
                         gain=round(gain, 4), chance=round(chance, 3))
            ok = gain > 0 and chance >= self.accept
            entry["verdict"] = (f"{'ACCEPTED' if ok else 'rejected'}: practice {100 * entry['practice']:.1f}% (best "
                                f"{100 * entry['best_practice']:.1f}% on the same games), drills "
                                f"{100 * entry['drills']:.0f}%, gain {100 * gain:+.1f} points, {100 * chance:.0f}% sure")
            self.say(f"round {rnd} try {n}: {entry['verdict']} - {note_short(entry['note'])}")
            if ok and (winner is None or gain > winner[0]["gain"]):
                winner = (entry, book)
        winner = self.merged(rnd, best, old, tried, winner, units, drills) or winner
        new_drills = self.add_drills(best)
        for entry, _ in tried:  # every program's failures are new experience
            new_drills += self.add_drills(entry["try"])
        if winner:
            entry, book = winner
            st["best"] = entry["try"]
            if book:
                st["notebook"] = book[:6000]
            shutil.copy(self.path(entry["try"]), self.out / "agent.py")
        if new_drills or winner:  # the best is scored on every drill in the bank
            res = self.results[st["best"]]
            missing = [d for d in st["drills"] if d["id"] not in res["drills"]]
            if missing:
                res["drills"].update(self.runner.drills(self.path(st["best"]), missing))
        self.monitor(changed=bool(winner))
        self.save()

    def merged(self, rnd, best, old, tried, winner, units, drills):
        """Candidates that each gained a little, on different problems, are combined into one more candidate: small
        real gains add up instead of each falling short of the bar alone. -> (entry, notebook) if it wins."""
        good = sorted((e for e, _ in tried if e.get("gain", 0) > 0), key=lambda e: -e["gain"])
        if len(good) < 2:
            return None
        base = self.path(best).read_text()
        try:
            code, whole = merge(base, [self.path(e["try"]).read_text() for e in good])
            compile(code, "agent.py", "exec")
        except (SyntaxError, ValueError) as e:
            self.say(f"round {rnd}: the merge of tries {[e['try'] for e in good]} did not compile ({e})")
            return None
        if code == base or whole < 2:
            return None
        n = len(self.st["tries"])
        self.path(n).write_text(code)
        entry = {"try": n, "round": rnd, "parent": best, "merged": [e["try"] for e in good],
                 "note": "merged: " + " + ".join(f"try {e['try']}" for e in good)}
        self.st["tries"].append(entry)
        self.evaluate(n, units, drills)
        gain, chance = compare(self.scores(n), old)
        entry.update(practice=self.mean(n), best_practice=self.mean(best), gain=round(gain, 4),
                     drills=round(sum(v["score"] for v in self.results[n]["drills"].values()) /
                                  max(1, len(self.results[n]["drills"])), 4), chance=round(chance, 3))
        ok = gain > 0 and chance >= self.accept and (winner is None or gain > winner[0]["gain"])
        entry["verdict"] = (f"{'ACCEPTED' if ok else 'rejected'}: practice {100 * entry['practice']:.1f}% (best "
                            f"{100 * entry['best_practice']:.1f}% on the same games), drills "
                            f"{100 * entry['drills']:.0f}%, gain {100 * gain:+.1f} points, {100 * chance:.0f}% sure")
        self.say(f"round {rnd} try {n}: {entry['verdict']} - {entry['note']}")
        book = next((b for e, b in tried if e["try"] == good[0]["try"]), None)
        tried.append((entry, None))
        return (entry, book) if ok else None

    def monitor(self, changed=True):
        st = self.st
        prev = st["curve"][-1]["monitor"] if st["curve"] else None
        if changed or prev is None:
            res = self.runner.levels(self.path(st["best"]), self.game.monitor)
            mon = round(sum(r["score"] for r in res) / len(res), 4)
        else:
            mon = prev
        res = self.results[st["best"]]
        point = {"round": st["round"], "tries": len(st["tries"]), "model_calls": st["model_calls"],
                 "model_s": round(st["model_s"]), "practice_s": round(st["practice_s"]), "best": st["best"],
                 "practice": self.mean(st["best"]), "monitor": mon, "bank": len(st["drills"]),
                 "drills_passed": round(sum(res["drills"].get(d["id"], {}).get("score", 0) >= 0.7
                                            for d in st["drills"]) / max(1, len(st["drills"])), 3)}
        st["curve"].append(point)
        self.say(f"round {st['round']}: best try {st['best']}, practice {100 * point['practice']:.1f}%, monitor "
                 f"{100 * mon:.1f}%, drills {len(st['drills'])} ({100 * point['drills_passed']:.0f}% passed)")

    def save(self):
        self.state_f.write_text(json.dumps(self.st, indent=1))

    def start(self):
        st = self.st
        if st["best"] is None:
            self.path(0).write_text(self.game.starter)
            st["tries"].append({"try": 0, "round": 0, "note": "the starting skeleton", "parent": None})
            self.evaluate(0, self.units(0), [])
            res = self.results[0]
            st["tries"][0].update(practice=self.mean(0), verdict="the start")
            st["best"] = 0
            shutil.copy(self.path(0), self.out / "agent.py")
            self.add_drills(0)
            res["drills"].update(self.runner.drills(self.path(0), st["drills"]) if st["drills"] else {})
            self.monitor()
            self.save()
        elif st["best"] not in self.results:  # resuming: the best plays the last round's games and the drills again
            self.evaluate(st["best"], self.units(st["round"]), st["drills"])
            shutil.rmtree(self.new(st["best"]), ignore_errors=True)

    def run(self, rounds):
        self.start()
        while self.st["round"] < rounds:
            self.round()
        st = self.st
        if self.game.held_out and st.get("held_out", {}).get("try") != st["best"]:
            res = self.runner.levels(self.path(st["best"]), self.game.held_out)
            st["held_out"] = {"try": st["best"], "score": round(sum(r["score"] for r in res) / len(res), 4),
                              "levels": [{k: r[k] for k in ("level", "score", "tally")} for r in res]}
            self.say(f"held out ({len(res)} levels never practised): {100 * st['held_out']['score']:.1f}%")
        st["finished"] = time.strftime("%Y-%m-%d %H:%M")
        self.save()
        (self.out / "summary.json").write_text(json.dumps(summary(st), indent=1))
        return st


def note_short(s):
    return s if len(s) < 110 else s[:107] + "..."


def summary(st):
    """learn.json without the drills' details: what to publish next to the agent."""
    return {**{k: v for k, v in st.items() if k not in ("drills", "tries")}, "drill_bank": len(st["drills"]),
            "tries": [{k: t.get(k) for k in ("try", "round", "verdict", "note")} for t in st["tries"]]}


def model(spec):
    from relaymcp.host import decide
    kind, _, name = spec.partition(":")
    if kind == "copilot":
        return decide.copilot_generate(name, system=SYSTEM, timeout=900)
    gen = decide.generator(spec)
    return lambda p: gen(SYSTEM + "\n\n" + p)


def learn(game, generate, out, rounds=10, width=3, workers=4, isolate=True, say=print, label=None):
    """Practise for `rounds` rounds of `width` candidates; the best program is out/agent.py, the story out/learn.json
    (its outline out/summary.json). Run it again with the same out to carry on where it stopped."""
    return Learner(game, generate, out, width, workers, isolate, say, label).run(rounds)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_run":  # a child process: play a level or a level's drills, print the result
        _, name, method, args = argv
        print(json.dumps(getattr(GAMES[name](), method)(*json.loads(args))), flush=True)
        return
    if argv and argv[0] == "_play":  # older child-process form: one level
        _, name, path, level, seed = argv
        print(json.dumps(GAMES[name]().play(path, level, int(seed))), flush=True)
        return
    ap = argparse.ArgumentParser(prog="spinal learn")
    ap.add_argument("game", choices=sorted(GAMES))
    ap.add_argument("--model", default="copilot:claude-opus-5.5")
    ap.add_argument("--out", help="folder for the tries, the drills, the best agent (agent.py) and learn.json")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--width", type=int, default=3, help="candidates per round, written and played in parallel")
    ap.add_argument("--workers", type=int, default=4, help="levels played at once")
    ap.add_argument("--test", metavar="AGENT", help="just score an agent on the held-out levels")
    a = ap.parse_args(argv)
    game = GAMES[a.game]()
    if a.test:
        if not game.held_out:
            raise SystemExit(f"{a.game} is scored by the arena: spinal arena run --agent plugin:{a.test}:Agent")
        res = Runner(game, Path(a.test).parent / "drills", a.workers).levels(a.test, game.held_out)
        print(report_levels(res, detail=False))
        print(f"\nmean: {100 * sum(r['score'] for r in res) / len(res):.1f}%")
        return
    out = a.out or f"learned/{a.game}-{re.sub(r'[^a-z0-9.]+', '-', a.model.lower())}"
    learn(game, model(a.model), out, a.rounds, a.width, a.workers, label=a.model, say=lambda m: print(m, flush=True))


if __name__ == "__main__":
    main()
