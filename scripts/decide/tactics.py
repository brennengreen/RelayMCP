"""Can a small local model, run as a decision function, follow a plain-English tactical intent? Scenarios are game
states (mobs, health, drops, task); the truth is the intent written as rules. Reports agreement, per-answer recall
and latency per model and state format.

    python scripts/decide/tactics.py [--models a,b] [--formats json,facts,features] [--n 40] [--calibrate]
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from relaymcp.host import decide  # noqa: E402

HOSTILE = ["zombie", "skeleton", "creeper", "spider"]
PASSIVE = ["pig", "cow", "sheep"]
ITEMS = ["arrow", "rotten_flesh", "bone", "string", "porkchop"]

INTENT = """Zombies, skeletons, creepers and spiders are hostile; pigs, cows and sheep are harmless. Distances are in blocks.
1. If a creeper is 6 blocks away or closer, retreat.
2. Otherwise, if health is 6 or less and a hostile mob is 10 blocks away or closer, retreat.
3. Otherwise, if a hostile mob is 5 blocks away or closer, fight.
4. Otherwise, if an item drop is 4 blocks away or closer, collect it.
5. Otherwise, keep working."""

QUESTION = decide.Question.make(
    "What should the player do now?",
    {"keep_working": "carry on with the current task", "fight": "face the nearest hostile mob and attack it",
     "retreat": "move away from the threat", "collect": "pick up the nearby item drop"},
    INTENT)


def oracle(s):
    hostile = [m for m in s["mobs"] if m["type"] in HOSTILE]
    if any(m["type"] == "creeper" and m["distance"] <= 6 for m in hostile):
        return "retreat"
    if s["health"] <= 6 and any(m["distance"] <= 10 for m in hostile):
        return "retreat"
    if any(m["distance"] <= 5 for m in hostile):
        return "fight"
    if any(d["distance"] <= 4 for d in s["drops"]):
        return "collect"
    return "keep_working"


def scenario(rng):
    mobs = [{"type": rng.choice(HOSTILE + PASSIVE), "distance": round(rng.uniform(1.0, 16.0), 1),
             "bearing": rng.randint(-180, 180)} for _ in range(rng.randint(0, 3))]
    drops = [{"item": rng.choice(ITEMS), "distance": round(rng.uniform(0.5, 10.0), 1)} for _ in range(rng.randint(0, 2))]
    health = rng.randint(1, 6) if rng.random() < 0.35 else rng.randint(7, 20)
    return {"health": health, "task": "building a house", "progress": rng.randint(0, 95), "mobs": mobs, "drops": drops}


def balanced(n_per_class, seed=7):
    rng, out = random.Random(seed), {name: [] for name in QUESTION.names}
    while any(len(v) < n_per_class for v in out.values()):
        s = scenario(rng)
        k = oracle(s)
        if len(out[k]) < n_per_class:
            out[k].append(s)
    items = [(s, k) for k in out for s in out[k]]
    rng.shuffle(items)
    return items


def side(bearing):
    b = (bearing + 180) % 360 - 180
    return "ahead" if abs(b) <= 30 else ("behind" if abs(b) >= 150 else ("right" if b > 0 else "left"))


def as_facts(s):
    mobs = sorted(s["mobs"], key=lambda m: m["distance"])
    mob_text = ", ".join(f"{m['type']} {m['distance']} blocks {side(m['bearing'])} "
                         f"({'hostile' if m['type'] in HOSTILE else 'harmless'})" for m in mobs) or "none"
    drop_text = ", ".join(f"{d['item']} {d['distance']} blocks" for d in sorted(s["drops"], key=lambda d: d["distance"])) or "none"
    return (f"Health: {s['health']} of 20.\nTask: {s['task']} ({s['progress']}% done).\nMobs: {mob_text}.\n"
            f"Item drops: {drop_text}.")


def as_features(s):
    def nearest(ms):
        return min((m["distance"] for m in ms), default=None)
    hostile = [m for m in s["mobs"] if m["type"] in HOSTILE]
    fmt = lambda d: "none" if d is None else f"{d} blocks"  # noqa: E731
    return (f"Health: {s['health']} of 20.\nNearest creeper: {fmt(nearest([m for m in hostile if m['type'] == 'creeper']))}."
            f"\nNearest hostile mob: {fmt(nearest(hostile))}.\nNearest item drop: {fmt(nearest(s['drops']))}.")


FORMATS = {"json": decide.render_state, "facts": as_facts, "features": as_features}


def run(model, formats, n, calibrate, shots=0):
    d = decide.MLXDecider(model, calibrate=calibrate)
    items = balanced(n)
    rows = []
    for fmt in formats:
        render = FORMATS[fmt]
        q = QUESTION
        if shots:  # worked examples from a separate draw (never the test states)
            ex = balanced(shots, seed=99)
            q = decide.Question.make(QUESTION.text, dict(QUESTION.answers), QUESTION.intent,
                                     [(render(s), k) for s, k in ex])
        d.decide(q, render(items[0][0]))  # prepare the question and warm the kernels
        got, ms = [], []
        for s, _ in items:
            r = d.decide(q, render(s))
            got.append(r)
            ms.append(r.ms)
        truth = [k for _, k in items]
        recall = {k: sum(g.choice == k for g, t in zip(got, truth) if t == k) / truth.count(k) for k in q.names}
        conf_right = [g.confidence for g, t in zip(got, truth) if g.choice == t]
        conf_wrong = [g.confidence for g, t in zip(got, truth) if g.choice != t]
        rows.append({"model": model.split("/")[-1], "format": fmt, "agree": decide.agreement(got, truth),
                     "recall": recall, "p50": decide.percentile(ms, 50), "p95": decide.percentile(ms, 95),
                     "prefix_tokens": d._prepared[q]["prefix_tokens"],
                     "conf_right": sum(conf_right) / max(1, len(conf_right)),
                     "conf_wrong": sum(conf_wrong) / max(1, len(conf_wrong))})
        r = rows[-1]
        print(f"{r['model']:28s} {fmt:9s} agree {r['agree']:.0%}  p50 {r['p50']:.1f} ms  p95 {r['p95']:.1f} ms  "
              f"recall " + " ".join(f"{k[:5]} {v:.0%}" for k, v in recall.items()) +
              f"  conf right {r['conf_right']:.2f} wrong {r['conf_wrong']:.2f}  (prefix {r['prefix_tokens']} tok)", flush=True)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="mlx-community/Qwen3-0.6B-4bit")
    ap.add_argument("--formats", default="json,facts,features")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--shots", type=int, default=0, help="worked examples per answer in the cached prompt")
    a = ap.parse_args()
    for m in a.models.split(","):
        run(m, a.formats.split(","), a.n, a.calibrate, a.shots)
