"""Where did the time and tokens go in an agent session that drove the handheld?

    python scripts/analyze_session.py ~/.copilot/session-state/<session-id>/events.jsonl [--prefix ally]
        [--since 18:29 --until 18:46]

Splits each device step into model thinking vs. device call, summarizes device tools (latency, output size, images),
and reports premium requests. From Copilot's local session store it adds every model call's time to first token and
duration against the context size, the part of a session's wall time spent in the model, and how many model calls a
task took. --since/--until (local HH:MM on the session's first day) measure one episode, e.g. a benchmark task.
Use it before/after a change to see whether real sessions got faster and leaner.
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


CONTEXT_BUCKETS = ((100_000, "<100k"), (250_000, "100-250k"), (450_000, "250-450k"), (float("inf"), ">450k"))


def model_calls(db: str, session_id: str, since: float | None = None, until: float | None = None) -> dict:
    """Per-model-call timing from Copilot's session store (read-only): calls, model time, time to first token by
    context size."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("SELECT created_at, model, input_tokens, duration_ms, time_to_first_token_ms, reasoning_effort "
                           "FROM assistant_usage_events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    except sqlite3.Error as e:
        return {"error": f"couldn't read {db}: {e}"}
    calls = []
    for created, model, tokens, dur, ttft, effort in rows:
        try:  # SQLite's datetime('now') is UTC
            t = datetime.fromisoformat(created.replace(" ", "T")).replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
        except (TypeError, ValueError):
            t = None
        if (since and t and t < since) or (until and t and t > until):
            continue
        calls.append({"t": t, "model": model, "tokens": tokens or 0, "dur": (dur or 0) / 1000, "ttft": (ttft or 0) / 1000,
                      "effort": effort})
    if not calls:
        return {"calls": 0}
    by_bucket = collections.defaultdict(list)
    for c in calls:
        label = next(name for limit, name in CONTEXT_BUCKETS if c["tokens"] < limit)
        by_bucket[label].append(c["ttft"])
    return {
        "calls": len(calls),
        "model_min": round(sum(c["dur"] for c in calls) / 60, 1),
        "call_s_median": round(st.median(c["dur"] for c in calls), 1),
        "ttft_s_median": round(st.median(c["ttft"] for c in calls), 1),
        "ttft_by_context": {name: {"n": len(by_bucket[name]), "median_s": round(st.median(by_bucket[name]), 1)}
                            for _, name in CONTEXT_BUCKETS if by_bucket.get(name)},
        "context_tokens_max": max(c["tokens"] for c in calls),
        "models": sorted({f"{c['model']}/{c['effort'] or '-'}" for c in calls}),
    }


def analyze(path: str, prefixes: tuple[str, ...] = ("ally",), since: float | None = None,
            until: float | None = None) -> dict:
    starts, done, asks = {}, {}, []
    usage, models, assets = None, [], []
    for line in open(path, "rb"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = _ts(e)
        if t and ((since and t < since) or (until and t > until)):
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


def episode_window(events_path: str, since: str | None, until: str | None) -> tuple[float | None, float | None]:
    """--since/--until (local HH:MM) as timestamps on the day the session started; the whole session if not given."""
    first = last = None
    for line in open(events_path, "rb"):
        try:
            t = _ts(json.loads(line))
        except ValueError:
            continue
        if t:
            first = t if first is None else first
            last = t
    if first is None:
        return None, None
    day = datetime.fromtimestamp(first)

    def at(hhmm: str) -> float:
        h, m = (int(x) for x in hhmm.split(":"))
        return day.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()

    return (at(since) if since else first), (at(until) if until else last)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("events", help="path to a session's events.jsonl")
    parser.add_argument("--prefix", action="append", help="device tool name prefix (default: ally)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", default=str(__import__("pathlib").Path.home() / ".copilot" / "session-store.db"),
                        help="Copilot's session store (for per-call model timing)")
    parser.add_argument("--since", help="local HH:MM: only measure from here (one episode)")
    parser.add_argument("--until", help="local HH:MM: only measure up to here")
    args = parser.parse_args()
    window = episode_window(args.events, args.since, args.until)
    report = analyze(args.events, tuple(args.prefix or ["ally"]), *(window if args.since or args.until else (None, None)))
    session_id = __import__("pathlib").Path(args.events).resolve().parent.name
    report["model_calls"] = model_calls(args.db, session_id, *window)
    wall = (window[1] - window[0]) / 60 if all(window) else None
    if wall and report["model_calls"].get("calls"):
        report["model_calls"]["wall_min"] = round(wall, 1)
        report["model_calls"]["model_share"] = f"{min(100, round(report['model_calls']['model_min'] / wall * 100))}%"
    if args.json:
        print(json.dumps(report, indent=1))
        return
    print(f"device steps: {report['device_steps']}  median {report['step_s_median']} s/step = "
          f"{report['model_s_median']} s model (p90 {report['model_s_p90']}) + {report['device_s_median']} s device")
    print(f"models: {', '.join(report['models']) or '?'}  premium requests: {report.get('premium_requests', '?')}  "
          f"images logged: {report['images_logged']} (avg {report['image_kb_avg']} KB)")
    m = report["model_calls"]
    if m.get("calls"):
        share = f", {m['model_share']} of {m['wall_min']} min wall time" if m.get("model_share") else ""
        print(f"model calls: {m['calls']} ({m['model_min']} min in the model{share}); median call {m['call_s_median']} s, "
              f"first token {m['ttft_s_median']} s; largest context {m['context_tokens_max']:,} tokens")
        print("  first token by context: " + ", ".join(f"{k} {v['median_s']} s (n={v['n']})" for k, v in m["ttft_by_context"].items()))
    elif m.get("error"):
        print(f"model calls: {m['error']}")
    print(f"{'tool':34} {'n':>4} {'med_s':>6} {'p90_s':>6} {'chars':>6} {'img':>4}")
    for name, t in report["tools"].items():
        print(f"{name[:34]:34} {t['n']:4} {t['median_s']:6.2f} {t['p90_s']:6.2f} {t['median_chars']:6} {t['images']:4}")


if __name__ == "__main__":
    main()
