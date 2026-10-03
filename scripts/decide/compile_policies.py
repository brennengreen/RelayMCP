"""Compile plain-English tactical intents into checked policies with a local model, then score them on the same
balanced scenarios as tactics.py: agreement with the intent's rules, microseconds per decision, seconds to compile."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import tactics as ev  # noqa: E402
from relaymcp.host import decide  # noqa: E402

INTENT_B = """Zombies, skeletons, creepers and spiders are hostile; pigs, cows and sheep are harmless. Distances are in blocks.
We are avoiding fights tonight.
1. If any hostile mob is 8 blocks away or closer, retreat.
2. Otherwise, if health is below 15 and a porkchop drop is 6 blocks away or closer, collect it.
3. Otherwise, if any item drop is 3 blocks away or closer, collect it.
4. Otherwise, keep working. Never choose fight."""


def oracle_b(s):
    hostile = [m for m in s["mobs"] if m["type"] in ev.HOSTILE]
    if any(m["distance"] <= 8 for m in hostile):
        return "retreat"
    if s["health"] < 15 and any(d["item"] == "porkchop" and d["distance"] <= 6 for d in s["drops"]):
        return "collect"
    if any(d["distance"] <= 3 for d in s["drops"]):
        return "collect"
    return "keep_working"


SCHEMA = ("Fields: health (1-20), task (str), progress (0-100), mobs (list of {type, distance, bearing}), "
          "drops (list of {item, distance}).")

if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"
    gen = decide.ollama_generate(model)
    import random
    rng = random.Random(3)
    samples = [ev.scenario(rng) for _ in range(200)]
    for label, intent, truth_fn in (("A (five rules)", ev.INTENT, ev.oracle), ("B (avoid fights)", INTENT_B, oracle_b)):
        q = decide.Question.make(ev.QUESTION.text, dict(ev.QUESTION.answers), intent)
        try:
            pol = decide.compile_policy(q, samples, gen, SCHEMA)
        except Exception as e:  # noqa: BLE001
            print(f"intent {label}: compile failed: {e}")
            continue
        rng2 = random.Random(11)
        test = [ev.scenario(rng2) for _ in range(5000)]
        t = time.perf_counter()
        got = [pol.decide(q, s) for s in test]
        per_us = (time.perf_counter() - t) / len(test) * 1e6
        truth = [truth_fn(s) for s in test]
        classes = sorted(set(truth))
        recall = {k: sum(g.choice == k for g, tr in zip(got, truth) if tr == k) / truth.count(k) for k in classes}
        print(f"intent {label}: {model} compiled in {pol.compile_ms / 1000:.1f} s; agreement "
              f"{decide.agreement(got, truth):.2%} on {len(test)} states; {per_us:.1f} us per decision; recall "
              + " ".join(f"{k} {v:.0%}" for k, v in recall.items()))
        print("--- policy source ---\n" + pol.source.strip()[:1200])
