"""Claude's Spinal Score entry: a hand-written agent (no model at play time), written by Claude Opus 5.5 and tuned only on
practice maps (Freedoom 2 MAP05 and later), never on the season's maps.

    spinal arena run --board reflex --agent plugin:examples/claude_agent.py:Agent --name "Claude (hand-written agent)"

How it plays, from what the game hands it (state, on-screen objects, the layout):
- A height map of the level: every 16-unit cell gets its sector's floor and ceiling, so it knows steps it can climb,
  ledges it can only drop from, openings too low to pass and shut doors (worth a push and a use).
- Exploration: it ray-casts its view over the map and walks to the nearest reachable place it has not seen yet,
  through doors (and walls that are really doors: that is where secrets hide), pressing use when blocked.
- A path search (Dijkstra, away from walls) that runs a slice per tic so a decision never costs a tic.
- Fights: it turns onto the target and fires on the same tic, taps the trigger for accurate pistol and chaingun
  shots, strafes, keeps away from melee monsters, turns round when hit from behind and gives up on monsters it can't hurt.
- Items: it remembers what it saw and fetches what it needs (health when hurt, weapons, ammo when low, armor).
"""
import math
import random

import numpy as np

CELL = 16.0
STEP = 24.0
HEIGHT = 56.0
INF = 1 << 30

HP = {"Zombieman": 20, "ShotgunGuy": 30, "WolfensteinSS": 50, "DoomImp": 60, "ChaingunGuy": 70, "LostSoul": 100,
      "Demon": 150, "Spectre": 150, "Revenant": 300, "Cacodemon": 400, "PainElemental": 400, "HellKnight": 500,
      "Arachnotron": 500, "Fatso": 600, "Archvile": 700, "BaronOfHell": 1000, "SpiderMastermind": 3000,
      "Cyberdemon": 4000}
HITSCAN = {"Zombieman", "ShotgunGuy", "ChaingunGuy", "WolfensteinSS", "SpiderMastermind"}
MELEE = {"Demon", "Spectre", "LostSoul"}
ITEM_VALUE = {"Medikit": 25, "Stimpack": 10, "HealthBonus": 1, "GreenArmor": 40, "BlueArmor": 60, "ArmorBonus": 2,
              "Shotgun": 120, "SuperShotgun": 140, "Chaingun": 130, "RocketLauncher": 110, "PlasmaRifle": 110,
              "BFG9000": 100, "Chainsaw": -1, "Clip": 10, "ClipBox": 25, "Shell": 10, "ShellBox": 25,
              "RocketAmmo": 8, "RocketBox": 20, "Cell": 10, "CellPack": 25}


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


class Level:
    """The layout as a grid: sector per cell (floor, ceiling), walls, sight blockers and the moves allowed between
    neighbouring cells (a step up of at most 24, an opening of at least 56, or a shut door)."""

    def __init__(self, layout):
        owners = {}
        for i, sc in enumerate(layout):
            for ln in sc.lines:
                owners.setdefault(id(ln), [ln, []])[1].append(i)
        xs = [p for ln, _ in owners.values() for p in (ln.x1, ln.x2)]
        ys = [p for ln, _ in owners.values() for p in (ln.y1, ln.y2)]
        self.x0, self.y0 = min(xs) - 2 * CELL, min(ys) - 2 * CELL
        self.W = int((max(xs) - self.x0) / CELL) + 3
        self.H = int((max(ys) - self.y0) / CELL) + 3
        H, W = self.H, self.W
        cx = self.x0 + (np.arange(W) + 0.5) * CELL
        cy = self.y0 + (np.arange(H) + 0.5) * CELL
        sec = np.full((H, W), -1, np.int32)
        best = np.full((H, W), np.inf)
        for i, sc in enumerate(layout):
            if not sc.lines:
                continue
            a = np.array([(ln.x1, ln.y1, ln.x2, ln.y2) for ln in sc.lines], float)
            c0, c1 = np.searchsorted(cx, a[:, [0, 2]].min()), np.searchsorted(cx, a[:, [0, 2]].max())
            r0, r1 = np.searchsorted(cy, a[:, [1, 3]].min()), np.searchsorted(cy, a[:, [1, 3]].max())
            if c1 <= c0 or r1 <= r0:
                continue
            px, py = cx[c0:c1][None, :], cy[r0:r1][:, None]
            inside = np.zeros((r1 - r0, c1 - c0), bool)
            for x1, y1, x2, y2 in a:
                if y1 == y2:
                    continue
                cross = (y1 > py) != (y2 > py)
                xi = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
                inside ^= cross & (px < xi)
            area = (a[:, [0, 2]].max() - a[:, [0, 2]].min()) * (a[:, [1, 3]].max() - a[:, [1, 3]].min())
            sub = best[r0:r1, c0:c1]
            take = inside & (area < sub)
            sub[take] = area
            sec[r0:r1, c0:c1][take] = i
        self.sec = sec
        fh = np.array([s.floor_height for s in layout] + [0], float)
        ch = np.array([s.ceiling_height for s in layout] + [0], float)
        self.floor, self.ceil = fh[sec], ch[sec]
        void = sec < 0
        self.door_sector = np.array([s.ceiling_height - s.floor_height <= 8 for s in layout] + [False])
        door = self.door_sector[sec] & ~void
        wall = np.zeros((H, W), bool)
        sight = np.zeros((H, W), bool)
        self.doors = []  # (x, y) midpoints of lines next to a shut sector: doors, lifts, secret walls
        for ln, secs in owners.values():
            if ln.is_blocking and (len(set(secs)) == 1 or len(secs) == 1):
                self._raster(wall, ln)
                self._raster(sight, ln)
            elif ln.is_blocking:
                self._raster(wall, ln)
            elif any(self.door_sector[s] for s in secs):
                self._raster(sight, ln)
                self.doors.append(((ln.x1 + ln.x2) / 2, (ln.y1 + ln.y2) / 2))
        self.sight = sight | void | door
        free = ~void & ~wall
        near = ~free
        grown = near.copy()
        grown[1:, :] |= near[:-1, :]
        grown[:-1, :] |= near[1:, :]
        grown[:, 1:] |= near[:, :-1]
        grown[:, :-1] |= near[:, 1:]
        self.free = free
        self.cost = np.where(grown, 4, 1).astype(np.int32)
        self.moves = []  # (flat offset, allowed-from array)
        f, c = self.floor, self.ceil
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            ok = np.zeros((H, W), bool)
            src = (slice(max(0, -dr), H - max(0, dr)), slice(max(0, -dc), W - max(0, dc)))
            dst = (slice(src[0].start + dr, src[0].stop + dr), slice(src[1].start + dc, src[1].stop + dc))
            up = f[dst] - f[src] <= STEP
            opening = (np.minimum(c[src], c[dst]) - np.maximum(f[src], f[dst]) >= HEIGHT) | door[src] | door[dst]
            ok[src] = free[src] & free[dst] & up & opening
            self.moves.append((dr * W + dc, ok.ravel().tolist()))
        self.costl = self.cost.ravel().tolist()
        self.banned = [0] * (H * W)  # tic until which a cell is off limits (stuck there, a locked door)
        self.seen = np.zeros((H, W), bool)
        self.hazard = set()

    def _raster(self, grid, ln):
        n = int(max(abs(ln.x2 - ln.x1), abs(ln.y2 - ln.y1)) / (CELL / 3)) + 1
        t = np.linspace(0, 1, n + 1)
        r, c = self.cell_arr(ln.x1 + t * (ln.x2 - ln.x1), ln.y1 + t * (ln.y2 - ln.y1))
        grid[r, c] = True

    def cell_arr(self, x, y):
        c = np.clip(((x - self.x0) / CELL).astype(int), 0, self.W - 1)
        r = np.clip(((y - self.y0) / CELL).astype(int), 0, self.H - 1)
        return r, c

    def cell(self, x, y):
        return (min(self.H - 1, max(0, int((y - self.y0) / CELL))), min(self.W - 1, max(0, int((x - self.x0) / CELL))))

    def flat(self, x, y):
        r, c = self.cell(x, y)
        return r * self.W + c

    def point(self, i):
        r, c = divmod(i, self.W)
        return self.x0 + (c + 0.5) * CELL, self.y0 + (r + 0.5) * CELL

    def look(self, x, y, ang, rng=1100.0, half=46.0):
        """Mark what is in view as seen: rays over the field of view, stopped by walls and shut doors."""
        angs = np.radians(ang + np.linspace(-half, half, 33))[:, None]
        d = np.arange(8.0, rng, 10.0)[None, :]
        r, c = self.cell_arr(x + np.cos(angs) * d, y + np.sin(angs) * d)
        blk = self.sight[r, c]
        first = np.where(blk.any(1), blk.argmax(1), blk.shape[1])
        keep = np.arange(blk.shape[1])[None, :] < first[:, None]
        self.seen[r[keep], c[keep]] = True
        r0, c0 = self.cell(x, y)
        self.seen[max(0, r0 - 3):r0 + 4, max(0, c0 - 3):c0 + 4] = True

    def walkable(self, x, y):
        r, c = self.cell(x, y)
        return bool(self.free[r, c])


class Field:
    """Dijkstra from one cell over the allowed moves (dial's buckets: costs are small integers), run in slices."""

    def __init__(self, lvl, src, tic):
        self.lvl, self.src, self.tic = lvl, src, tic
        n = lvl.H * lvl.W
        self.dist, self.par = [INF] * n, [-1] * n
        self.dist[src] = 0
        self.buckets, self.d, self.done = [[src]], 0, False

    def run(self, budget, tic):
        lvl, dist, par, buckets = self.lvl, self.dist, self.par, self.buckets
        moves, cost, banned = lvl.moves, lvl.costl, lvl.banned
        while budget > 0:
            if self.d >= len(buckets):
                self.done = True
                return
            b = buckets[self.d]
            if not b:
                self.d += 1
                continue
            u = b.pop()
            d = self.d
            if dist[u] != d:
                continue
            budget -= 1
            for off, ok in moves:
                if ok[u]:
                    v = u + off
                    if banned[v] > tic:
                        continue
                    nd = d + cost[v]
                    if nd < dist[v]:
                        dist[v], par[v] = nd, u
                        while len(buckets) <= nd:
                            buckets.append([])
                        buckets[nd].append(v)

    def path(self, goal):
        if self.dist[goal] >= INF:
            return None
        out, u = [], goal
        while u != -1:
            out.append(u)
            u = self.par[u]
        out.reverse()
        return out


class Agent:
    def __init__(self):
        self.layout = None
        self.stats = {"bans": 0, "uses": 0, "dead": 0, "unreachable": 0, "turnarounds": 0, "revived": 0}

    def debug(self):
        lvl = self.lvl
        seen = float((lvl.seen & lvl.free).sum() / max(1, lvl.free.sum())) if lvl else 0
        return {**self.stats, "seen": round(seen, 2)}

    def reset(self, layout):
        self.layout = layout
        self.lvl = Level(layout)
        self.field = self.next_field = None
        self.goal = None  # (kind, flat cell, ref)
        self.path, self.pi = None, 0
        self.items = {}  # id -> item
        self.monsters = {}  # id -> (tic last seen, entry)
        self.dead, self.ignore = {}, {}  # dead: id -> tic it died (to forget it), ignore: id -> tic
        self.kills = self.damage = self.dealt = 0
        self.health = 100
        self.target, self.fired_at, self.dry, self.dry_pos = None, -99, 0, None
        self.hist = []
        self.stuck, self.unstick, self.use_cd = 0, 0, 0
        self.prog_goal, self.prog_best, self.prog_t = None, 0, 0
        self.bancount = {}
        self.trail = []
        self.cover, self.cover_retry, self.hurt = None, 0, []
        self.strafe, self.strafe_t = 1, 0
        self.spin = 0
        self.fire_toggle = False
        self.weapons = {"Pistol"}
        self.last_hit = -99
        self.rng = random.Random(7)
        self.trace = []
        self.hurtlog = []
        self.prev_mon = {}
        self.blind_t = -99
        self.avoided = set()

    # -- memory --------------------------------------------------------------------------------------------------

    def remember(self, st, tic):
        x, y = st["x"], st["y"]
        for it in st["items"]:
            self.items[it["id"]] = it
            if it["name"] == "Chainsaw" and it["id"] not in self.avoided:
                # picking it up switches to it and there is no button to switch back: keep off it
                self.avoided.add(it["id"])
                lvl = self.lvl
                r0, c0 = lvl.cell(it["x"], it["y"])
                for dr in range(-3, 4):
                    for dc in range(-3, 4):
                        if 0 <= r0 + dr < lvl.H and 0 <= c0 + dc < lvl.W and dr * dr + dc * dc <= 10:
                            lvl.banned[(r0 + dr) * lvl.W + c0 + dc] = INF
        seen_i = {i["id"] for i in st["items"]}
        for k, it in list(self.items.items()):
            d = math.hypot(it["x"] - x, it["y"] - y)
            if d < 30 or (k not in seen_i and d < 120 and abs(wrap(math.degrees(math.atan2(it["y"] - y, it["x"] - x))
                                                                - st["angle"])) < 25):
                del self.items[k]  # picked up, or it should be in view and is not
                if d < 30 and it["kind"] == "weapon":
                    self.weapons.add(it["name"])
        for m in st["monsters"]:
            if self.dead.pop(m["id"], None) is not None:  # corpses are never listed: on screen means alive
                self.stats["revived"] += 1
            self.monsters[m["id"]] = (tic, m)
        for k in [k for k, (t, _) in self.monsters.items() if k in self.dead or tic - t > 35 * 40]:
            del self.monsters[k]

    def credit(self, st, tic, now_ids):
        """A dying monster drops off the list of things in view, so a kill goes to the one that just vanished: the one
        being shot, else the nearest to the crosshair. Only to forget it (a remembered monster is not hunted)."""
        gone = [m for k, m in self.prev_mon.items() if k not in now_ids]
        for _ in range(st["kills"] - self.kills):
            cand = self.target if any(m["id"] == self.target for m in gone) else \
                min(gone, key=lambda m: abs(m["bearing"]), default={"id": None})["id"]
            if cand is None:
                break
            self.dead[cand] = tic
            gone = [m for m in gone if m["id"] != cand]
            self.stats["dead"] += 1
            if cand == self.target:
                self.target = None

    # -- planning ------------------------------------------------------------------------------------------------

    def plan(self, st, tic):
        """With a fresh distance field: the best thing to go and do."""
        lvl, f = self.lvl, self.field
        dist = self.dist_np = np.asarray(f.dist, dtype=np.int64).reshape(lvl.H, lvl.W)
        reach = dist < INF
        best, score = None, -1.0

        def consider(kind, flat, value, ref=None):
            nonlocal best, score
            d = f.dist[flat]
            if d >= INF:
                return
            s = value / (1.0 + d * CELL / 450.0)
            if self.goal and self.goal[0] == kind and self.goal[2] == ref and (kind != "explore" or self.goal[1] == flat):
                s *= 1.4  # commitment
            if s > score:
                best, score = (kind, flat, ref), s

        hp = st["health"]
        low_ammo = st["ammo"] < 15
        for k, it in self.items.items():
            v = ITEM_VALUE.get(it["name"], 5)
            if v < 0:
                continue
            if it["kind"] == "health":
                if hp >= 100 and it["name"] != "HealthBonus":
                    continue
                v = min(v, 100 - hp) * (2.5 if hp < 50 else 1.0) + 1
            elif it["kind"] == "weapon":
                v = v if it["name"] not in self.weapons else 25
            elif it["kind"] == "ammo":
                v *= 3 if low_ammo else 0.8
            consider("item", lvl.flat(it["x"], it["y"]), v, k)
        if hp > 45 and st["ammo"] > 0:
            for k, (t, m) in self.monsters.items():
                if k in self.dead or self.ignore.get(k, -1) > tic:
                    continue
                v = 30 if tic - t < 35 * 5 else 12
                if HP.get(m["name"], 100) >= 400 and not (self.weapons - {"Pistol", "Chainsaw"}):
                    v = 3
                consider("hunt", lvl.flat(m["x"], m["y"]), v, k)
        unseen = reach & ~lvl.seen & lvl.free
        if unseen.any():
            dd = np.where(unseen, dist, INF)
            flat = int(dd.argmin())
            consider("explore", flat, 20.0)
        if best is None:
            # all seen: revisit the doors (a use on each), then wander
            doors = [lvl.flat(x, y) for x, y in lvl.doors]
            doors = [d for d in doors if f.dist[d] < INF and not self.tried(d)]
            if doors:
                d = min(doors, key=lambda d: f.dist[d])
                best = ("door", d, None)
            else:
                cells = np.flatnonzero(reach.ravel())
                if len(cells):
                    best = ("wander", int(cells[self.rng.randrange(len(cells))]), None)
        if best != self.goal or self.path is None:
            self.goal = best
            self.path = f.path(best[1]) if best else None
            self.pi = 0
            if best and best[0] == "door":
                self.mark_tried(best[1])

    def tried(self, flat):
        return flat in getattr(self, "_tried", set())

    def mark_tried(self, flat):
        if not hasattr(self, "_tried"):
            self._tried = set()
        self._tried.add(flat)

    def think(self, st, tic):
        lvl = self.lvl
        here = lvl.flat(st["x"], st["y"])
        if not lvl.free.ravel()[here]:  # standing on a cell the grid calls blocked (a wall's edge): use a free neighbour
            r, c = divmod(here, lvl.W)
            for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < lvl.H and 0 <= cc < lvl.W and lvl.free[rr, cc]:
                    here = rr * lvl.W + cc
                    break
        if self.next_field is None:
            self.next_field = Field(lvl, here, tic)
        self.next_field.run(2500, tic)
        if self.next_field.done:
            self.field, self.next_field = self.next_field, None
            self.plan(st, tic)

    # -- control -------------------------------------------------------------------------------------------------

    def move_buttons(self, a, move_ang, face_ang):
        rel = wrap(move_ang - face_ang)
        a[2] = int(abs(rel) < 67.5)
        a[3] = int(abs(rel) > 112.5)
        a[4] = int(22.5 < rel < 157.5)
        a[5] = int(-157.5 < rel < -22.5)

    def clear_dir(self, x, y, ang, dist=56.0):
        for d in (dist / 2, dist):
            if not self.lvl.walkable(x + math.cos(math.radians(ang)) * d, y + math.sin(math.radians(ang)) * d):
                return False
        return True

    def path_heading(self, st):
        """The direction along the current path (a point a few cells ahead), or None."""
        p, lvl = self.path, self.lvl
        if not p:
            return None
        x, y = st["x"], st["y"]
        best, bd = self.pi, 1e18
        for i in range(self.pi, min(len(p), self.pi + 40)):
            px, py = lvl.point(p[i])
            d = (px - x) ** 2 + (py - y) ** 2
            if d < bd:
                best, bd = i, d
        self.pi = best
        if best >= len(p) - 1 and bd < 24 ** 2:
            self.arrive(st)
            return None
        tx, ty = lvl.point(p[min(len(p) - 1, best + 4)])
        return math.degrees(math.atan2(ty - y, tx - x))

    def arrive(self, st):
        lvl = self.lvl
        if self.goal:
            r, c = divmod(self.goal[1], lvl.W)
            lvl.seen[max(0, r - 4):r + 5, max(0, c - 4):c + 5] = True
            if self.goal[0] == "hunt":
                self.ignore[self.goal[2]] = self.tic + 35 * 20
                self.monsters.pop(self.goal[2], None)
            elif self.goal[0] == "item":
                self.items.pop(self.goal[2], None)
        self.goal, self.path = None, None
        if self.field:
            self.plan(st, self.tic)

    def pick_target(self, st, tic):
        vis = [m for m in st["monsters"] if self.ignore.get(m["id"], -1) <= tic]
        if not vis:
            return None
        armed = st["ammo"] > 0

        def key(m):
            k = m["distance"]
            if m["name"] in HITSCAN:
                k *= 0.6
            if m["name"] in MELEE:
                k *= 0.8
            if HP.get(m["name"], 100) >= 400 and not (self.weapons - {"Pistol", "Chainsaw"}):
                k *= 2.5
            k *= 1 + abs(m["bearing"]) / 90.0
            if m["id"] == self.target:
                k *= 0.7
            return k
        m = min(vis, key=key)
        if not armed and (m["distance"] > 700 or HP.get(m["name"], 100) >= 400):
            return None  # a melee weapon in hand: only what it can reach and cut down
        return m

    def act(self, state, tic):
        st = state
        if st["layout"] is not self.layout or tic == 0:
            self.reset(st["layout"])
        self.tic = tic
        lvl = self.lvl
        x, y, ang = st["x"], st["y"], st["angle"]
        now_ids = {mm["id"] for mm in st["monsters"]}
        if st["kills"] > self.kills:
            self.credit(st, tic, now_ids)
        self.kills = st["kills"]
        self.prev_mon = {mm["id"]: mm for mm in st["monsters"]}
        self.remember(st, tic)
        if tic % 3 == 0:
            lvl.look(x, y, ang)
        hit = st["damage"] > self.damage
        if hit:
            self.hurt.append((tic, st["damage"] - self.damage))
            vis = [mm["name"] for mm in st["monsters"]]
            mode = "cover" if self.cover else (self.goal[0] if self.goal else "-")
            self.hurtlog.append((tic, st["damage"] - self.damage, ",".join(sorted(set(vis))) or "UNSEEN:" + mode))
        self.damage = st["damage"]
        self.think(st, tic)

        a = [0, 1, 0, 0, 0, 0, 0.0, 0]
        self.hist.append((x, y))
        if len(self.hist) > 20:
            self.hist.pop(0)
        moved = math.hypot(x - self.hist[0][0], y - self.hist[0][1]) if len(self.hist) >= 12 else 99

        m = self.pick_target(st, tic)
        self.trace.append((tic, x, y, st["health"], self.goal[0] if self.goal else "-", m["name"] if m else ""))
        if hit and m is None:
            # hurt by something it can't see: the harness hides monsters near the crosshair, so shoot ahead
            # first; hurt again while doing that: it's behind, turn round
            if tic - self.blind_t > 40:
                self.blind_t = tic
                self.stats["blind"] = self.stats.get("blind", 0) + 1
            elif tic - self.blind_t > 12 and self.spin <= 0:
                self.spin, self.blind_t = 6, -99
                self.stats["turnarounds"] += 1
        if m is None and tic - self.blind_t <= 30 and st["ammo"] > 0 and self.spin <= 0:
            a[0] = int(tic % 2 == 0)
            a[6] = 3.0 if (tic // 5) % 2 else -3.0
            a[4 if (tic // 15) % 2 else 5] = 1
            return a
        ch = self.cover_heading(st, tic)
        if m is not None:
            self.spin = 0
            a = self.fight(st, tic, m, a, moved)
            if ch == "hold":
                a[2:6] = [0, 0, 0, 0]
                a[4 if (tic // 12) % 2 else 5] = 1  # sway in place
            elif ch is not None:
                self.move_buttons(a, ch, ang)
            return a
        if ch == "hold":
            ts = self.threats(st, tic)
            if ts:
                tx, ty = ts[0]
                a[6] = max(-30.0, min(30.0, wrap(math.degrees(math.atan2(ty - y, tx - x)) - ang)))
            return a
        if ch is not None:
            a[6] = max(-30.0, min(30.0, wrap(ch - ang)))
            self.move_buttons(a, ch, ang)
            return a
        if self.spin > 0:
            self.spin -= 1
            a[6] = 30.0
        heading = self.path_heading(st)
        if heading is None:
            a[6] = 10.0 if self.spin <= 0 else a[6]  # nothing to do yet: look around
            return a
        if self.unstick > 0:
            self.unstick -= 1
            self.move_buttons(a, heading + self.unstick_dir, ang)
            a[6] = max(-30.0, min(30.0, wrap(heading - ang))) * 0.5 if self.spin <= 0 else a[6]
            return a
        if self.spin <= 0:
            a[6] = max(-30.0, min(30.0, wrap(heading - ang)))
        self.move_buttons(a, heading, ang)
        if moved < 10:
            self.stuck += 1
        else:
            self.stuck = max(0, self.stuck - 2)
        if self.stuck > 8 and self.use_cd <= 0:
            a[7] = 1  # a door, a lift, a switch: open it
            self.use_cd = 30
            self.stats["uses"] += 1
        self.use_cd -= 1
        left = len(self.path) - self.pi if self.path else 0
        if self.goal != self.prog_goal or left < self.prog_best - 2:
            self.prog_goal, self.prog_best, self.prog_t = self.goal, left, tic
        self.trail.append((x, y))
        del self.trail[:-200]
        far = max(abs(x - px) + abs(y - py) for px, py in self.trail[-175:]) if len(self.trail) >= 175 else 999
        if self.stuck > 40 or tic - self.prog_t > 35 * 5 or far < 64:
            self.trail.clear()  # no closer to the goal in 5 s: give it up for now
            self.prog_goal = None
            self.ban_ahead(st, heading)
        elif self.stuck > 20 and self.stuck % 10 == 0:
            self.unstick, self.unstick_dir = 10, self.rng.choice((-70, 70))
        return a

    def ban_ahead(self, st, heading):
        """Can't get on: put a disc of cells ahead off limits (a locked door, a ledge, a thing in the way), for longer
        each time, and drop the goal."""
        lvl = self.lvl
        self.stats["bans"] += 1
        self.stuck = 0
        hx = st["x"] + math.cos(math.radians(heading)) * 40
        hy = st["y"] + math.sin(math.radians(heading)) * 40
        r0, c0 = lvl.cell(hx, hy)
        for dr in range(-3, 4):
            for dc in range(-3, 4):
                r, c = r0 + dr, c0 + dc
                if dr * dr + dc * dc <= 10 and 0 <= r < lvl.H and 0 <= c < lvl.W:
                    if (r, c) == lvl.cell(st["x"], st["y"]):
                        continue
                    i = r * lvl.W + c
                    n = self.bancount[i] = self.bancount.get(i, 0) + 1
                    lvl.banned[i] = self.tic + 35 * 20 * 2 ** min(n - 1, 4)
        if self.goal and self.goal[0] in ("item", "hunt"):
            self.ignore[self.goal[2]] = self.tic + 35 * 15
            if self.goal[0] == "item":
                self.items.pop(self.goal[2], None)
        self.goal = self.path = None
        self.next_field = None

    def threats(self, st, tic):
        out = {m["id"]: (m["x"], m["y"]) for m in st["monsters"]}
        for k, (t, m) in self.monsters.items():
            if tic - t < 70 and k not in self.dead and k not in out and self.ignore.get(k, -1) <= tic:
                out[k] = (m["x"], m["y"])
        return list(out.values())[:6]

    def find_cover(self, st, threats):
        """The nearest reachable cell no threat can see (walls and shut doors in the way), not next to a threat."""
        lvl = self.lvl
        if self.field is None:
            return None
        d = self.dist_np.ravel()
        cand = np.flatnonzero((d <= 70) & lvl.free.ravel())
        if not len(cand):
            return None
        if len(cand) > 400:
            cand = cand[np.random.default_rng(self.tic).choice(len(cand), 400, replace=False)]
        r, c = np.divmod(cand, lvl.W)
        px = lvl.x0 + (c + 0.5) * CELL
        py = lvl.y0 + (r + 0.5) * CELL
        hidden = np.ones(len(cand), bool)
        near = np.zeros(len(cand))
        t = np.linspace(0.04, 0.96, 28)[None, :]
        for tx, ty in threats:
            rr, cc = lvl.cell_arr(tx + (px[:, None] - tx) * t, ty + (py[:, None] - ty) * t)
            hidden &= lvl.sight[rr, cc].any(1)
            near += np.hypot(px - tx, py - ty) < 260
        if not hidden.any():
            return None
        score = d[cand] + near * 60 + (~hidden) * 10 ** 6
        flat = int(cand[score.argmin()])
        path = self.field.path(flat)
        return (flat, path, 0, self.tic) if path else None

    def follow(self, st, path, i):
        """-> (heading, index, arrived) along a cell path."""
        lvl, x, y = self.lvl, st["x"], st["y"]
        best, bd = i, 1e18
        for j in range(i, min(len(path), i + 40)):
            px, py = lvl.point(path[j])
            dd = (px - x) ** 2 + (py - y) ** 2
            if dd < bd:
                best, bd = j, dd
        if best >= len(path) - 1 and bd < 20 ** 2:
            return None, best, True
        tx, ty = lvl.point(path[min(len(path) - 1, best + 3)])
        return math.degrees(math.atan2(ty - y, tx - x)), best, False

    def cover_heading(self, st, tic):
        """Under fire and hurt: get out of sight, then wait there a moment."""
        hp = st["health"]
        self.hurt = [(t, d) for t, d in self.hurt if tic - t < 105]
        recent = sum(d for _, d in self.hurt)
        threats = self.threats(st, tic)
        if self.cover is None and threats and (hp < 40 or recent >= 25) and tic >= self.cover_retry:
            self.cover = self.find_cover(st, threats)
            self.cover_retry = tic + 35
            if self.cover:
                self.stats["covers"] = self.stats.get("covers", 0) + 1
        if self.cover is None:
            return None
        flat, path, i, t0 = self.cover
        if i < 0:  # in cover: hold a moment
            if tic > -i or (not threats and hp >= 40) or (self.hurt and self.hurt[-1][0] == tic):
                self.cover = None
            return "hold"
        if tic - t0 > 35 * 5:
            self.cover = None
            return None
        h, i, arrived = self.follow(st, path, i)
        if arrived:
            self.cover = (flat, path, -(tic + (70 if hp >= 40 else 140)), t0)
            return "hold"
        self.cover = (flat, path, i, t0)
        return h

    def fight(self, st, tic, m, a, moved):
        x, y, ang = st["x"], st["y"], st["angle"]
        if m["id"] != self.target:
            self.target, self.dry, self.dry_pos = m["id"], 0, (m["x"], m["y"])
        b = m["bearing"]
        a[6] = max(-30.0, min(30.0, b))
        aligned = abs(b) < 28 and abs(b - a[6]) < max(1.2, math.degrees(math.atan2(14, max(m["distance"], 1))))
        if aligned and st["ammo"] > 0:
            self.fire_toggle = not self.fire_toggle if m["distance"] > 250 else True
            a[0] = int(self.fire_toggle)
        elif abs(b) < 20 and m["distance"] < 90:
            a[0] = 1  # fists
        if a[0]:
            self.fired_at = tic
            if st["dealt"] > self.dealt:
                self.dry, self.dry_pos = 0, (m["x"], m["y"])
            else:
                self.dry += 1
            if self.dry > 45:  # firing and not hurting it: out of reach, for now
                self.ignore[m["id"]] = tic + 35 * 8
                self.stats["unreachable"] += 1
                self.dry, self.target = 0, None
        self.dealt = st["dealt"]
        # movement: strafe, keep a distance, back off from melee monsters
        self.strafe_t -= 1
        to_m = ang + b
        side = to_m + 90 * self.strafe
        if self.strafe_t <= 0 or not self.clear_dir(x, y, side):
            self.strafe = -self.strafe if self.strafe_t > 0 or self.rng.random() < 0.6 else self.strafe
            self.strafe_t = self.rng.randint(14, 35)
            side = to_m + 90 * self.strafe
        d = m["distance"]
        if m["name"] in MELEE or (st["ammo"] == 0 and d > 60):
            want = 320 if st["ammo"] > 0 else 0
        elif st["health"] < 35:
            want = 700
        else:
            want = 350 if "Pistol" in self.weapons and len(self.weapons) == 1 else 220
        mx = math.cos(math.radians(side))
        my = math.sin(math.radians(side))
        if d < want - 60 and self.clear_dir(x, y, to_m + 180):
            mx -= math.cos(math.radians(to_m)) * 1.2
            my -= math.sin(math.radians(to_m)) * 1.2
        elif (d > want + 250 or (st["ammo"] == 0 and d > 50)) and self.clear_dir(x, y, to_m, min(56.0, d - 30)):
            mx += math.cos(math.radians(to_m)) * 1.2
            my += math.sin(math.radians(to_m)) * 1.2
        self.move_buttons(a, math.degrees(math.atan2(my, mx)), ang)
        return a
