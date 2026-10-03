# Bench task "house" (Minecraft Creative, controller): a 5x5 plank house, closed loop on game telemetry (the
# RelayMCP Telemetry pack) and the camera servo. Every block is aimed with face_point at a face of the block it rests
# on, placed with a tap that doesn't wait once telemetry shows the crosshair on that face, and checked in telemetry
# (the looked-at block becomes the new one). The hotbar slot and the
# pillar jumps are read from telemetry too: no clamps, no open-loop turns, no picture diffs, no blind waits.
# Start: standing on flat ground at a block's centre (relay:tp), hotbar 0 planks, 2 glass, 3 door.
PLANKS, GLASS, DOOR = 0, 2, 3
WINDOWS = {(0, 2), (2, 0), (-2, 0)}
DOORWAY = (0, -2)
TOL = 1.5  # degrees: a face is a metre across, 2-3 m away; the aim only has to land inside it
stats = {"placed": 0, "retries": 0, "failed": [], "aim_misses": [], "phases": {}}


def tel():
    s = telemetry()
    if not s:
        raise RuntimeError("no telemetry (is the RelayMCP Telemetry pack on this world?)")
    return s


def looked():
    lk = tel().get("look")
    return tuple(lk[:3]) if lk else None


FACES = {(0, 1, 0): "up", (0, -1, 0): "down", (0, 0, -1): "north", (0, 0, 1): "south", (1, 0, 0): "east",
         (-1, 0, 0): "west"}  # the support's face a new block attaches to, by where the new block goes


def looked_at():
    """(block, face) under the crosshair: placing attaches to that face, so the block alone isn't enough (a hit on the
    support's underside put a block below it)."""
    lk = tel().get("look")
    return (tuple(lk[:3]), str(lk[3]).lower()) if lk else (None, None)


def slot(i):
    for _ in range(12):
        now = tel()["slot"]
        if now == i:
            return True
        tap("rb" if (i - now) % 9 <= 4 else "lb", ms=40)
        until(lambda: tel()["slot"] != now, timeout=0.5, hz=60)
    return tel()["slot"] == i


def ring(r):
    cells = [(i, r) for i in range(-r, r + 1)] + [(r, j) for j in range(r - 1, -r - 1, -1)]
    cells += [(i, -r) for i in range(r - 1, -r - 1, -1)] + [(-r, j) for j in range(-r + 1, r)]
    return [c for c in cells if abs(c[0]) == abs(c[1])] + [c for c in cells if abs(c[0]) != abs(c[1])]  # corners first


def put(cell, support, point, name, which):
    """Place into `cell` by aiming at `point` on a face of `support` (world block coordinates)."""
    for attempt in range(2):
        if not slot(which):
            stats["failed"].append([name, "slot"])
            return False
        face_point(*point, tol=TOL if attempt == 0 else 0.5, timeout=2.5)  # a miss: aim finer (far, grazing faces)
        want = (support, FACES[tuple(c - s for c, s in zip(cell, support))])
        until(lambda: looked_at() == want, timeout=0.3, hz=60)  # on the support's right face, nothing in front
        if looked_at() != want:
            stats["retries"] += 1
            stats["aim_misses"].append([name, looked_at()[1]])
            continue
        tap("lt", ms=50)
        if until(lambda: looked() == cell, timeout=0.6, hz=60):
            stats["placed"] += 1
            return True
        stats["retries"] += 1
    stats["failed"].append([name, "look", list(looked_at())])
    return False


def up_one(bx, bz):
    """Jump and place a block under the feet at the top of the jump: stand one block higher."""
    y0 = tel()["y"]
    face(tel()["yaw"], -89.5, tol=2.0)
    slot(PLANKS)
    tap("a", ms=60)
    until(lambda: tel()["y"] >= y0 + 1.05, timeout=1.0, hz=60)
    tap("lt", ms=50)
    return until(lambda: tel()["ground"] and tel()["y"] >= y0 + 0.99, timeout=1.5, hz=60) is not None


def down_one():
    y0 = tel()["y"]
    face(tel()["yaw"], -89.5, tol=2.0)
    pad(rt=1)
    ok = until(lambda: tel()["y"] <= y0 - 0.99 and tel()["ground"], timeout=2.0, hz=60)
    pad()
    return ok is not None


t0 = elapsed()
s = tel()
bx, fy, bz = math.floor(s["x"]), round(s["y"]), math.floor(s["z"])  # the feet's cell; the floor is fy - 1
cx, cz = bx + 0.5, bz + 0.5
for layer in (0, 1):                                                  # walls, from the floor
    for i, j in ring(2):
        if (i, j) == DOORWAY:
            continue
        which = GLASS if layer == 1 and (i, j) in WINDOWS else PLANKS
        cell = (bx + i, fy + layer, bz + j)
        put(cell, (bx + i, fy + layer - 1, bz + j), (cx + i, fy + layer, cz + j), f"wall{layer} {i},{j}", which)
stats["phases"]["walls01"] = round(elapsed() - t0, 1)
up = [up_one(bx, bz)]
for i, j in ring(2):                                                  # layer 2, and the lintel over the doorway
    if (i, j) == DOORWAY:
        put((bx + i, fy + 2, bz + j), (bx + i + 1, fy + 2, bz + j), (bx + i + 1.0, fy + 2.5, cz + j), "lintel", PLANKS)
    else:
        put((bx + i, fy + 2, bz + j), (bx + i, fy + 1, bz + j), (cx + i, fy + 2, cz + j), f"wall2 {i},{j}", PLANKS)
stats["phases"]["wall2"] = round(elapsed() - t0, 1)
up.append(up_one(bx, bz))
for i, j in ring(2):                                                  # roof: the outer ring on the wall tops
    put((bx + i, fy + 3, bz + j), (bx + i, fy + 2, bz + j), (cx + i, fy + 3, cz + j), f"roof {i},{j}", PLANKS)
for i, j in ring(1):                                                  # the inner ring, against the outer ring's insides
    si, sj = (i + (1 if i > 0 else -1), j) if i else (i, j + (1 if j > 0 else -1))
    face_x = bx + si + (0.0 if si > i else 1.0) if si != i else cx + i
    face_z = bz + sj + (0.0 if sj > j else 1.0) if sj != j else cz + j
    put((bx + i, fy + 3, bz + j), (bx + si, fy + 3, bz + sj), (face_x, fy + 3.5, face_z), f"roof {i},{j}", PLANKS)
stats["phases"]["roof"] = round(elapsed() - t0, 1)
down = [down_one(), down_one()]
put((bx, fy + 3, bz), (bx, fy + 3, bz + 1), (cx, fy + 3.5, bz + 1.0), "roof centre", PLANKS)  # from below
put((bx, fy, bz - 2), (bx, fy - 1, bz - 2), (cx, fy, cz - 2), "door", DOOR)
stats["phases"]["built"] = round(elapsed() - t0, 1)
stop_facing()
result = {"seconds": round(elapsed() - t0, 1), "origin": [bx, fy, bz], "up": up, "down": down, **stats}
