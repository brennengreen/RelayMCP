"""Minecraft benchmark harness (Phase 0 gym), driving the handheld's MCP server from the Mac.

    bench.py TASK [--x X --y Y --z Z] [--seed N]
      step       camera step response: yaw 10-180 degrees and pitch +-30/60, model and confirmed times, final error
      pursuit    keep facing a target sweeping at 20, 40 and 80 degrees/s: RMS error (bias and jitter)
      reaction   a pig summoned at a random bearing, 5 times: time to face it, then hold it while it wanders
      course     10 random waypoints, walking while facing the way: time vs ideal, cross-track error, stops
      house      the 5x5 house (tasks/house.py), checked block by block
      suite      all of the above, one scorecard; every run is saved in scripts/mcbench/results/

Resets and scoring go through the RelayMCP Telemetry pack's commands (/scriptevent relay:..., typed in chat between
trials): exact start pose, a cleared area, and the built region read back block by block. Needs the pack on the world
and the game in front with the pad connected (turn mode off: this is real time). The task programs (tasks/*.py) run
against a local simulator first: mcgym.py.
"""
import argparse
import json
import math
import random
import sys
import time

from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))
from relaymcp.host import bench as mcp, config  # noqa: E402
from mcgym import route  # noqa: E402  (the same routes as the local gym)

_cfg = config.load()
HW = dict((n, u) for n, u, _ in config.mcp_servers(_cfg))[f"{_cfg['device']['name']}-handheld"]
ANGLE_LAG = 0.045  # a tick's angles trail the picture by this much (measured on the handheld)


def call(tool, args, timeout=120):
    dt, msg, _ = mcp._call(HW, tool, args, timeout=timeout)
    res = msg.get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise RuntimeError(f"{tool}: {text[:400]}")
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def cursor():
    return call("state", {"action": "read", "topic": "minecraft.reply", "max_items": 1}).get("cursor", 0)


def wait_reply(name, since, timeout):
    """The pack's reply to relay:<name> after cursor `since`, polled (a deployed `state` wait before aedac7a treated
    since=0 as "from now" and missed a reply that came before it started)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = call("state", {"action": "read", "topic": "minecraft.reply", "since": since, "max_items": 50})
        for e in r.get("events", []):
            if (e.get("data") or {}).get("reply") == name:
                return e
        time.sleep(0.1)
    return None


def relay(name, args, wait_s=4.0):
    """Send /scriptevent relay:<name> {args} through chat ("/" opens it with the slash typed, in controller mode)
    and wait for the pack's reply; once more after closing the chat if none comes."""
    for _ in range(2):
        since = cursor()
        call("act", {"steps": [{"key": ["/"]}, {"wait": 350}, {"type": f"scriptevent relay:{name} {json.dumps(args)}"},
                               {"wait": 80}, {"key": ["enter"]}, {"wait": 150}]})
        ev = wait_reply(name, since, wait_s)
        if ev:
            data = ev.get("data") or {}
            if data.get("error"):
                raise RuntimeError(f"relay:{name}: {data['error']}")
            return data
        call("act", {"steps": [{"key": ["escape"]}, {"wait": 400}]})  # a chat left open, or a mistyped line
        ensure_running()  # Escape with no chat open pauses the game instead
    raise RuntimeError(f"no reply to relay:{name} (is the pack loaded? /reload after changes)")


def telemetry():
    return call("state", {"action": "read", "topic": "minecraft"}).get("latest", {}).get("minecraft")


def ensure_running():
    """A focus steal (Armoury Crate's notice on pad plug-in) pauses Minecraft, and paused worlds stop ticking: resume
    with B until telemetry's tick moves again."""
    for _ in range(3):
        a = (telemetry() or {}).get("tick")
        time.sleep(0.6)
        b = (telemetry() or {}).get("tick")
        if a is not None and b is not None and b > a:
            return
        call("gamepad_press", {"buttons": ["b"]})
        time.sleep(1.0)
    raise SystemExit("the game stays paused (telemetry's tick doesn't move)")


def land():
    """Stop flying (creative): double-tap A, then fall to the ground."""
    for _ in range(2):
        s = telemetry() or {}
        if not s.get("fly"):
            return True
        call("gamepad_sequence", {"steps": [{"buttons": ["a"], "ms": 60}, {"ms": 90}, {"buttons": ["a"], "ms": 60}, {"ms": 700}]})
    return not (telemetry() or {}).get("fly")


def blueprint(bx, fy, bz):
    """What tasks/house.py should leave: {(x, y, z): block}."""
    def ring(r):
        return [(i, j) for i in range(-r, r + 1) for j in range(-r, r + 1) if max(abs(i), abs(j)) == r]
    want = {}
    for i, j in ring(2):
        for layer in (0, 1, 2):
            if (i, j) == (0, -2) and layer < 2:
                continue
            glass = layer == 1 and (i, j) in {(0, 2), (2, 0), (-2, 0)}
            want[(bx + i, fy + layer, bz + j)] = "glass" if glass else "oak_planks"
    for i in range(-2, 3):
        for j in range(-2, 3):
            want[(bx + i, fy + 3, bz + j)] = "oak_planks"
    want[(bx, fy, bz - 2)] = "door"
    want[(bx, fy + 1, bz - 2)] = "door"
    return want


def score(want, found):
    got = {(b[0], b[1], b[2]): b[3] for b in found}
    right = [c for c, blk in want.items() if c in got and (blk in got[c] or (blk == "door" and "door" in got[c]))]
    missing = [c for c in want if c not in got]
    wrong = [[*c, got[c]] for c in want if c in got and c not in right]
    extra = [[*c, got[c]] for c in got if c not in want]
    return {"right": len(right), "of": len(want), "missing": missing[:12], "wrong": wrong[:12], "extra": extra[:12]}


def house(args):
    s = telemetry()
    if not s:
        raise SystemExit("no telemetry: load the world with the RelayMCP Telemetry pack")
    bx, fy, bz = origin(args, s)
    prepare()
    reset(bx, fy, bz, box=4, height=6)
    code = task_code("house")
    t0 = time.time()
    run = program(code, {}, max_s=240)
    wall = time.time() - t0
    found = relay("blocks", {"from": [bx - 2, fy, bz - 2], "to": [bx + 2, fy + 3, bz + 2]}).get("blocks", [])
    return {"task": "house", "state": run.get("state"), "reason": run.get("reason"), "wall_s": round(wall, 1),
            "result": run.get("result"), "cadence": run.get("cadence"), "servo": run.get("servo"),
            "check": score(blueprint(bx, fy, bz), found)}


# --- shared set-up ---------------------------------------------------------------------------------------------------

def origin(args, s):
    return (args.x if args.x is not None else int(s["x"] // 1), args.y if args.y is not None else int(round(s["y"])),
            args.z if args.z is not None else int(s["z"] // 1))


def prepare():
    call("focus_window", {"target": "Minecraft", "pause": {}})  # real time: no turn mode
    call("gamepad_connect", {"keep_plugged": True})
    ensure_running()


def reset(bx, fy, bz, box=4, height=4, yaw=0):
    """Stand at the block's centre on cleared, mob-free ground."""
    relay("tp", {"x": bx + 0.5, "y": fy, "z": bz + 0.5, "yaw": yaw, "pitch": 0})  # first: the area's chunks load
    time.sleep(1.0)
    relay("clear", {"radius": 32})
    relay("fill", {"from": [bx - box, fy, bz - box], "to": [bx + box, fy + height, bz + box], "block": "air"})
    relay("tp", {"x": bx + 0.5, "y": fy, "z": bz + 0.5, "yaw": yaw, "pitch": 0})
    if not land():
        raise SystemExit("still flying after double-tapping A")
    time.sleep(0.5)


def program(code, args, max_s=60, wait=True):
    return call("behavior", {"action": "start", "kind": "program", "max_s": max_s,
                             "params": {"code": code, "args": args, "wait": wait}}, timeout=max_s + 60)


def task_code(name):
    return (HERE / "tasks" / f"{name}.py").read_text()


def finished(rid, timeout):
    """Poll a running behavior until it ends; its final status."""
    end, since, st = time.time() + timeout, 0, {}
    while time.time() < end:
        st = call("behavior", {"action": "status", "id": rid, "since": since})
        if st.get("state") not in (None, "running", "queued"):
            return st
        time.sleep(0.3)
    call("behavior", {"action": "stop", "id": rid})
    return st


def card(task, run, **extra):
    return {"task": task, "state": run.get("state"), "reason": run.get("reason"), "result": run.get("result"),
            "cadence": run.get("cadence"), **extra}


# --- tasks -----------------------------------------------------------------------------------------------------------

def step(args):
    s = telemetry()
    bx, fy, bz = origin(args, s)
    prepare()
    reset(bx, fy, bz)
    run = program(task_code("step"), {"STEPS": [10, 30, 60, 90, 180, -90, -30], "PITCHES": [30.0, -30.0, 0.0]})
    return card("step", run)


def pursuit(args):
    s = telemetry()
    bx, fy, bz = origin(args, s)
    prepare()
    reset(bx, fy, bz)
    out = []
    for rate in (20.0, 40.0, 80.0):
        run = program(task_code("pursuit"), {"RATE": rate, "PITCH": 0.0, "DUR": 7.0, "ACQUIRE": 1.5,
                                             "ANGLE_LAG": ANGLE_LAG})
        out.append(card("pursuit", run, rate=rate))
    return {"task": "pursuit", "runs": out}


def reaction(args, trials=5):
    s = telemetry()
    bx, fy, bz = origin(args, s)
    rng = random.Random(args.seed)
    prepare()
    reset(bx, fy, bz, box=8, height=4)
    out = []
    for _ in range(trials):
        relay("clear", {"radius": 32})
        bearing = rng.uniform(-150, 150)
        rid = program(task_code("reaction"), {"KIND": "pig", "WAIT_S": 10.0, "HOLD": 3.0}, max_s=30, wait=False)["id"]
        t0 = time.time()
        while time.time() - t0 < 6:
            st = call("behavior", {"action": "status", "id": rid})
            if any(e.get("msg") == "ready" for e in st.get("new_events") or st.get("events") or []):
                break
            time.sleep(0.15)
        s = telemetry()
        th = math.radians(s["yaw"] + bearing)
        at = [round(s["x"] - math.sin(th) * 6.0, 2), s["y"], round(s["z"] + math.cos(th) * 6.0, 2)]
        relay("summon", {"type": "pig", "at": at})
        run = finished(rid, 40)
        out.append(card("reaction", run, bearing_asked=round(bearing, 1)))
    relay("clear", {"radius": 32})
    return {"task": "reaction", "runs": out}


def course(args, n=10):
    s = telemetry()
    bx, fy, bz = origin(args, s)
    rng = random.Random(args.seed)
    pts = route(rng, bx + 0.5, bz + 0.5, n, box=11)  # within the cleared box
    prepare()
    reset(bx, fy, bz, box=13, height=3)
    run = program(task_code("course"), {"WPS": pts, "TOL": 0.35, "STOP_MS": 1.0, "MODE": args.mode}, max_s=90)
    return card("course", run, waypoints=pts)


def summary(c):
    """One scorecard line per task."""
    cad = lambda r: (r or {}).get("cadence") or {}  # noqa: E731
    floor = lambda r: f"cadence worst {cad(r).get('worst_ms')} ms, p95 {cad(r).get('p95_ms')}, >50 ms {cad(r).get('over_50ms')}"  # noqa: E731
    t = c.get("task")
    if c.get("state") not in (None, "done"):
        return f"{t}: {c.get('state')} {c.get('reason')}"
    if t == "step":
        rows = (c.get("result") or {}).get("rows") or []
        y90 = next((r for r in rows if r["axis"] == "yaw" and r["step"] == 90), {})
        worst = max((max(abs(r["yaw_err"]), abs(r["pitch_err"])) for r in rows), default=None)
        return (f"step: 90 deg in {y90.get('model_s')} s (confirmed {y90.get('confirm_s')} s); worst final error "
                f"{worst} deg; confirmed {sum(r['confirmed'] for r in rows)}/{len(rows)}; {floor(c)}")
    if t == "pursuit":
        parts = [f"{r['rate']:g}/s rms {(r.get('result') or {}).get('rms')} (bias {(r.get('result') or {}).get('bias')}, "
                 f"jitter {(r.get('result') or {}).get('jitter')})" for r in c["runs"]]
        return "pursuit: " + "; ".join(parts) + f"; {floor(c['runs'][-1])}"
    if t == "reaction":
        ok = [r for r in c["runs"] if r.get("state") == "done"]
        on = [r["result"]["on_target_s"] for r in ok]
        conf = [r["result"]["confirmed_s"] for r in ok]
        hold = [r["result"]["hold_rms"] for r in ok if r["result"].get("hold_rms") is not None]
        return (f"reaction: {len(ok)}/{len(c['runs'])} faced; on target {sum(on) / len(on):.2f} s mean, "
                f"{max(on):.2f} max (confirmed {sum(conf) / len(conf):.2f} s); hold rms "
                f"{sum(hold) / len(hold):.2f} deg; " + floor(ok[-1]) if ok else "reaction: none faced")
    if t == "course":
        r = c.get("result") or {}
        return (f"course ({r.get('mode')}): {r.get('reached')}/{r.get('of')} waypoints in {r.get('seconds')} s (ideal "
                f"{r.get('ideal_s')} s); cross-track max {r.get('xtrack_max')} m, rms {r.get('xtrack_rms')}; stops "
                f"{r.get('stops')}; {floor(c)}")
    if t == "house":
        r, k = c.get("result") or {}, c.get("check") or {}
        return (f"house: {k.get('right')}/{k.get('of')} right, {len(k.get('extra') or [])} extra, "
                f"{r.get('seconds')} s, {r.get('retries')} retries; {floor(c)}")
    return f"{t}: ?"


TASKS = {"step": step, "pursuit": pursuit, "reaction": reaction, "course": course, "house": house}


def save(cards):
    path = HERE / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cards, indent=1))
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=[*TASKS, "suite"])
    ap.add_argument("--x", type=int)
    ap.add_argument("--y", type=int)
    ap.add_argument("--z", type=int)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mode", choices=["path", "points"], default="path", help="course: walk_path or walk_to each")
    a = ap.parse_args()
    names = list(TASKS) if a.task == "suite" else [a.task]
    cards = []
    for i, name in enumerate(names):
        if a.task == "suite":  # each task on its own ground, 40 blocks apart along x
            s0 = telemetry()
            a.x = (a.x if a.x is not None else int(s0["x"] // 1)) + (40 if i else 0)
            a.y = a.y if a.y is not None else int(round(s0["y"]))
            a.z = a.z if a.z is not None else int(s0["z"] // 1)
        try:
            c = TASKS[name](a)
        except Exception as e:  # one broken task shouldn't lose the others' results
            c = {"task": name, "state": "error", "reason": f"{type(e).__name__}: {e}"}
        cards.append(c)
        print(summary(c), flush=True)
    print("saved", save(cards))
