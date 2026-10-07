"""H4: a fast neural student distilled from the model-written teacher, as data doubles.

The student is a small numpy MLP over a fixed encoding of the objects (no pixels: perception is H1's job). Data comes
from DAgger: the student plays (from the second batch on), the teacher program labels every state it reaches. States
come from the real game ("real"), or from the scientist's world model rolled forward from real states ("model": the
student practises in the learned simulator, the teacher labels imagined states). After each doubling of labels the
student is scored on the same unseen seeds and protocol as H2.

    python -m relaymcp.play.student run --game Breakout --teacher .science/h2/Breakout/policy/try009.py \\
        --models .science/h1 --source real --out .science/h4/Breakout-real
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

import numpy as np

from relaymcp.play.planner import STEPS, STICKY, copy_hist, hns, load
from relaymcp.play.scientist import HISTORY, Atari, clean

SLOTS = 8  # objects per category, nearest the player first
FEATS = 7  # present, x, y, w, h, dx, dy


class Encoder:
    """Object lists -> a fixed vector: for each category seen in training, SLOTS objects sorted by (y, x), each with
    its position, size and motion over the last step."""

    def __init__(self, cats):
        self.cats = sorted(cats)

    @property
    def size(self):
        return len(self.cats) * SLOTS * FEATS

    def __call__(self, history):
        now = history[-1]
        prev = history[-2] if len(history) > 1 else now
        v = np.zeros((len(self.cats), SLOTS, FEATS), np.float32)
        for ci, c in enumerate(self.cats):
            objs = sorted((o for o in now if o["cat"] == c), key=lambda o: (o["y"], o["x"]))[:SLOTS]
            olds = [o for o in prev if o["cat"] == c]
            for k, o in enumerate(objs):
                p = min(olds, key=lambda q: abs(q["x"] - o["x"]) + abs(q["y"] - o["y"]), default=o)
                dx, dy = o["x"] - p["x"], o["y"] - p["y"]
                if abs(dx) + abs(dy) > 32:
                    dx = dy = 0
                v[ci, k] = (1, o["x"] / 160, o["y"] / 210, o["w"] / 160, o["h"] / 210, dx / 8, dy / 8)
        return v.ravel()


class MLP:
    def __init__(self, n_in, n_out, hidden=128, seed=0):
        r = np.random.default_rng(seed)
        self.W = [r.normal(0, (2 / n_in) ** 0.5, (n_in, hidden)).astype(np.float32),
                  r.normal(0, (2 / hidden) ** 0.5, (hidden, hidden)).astype(np.float32),
                  r.normal(0, (1 / hidden) ** 0.5, (hidden, n_out)).astype(np.float32)]
        self.b = [np.zeros(hidden, np.float32), np.zeros(hidden, np.float32), np.zeros(n_out, np.float32)]

    def forward(self, x):
        h1 = np.maximum(0, x @ self.W[0] + self.b[0])
        h2 = np.maximum(0, h1 @ self.W[1] + self.b[1])
        return h1, h2, h2 @ self.W[2] + self.b[2]

    def act(self, x):
        return int(np.argmax(self.forward(x[None])[2][0]))

    def fit(self, X, y, epochs=30, lr=1e-3, batch=256, seed=0):
        """Cross-entropy with Adam, from scratch each time (the data is the teacher's, kept in full)."""
        rng = np.random.default_rng(seed)
        params = self.W + self.b
        m = [np.zeros_like(p) for p in params]
        v = [np.zeros_like(p) for p in params]
        t = 0
        for _ in range(epochs):
            idx = rng.permutation(len(X))
            for s in range(0, len(X), batch):
                j = idx[s:s + batch]
                x, yy = X[j], y[j]
                h1, h2, z = self.forward(x)
                z = z - z.max(1, keepdims=True)
                p = np.exp(z)
                p /= p.sum(1, keepdims=True)
                g = p
                g[np.arange(len(j)), yy] -= 1
                g /= len(j)
                gW2, gb2 = h2.T @ g, g.sum(0)
                d2 = (g @ self.W[2].T) * (h2 > 0)
                gW1, gb1 = h1.T @ d2, d2.sum(0)
                d1 = (d2 @ self.W[1].T) * (h1 > 0)
                gW0, gb0 = x.T @ d1, d1.sum(0)
                grads = [gW0, gW1, gW2, gb0, gb1, gb2]
                t += 1
                for i, (pp, gg) in enumerate(zip(params, grads)):
                    m[i] = 0.9 * m[i] + 0.1 * gg
                    v[i] = 0.999 * v[i] + 0.001 * gg * gg
                    pp -= lr * (m[i] / (1 - 0.9 ** t)) / (np.sqrt(v[i] / (1 - 0.999 ** t)) + 1e-8)
        return self

    def accuracy(self, X, y):
        return float((np.argmax(self.forward(X)[2], 1) == y).mean()) if len(X) else 0.0


class Teacher:
    def __init__(self, path, names, world_model=None):
        self.mod, self.names = load(path, "teacher"), names
        self.wm = load(world_model, "world_model").predict if world_model else None
        self.hybrid = "predict" in self.mod.act.__code__.co_varnames[:self.mod.act.__code__.co_argcount]

    def __call__(self, hist):
        args = (copy_hist(hist), list(self.names)) + ((self.wm,) if self.hybrid else ())
        try:
            a = self.mod.act(*args)
            return self.names.index(a) if a in self.names else 0
        except Exception:
            return 0


def episode(game, choose, seed, steps=STEPS, on_state=None):
    """Play the real game (sticky buttons) with choose(history) -> button index. -> score"""
    w = Atari(game)
    e = w.env(STICKY)
    e.reset(seed=seed)
    hist, score = [w.objects(e)], 0.0
    for _ in range(steps):
        h = hist[-HISTORY:]
        if on_state:
            on_state(h)
        _, r, term, trunc, _ = e.step(choose(h))
        score += r
        hist = (hist + [w.objects(e)])[-HISTORY:]
        if term or trunc:
            break
    e.close()
    return score


class Student:
    def __init__(self, game, teacher, out, source="real", world_model=None, say=print, seed=0):
        self.game, self.out, self.source, self.say = game, Path(out), source, say
        self.out.mkdir(parents=True, exist_ok=True)
        self.names = Atari(game).actions()
        self.teacher = Teacher(teacher, self.names, world_model)
        self.wm = load(world_model, "world_model").predict if world_model else None
        self.rng = random.Random(seed)
        self.X, self.y, self.net, self.enc = [], [], None, None

    def encoder(self):
        cats = set()
        episode(self.game, lambda h: self.teacher(h), 900, 600, lambda h: cats.update(o["cat"] for o in h[-1]))
        return Encoder(cats)

    def policy(self, beta):
        """DAgger's mixture: the teacher with probability beta, else the student."""
        def choose(h):
            if self.net is None or self.rng.random() < beta:
                return self.teacher(h)
            return self.net.act(self.enc(h))
        return choose

    def collect(self, n, beta, seed):
        """n labelled states. real: from the student's (mixture's) own play. model: from real states of that play,
        k imagined steps in the world model under the mixture, labelled by the teacher."""
        got = 0
        choose = self.policy(beta)

        def label(h):
            nonlocal got
            if got >= n:
                return
            self.X.append(self.enc(h))
            self.y.append(self.teacher(h))
            got += 1
            if self.source == "model" and self.wm and got < n:
                im = copy_hist(h)
                for _ in range(8):
                    a = choose(im)
                    try:
                        nxt = clean(self.wm(copy_hist(im), self.names[a]))
                    except Exception:
                        break
                    im = (im + [nxt])[-HISTORY:]
                    self.X.append(self.enc(im))
                    self.y.append(self.teacher(im))
                    got += 1
                    if got >= n:
                        break
        s = seed
        while got < n:
            episode(self.game, choose, s, STEPS, label)
            s += 1
        return s

    def evaluate(self, seeds=(1, 2, 3, 4)):
        scores = [episode(self.game, lambda h: self.net.act(self.enc(h)), s) for s in seeds]
        return sum(hns(self.game, x) for x in scores) / len(scores), scores

    def run(self, start=2500, doublings=5):
        self.enc = self.encoder()
        state = {"game": self.game, "source": self.source, "features": self.enc.size, "curve": []}
        t_scores = [episode(self.game, lambda h: self.teacher(h), s) for s in (1, 2, 3, 4)]
        state["teacher"] = {"hns": sum(hns(self.game, x) for x in t_scores) / 4, "scores": t_scores}
        self.say(f"{self.game} teacher: {100 * state['teacher']['hns']:.0f}% of human {t_scores}")
        n, seed = start, 5000
        for d in range(doublings + 1):
            beta = 1.0 if d == 0 else 0.5 ** d
            seed = self.collect(n - len(self.X), beta, seed)
            X, y = np.array(self.X, np.float32), np.array(self.y, np.int64)
            cut = int(0.9 * len(X))
            self.net = MLP(self.enc.size, len(self.names)).fit(X[:cut], y[:cut])
            acc = self.net.accuracy(X[cut:], y[cut:])
            t0 = time.perf_counter()
            for _ in range(200):
                self.net.act(X[0])
            us = 1e6 * (time.perf_counter() - t0) / 200
            h, scores = self.evaluate()
            state["curve"].append({"labels": len(X), "hns": h, "scores": scores, "agree": acc, "us": us})
            (self.out / "student.json").write_text(json.dumps(state, indent=1))
            self.say(f"{self.game} {self.source} {len(X)} labels: {100 * h:.0f}% of human {scores}, agrees with "
                     f"the teacher {100 * acc:.0f}%, {us:.0f} us a decision")
            n *= 2
        np.savez(self.out / "student.npz", *self.net.W, *self.net.b, cats=np.array(self.enc.cats))
        return state


def main(argv=None):
    ap = argparse.ArgumentParser(prog="student")
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--game", required=True)
    ap.add_argument("--teacher", required=True)
    ap.add_argument("--models", help="the scientist's folder: <models>/<game>/world_model.py")
    ap.add_argument("--source", choices=["real", "model"], default="real")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=int, default=2500)
    ap.add_argument("--doublings", type=int, default=4)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    wm = str(Path(a.models) / a.game / "world_model.py") if a.models else None
    Student(a.game, a.teacher, a.out, a.source, wm, say=lambda m: print(time.strftime("%H:%M ") + m, flush=True)
            ).run(a.start, a.doublings)


if __name__ == "__main__":
    main()
