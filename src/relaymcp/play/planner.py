"""H2: does planning through a learned world model beat writing the policy directly, at equal model calls?

Both arms see the true objects (OCAtari) and are written by the same frontier model with the same budget:
- "policy": the model writes act(history, buttons) -> button.
- "planner": the model writes value(history) -> float; a planner tries button sequences inside the world model the
  scientist learned (scientist.py) and presses the first button of the best.
Scores are human-normalised (0% = random play, 100% = the human reference of Mnih et al. 2015), on 5-minute
episodes with sticky buttons (25%), on seeds never practised.

    python -m relaymcp.play.planner run --mode planner --models .science/h1 --out .science/h2/planner
    python -m relaymcp.play.planner report --out .science/h2
"""

import argparse
import importlib.util
import json
import random
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from relaymcp.play.learn import apply_edits, parse
from relaymcp.play.scientist import HISTORY, Atari, clean, show

# (random, human) episode scores, Mnih et al. 2015 / Badia et al. 2020 (Agent57) tables
HUMAN = {"Breakout": (1.7, 30.5), "Pong": (-20.7, 14.6), "Freeway": (0.0, 29.6), "SpaceInvaders": (148.0, 1668.7),
         "Asterix": (210.0, 8503.3), "Seaquest": (68.4, 42054.7), "MsPacman": (307.3, 6951.6), "Boxing": (0.1, 12.1),
         "Frostbite": (65.2, 4334.7), "BankHeist": (14.2, 753.1), "Kangaroo": (52.0, 3035.0),
         "Skiing": (-17098.1, -4336.9)}
STEPS = 4500  # agent steps of 4 frames: 5 minutes, the length of the human reference episodes (Mnih et al. 2015)
STICKY = 0.25  # the standard evaluation protocol (Machado et al. 2018); without it every seed plays the same
HORIZON, CANDIDATES, HOLDS, REPLAN, GAMMA = 10, 24, 6, 2, 0.95
TIME_LIMIT = 600

SYSTEM = """You write programs that play Atari games well, and improve them from evidence of how they played. \
Reason about the game's rules and the failures you are shown; prefer general strategy over special cases."""

POLICY_STARTER = '''"""Presses random buttons (a starting point)."""
import random


def act(history, buttons):
    return random.choice(buttons)
'''

HYBRID_STARTER = '''"""Presses random buttons (a starting point)."""
import random


def act(history, buttons, predict):
    return random.choice(buttons)
'''

VALUE_STARTER = '''"""Every situation is as good as any other (a starting point)."""


def value(history):
    return 0.0
'''


def hns(game, score):
    lo, hi = HUMAN[game]
    return (score - lo) / (hi - lo)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def copy_hist(h):
    return [[dict(o) for o in x] for x in h]


class Planner:
    """Random shooting with the last best plan kept: button sequences held 2-4 steps each (and a few buttons held
    throughout), scored by the discounted sum of value() along the world model's predictions; replans every REPLAN
    steps."""

    def __init__(self, predict, value, buttons, seed=0, horizon=HORIZON, candidates=CANDIDATES):
        self.predict, self.value, self.buttons = predict, value, buttons
        self.rng, self.h, self.n = random.Random(seed), horizon, candidates
        self.prev, self.left = None, 0

    def random_plan(self):
        plan = []
        while len(plan) < self.h:
            plan += [self.rng.choice(self.buttons)] * self.rng.randint(2, 4)
        return plan[:self.h]

    def score(self, hist, plan):
        h, total, disc = copy_hist(hist), 0.0, 1.0
        for a in plan:
            nxt = clean(self.predict(copy_hist(h), a))
            h = (h + [nxt])[-HISTORY:]
            total += disc * float(self.value(copy_hist(h)))
            disc *= GAMMA
        return total

    def act(self, hist):
        if self.prev and self.left > 0:
            self.left -= 1
            self.prev = self.prev[1:] + [self.prev[-1]]
            return self.prev[0]
        holds = self.buttons if len(self.buttons) <= HOLDS else self.rng.sample(self.buttons, HOLDS)
        plans = [[b] * self.h for b in holds]
        plans += [self.random_plan() for _ in range(self.n - len(plans))]
        if self.prev:
            plans[-1] = self.prev[1:] + [self.prev[-1]]
        best = max(plans, key=lambda p: self.score(hist, p))
        self.prev, self.left = best, REPLAN - 1
        return best[0]


def play(game, mode, code, seed, steps=STEPS, world_model=None):
    """One capped episode. -> {"score", "hns", "events", "steps", "ms", "error"}"""
    w = Atari(game)
    e = w.env(STICKY)
    names = e.unwrapped.get_action_meanings()
    e.reset(seed=seed)
    agent = load(code, "agent")
    if mode == "planner":
        planner = Planner(load(world_model, "world_model").predict, agent.value, names, seed)
        choose = planner.act
    elif mode == "hybrid":
        random.seed(seed)
        wm = load(world_model, "world_model").predict

        def choose(h):
            return agent.act(copy_hist(h), list(names), wm)
    else:
        random.seed(seed)
        def choose(h):
            return agent.act(copy_hist(h), list(names))
    hist, score, events, lat, err = [w.objects(e)], 0.0, [], [], None
    lives = e._env.unwrapped.ale.lives()
    t = 0
    for t in range(steps):
        t0 = time.perf_counter()
        try:
            a = choose(hist[-HISTORY:])
            if a not in names:
                raise ValueError(f"unknown button {a!r}")
        except Exception:
            err = err or traceback.format_exc()[-1200:]
            a = "NOOP"
        lat.append(1000 * (time.perf_counter() - t0))
        _, r, term, trunc, _ = e.step(names.index(a))
        hist = (hist + [w.objects(e)])[-HISTORY - 8:]
        score += r
        if r:
            events.append(f"step {t}: +{r:g}")
        lv = e._env.unwrapped.ale.lives()
        if lv < lives:
            events.append(f"step {t}: lost a life; the objects just before:\n"
                          + "".join(f"    {show(h, 20)}\n" for h in hist[-11:-1:2]))
        lives = lv
        if term or trunc:
            events.append(f"step {t}: game over")
            break
    e.close()
    lat.sort()
    return {"score": score, "hns": hns(game, score), "events": events, "steps": t + 1,
            "ms": lat[int(0.95 * (len(lat) - 1))] if lat else 0.0, "error": err}


def play_isolated(*args):
    cmd = [sys.executable, "-m", "relaymcp.play.planner", "_play", json.dumps(args)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIME_LIMIT)
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
        if line:
            return json.loads(line)
        err = (p.stderr or p.stdout)[-2000:]
    except subprocess.TimeoutExpired:
        err = f"the episode took longer than {TIME_LIMIT} s"
    return {"score": HUMAN[args[0]][0], "hns": 0.0, "events": [], "steps": 0, "ms": 0.0, "error": err}


def paired(new, old, n=2000, rng=None):
    rng = rng or random.Random(0)
    d = [a - b for a, b in zip(new, old)]
    if not any(d):
        return 0.0, 0.0
    return sum(d) / len(d), sum(sum(rng.choice(d) for _ in d) > 0 for _ in range(n)) / n


class Coach:
    """Rounds of `width` candidates written by the model; each played on fresh seeds against the best, paired."""

    def __init__(self, game, mode, generate, out, world_model=None, width=2, episodes=4, workers=4, say=print):
        self.game, self.mode, self.generate, self.out = game, mode, generate, Path(out)
        self.wm, self.width, self.episodes, self.say = world_model, width, episodes, say
        self.pool = ThreadPoolExecutor(workers)
        self.out.mkdir(parents=True, exist_ok=True)
        self.state = self.out / "coach.json"

    def path(self, n):
        return self.out / f"try{n:03d}.py"

    def seeds(self, rnd):
        return [1000 + self.episodes * rnd + i for i in range(self.episodes)]

    def evaluate(self, n, seeds):
        return list(self.pool.map(lambda s: play_isolated(self.game, self.mode, str(self.path(n)), s, STEPS,
                                                          self.wm), seeds))

    def save(self):
        self.state.write_text(json.dumps(self.st, indent=1))

    def start(self):
        if self.state.exists():
            self.st = json.loads(self.state.read_text())
            return
        self.path(0).write_text({"policy": POLICY_STARTER, "hybrid": HYBRID_STARTER}.get(self.mode, VALUE_STARTER))
        names = Atari(self.game).actions()
        self.st = {"game": self.game, "mode": self.mode, "buttons": names, "round": 0, "calls": 0, "best": 0,
                   "notebook": "", "tries": [{"try": 0, "verdict": "starter"}], "last": None, "curve": []}
        self.monitor()
        self.save()

    def monitor(self):
        res = self.evaluate(self.st["best"], [1, 2, 3, 4])  # never practised
        point = {"round": self.st["round"], "calls": self.st["calls"], "best": self.st["best"],
                 "hns": sum(r["hns"] for r in res) / len(res), "scores": [r["score"] for r in res],
                 "ms_p95": max(r["ms"] for r in res)}
        self.st["curve"].append(point)
        return point

    def describe(self, res):
        out = []
        for r in res:
            ev = r["events"]
            out.append(f"episode: score {r['score']:g} ({100 * r['hns']:.0f}% of human) in {r['steps']} steps, "
                       f"slowest decisions {r['ms']:.1f} ms\n" + "".join(f"  {x}\n" for x in ev[:14])
                       + (f"  ... {len(ev) - 14} more events\n" if len(ev) > 14 else "")
                       + (f"  ERROR:\n{r['error']}\n" if r.get("error") else ""))
        return "\n".join(out)

    def prompt(self):
        st = self.st
        code = self.path(st["best"]).read_text()
        rules = ""
        if self.mode == "planner":
            wm = Path(self.wm)
            book = json.loads((wm.parent / "science.json").read_text()).get("notebook", "")
            rules = f"""
HOW YOUR PROGRAM IS USED: every {REPLAN} steps a planner tries {CANDIDATES} button sequences of {HORIZON} steps inside a learned world model (predict(history, button) -> next objects, written by a scientist from \
recorded play) and presses the first button of the sequence with the highest sum of value(history) along the \
predicted future (discount {GAMMA}). history is the last up to {HISTORY} object lists, oldest first. Write \
value(history) -> float: how good the situation is for winning (higher is better). The world model is imperfect; \
its rules, as the scientist wrote them:
{book[:3000]}

YOUR TASK: improve value.py."""
        elif self.mode == "hybrid":
            wm = Path(self.wm)
            book = json.loads((wm.parent / "science.json").read_text()).get("notebook", "")
            rules = f"""
HOW YOUR PROGRAM IS USED: each step act(history, buttons, predict) gets the last up to {HISTORY} object lists \
(oldest first; history[-1] is now), the list of button names, and predict(history, button) -> the object list one \
step later: a world model a scientist learned from recorded play (imperfect; about 0.1-1 ms a call). Return the \
button to press. Use predict to look ahead (search, simulate the ball, test what a button does) where it helps, or \
not at all. The world model's rules, as the scientist wrote them:
{book[:3000]}

YOUR TASK: improve policy.py."""
        else:
            rules = f"""
HOW YOUR PROGRAM IS USED: each step act(history, buttons) gets the last up to {HISTORY} object lists (oldest first; \
history[-1] is now) and the list of button names, and returns the button to press. YOUR TASK: improve policy.py."""
        speed = (f"Keep value fast (the planner calls it about {CANDIDATES * HORIZON} times a decision)"
                 if self.mode == "planner" else "Decide well within 66 ms (one step of real time)")
        return f"""GAME: {st['game']} (Atari 2600). One step = 4 frames (15 steps a second). Buttons: \
{', '.join(st['buttons'])}. Objects are read from the console's memory: dicts {{"cat","x","y","w","h"}} (pixels, \
top-left; screen 160x210). Episodes last at most {STEPS} steps (5 minutes); the console \
repeats the previous button instead of the new one 25% of the time. The score is the game's own score, also given as a \
% of a human reference ({HUMAN[st['game']][1]:g}) above random play ({HUMAN[st['game']][0]:g}).
{rules}
{speed}; standard library only; no state that depends on the order of calls.

HOW THE CURRENT PROGRAM PLAYED (fresh seeds)
{self.describe(st['last']) if st['last'] else '(not played yet)'}

{self.tried()}
YOUR NOTEBOOK
{st['notebook'] or '(empty)'}

THE CURRENT PROGRAM
```python
{code}
```

Reply with:
NOTE: one sentence on what you changed and why.
```notebook
what you know about playing this game well, and what to try next (it is all you will remember)
```
Then SEARCH/REPLACE blocks:
<<<<<<< SEARCH
exact lines
=======
new lines
>>>>>>> REPLACE
or the whole new file in one ```python block."""

    def tried(self):
        recent = self.st["tries"][1:][-6:]
        if not recent:
            return ""
        return "RECENT TRIES (only accepted ones changed the program below)\n" + "".join(
            f"  try {t['try']} (round {t.get('round')}): {t['verdict'][:80]}"
            + (f", {100 * t['hns']:.0f}% of human" if "hns" in t else "") + f" - {t.get('note', '')[:200]}\n"
            for t in recent)

    def candidate(self, k):
        try:
            reply = self.generate(self.prompt())
        except Exception as ex:
            return {"error": f"model call failed: {ex!r}"}
        note, book, program, edits = parse(reply)
        try:
            code = program or (apply_edits(self.path(self.st["best"]).read_text(), edits) if edits else None)
        except ValueError as ex:
            return {"error": str(ex), "note": note, "book": book}
        return {"code": code, "note": note, "book": book} if code else {"error": "no change", "note": note}

    def round(self):
        st = self.st
        st["round"] += 1
        seeds = self.seeds(st["round"])
        with ThreadPoolExecutor(self.width + 1) as pool:
            fut_best = pool.submit(self.evaluate, st["best"], seeds)
            cands = list(pool.map(self.candidate, range(self.width)))
            best_res = fut_best.result()
        st["calls"] += self.width
        best = [r["hns"] for r in best_res]
        winner = None
        for c in cands:
            n = len(st["tries"])
            t = {"try": n, "round": st["round"], "note": c.get("note", "")}
            if "code" not in c:
                t["verdict"] = "no program: " + c["error"][:200]
                st["tries"].append(t)
                continue
            self.path(n).write_text(c["code"])
            res = self.evaluate(n, seeds)
            gain, p = paired([r["hns"] for r in res], best)
            t.update(hns=sum(r["hns"] for r in res) / len(res), gain=gain, sure=p,
                     verdict="accepted" if gain > 0 and p >= 0.8 else "rejected")
            st["tries"].append(t)
            self.say(f"{st['game']} {self.mode} round {st['round']} try {n}: {t['verdict']}: {100 * t['hns']:.0f}% "
                     f"of human (best {100 * sum(best) / len(best):.0f}% on the same seeds, {100 * p:.0f}% sure) - "
                     f"{t['note'][:90]}")
            if t["verdict"] == "accepted" and (not winner or gain > winner[1]):
                winner = (n, gain, res, c)
        if winner:
            st["best"], st["last"] = winner[0], winner[2]
            st["notebook"] = (winner[3].get("book") or st["notebook"])[:6000]
        else:  # a rejected try's notebook would describe code that is not there
            st["last"] = best_res
        p = self.monitor()
        self.save()
        self.say(f"{st['game']} {self.mode} round {st['round']}: best try {st['best']}, unseen seeds "
                 f"{100 * p['hns']:.0f}% of human (scores {p['scores']}), decisions p95 {p['ms_p95']:.0f} ms")

    def run(self, rounds):
        self.start()
        while self.st["round"] < rounds:
            self.round()
        return self.st


def model(spec):
    from relaymcp.host import decide
    kind, _, name = spec.partition(":")
    if kind == "copilot":
        return decide.copilot_generate(name, system=SYSTEM, timeout=900)
    gen = decide.generator(spec)
    return lambda p: gen(SYSTEM + "\n\n" + p)


def report(out):
    lines = [f"{'game':14} {'mode':8} {'rnd':>3} {'calls':>5} {'start':>7} {'now':>7} {'p95 ms':>7}"]
    by = {}
    for f in sorted(Path(out).glob("*/*/coach.json")):
        st = json.loads(f.read_text())
        c = st["curve"]
        by.setdefault(st["game"], {})[st["mode"]] = c[-1]["hns"]
        lines.append(f"{st['game']:14} {st['mode']:8} {c[-1]['round']:3d} {c[-1]['calls']:5d} "
                     f"{100 * c[0]['hns']:6.0f}% {100 * c[-1]['hns']:6.0f}% {c[-1]['ms_p95']:7.1f}")
    both = {g: m for g, m in by.items() if "planner" in m and "policy" in m}
    for g, m in by.items():
        if "hybrid" in m and "policy" in m:
            lines.append(f"{g}: hybrid {100 * m['hybrid']:.0f}% vs policy {100 * m['policy']:.0f}%")
    if both:
        wins = sum(m["planner"] > m["policy"] for m in both.values())
        mp = sum(m["planner"] for m in both.values()) / len(both)
        mq = sum(m["policy"] for m in both.values()) / len(both)
        lines.append(f"\nplanner beats policy on {wins}/{len(both)} games; mean {100 * mp:.0f}% vs {100 * mq:.0f}% "
                     "of human")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_play":
        print(json.dumps(play(*json.loads(argv[1]))), flush=True)
        return
    ap = argparse.ArgumentParser(prog="planner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["planner", "policy", "hybrid"], required=True)
    r.add_argument("--games", required=True)
    r.add_argument("--models", help="the scientist's folder (planner mode): <models>/<game>/world_model.py")
    r.add_argument("--out", required=True)
    r.add_argument("--rounds", type=int, default=6)
    r.add_argument("--width", type=int, default=2)
    r.add_argument("--parallel", type=int, default=2, help="games at once")
    r.add_argument("--workers", type=int, default=3, help="episodes at once, per game")
    r.add_argument("--model", default="copilot:claude-opus-5.5")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        print(report(a.out))
        return
    gen = model(a.model)

    def one(game):
        wm = None
        if a.mode in ("planner", "hybrid"):
            wm = str(Path(a.models) / game / "world_model.py")
            if not Path(wm).exists():  # the scientist has not finished: use its best so far
                st = json.loads((Path(a.models) / game / "science.json").read_text())
                wm = str(Path(a.models) / game / f"try{st['best']:03d}.py")
        try:
            Coach(game, a.mode, gen, Path(a.out) / game / a.mode, wm, a.width, workers=a.workers,
                  say=lambda m: print(time.strftime("%H:%M ") + m, flush=True)).run(a.rounds)
        except Exception:
            print(f"{game} FAILED\n{traceback.format_exc()}", flush=True)
    with ThreadPoolExecutor(a.parallel) as pool:
        list(pool.map(one, a.games.split(",")))
    print(report(a.out), flush=True)


if __name__ == "__main__":
    main()
