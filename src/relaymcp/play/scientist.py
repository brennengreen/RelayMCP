"""The scientist: a frontier model learns a game's rules as code, by experiment (H1 of docs/research/learning-games.md).

For each game the model writes `predict(history, action) -> next objects`, a world model in plain Python. It sees
where the current model is most wrong, may run experiments (from any recorded moment, its own button presses), and
edits the code. A change is kept only if it predicts the recorded play better; progress is measured on play from
seeds it never saw, against the true object positions read from the console's memory (OCAtari), and against two
baselines: "nothing moves" and "everything keeps its velocity".

    python -m relaymcp.play.scientist run --out .science/h1 --rounds 8
    python -m relaymcp.play.scientist report --out .science/h1
"""

import argparse
import json
import math
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from relaymcp.play.learn import apply_edits, parse

PRACTICE = ["Breakout", "Pong", "Freeway", "SpaceInvaders", "Asterix", "Seaquest", "MsPacman", "Boxing"]
HELD_OUT = ["Frostbite", "BankHeist", "Kangaroo", "Skiing"]
CAP = 16.0  # an object's error is its distance in pixels, at most CAP; a missing or extra object costs CAP
FRAMESKIP = 4
HISTORY = 4
TRAIN_SEEDS, TEST_SEEDS = (0, 1), (100, 101)
STEPS = 1500  # per seed
MAX_EXPERIMENTS, MAX_EXPERIMENT_STEPS = 3, 30

SYSTEM = """You are a scientist discovering the rules of a video game, and you write them down as a Python world \
model that predicts what happens next. You are judged only on how well your code predicts play you have not seen. \
Reason from evidence: form hypotheses, test them with experiments, keep what predicts. Prefer general rules \
(velocities, bounces, walls, spawns, periodic motion, what the player's buttons do) over memorised special cases."""

STARTER = '''"""World model: nothing moves (a starting point)."""


def predict(history, action):
    return [dict(o) for o in history[-1]]
'''


# -- objects and error ------------------------------------------------------------------------------------------------

def show(objs, limit=40):
    """A compact one-line view of an object list."""
    parts = [f"{o['cat']}({o['x']},{o['y']} {o['w']}x{o['h']})" for o in objs[:limit]]
    return " ".join(parts) + (f" +{len(objs) - limit} more" if len(objs) > limit else "") or "(nothing)"


def match(pred, gt):
    """Pairs of (pred, gt) of the same category, nearest first, and the unmatched of each. -> (pairs, extra, missing)
    with pairs as (p, g, distance)."""
    pairs, extra, missing = [], [], []
    for cat in {o["cat"] for o in pred} | {o["cat"] for o in gt}:
        P = [o for o in pred if o["cat"] == cat]
        G = [o for o in gt if o["cat"] == cat]
        # objects exactly where predicted pair at once (most objects, most steps, don't move)
        at = {}
        for j, g in enumerate(G):
            at.setdefault((g["x"], g["y"]), []).append(j)
        up, ug = set(), set()
        for i, p in enumerate(P):
            js = at.get((p["x"], p["y"]))
            if js:
                j = js.pop()
                up.add(i)
                ug.add(j)
                pairs.append((p, G[j], 0.0))
        P2 = [i for i in range(len(P)) if i not in up]
        G2 = [j for j in range(len(G)) if j not in ug]
        cand = sorted((math.hypot(P[i]["x"] - G[j]["x"], P[i]["y"] - G[j]["y"]), i, j) for i in P2 for j in G2)
        for d, i, j in cand:
            if i not in up and j not in ug:
                up.add(i)
                ug.add(j)
                pairs.append((P[i], G[j], d))
        extra += [P[i] for i in range(len(P)) if i not in up]
        missing += [G[j] for j in range(len(G)) if j not in ug]
    return pairs, extra, missing


def error(pred, gt):
    """Total error of a predicted object list against the truth, and how much each category contributes."""
    pairs, extra, missing = match(pred, gt)
    by = {}
    for _p, g, d in pairs:
        by[g["cat"]] = by.get(g["cat"], 0.0) + min(CAP, d)
    for o in extra + missing:
        by[o["cat"]] = by.get(o["cat"], 0.0) + CAP
    return sum(by.values()), by


def clean(pred):
    """The model's output as a list of object dicts with int-able x, y, w, h, or raise."""
    out = []
    for o in pred:
        out.append({"cat": str(o["cat"]), "x": float(o["x"]), "y": float(o["y"]),
                    "w": float(o.get("w", 0)), "h": float(o.get("h", 0))})
    return out


def constant_velocity(history, action=None):
    """Baseline: every object keeps the motion it had over the last step."""
    last = history[-1]
    if len(history) < 2:
        return [dict(o) for o in last]
    pairs, _, _ = match(last, history[-2])
    out = [dict(o) for o in last]
    moved = {id(p): (p["x"] - g["x"], p["y"] - g["y"]) for p, g, d in pairs if d <= CAP}
    for o, src in zip(out, last):
        dx, dy = moved.get(id(src), (0, 0))
        o["x"], o["y"] = src["x"] + dx, src["y"] + dy
    return out


def no_change(history, action=None):
    return [dict(o) for o in history[-1]]


# -- recorded play ----------------------------------------------------------------------------------------------------

def transitions(segments):
    """(segment index, step, history, action, next objects) for every step of every recorded segment."""
    for si, seg in enumerate(segments):
        for t, a in enumerate(seg["actions"]):
            yield si, t, seg["objs"][max(0, t + 1 - HISTORY):t + 1], a, seg["objs"][t + 1]


class Atari:
    """An Atari game seen through OCAtari: true objects from the console's memory, steps of FRAMESKIP frames."""

    def __init__(self, game):
        self.game = game
        self.lock = threading.Lock()

    def env(self, sticky=0.0):
        """sticky: the chance the console repeats the last button instead (0.25 is the standard evaluation
        protocol, Machado et al. 2018: without it a seed changes nothing)."""
        from ocatari.core import OCAtari
        return OCAtari(f"ALE/{self.game}-v5", mode="ram", hud=False, render_mode="rgb_array", frameskip=FRAMESKIP,
                       repeat_action_probability=sticky)

    @staticmethod
    def objects(e):
        return [{"cat": o.category, "x": int(o.x), "y": int(o.y), "w": int(o.w), "h": int(o.h)}
                for o in e.objects if o.category != "NoObject" and getattr(o, "visible", True)]

    def actions(self):
        e = self.env()
        names = e.unwrapped.get_action_meanings()
        e.close()
        return names

    def record(self, seed, steps=STEPS):
        """Play with a random policy that holds each button 1-8 steps; a segment per life of the console (a reset
        seed and the presses, so any moment can be replayed exactly)."""
        rng = random.Random(seed)
        e, names, segs, left, k = self.env(), None, [], steps, 0
        names = e.unwrapped.get_action_meanings()
        while left > 0:
            rs = seed * 1000 + k
            e.reset(seed=rs)
            seg = {"reset": rs, "actions": [], "objs": [self.objects(e)]}
            a, hold = 0, 0
            while left > 0:
                if hold <= 0:
                    a, hold = rng.randrange(len(names)), rng.randint(1, 8)
                hold -= 1
                _, _, term, trunc, _ = e.step(a)
                seg["actions"].append(names[a])
                seg["objs"].append(self.objects(e))
                left -= 1
                if term or trunc:
                    break
            segs.append(seg)
            k += 1
        e.close()
        return segs

    def experiment(self, seg, step, actions):
        """From a recorded moment (segment, step), press `actions`; -> the objects before and after each press."""
        e = self.env()
        names = e.unwrapped.get_action_meanings()
        e.reset(seed=seg["reset"])
        for a in seg["actions"][:step]:
            e.step(names.index(a))
        out = [self.objects(e)]
        for a in actions:
            _, _, term, trunc, _ = e.step(names.index(a))
            out.append(self.objects(e))
            if term or trunc:
                break
        e.close()
        return out


# -- scoring the model's code in a child process ---------------------------------------------------------------------

def score_file(code_path, data_path):
    """Run predict over every recorded transition. -> {"errors": [...], "by": {cat: total}, "failures": n,
    "first_failure": str}"""
    import copy
    import importlib.util
    sys.path.insert(0, str(Path(code_path).resolve().parent))  # a library.py next to it can be imported
    spec = importlib.util.spec_from_file_location("world_model", code_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    segs = json.loads(Path(data_path).read_text())
    errs, by, fails, first = [], {}, 0, None
    for _, _, hist, a, nxt in transitions(segs):
        try:
            pred = clean(mod.predict(copy.deepcopy(hist), a))
        except Exception:
            fails += 1
            first = first or traceback.format_exc()[-1500:]
            pred = []
        e, b = error(pred, nxt)
        errs.append(round(e, 3))
        for k, v in b.items():
            by[k] = by.get(k, 0.0) + v
    return {"errors": errs, "by": by, "failures": fails, "first_failure": first}


def rollout_file(code_path, data_path, k=8, every=10):
    """Open-loop: from the true history at every `every`-th step, feed the model its own predictions for k steps of
    the recorded buttons; -> mean error at step k (what a planner relies on)."""
    import copy
    import importlib.util
    sys.path.insert(0, str(Path(code_path).resolve().parent))
    spec = importlib.util.spec_from_file_location("world_model", code_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return rollout_errors(mod.predict, json.loads(Path(data_path).read_text()), k, every, copy.deepcopy)


def rollout_errors(fn, segs, k=8, every=10, copy=lambda x: [[dict(o) for o in h] for h in x]):
    errs = []
    for seg in segs:
        for t in range(0, len(seg["actions"]) - k, every):
            hist = copy(seg["objs"][max(0, t + 1 - HISTORY):t + 1])
            try:
                for j in range(k):
                    nxt = clean(fn(copy(hist), seg["actions"][t + j]))
                    hist = (hist + [nxt])[-HISTORY:]
                errs.append(error(hist[-1], seg["objs"][t + k])[0])
            except Exception:
                errs.append(error([], seg["objs"][t + k])[0])
    return sum(errs) / max(1, len(errs))


def score(code_path, data_path, timeout=300, mode="_score"):
    cmd = [sys.executable, "-m", "relaymcp.play.scientist", mode, str(code_path), str(data_path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
        if line:
            return json.loads(line)
        return {"crash": (p.stderr or p.stdout)[-2500:]}
    except subprocess.TimeoutExpired:
        return {"crash": f"scoring took longer than {timeout} s (predict is too slow or stuck)"}


def baseline(fn, segs):
    return [error(clean(fn(h, a)), n)[0] for _, _, h, a, n in transitions(segs)]


def paired(new, old, n=2000, rng=None):
    """-> (relative reduction of mean error, chance the reduction is real) from a paired bootstrap."""
    rng = rng or random.Random(0)
    d = [o - x for x, o in zip(new, old)]
    mo = sum(old) / len(old) or 1e-9
    mean = sum(d) / len(d)
    if not any(d):
        return 0.0, 0.0
    # bootstrap blocks of 25 steps: neighbouring steps are not independent
    blocks = [d[i:i + 25] for i in range(0, len(d), 25)]
    wins = 0
    for _ in range(n):
        s = [x for _ in blocks for x in rng.choice(blocks)]
        wins += sum(s) > 0
    return mean / mo, wins / n


# -- the loop ---------------------------------------------------------------------------------------------------------

EXPERIMENT_RE = re.compile(r"^EXPERIMENT:?\s*(\{.*\})\s*$", re.M)


def experiments_in(reply):
    out = []
    for m in EXPERIMENT_RE.finditer(reply):
        try:
            x = json.loads(m.group(1))
            out.append({"segment": int(x["segment"]), "step": int(x["step"]),
                        "actions": [str(a) for a in x["actions"]][:MAX_EXPERIMENT_STEPS]})
        except Exception:
            continue
    return out


class Scientist:
    def __init__(self, world, generate, out, width=2, experiments=True, say=print, library=None):
        self.world, self.generate, self.out = world, generate, Path(out)
        self.library = Path(library).read_text() if library else None
        self.width, self.use_experiments, self.say = width, experiments, say
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "science.json"

    # data
    def data(self):
        tr, te = self.out / "train.json", self.out / "test.json"
        if not tr.exists():
            tr.write_text(json.dumps([s for seed in TRAIN_SEEDS for s in self.world.record(seed)]))
            te.write_text(json.dumps([s for seed in TEST_SEEDS for s in self.world.record(seed)]))
        self.train, self.test = json.loads(tr.read_text()), json.loads(te.read_text())
        return tr, te

    def save(self):
        self.state_path.write_text(json.dumps(self.st, indent=1))

    def path(self, n):
        return self.out / f"try{n:03d}.py"

    def start(self):
        tr, te = self.data()
        if self.state_path.exists():
            self.st = json.loads(self.state_path.read_text())
            return
        self.path(0).write_text(STARTER)
        if self.library:
            (self.out / "library.py").write_text(self.library)
        base = {"no_change": baseline(no_change, self.train), "constant_velocity": baseline(constant_velocity,
                                                                                            self.train)}
        test_base = {"no_change": baseline(no_change, self.test),
                     "constant_velocity": baseline(constant_velocity, self.test)}
        self.st = {"game": self.world.game, "actions": self.world.actions(), "best": 0, "round": 0, "calls": 0,
                   "notebook": "", "tries": [{"try": 0, "verdict": "starter", "note": "nothing moves"}],
                   "pending": [], "results": [], "train_base": {k: sum(v) / len(v) for k, v in base.items()},
                   "test_base": {k: sum(v) / len(v) for k, v in test_base.items()}, "curve": []}
        r = score(self.path(0), tr)
        self.st["best_train"] = r
        self.monitor()
        self.save()

    def monitor(self):
        r = score(self.path(self.st["best"]), self.out / "test.json")
        m = sum(r["errors"]) / len(r["errors"]) if "errors" in r else None
        tb, trb = self.st["test_base"], self.st["best_train"]
        point = {"round": self.st["round"], "calls": self.st["calls"], "best": self.st["best"],
                 "train": sum(trb["errors"]) / len(trb["errors"]), "test": m,
                 "vs_no_change": m / tb["no_change"] if m is not None and tb["no_change"] else None,
                 "vs_constant_velocity": m / tb["constant_velocity"] if m is not None and tb["constant_velocity"]
                 else None}
        self.st["curve"].append(point)
        return point

    # prompt
    def worst(self, k=6, offset=0):
        errs = self.st["best_train"]["errors"]
        ts = list(transitions(self.train))
        order = sorted(range(len(errs)), key=lambda i: -errs[i])
        # spread the examples out: at most one per 10 steps of a segment
        picked, seen = [], set()
        for i in order:
            si, t = ts[i][0], ts[i][1]
            if (si, t // 10) in seen:
                continue
            seen.add((si, t // 10))
            picked.append(i)
            if len(picked) >= k * (offset + 1):
                break
        return [ts[i] + (errs[i],) for i in picked[k * offset:k * (offset + 1)]]

    def prompt(self, k):
        st, code = self.st, self.path(self.st["best"]).read_text()
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"wm{k}", self.path(st["best"]))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            mod = None
        lines = []
        for si, t, hist, a, nxt, e in self.worst(offset=k):
            try:
                pred = clean(mod.predict([[dict(o) for o in x] for x in hist], a)) if mod else []
            except Exception as ex:
                pred = f"(raised {ex!r})"
            _, by = error(pred, nxt) if isinstance(pred, list) else (0, {})
            lines.append(f"segment {si} step {t}, error {e:.0f} (by category: "
                         + ", ".join(f"{c} {v:.0f}" for c, v in sorted(by.items(), key=lambda x: -x[1])[:5]) + ")\n"
                         + "".join(f"  t-{len(hist) - 1 - j}: {show(h)}\n" for j, h in enumerate(hist))
                         + f"  action: {a}\n  truth t+1: {show(nxt)}\n"
                         + f"  yours t+1: {show(pred) if isinstance(pred, list) else pred}\n")
        tb = st["train_base"]
        best = st["best_train"]
        by = sorted(best["by"].items(), key=lambda x: -x[1])
        n = len(best["errors"])
        exp = ""
        if st["results"]:
            exp = "\nRESULTS OF YOUR LAST EXPERIMENTS\n" + "\n".join(st["results"]) + "\n"
        fail = ""
        if best.get("failures"):
            fail = f"\nYour predict raised on {best['failures']} steps (counted as predicting nothing):\n" \
                   f"{best['first_failure']}\n"
        recent = [t for t in st["tries"][1:] if "train" in t or "verdict" in t][-6:]
        tried = "".join(f"  try {t['try']} (round {t.get('round')}): {t['verdict'][:80]}"
                        + (f", error {t['train']:.2f}" if "train" in t else "") + f" - {t.get('note', '')[:200]}\n"
                        for t in recent)
        tried = f"\nRECENT TRIES (only accepted ones changed the program below)\n{tried}" if tried else ""
        segs = ", ".join(f"{i}: {len(s['actions'])} steps" for i, s in enumerate(self.train))
        sample_seg = self.train[0]
        sample = "".join(f"  step {t}: {show(sample_seg['objs'][t], 12)}  then {sample_seg['actions'][t]}\n"
                         for t in range(0, min(12, len(sample_seg["actions"]))))
        return f"""GAME: {st['game']} (Atari 2600). One step = {FRAMESKIP} frames. Buttons: {', '.join(st['actions'])}.
Objects are read from the console's memory each step: category, x, y (top left, pixels; the screen is 160 wide, 210 \
high), w, h. Recorded play: a random player holding each button 1-8 steps; segments (one per game over): {segs}.

YOUR TASK: improve world_model.py. predict(history, action) gets the last up to {HISTORY} object lists (oldest first; \
history[-1] is now) as lists of dicts {{"cat","x","y","w","h"}} and the button pressed now, and returns the object \
list one step later. Error per step: for each category, objects are paired nearest first; a pair costs its distance \
(at most {CAP:.0f}); an object you miss or invent costs {CAP:.0f}. Lower is better; it is measured on play you never \
see. Keep predict fast (well under 1 ms) and deterministic; no imports beyond the standard library.

SCORE NOW: mean error per step {sum(best['errors']) / n:.2f} over {n} steps (nothing-moves baseline \
{tb['no_change']:.2f}, keep-velocity baseline {tb['constant_velocity']:.2f}).
Error by category (total): {', '.join(f'{c} {v:.0f}' for c, v in by[:10])}
{fail}
THE START OF SEGMENT 0
{sample}
WHERE THE MODEL IS MOST WRONG
{chr(10).join(lines)}{exp}{tried}
{self.library_text()}
YOUR NOTEBOOK (rules you believe, open questions)
{st['notebook'] or '(empty)'}

world_model.py NOW
```python
{code}
```

Reply with:
NOTE: one sentence on what you changed and why.
```notebook
your updated notebook: the rules you have evidence for, and what is still unknown (it is all you will remember)
```
{"Up to " + str(MAX_EXPERIMENTS) + " experiments, run before your next turn, one per line, e.g." + chr(10)
 + 'EXPERIMENT: {"segment": 0, "step": 40, "actions": ["FIRE", "NOOP", "LEFT"]}' + chr(10)
 + f"(replays segment 0 to step 40, then presses those buttons, at most {MAX_EXPERIMENT_STEPS}; you see the "
 + "objects after each press). Use them to test a hypothesis you are unsure of." + chr(10)
 if self.use_experiments else ""}
Then changes to world_model.py as SEARCH/REPLACE blocks:
<<<<<<< SEARCH
exact lines from the file
=======
new lines
>>>>>>> REPLACE
or the whole new file in one ```python block."""

    def library_text(self):
        if not self.library:
            return ""
        return ("\nYOUR LIBRARY: rules and tools distilled from the world models of other games you learned; "
                "world_model.py may `from library import ...` (it sits next to it). Use what fits; the game is new.\n"
                f"```python\n{self.library}\n```\n")

    def candidate(self, k):
        st = self.st
        try:
            reply = self.generate(self.prompt(k))
        except Exception as ex:
            return {"error": f"model call failed: {ex!r}"}
        note, book, program, edits = parse(reply)
        exps = experiments_in(reply) if self.use_experiments else []
        base = self.path(st["best"]).read_text()
        try:
            code = program if program else apply_edits(base, edits) if edits else None
        except ValueError as ex:
            return {"error": str(ex), "note": note, "book": book, "experiments": exps}
        if not code:
            return {"error": "no change", "note": note, "book": book, "experiments": exps}
        return {"code": code, "note": note, "book": book, "experiments": exps}

    def run_experiments(self, exps):
        out = []
        for x in exps[:MAX_EXPERIMENTS]:
            try:
                seg = self.train[x["segment"]]
                step = max(0, min(x["step"], len(seg["actions"])))
                bad = [a for a in x["actions"] if a not in self.st["actions"]]
                if bad:
                    out.append(f"{json.dumps(x)}: unknown buttons {bad}")
                    continue
                objs = self.world.experiment(seg, step, x["actions"])
                body = f"  start: {show(objs[0], 25)}\n" + "".join(
                    f"  {a} -> {show(o, 25)}\n" for a, o in zip(x["actions"], objs[1:]))
                out.append(f"{json.dumps(x)}\n{body[:3500]}")
            except Exception as ex:
                out.append(f"{json.dumps(x)}: failed ({ex!r})")
        return out

    def round(self):
        st = self.st
        st["round"] += 1
        rnd = st["round"]
        with ThreadPoolExecutor(self.width) as pool:
            cands = list(pool.map(self.candidate, range(self.width)))
        st["calls"] += self.width
        best_errs = st["best_train"]["errors"]
        results, exps, winner = [], [], None
        for c in cands:
            n = len(st["tries"])
            t = {"try": n, "round": rnd, "note": c.get("note", ""), "book": c.get("book")}
            exps += c.get("experiments", [])
            if "code" not in c:
                t["verdict"] = f"no program: {c['error'][:200]}"
                st["tries"].append(t)
                continue
            self.path(n).write_text(c["code"])
            r = score(self.path(n), self.out / "train.json")
            if "crash" in r:
                t["verdict"] = "crashed: " + r["crash"][-300:]
                st["tries"].append(t)
                continue
            gain, p = paired(r["errors"], best_errs)
            mean = sum(r["errors"]) / len(r["errors"])
            t.update(train=mean, gain=gain, sure=p, failures=r["failures"])
            ok = gain > 0.005 and p >= 0.9
            t["verdict"] = "accepted" if ok else "rejected"
            st["tries"].append(t)
            if ok and (winner is None or gain > winner[1]):
                winner = (n, gain, r, c)
            results.append(t)
            self.say(f"{st['game']} round {rnd} try {n}: {t['verdict']}: train error {mean:.2f} "
                     f"({100 * gain:+.1f}%, {100 * p:.0f}% sure) - {t['note'][:100]}")
        if winner:
            n, _, r, c = winner
            st["best"], st["best_train"] = n, r
            for t in st["tries"]:
                if t["try"] == n:
                    t["verdict"] = "ACCEPTED"
        if winner and winner[3].get("book"):  # a rejected try's notebook would describe code that is not there
            st["notebook"] = winner[3]["book"][:6000]
        st["results"] = self.run_experiments(exps) if self.use_experiments else []
        st["experiments_run"] = st.get("experiments_run", 0) + len(st["results"])
        p = self.monitor()
        self.save()
        self.say(f"{st['game']} round {rnd}: best try {st['best']}, train {p['train']:.2f}, unseen {p['test']:.2f} "
                 f"= {100 * p['vs_no_change']:.0f}% of nothing-moves, {100 * p['vs_constant_velocity']:.0f}% of "
                 f"keep-velocity")

    def run(self, rounds):
        self.start()
        while self.st["round"] < rounds:
            self.round()
        (self.out / "world_model.py").write_text(self.path(self.st["best"]).read_text())
        return self.st


LIBRARY_PROMPT = """Below are world models you wrote for {n} Atari games, each learned from recorded play, with \
your notebooks. Distil them into library.py: a module of general, reusable building blocks for world models of NEW \
Atari games you have not seen (e.g. pairing objects across steps, velocity and acceleration estimates, bounces off \
walls and paddles, periodic and lane motion, scrolling, spawning and disappearing objects, player movement driven by \
buttons with lag and inertia, blinking objects, collisions). Each function: a docstring saying what rule it models \
and how to fit or call it; pure functions or small classes; standard library only; no game-specific constants that \
would not transfer. Also include a short docstring at the top listing the rules that recurred across games and how to \
test for each in a new game. Reply with the whole library in one ```python block.

{models}"""


def distill(models_dir, games, generate):
    """One model call: the world models of `games` -> library.py source (checked that it imports)."""
    parts = []
    for g in games:
        d = Path(models_dir) / g
        st = json.loads((d / "science.json").read_text())
        parts.append(f"=== {g} ===\nNOTEBOOK:\n{st['notebook'][:3000]}\nworld_model.py:\n```python\n"
                     f"{(d / 'world_model.py').read_text()}\n```\n")
    reply = generate(LIBRARY_PROMPT.format(n=len(games), models="\n".join(parts)))
    _, _, program, _ = parse(reply)
    if not program:
        raise ValueError("no library in the reply")
    compile(program, "library.py", "exec")
    ns = {}
    exec(program, ns)
    return program


def model(spec):
    from relaymcp.host import decide
    kind, _, name = spec.partition(":")
    if kind == "copilot":
        return decide.copilot_generate(name, system=SYSTEM, timeout=900)
    gen = decide.generator(spec)
    return lambda p: gen(SYSTEM + "\n\n" + p)


def report(out, rollout=False):
    rows, roll = [], []
    for f in sorted(Path(out).glob("*/science.json")):
        st = json.loads(f.read_text())
        c = st["curve"]
        rows.append((st["game"], c[-1]["round"], c[-1]["calls"], st["test_base"]["no_change"],
                     st["test_base"]["constant_velocity"], c[0]["test"], c[-1]["test"], c[-1]["vs_no_change"],
                     c[-1]["vs_constant_velocity"], st.get("experiments_run", 0)))
        if rollout:
            test = json.loads((f.parent / "test.json").read_text())
            r = score(f.parent / f"try{st['best']:03d}.py", f.parent / "test.json", mode="_rollout")
            roll.append((st["game"], rollout_errors(no_change, test), rollout_errors(constant_velocity, test),
                         r.get("rollout")))
    lines = [f"{'game':14} {'rnd':>3} {'calls':>5} {'no-chg':>7} {'keep-v':>7} {'start':>7} {'now':>7} "
             f"{'/no-chg':>7} {'/keep-v':>7} {'exps':>4}"]
    for g, r, n, b0, b1, s, e, v0, v1, x in rows:
        lines.append(f"{g:14} {r:3d} {n:5d} {b0:7.2f} {b1:7.2f} {s:7.2f} {e:7.2f} {100 * v0:6.0f}% "
                     f"{100 * v1:6.0f}% {x:4d}")
    passed = sum(1 for row in rows if row[7] <= 0.5)
    lines.append(f"\nH1 gate (error <= 50% of nothing-moves on unseen play): {passed}/{len(rows)} games "
                 f"(pass needs 8 of 12)")
    lines.append(f"beats keep-velocity: {sum(1 for row in rows if row[8] < 1)}/{len(rows)} games")
    if roll:
        lines.append(f"\n8 steps open-loop on unseen play\n{'game':14} {'no-chg':>7} {'keep-v':>7} {'model':>7} "
                     f"{'/no-chg':>7}")
        for g, a, b, m in roll:
            lines.append(f"{g:14} {a:7.2f} {b:7.2f} {m if m is not None else float('nan'):7.2f} "
                         f"{100 * m / a if m is not None and a else float('nan'):6.0f}%")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_score":
        print(json.dumps(score_file(argv[1], argv[2])), flush=True)
        return
    if argv and argv[0] == "_rollout":
        print(json.dumps({"rollout": rollout_file(argv[1], argv[2])}), flush=True)
        return
    ap = argparse.ArgumentParser(prog="scientist")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--games", default=",".join(PRACTICE + HELD_OUT))
    r.add_argument("--rounds", type=int, default=8)
    r.add_argument("--width", type=int, default=2)
    r.add_argument("--parallel", type=int, default=4, help="games learned at once")
    r.add_argument("--no-experiments", action="store_true")
    r.add_argument("--library", help="library.py offered to the world models (see distill)")
    r.add_argument("--model", default="copilot:claude-opus-5.5")
    d = sub.add_parser("distill")
    d.add_argument("--models", required=True)
    d.add_argument("--games", default=",".join(PRACTICE))
    d.add_argument("--out", required=True, help="library.py to write")
    d.add_argument("--model", default="copilot:claude-opus-5.5")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    p.add_argument("--rollout", action="store_true", help="also score 8 steps open-loop (slower)")
    a = ap.parse_args(argv)
    if a.cmd == "report":
        print(report(a.out, a.rollout))
        return
    gen = model(a.model)
    if a.cmd == "distill":
        Path(a.out).write_text(distill(a.models, a.games.split(","), gen))
        print(f"wrote {a.out}")
        return

    def one(game):
        try:
            Scientist(Atari(game), gen, Path(a.out) / game, a.width, not a.no_experiments,
                      say=lambda m: print(time.strftime("%H:%M ") + m, flush=True), library=a.library).run(a.rounds)
        except Exception:
            print(f"{game} FAILED\n{traceback.format_exc()}", flush=True)
    with ThreadPoolExecutor(a.parallel) as pool:
        list(pool.map(one, a.games.split(",")))
    print(report(a.out), flush=True)


if __name__ == "__main__":
    main()
