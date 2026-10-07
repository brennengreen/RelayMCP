"""The compiler for Doom: practice with search in the game, then a fast student that keeps improving with practice.

A model-written agent (the base, e.g. the one `spinal learn` wrote) plays. In practice the game can be saved and
loaded, so at every contested moment (a monster on screen, or damage taken) the teacher tries each tactical option
from a saved game for a short while and keeps playing the base afterwards: keep the base's moves, or override its
movement (forward, back, strafe, back-strafe, stand) while it keeps aiming and firing. The options are scored by what
the level's tally rewards (kills, damage dealt, area explored, the exit) and what ends it (damage taken, death), the
best is played, and the moment is labelled with every option's value. A small network (the student) learns to pick
the option from what is on screen; from the next round on, the searches continue with the student in the loop
(expert iteration), and some moments are played the student's way so it learns from its own mistakes (DAgger).

The judged game is real time, with no saves: there the student picks in microseconds. Practice is on MAP05-MAP12,
progress is measured on MAP13-MAP16 (never practised), and MAP01-MAP04 (the Spinal Score) are only played for a
ranked run.

    python -m relaymcp.play.compiler run --base .learn-runs/v2/doom/agent.py --out .science/doom --rounds 6
    python -m relaymcp.play.compiler report --out .science/doom
"""

import argparse
import copy
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

PRACTICE = ("MAP05", "MAP06", "MAP07", "MAP08", "MAP09", "MAP10", "MAP11", "MAP12")
MONITOR = ("MAP13", "MAP14", "MAP15", "MAP16")
OPTIONS = ("base", "forward", "back", "left", "right", "back-left", "back-right", "stand")
MOVES = {1: (1, 0, 0, 0), 2: (0, 1, 0, 0), 3: (0, 0, 1, 0), 4: (0, 0, 0, 1), 5: (0, 1, 1, 0), 6: (0, 1, 0, 1),
         7: (0, 0, 0, 0)}  # forward, back, left, right (the action's slots 2-5)
HOLD = 12  # tics an option is held (about a third of a second)
HORIZON = 48  # tics of the base after it, to see what the option led to
CELL = 64
# what an option is worth: the tally's parts, and what ends a run
VALUE = {"kill": 1.0, "dealt": 0.003, "hurt": -0.02, "died": -3.0, "cell": 0.05, "exit": 5.0}
MARGIN = 0.0  # an option replaces the base's own moves only if it is worth this much more
# experiments override these from the environment, e.g. SPINAL_COMPILER='{"HORIZON": 96, "MARGIN": 0.3}'
for _k, _v in json.loads(os.environ.get("SPINAL_COMPILER") or "{}").items():
    if _k == "VALUE":
        VALUE.update(_v)
    elif _k in ("HOLD", "HORIZON", "MARGIN"):
        globals()[_k] = _v
SLOTS = 6
RAYS = 8


def monster_names():
    from relaymcp.play.doom import MONSTERS
    return sorted(MONSTERS)


NAMES = None
ITEM_KINDS = ("health", "ammo", "weapon", "armor")


# -- what the student sees ------------------------------------------------------------------------------------------

class Walls:
    """The level's blocking lines as arrays, for distances to walls along rays."""

    _cache: dict = {}

    @classmethod
    def of(cls, layout):
        k = id(layout)
        if k not in cls._cache:
            seen, segs = set(), []
            for sec in layout:
                for ln in sec.lines:
                    if ln.is_blocking:
                        key = (ln.x1, ln.y1, ln.x2, ln.y2)
                        if key not in seen:
                            seen.add(key)
                            segs.append(key)
            a = np.array(segs or [(0, 0, 0, 0)], np.float64)
            cls._cache[k] = (a[:, :2], a[:, 2:] - a[:, :2])
        return cls._cache[k]


def rays(st, n=RAYS, far=512.0):
    """Distance to the nearest blocking wall along n directions around the player's facing, over `far`."""
    p, e = Walls.of(st["layout"])
    out = np.ones(n, np.float32)
    x, y = st["x"], st["y"]
    for i in range(n):
        a = math.radians(st["angle"] + 360.0 * i / n)
        dx, dy = math.cos(a), math.sin(a)
        qx, qy = p[:, 0] - x, p[:, 1] - y
        den = dx * e[:, 1] - dy * e[:, 0]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qx * e[:, 1] - qy * e[:, 0]) / den
            u = (qx * dy - qy * dx) / den
        ok = (np.abs(den) > 1e-9) & (t > 0) & (u >= 0) & (u <= 1)
        if ok.any():
            out[i] = min(far, float(t[ok].min())) / far
    return out


class Senses:
    """The student's view of a tic: the player's state, recent damage, the monsters and items on screen, and how far
    the walls are. Keeps a short memory of damage (so it is part of the agent and copied with it)."""

    def __init__(self):
        self.hp = []

    def see(self, st):
        self.hp = (self.hp + [st["health"]])[-36:]

    def __call__(self, st):
        global NAMES
        NAMES = NAMES or monster_names()
        hurt = max(0, self.hp[0] - st["health"]) if self.hp else 0
        v = [st["health"] / 100, min(st["ammo"], 200) / 50, hurt / 20, float(st["ammo"] == 0)]
        mons = [m for m in st["monsters"] if m.get("visible", True)][:SLOTS]
        for k in range(SLOTS):
            if k < len(mons):
                m, b = mons[k], math.radians(mons[k]["bearing"])
                one = [float(m["name"] == n) for n in NAMES]
                v += [1.0, m["distance"] / 1000, math.sin(b), math.cos(b)] + one
            else:
                v += [0.0] * (4 + len(NAMES))
        for kind in ITEM_KINDS:
            it = next((i for i in st["items"] if i["kind"] == kind), None)
            if it:
                b = math.radians(it["bearing"])
                v += [1.0, it["distance"] / 1000, math.sin(b), math.cos(b)]
            else:
                v += [0.0, 0.0, 0.0, 0.0]
        return np.concatenate([np.array(v, np.float32), rays(st)])


def contested(st, senses):
    return any(m.get("visible", True) for m in st["monsters"]) or (
        len(senses.hp) > 1 and senses.hp[0] > st["health"])


# -- the agent: the base, with the student choosing its tactics -----------------------------------------------------

def load_base(path):
    from relaymcp.play.learn import load_agent
    return load_agent(path)


class Net:
    """The student's weights (a 2-layer MLP) and its forward pass, small enough to save as JSON."""

    def __init__(self, W, b, mean, std):
        self.W, self.b, self.mean, self.std = W, b, mean, std

    @classmethod
    def load(cls, path):
        d = json.loads(Path(path).read_text())
        f = lambda xs: [np.array(x, np.float32) for x in xs]  # noqa: E731
        return cls(f(d["W"]), f(d["b"]), np.array(d["mean"], np.float32), np.array(d["std"], np.float32))

    def save(self, path):
        Path(path).write_text(json.dumps({"W": [w.tolist() for w in self.W], "b": [b.tolist() for b in self.b],
                                          "mean": self.mean.tolist(), "std": self.std.tolist(),
                                          "options": list(OPTIONS)}))

    def logits(self, x):
        h = (x - self.mean) / self.std
        for i, (w, b) in enumerate(zip(self.W, self.b)):
            h = h @ w + b
            if i < len(self.W) - 1:
                h = np.maximum(0, h)
        return h


class Compiled:
    """An arena agent: the base plays every tic; when contested, the student picks the tactic every HOLD tics.
    force: (option, tics) to play an option chosen from outside (the teacher)."""

    def __init__(self, base, net=None):
        self.base, self.net, self.senses = base, net, Senses()
        self.option, self.until, self.force = 0, -1, None
        self.cells = set()

    def decide(self, st, tic):
        if self.net is None:
            return 0
        return int(np.argmax(self.net.logits(self.senses(st))))

    def act(self, st, tic):
        self.senses.see(st)
        self.cells.add((int(st["x"] // CELL), int(st["y"] // CELL)))
        a = list(self.base.act(st, tic))
        a += [0] * (8 - len(a))
        if self.force is not None:
            self.option, self.until = self.force[0], tic + self.force[1]
            self.force = None
        elif tic >= self.until:
            self.option, self.until = (self.decide(st, tic), tic + HOLD) if contested(st, self.senses) else (0, tic)
        if self.option:
            a[2:6] = MOVES[self.option]
            a[1] = 1
        return a


# -- practice with search ----------------------------------------------------------------------------------------------

class Game:
    """A practice level in sync mode, played one tic at a time by an agent (as the arena does for a plugin)."""

    def __init__(self, level, seed, seconds):
        from relaymcp.play import doom
        self.doom = doom
        # the time limit is kept here, by tics played: ViZDoom's episode clock runs on through searches and loads
        self.limit = int(seconds * 35)
        self.d = doom.Doom(seconds * 20, seed, level=level, fair=True, realtime=False)
        self.d.new_episode()
        self.d.calibrate()
        self.tic = 0

    def done(self):
        return self.d.g.is_episode_finished() or self.tic >= self.limit

    def state(self):
        s = self.d.g.get_state()
        return self.d.read(s) if s is not None else None

    def step(self, agent):
        st = self.state()
        a = list(agent.act(st, self.tic))
        a[6] = self.d.turn(float(a[6]), gain=1.0, cap=30.0)
        self.d.g.make_action(a)
        self.tic += 1
        return st

    def load(self, path, tic):
        """Back to a saved game. The buttons are released first: ViZDoom carries the last action across a load (the
        turn above all), so without this, plays from the same save differ."""
        if self.d.g.is_episode_finished():  # a search died or ran out of time: a load alone doesn't bring it back
            self.d.g.new_episode()
        self.d.g.set_action([0] * 8)
        self.d.g.load(path)
        self.tic = tic

    def exited(self):
        g = self.d.g
        return g.is_episode_finished() and not g.is_player_dead() and g.get_episode_time() < g.get_episode_timeout() - 2

    def tally(self):
        import vizdoom as vzd
        gv = self.d.g.get_game_variable
        return {"kills": int(gv(vzd.GameVariable.KILLCOUNT)), "secrets": int(gv(vzd.GameVariable.SECRETCOUNT)),
                "dealt": float(gv(vzd.GameVariable.DAMAGECOUNT)), "taken": float(gv(vzd.GameVariable.DAMAGE_TAKEN)),
                "died": bool(self.d.g.is_player_dead())}


def value(before, after, new_cells, exited):
    return (VALUE["kill"] * (after["kills"] - before["kills"]) + VALUE["dealt"] * (after["dealt"] - before["dealt"])
            + VALUE["hurt"] * (after["taken"] - before["taken"]) + VALUE["died"] * after["died"]
            + VALUE["cell"] * new_cells + VALUE["exit"] * exited)


def rollout(game, agent, option, sav, tic0):
    """From the saved game: the option for HOLD tics, then the agent for HORIZON tics. -> value."""
    game.load(sav, tic0)
    before, cells = game.tally(), set(agent.cells)
    agent.force = (option, HOLD)
    for _ in range(HOLD + HORIZON):
        if game.done():
            break
        game.step(agent)
    return value(before, game.tally(), len(agent.cells - cells), game.exited())


def practice(base_path, net_path, level, seed, out, student_share=0.0, search=True, seconds=None):
    """One practice level. search: label contested moments with every option's value and play the best (or, with
    probability student_share, the student's choice). -> the level's tally and score; labels saved to out (.npz)."""
    from relaymcp.play import arena
    from relaymcp.play.learn import Doom as LearnDoom
    monsters, secrets, par = LearnDoom()._check(level) if level not in arena.MAPS else arena.SEASON[level]
    game = Game(level, seed, seconds or min(arena.CAP * par, 300))
    net = Net.load(net_path) if net_path else None
    agent = Compiled(load_base(base_path), net)
    rng = random.Random(seed * 7919 + int(level[3:]))
    X, V, chosen = [], [], []
    sav = os.path.join(tempfile.mkdtemp(prefix="spinal-c-"), "s.sav")
    t0 = time.perf_counter()
    while not game.done():
        st = game.state()
        if st is None:
            break
        if search and game.tic >= agent.until and contested(st, agent.senses):
            x = agent.senses(st)
            game.d.g.save(sav)
            snap, tic0 = copy.deepcopy(agent), game.tic
            vals = []
            for o in range(len(OPTIONS)):
                vals.append(rollout(game, copy.deepcopy(snap), o, sav, tic0))
            game.load(sav, tic0)
            agent = snap
            pick = int(np.argmax(vals))
            if vals[pick] - vals[0] <= MARGIN:
                pick = 0
            if net is not None and rng.random() < student_share:
                pick = int(np.argmax(net.logits(x)))
            X.append(x)
            V.append(vals)
            chosen.append(pick)
            agent.force = (pick, HOLD)
        game.step(agent)
    t = game.tally()
    exited = game.exited()
    game_s = game.tic / 35
    game.d.close()
    k = min(1.0, t["kills"] / monsters)
    s = min(1.0, t["secrets"] / secrets) if secrets else 1.0
    e = min(1.0, par / max(game_s, 1.0)) if exited else 0.0
    if out and X:
        np.savez_compressed(out, X=np.array(X, np.float32), V=np.array(V, np.float32), chosen=np.array(chosen))
    return {"level": level, "seed": seed, "score": round((k + s + e) / 3, 4), "kills": t["kills"],
            "monsters": monsters, "secrets": t["secrets"], "exited": exited, "died": t["died"],
            "game_s": round(game_s, 1), "labels": len(X), "wall_s": round(time.perf_counter() - t0, 1)}


def run_child(*args, timeout=3600):
    cmd = [sys.executable, "-m", "relaymcp.play.compiler", "_practice", json.dumps(args)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
        return json.loads(line) if line else {"error": (p.stderr or p.stdout)[-1500:], "score": 0.0}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "score": 0.0}


# -- training --------------------------------------------------------------------------------------------------------

def soft(V, temp=0.25):
    z = (V - V.max(1, keepdims=True)) / temp
    p = np.exp(z)
    return p / p.sum(1, keepdims=True)


def train(files, out, hidden=128, epochs=40, seed=0):
    from relaymcp.play.student import MLP
    X = np.concatenate([np.load(f)["X"] for f in files])
    V = np.concatenate([np.load(f)["V"] for f in files])
    mean, std = X.mean(0), X.std(0) + 1e-3
    Xn = (X - mean) / std
    Vt = V.copy()
    Vt[:, 0] += MARGIN  # as in practice: the base's own moves unless an option is clearly better
    m = MLP(X.shape[1], len(OPTIONS), hidden, seed).fit(Xn, soft(Vt), epochs=epochs)
    Net(m.W, m.b, mean.astype(np.float32), std.astype(np.float32)).save(out)
    best = Vt.argmax(1)
    agree = float((np.argmax(m.forward(Xn)[2], 1) == best).mean())
    regret = float((V.max(1) - V[np.arange(len(V)), np.argmax(m.forward(Xn)[2], 1)]).mean())
    return {"labels": len(X), "agree": round(agree, 3), "regret": round(regret, 3),
            "base_regret": round(float((V.max(1) - V[:, 0]).mean()), 3)}


# -- the loop ---------------------------------------------------------------------------------------------------------

def say(msg):
    print(time.strftime("%H:%M ") + msg, flush=True)


def evaluate(base, net, maps, seeds, workers):
    jobs = [(base, net, m, s, None, 0.0, False) for m in maps for s in seeds]
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda j: run_child(*j), jobs))
    return res


def mean_score(res):
    return round(100 * sum(r.get("score", 0) for r in res) / max(1, len(res)), 2)


def run(base, out, rounds, workers=4, seeds_per_round=2):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stp = out / "compiler.json"
    st = json.loads(stp.read_text()) if stp.exists() else {"base": str(base), "round": 0, "curve": [], "games": []}
    if not st["curve"]:
        res = evaluate(base, None, MONITOR, (1, 2), workers)
        st["curve"].append({"round": 0, "labels": 0, "monitor": mean_score(res), "monitor_games": res})
        say(f"base on unseen maps: {mean_score(res)}%")
        stp.write_text(json.dumps(st, indent=1))
    while st["round"] < rounds:
        rnd = st["round"] + 1
        net = str(out / f"student{rnd - 1}.json") if rnd > 1 else None
        share = 0.0 if rnd == 1 else min(0.5, 0.15 * (rnd - 1))
        jobs = [(base, net, m, 1000 + seeds_per_round * rnd + i, str(out / f"r{rnd}-{m}-{i}.npz"), share, True)
                for m in PRACTICE for i in range(seeds_per_round)]
        with ThreadPoolExecutor(workers) as ex:
            res = list(ex.map(lambda j: run_child(*j), jobs))
        st["games"] += [{**r, "round": rnd} for r in res]
        files = sorted(str(f) for f in out.glob("r*-MAP*.npz"))
        fit = train(files, out / f"student{rnd}.json")
        mon = evaluate(base, str(out / f"student{rnd}.json"), MONITOR, (1, 2), workers)
        prac = evaluate(base, str(out / f"student{rnd}.json"), PRACTICE, (1,), workers)
        point = {"round": rnd, **fit, "teacher_practice": mean_score(res), "practice": mean_score(prac),
                 "monitor": mean_score(mon), "monitor_games": mon, "practice_games": prac}
        st["curve"].append(point)
        st["round"] = rnd
        stp.write_text(json.dumps(st, indent=1))
        say(f"round {rnd}: {fit['labels']} labels, student agrees {100 * fit['agree']:.0f}%; teacher in practice "
            f"{point['teacher_practice']}%, student practice {point['practice']}%, unseen maps {point['monitor']}%")
    return st


def report(out):
    st = json.loads((Path(out) / "compiler.json").read_text())
    lines = [f"{'round':>5} {'labels':>7} {'agree':>6} {'teacher':>8} {'practice':>8} {'unseen':>7}"]
    for p in st["curve"]:
        lines.append(f"{p['round']:5d} {p['labels']:7d} {100 * p.get('agree', 0):5.0f}% "
                     f"{p.get('teacher_practice', float('nan')):7.2f}% {p.get('practice', float('nan')):7.2f}% "
                     f"{p['monitor']:6.2f}%")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_practice":
        print(json.dumps(practice(*json.loads(argv[1]))), flush=True)
        return
    ap = argparse.ArgumentParser(prog="compiler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--base", required=True, help="the model-written agent the student steers")
    r.add_argument("--out", required=True)
    r.add_argument("--rounds", type=int, default=6)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--seeds", type=int, default=2, help="new seeds per practice map each round")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        print(report(a.out))
        return
    run(a.base, a.out, a.rounds, a.workers, a.seeds)
    print(report(a.out), flush=True)


if __name__ == "__main__":
    main()
