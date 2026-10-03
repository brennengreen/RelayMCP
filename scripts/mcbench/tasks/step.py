# Bench task "step": the camera's step response on game telemetry (ground truth). From rest, face() each yaw step
# and pitch target in turn: the time until the stick model says the camera will rest on target (what a program
# waits for), the time until telemetry confirms it, and the error telemetry shows once it has settled.
# Inputs: STEPS = yaw steps (degrees, right +), PITCHES = pitch targets (degrees, up +).


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


def fresh():
    """The next new telemetry sample (a look), or the latest."""
    k0 = (telemetry() or {}).get("tick")
    return until(lambda: (telemetry() or {}).get("tick") not in (None, k0) and telemetry(), timeout=0.5,
                 hz=120) or telemetry()


def settle(seconds):
    """Let a correction play out, looking all the while (a blind wait would count against the cadence)."""
    end = elapsed() + seconds
    until(lambda: telemetry() is None or elapsed() >= end, timeout=seconds + 0.2, hz=60)


def trial(axis, yaw, pitch, step):
    t0 = elapsed()
    a = face(yaw, pitch, timeout=4.0)
    model_s = elapsed() - t0
    c = face(yaw, pitch, timeout=3.0, confirm=True)
    confirm_s = elapsed() - t0
    settle(0.4)
    s = fresh()
    return {"axis": axis, "step": round(step, 1), "model_s": round(model_s, 3), "confirm_s": round(confirm_s, 3),
            "yaw_err": round(wrap(s["yaw"] - yaw), 2), "pitch_err": round(s["pitch"] - pitch, 2),
            "on_target": bool(a.get("on_target")), "confirmed": bool(c.get("on_target"))}


rows = []
s = fresh()
face(s["yaw"], 0.0, timeout=3.0, confirm=True)
settle(0.3)
for st in STEPS:
    s = fresh()
    rows.append(trial("yaw", s["yaw"] + st, 0.0, st))
for p in PITCHES:
    s = fresh()
    rows.append(trial("pitch", s["yaw"], p, p - s["pitch"]))
s = fresh()
face(s["yaw"], 0.0, timeout=3.0)
stop_facing()
result = {"rows": rows}
