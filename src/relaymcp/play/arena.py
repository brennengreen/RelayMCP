"""relaymcp arena: the Spinal Score, one hard test of real-time game agents, and two leaderboards.

The test (season 1): Freedoom: Phase 2 (free Doom II levels, shipped in ViZDoom's wheels) MAP01-MAP04 on
Ultra-Violence from a pistol start, in real time at 35 tics/s whatever the agent does: a slow decider loses tics.
Senses are fair: the agent gets its own state, the level's layout and the objects on screen (as a perfect detector
would see them), never what is behind a wall, so it has to remember the rest. Reading the game's files is not allowed.

Each map is scored with Doom's own tally, the way expert players rate a run (UV-Max): kills %, secrets %, and the exit
against par (par / time, 0 without an exit). An attempt ends at the exit, at death or at 3 x par. The Spinal Score is
the mean over the three parts and the maps. 100 % is every monster, every secret and the exit under par on every map:
elite human play. An expert who maxes each map at twice par scores 83 %. Spinal was built on the deathmatch arena,
which is practice and not scored: these levels are new to every entrant, Spinal included.

- Beat Spinal (board "reflex"): any architecture: a model picking every move, a decision model, your own code
  (`plugin:my_agent.py:Agent`), or Spinal's planner.
- Model league (board "models"): Spinal fixed, your model plugged in: it compiles the standard intent into the
  tactics (`--agent compiled --model ollama:... | openai:...`); also reports how closely the policy follows the intent.

    relaymcp arena run --board reflex --agent planner --name "Spinal planner"
    relaymcp arena run --board models --agent compiled --model ollama:qwen3.5:9b
    relaymcp arena run --agent plugin:my_agent.py:Agent --maps MAP01      # a quick try: unranked
    relaymcp arena render      # leaderboards/*/*.json -> LEADERBOARD.md, the README, site/leaderboard.json
"""
import json
import platform
import re
import struct
import subprocess
import time
from pathlib import Path

import relaymcp

SEASON = {"MAP01": (28, 3, 30), "MAP02": (59, 1, 90), "MAP03": (89, 3, 120), "MAP04": (136, 2, 120)}
"""Doom's totals for each map on Ultra-Violence: monsters (lost souls don't count, as in Doom), secrets, par (s)."""
MAPS = tuple(SEASON)
CAP = 3
SCENARIO = "Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses"
SCALE = "100% = every monster, every secret and the exit under par on every map (elite human play)"
BOARDS = {"reflex": "Beat Spinal: any architecture", "models": "Model league: Spinal with your model plugged in"}
COUNTED = {3004, 9, 65, 3001, 3002, 58, 3005, 69, 3003, 68, 71, 66, 67, 64, 16, 7, 84, 72}


def levels(maps=MAPS):
    """The totals read from the game's data, to check SEASON: monsters placed for Ultra-Violence in single player,
    secret sectors, and the par times in the DEHACKED lump."""
    from .doom import map_data, wad
    b, lumps = wad()
    _, p, s = next(x for x in lumps if x[0] == "DEHACKED")
    pars = {int(m): int(t) for m, t in re.findall(r"^\s*par\s+(\d+)\s+(\d+)\s*(?:#|$)", b[p:p + s].decode("latin1"), re.M)}
    out = {}
    for m in maps:
        lump = map_data(m)
        things = [struct.unpack_from("<hhhhh", lump["THINGS"], j) for j in range(0, len(lump["THINGS"]), 10)]
        specials = [struct.unpack_from("<h", lump["SECTORS"], j + 22)[0] for j in range(0, len(lump["SECTORS"]), 26)]
        out[m] = (sum(1 for t in things if t[3] in COUNTED and t[4] & 4 and not t[4] & 16),
                  sum(1 for sp in specials if (sp & 31) == 9 or sp & 128), pars[int(m[3:])])
    return out


def map_score(m, kills, secrets, exited, seconds):
    """Doom's tally for one map -> (kills, secrets, exit, mean), each 0-1."""
    monsters, total_secrets, par = SEASON[m]
    k = min(1.0, kills / monsters)
    s = min(1.0, secrets / total_secrets) if total_secrets else 1.0
    e = min(1.0, par / max(seconds, 1.0)) if exited else 0.0
    return k, s, e, (k + s + e) / 3


def git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).parent, timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def run(board, agent, model, name, out="leaderboards", maps=MAPS, seed=1):
    from . import doom
    tactics = doom.Tactics(agent, model or "qwen3.5:9b")
    per = []
    for m in maps:
        game = doom.Doom(CAP * SEASON[m][2], seed, level=m, fair=True)
        r = doom.episode(game, tactics)
        game.close()
        k, s, e, sc = map_score(m, r["kills"], r["secrets"], r["exited"], r["game_s"])
        per.append({"map": m, "score": round(100 * sc, 1), "kills": r["kills"], "secrets": r["secrets"],
                    "exited": r["exited"], "seconds": r["game_s"], "died": r["died"], "missed_tics": r["missed_tics"]})
        print(json.dumps(per[-1]), flush=True)
    lat = sorted(tactics.lat)
    on_intent = None
    if agent == "compiled":  # how closely the compiled policy follows the intent, on 1,000 random states
        import random
        states = [doom.sample_state(random.Random(5)) for _ in range(1000)]
        on_intent = round(sum(tactics.backend.fn(s) == doom.oracle(s) for s in states) / len(states), 3)
    parts = [map_score(p["map"], p["kills"], p["secrets"], p["exited"], p["seconds"]) for p in per]
    mean = lambda i: round(100 * sum(x[i] for x in parts) / len(parts), 1)  # noqa: E731
    ranked = tuple(maps) == MAPS
    result = {
        "board": board, "name": name or f"{agent}" + (f" + {model}" if model else ""), "agent": agent, "model": model,
        "scenario": SCENARIO if ranked else "unranked: " + ", ".join(maps), "score": mean(3),
        "metrics": {"kills": mean(0), "secrets": mean(1), "exit": mean(2), "exits": sum(p["exited"] for p in per),
                    "deaths": sum(p["died"] for p in per),
                    "decision_ms_p50": round(lat[len(lat) // 2], 3) if lat else None,
                    "decision_ms_p95": round(lat[int(len(lat) * 0.95)], 3) if lat else None,
                    "missed_tics": sum(p["missed_tics"] for p in per), "on_intent": on_intent,
                    "compile_s": round(getattr(tactics, "compile_s", 0), 1) or None},
        "maps": per, "relaymcp": relaymcp.__version__, "commit": git_sha(),
        "machine": f"{platform.system()} {platform.machine()}", "date": time.strftime("%Y-%m-%d"),
    }
    if ranked:
        folder = Path(out) / board
        folder.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", result["name"].lower()).strip("-")
        (folder / f"{slug}.json").write_text(json.dumps(result, indent=1) + "\n")
    print("SCORE " + json.dumps({"name": result["name"], "score": result["score"], "ranked": ranked,
                                 **result["metrics"]}), flush=True)
    return result


REQUIRED = {"board": str, "name": str, "agent": str, "scenario": str, "score": (int, float), "metrics": dict,
            "maps": list, "machine": str, "date": str}


def check(r, where="entry"):
    """A leaderboard entry: complete, for this season, and its score recomputed from the raw tally."""
    for k, t in REQUIRED.items():
        if not isinstance(r.get(k), t):
            raise ValueError(f"{where}: {k} missing or not {t}")
    if r["scenario"] != SCENARIO or [p.get("map") for p in r["maps"]] != list(MAPS):
        raise ValueError(f"{where}: not a full {SCENARIO!r} run")
    parts = [map_score(p["map"], p["kills"], p["secrets"], p["exited"], p["seconds"]) for p in r["maps"]]
    if abs(100 * sum(x[3] for x in parts) / len(parts) - r["score"]) > 0.11:
        raise ValueError(f"{where}: score {r['score']} does not match its tally")


def load(folder="leaderboards"):
    """Every entry, checked (a pull request with a malformed or miscounted entry fails CI here)."""
    boards = {}
    for board in BOARDS:
        rows = []
        for p in sorted(Path(folder, board).glob("*.json")):
            r = json.loads(p.read_text())
            check(r, str(p))
            if r["board"] != board:
                raise ValueError(f"{p}: entry for another board")
            rows.append(r)
        boards[board] = sorted(rows, key=lambda r: -r["score"])
    return boards


def latency(m):
    p95 = m.get("decision_ms_p95")
    return "-" if p95 is None else (f"{p95 * 1000:.0f} us" if p95 < 1 else f"{p95:.0f} ms")


def render(folder="leaderboards", out="LEADERBOARD.md", readme="README.md", site="site"):
    """LEADERBOARD.md, the top of each board in the README (between leaderboard markers), and the static site's
    data (site/leaderboard.json, for GitHub Pages)."""
    sections, top = [], []
    boards = load(folder)
    if site and Path(site).is_dir():
        Path(site, "leaderboard.json").write_text(json.dumps(
            {"scenario": SCENARIO, "scale": SCALE, "generated": time.strftime("%Y-%m-%d"),
             "boards": {b: {"title": BOARDS[b], "rows": boards[b]} for b in BOARDS}}, indent=1) + "\n")
    for board, title in BOARDS.items():
        lines = [f"## {title}", "", f"{SCENARIO}. {SCALE}.", "",
                 "| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(boards[board], 1):
            m = r["metrics"]
            on = "-" if m.get("on_intent") is None else f"{m['on_intent']:.0%}"
            lines.append(f"| {i} | {r['name']} | **{r['score']:.1f}%** | {m['kills']:.0f}% | {m['secrets']:.0f}% | "
                         f"{m['exits']}/{len(MAPS)} | {m['deaths']}/{len(MAPS)} | {latency(m)} | {m['missed_tics']} | {on} |")
        sections.append("\n".join(lines))
        top.append("\n".join([f"**{title}**", ""] + lines[4:9]))
    how = ("\n\n## Enter\n\nRun `relaymcp arena run --board reflex --agent plugin:my_agent.py:Agent --name \"...\"` "
           "(your architecture) or `--board models --agent compiled --model openai:<model>` (your model), then open a "
           "pull request with the JSON it writes under `leaderboards/`. Maintainers re-run the top entries. Agents may "
           "use only what the game hands them (the state and the screen); reading the game's files is not allowed.\n")
    Path(out).write_text("# Leaderboards: the Spinal Score\n\n" + "\n\n".join(sections) + how)
    r = Path(readme)
    if r.exists():
        text = r.read_text()
        a, b = "<!-- leaderboard:start -->", "<!-- leaderboard:end -->"
        if a in text and b in text:
            block = f"{SCENARIO}. {SCALE}.\n\n" + "\n\n".join(top)
            text = text[:text.index(a) + len(a)] + "\n" + block + "\n\n[Full leaderboards](LEADERBOARD.md)\n" + text[text.index(b):]
            r.write_text(text)
    return out
