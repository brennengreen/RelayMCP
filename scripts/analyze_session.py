"""Where did the time and tokens go in an agent session that drove the handheld?

    python scripts/analyze_session.py ~/.copilot/session-state/<session-id>/events.jsonl [--prefix ally]

Splits each device step into model thinking vs. device call, summarizes device tools (latency, output size, images),
and reports premium requests. Use it before/after a change to see whether real sessions got faster and leaner.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import statistics as st
from datetime import datetime


def _ts(event: dict) -> float | None:
    t = event.get("timestamp")
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() if t else None


def _p90(values: list[float]) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(len(v) * 0.9) - 1))] if v else 0.0


def analyze(path: str, prefixes: tuple[str, ...] = ("ally",)) -> dict:
    starts, done, asks = {}, {}, []
    usage, models, assets = None, [], []
    for line in open(path, "rb"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind, d = e.get("type"), e.get("data") or {}
        if kind == "assistant.message":
            asks.append((_ts(e), [(r.get("toolCallId"), r.get("name") or "") for r in d.get("toolRequests") or []]))
        elif kind == "tool.execution_start":
            starts[d.get("toolCallId")] = (_ts(e), d.get("toolName") or "")
        elif kind == "tool.execution_complete":
            res = d.get("result") or {}
            text = res.get("content") if isinstance(res, dict) else res
            done[d.get("toolCallId")] = (_ts(e), len(str(text or "")), '"type": "image"' in json.dumps(res))
        elif kind == "session.usage_checkpoint":
            usage = {"premium_requests": d.get("totalPremiumRequests")}
        elif kind == "session.model_change":
            models.append(f"{d.get('newModel')}/{d.get('reasoningEffort')}")
        elif kind == "session.binary_asset":
            assets.append(len(json.dumps(d)))
    is_device = lambda name: name.startswith(prefixes)  # noqa: E731
    tools = collections.defaultdict(lambda: {"dur": [], "chars": [], "images": 0})
    for cid, (t0, name) in starts.items():
        if cid in done and is_device(name):
            t1, chars, image = done[cid]
            a = tools[name]
            a["dur"].append(t1 - t0)
            a["chars"].append(chars)
            a["images"] += image
    think, device, last = [], [], None
    for t, ids in asks:
        if last and t and 0 < t - last < 900 and any(is_device(n) for _, n in ids):
            think.append(t - last)
            device.append(max([done[c][0] - starts[c][0] for c, _ in ids if c in done and c in starts] or [0.0]))
        finished = [done[c][0] for c, _ in ids if c in done]
        if finished:
            last = max(finished)
    return {
        "device_steps": len(think),
        "step_s_median": round(st.median([a + b for a, b in zip(think, device)]), 1) if think else None,
        "model_s_median": round(st.median(think), 1) if think else None,
        "model_s_p90": round(_p90(think), 1) if think else None,
        "device_s_median": round(st.median(device), 2) if device else None,
        "tools": {n: {"n": len(a["dur"]), "median_s": round(st.median(a["dur"]), 2), "p90_s": round(_p90(a["dur"]), 2),
                      "median_chars": int(st.median(a["chars"])), "images": a["images"]}
                  for n, a in sorted(tools.items(), key=lambda x: -len(x[1]["dur"]))},
        "images_logged": len(assets),
        "image_kb_avg": round(sum(assets) / len(assets) / 1000, 1) if assets else None,
        "models": models,
        **(usage or {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("events", help="path to a session's events.jsonl")
    parser.add_argument("--prefix", action="append", help="device tool name prefix (default: ally)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = analyze(args.events, tuple(args.prefix or ["ally"]))
    if args.json:
        print(json.dumps(report, indent=1))
        return
    print(f"device steps: {report['device_steps']}  median {report['step_s_median']} s/step = "
          f"{report['model_s_median']} s model (p90 {report['model_s_p90']}) + {report['device_s_median']} s device")
    print(f"models: {', '.join(report['models']) or '?'}  premium requests: {report.get('premium_requests', '?')}  "
          f"images logged: {report['images_logged']} (avg {report['image_kb_avg']} KB)")
    print(f"{'tool':34} {'n':>4} {'med_s':>6} {'p90_s':>6} {'chars':>6} {'img':>4}")
    for name, t in report["tools"].items():
        print(f"{name[:34]:34} {t['n']:4} {t['median_s']:6.2f} {t['p90_s']:6.2f} {t['median_chars']:6} {t['images']:4}")


if __name__ == "__main__":
    main()
