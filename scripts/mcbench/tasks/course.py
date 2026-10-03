# Bench task "course": walk a route through waypoints WPS = [[x, z], ...] (world coordinates) while the camera faces
# the next waypoint, as a person would. MODE "path": one walk_path (no stop at each point); "points": walk_to each in
# turn (eases to a stop at every one). Telemetry samples during the walk (a guard records them: guards are checked
# during every wait) give the cross-track error (distance from the route's straight legs) and stops (under STOP_MS
# m/s away from the start and the end). Inputs: WPS, TOL, STOP_MS, MODE.
WALK = 4.317
rec = {"pts": [], "last": None, "k": 0}


def nearest(px, pz):
    """(distance to the route, segment index), moving on from the last segment only (the route may cross itself)."""
    best = None
    for k in range(rec["k"], min(rec["k"] + 2, len(route) - 1)):
        (ax, az), (bx, bz) = route[k], route[k + 1]
        ux, uz = bx - ax, bz - az
        n = ux * ux + uz * uz or 1e-9
        u = max(0.0, min(1.0, ((px - ax) * ux + (pz - az) * uz) / n))
        d = math.hypot(px - (ax + u * ux), pz - (az + u * uz))
        if best is None or d <= best[0]:
            best = (d, k)
    rec["k"] = best[1]
    return best


def sample():
    s = telemetry()
    if s and s.get("tick") != rec["last"]:
        rec["last"] = s.get("tick")
        d, k = nearest(s["x"], s["z"])
        rec["pts"].append((s["x"], s["z"], math.hypot(s.get("vx", 0.0), s.get("vz", 0.0)) * 20.0, d, elapsed()))
    return False  # never fires: it only records


def heading():
    """Face the end of the leg being walked (eye height: level)."""
    s = telemetry() or {}
    k = min(rec["k"] + 1, len(route) - 1)
    bx, bz = route[k]
    if "x" not in s or math.hypot(bx - s["x"], bz - s["z"]) < 0.8:
        bx, bz = route[min(k + 1, len(route) - 1)]
    return (math.degrees(math.atan2(-(bx - s.get("x", 0.0)), bz - s.get("z", 0.0))), 0.0)


s = telemetry()
route = [(s["x"], s["z"])] + [(float(x), float(z)) for x, z in WPS]
total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))
guard(sample, "course-recorder")
keep_facing(heading)
t0 = elapsed()
if MODE == "path":
    r = walk_path(WPS, tol=TOL)
    arrived, reached = r["arrived"], r["reached"]
else:
    reached = 0
    for wx, wz in WPS:
        r = walk_to(wx, wz, tol=TOL, timeout=10.0)
        reached += bool(r["arrived"])
    arrived = reached == len(WPS)
took = elapsed() - t0
stop_facing()
pts = rec["pts"]
ex, ez = route[-1]
mid = [p for p in pts if math.hypot(p[0] - route[0][0], p[1] - route[0][1]) > 1.0
       and math.hypot(p[0] - ex, p[1] - ez) > 1.0]
stops, was_slow = 0, False
for p in mid:  # count each slow-down once
    slow = p[2] < STOP_MS
    stops += slow and not was_slow
    was_slow = slow
xt = [p[3] for p in pts]
result = {"mode": MODE, "seconds": round(took, 2), "ideal_s": round(total / WALK, 2), "arrived": bool(arrived),
          "reached": reached, "of": len(WPS), "end_miss": round(math.hypot(pts[-1][0] - ex, pts[-1][1] - ez), 2) if pts else None,
          "xtrack_max": round(max(xt), 2) if xt else None,
          "xtrack_rms": round(math.sqrt(sum(d * d for d in xt) / len(xt)), 2) if xt else None,
          "stops": stops, "samples": len(pts)}
