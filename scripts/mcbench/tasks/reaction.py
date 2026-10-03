# Bench task "reaction": wait for a new mob of KIND in telemetry (the bench summons it at a random bearing while
# this runs), then face its head until telemetry confirms, and keep it there while it wanders for HOLD seconds.
# Times run from the first sample that lists it; telemetry lists mobs every other tick, so positions are kept
# between those samples. Inputs: KIND, WAIT_S, HOLD.


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


def mobs():
    return (telemetry() or {}).get("mobs")


known = set()
for _ in range(3):  # a listing from before the summon
    m0 = until(mobs, timeout=0.5, hz=120) or []
    known |= {m[5] for m in m0}
t_ready = elapsed()
log("ready", known=len(known))


def newcomer():
    for m in mobs() or []:
        if m[0] == KIND and m[5] not in known:
            return m
    return None


first = until(newcomer, timeout=WAIT_S, hz=120)
if not first:
    raise RuntimeError(f"no new {KIND} appeared in {WAIT_S} s")
t_seen = elapsed()
mid = first[5]
spot = [first[1], first[4], first[3]]  # x, head height, z


def where():
    for m in mobs() or []:
        if m[5] == mid:
            spot[:] = [m[1], m[4], m[3]]
    return tuple(spot)


s = telemetry()
dx, dz = spot[0] - s["x"], spot[2] - s["z"]
bearing = wrap(math.degrees(math.atan2(-dx, dz)) - s["yaw"])
r = face_point(*where(), tol=1.5, timeout=4.0)
t_model = elapsed()
c = face_point(*where(), tol=1.5, timeout=3.0, confirm=True)
t_on = elapsed()
keep_facing(where)
errs, last = [], None
end = elapsed() + HOLD
while elapsed() < end:
    s = until(lambda: (telemetry() or {}).get("tick") not in (None, last) and telemetry(), timeout=0.3, hz=120)
    if not s:
        continue
    last = s["tick"]
    x, y, z = where()
    dx, dy, dz = x - s["x"], y - s["ey"], z - s["z"]
    want_yaw = math.degrees(math.atan2(-dx, dz))
    want_pitch = math.degrees(math.atan2(dy, math.hypot(dx, dz)))
    errs.append(math.hypot(wrap(s["yaw"] - want_yaw), s["pitch"] - want_pitch))
stop_facing()
result = {"bearing": round(bearing, 1), "seen_s": round(t_seen - t_ready, 3),
          "on_target_s": round(t_model - t_seen, 3), "confirmed_s": round(t_on - t_seen, 3),
          "on_target": bool(r.get("on_target")), "confirmed": bool(c.get("on_target")),
          "hold_rms": round(math.sqrt(sum(e * e for e in errs) / len(errs)), 2) if errs else None,
          "hold_max": round(max(errs), 2) if errs else None}
