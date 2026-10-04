"""Navigation for the Doom test bed: the level's geometry (ViZDoom sectors and lines) as a walkable grid, A* paths,
line-of-sight smoothing and a memory of where the player has been. Map units; 16-unit cells."""
import heapq
import math

import numpy as np

CELL = 16.0
STEP = 24.0       # the highest step a player climbs
HEIGHT = 56.0     # the player's height: lower openings block
DOOR = 8.0        # a sector this shut (ceiling near its floor) is a closed door, not a wall


class NavGrid:
    def __init__(self, sectors):
        xs = [p for sc in sectors for ln in sc.lines for p in (ln.x1, ln.x2)]
        ys = [p for sc in sectors for ln in sc.lines for p in (ln.y1, ln.y2)]
        self.x0, self.y0 = min(xs) - CELL, min(ys) - CELL
        self.w = int((max(xs) - self.x0) / CELL) + 2
        self.h = int((max(ys) - self.y0) / CELL) + 2
        self.visits = np.zeros((self.h, self.w), np.float32)
        self.update(sectors)

    def update(self, sectors):
        """(Re)build the walls from the sectors as they are now: doors open, lifts move."""
        owners, hard = {}, set()
        for sc in sectors:
            for ln in sc.lines:
                key = tuple(sorted(((round(ln.x1), round(ln.y1)), (round(ln.x2), round(ln.y2)))))
                owners.setdefault(key, []).append(sc)
                if getattr(ln, "is_blocking", False):
                    hard.add(key)
        blocked = np.zeros((self.h, self.w), bool)
        for (a, b), secs in owners.items():
            if len(secs) >= 2 and (a, b) not in hard:  # a two-sided line blocks if its step or opening is impassable
                f = [s.floor_height for s in secs[:2]]
                c = [s.ceiling_height for s in secs[:2]]
                closed = any(cc - ff <= DOOR for cc, ff in zip(c, f))  # a shut door between floors: worth a try
                if abs(f[0] - f[1]) <= STEP and (min(c) - max(f) >= HEIGHT or closed):
                    continue
            self._raster(blocked, a, b)
        grown = blocked.copy()  # keep a player's radius (16) off the walls
        grown[1:, :] |= blocked[:-1, :]
        grown[:-1, :] |= blocked[1:, :]
        grown[:, 1:] |= blocked[:, :-1]
        grown[:, :-1] |= blocked[:, 1:]
        self.blocked = grown
        self.reach, self.reach_cells = None, None

    def reachable(self, x, y):
        """The cells the player can walk to from (x, y) (a flood fill; cached until the walls change)."""
        s = self.nearest_free(self.cell(x, y))
        if s is None:
            return None
        if self.reach is None or not self.reach[s]:
            free, reach = ~self.blocked, np.zeros_like(self.blocked)
            reach[s] = True
            while True:
                grow = reach.copy()
                grow[1:, :] |= reach[:-1, :]
                grow[:-1, :] |= reach[1:, :]
                grow[:, 1:] |= reach[:, :-1]
                grow[:, :-1] |= reach[:, 1:]
                grow &= free
                if np.array_equal(grow, reach):
                    break
                reach = grow
            self.reach, self.reach_cells = reach, np.argwhere(reach)
        return self.reach

    def sample(self, x, y, rng):
        """A random cell the player can reach from (x, y), or None."""
        if self.reachable(x, y) is None or not len(self.reach_cells):
            return None
        r, c = self.reach_cells[rng.randrange(len(self.reach_cells))]
        return int(r), int(c)

    def cell(self, x, y):
        return (min(self.h - 1, max(0, int((y - self.y0) / CELL))), min(self.w - 1, max(0, int((x - self.x0) / CELL))))

    def point(self, c):
        return self.x0 + (c[1] + 0.5) * CELL, self.y0 + (c[0] + 0.5) * CELL

    def _raster(self, grid, a, b):
        n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / (CELL / 2)) + 1
        for i in range(n + 1):
            t = i / n
            r, c = self.cell(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            grid[r, c] = True

    def free(self, c):
        return 0 <= c[0] < self.h and 0 <= c[1] < self.w and not self.blocked[c]

    def nearest_free(self, c, radius=4):
        if self.free(c):
            return c
        for r in range(1, radius + 1):
            for dr in range(-r, r + 1):
                for dc in range(-r, r + 1):
                    if self.free((c[0] + dr, c[1] + dc)):
                        return (c[0] + dr, c[1] + dc)
        return None

    def sight(self, a, b):
        """No blocked cell on the straight line between two cells."""
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        for i in range(1, n):
            t = i / n
            if self.blocked[round(a[0] + t * (b[0] - a[0])), round(a[1] + t * (b[1] - a[1]))]:
                return False
        return True

    def path(self, start_xy, goal_xy, avoid=None, max_nodes=20000):
        """A* (8-connected) -> smoothed waypoints in map units, or None. avoid: cost per cell (danger)."""
        s, g = self.nearest_free(self.cell(*start_xy)), self.nearest_free(self.cell(*goal_xy))
        if s is None or g is None:
            return None
        h = lambda c: math.hypot(c[0] - g[0], c[1] - g[1])  # noqa: E731
        openq, came, cost, seen = [(h(s), 0.0, s)], {s: None}, {s: 0.0}, 0
        while openq:
            _, d, c = heapq.heappop(openq)
            if c == g:
                break
            if d > cost.get(c, 1e18):
                continue
            seen += 1
            if seen > max_nodes:
                return None
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if not dr and not dc:
                        continue
                    n = (c[0] + dr, c[1] + dc)
                    if not self.free(n) or (dr and dc and not (self.free((c[0] + dr, c[1])) and self.free((c[0], c[1] + dc)))):
                        continue
                    nd = d + (1.4142 if dr and dc else 1.0) + (avoid[n] if avoid is not None else 0.0)
                    if nd < cost.get(n, 1e18):
                        cost[n], came[n] = nd, c
                        heapq.heappush(openq, (nd + h(n), nd, n))
        if g not in came:
            return None
        cells, c = [], g
        while c is not None:
            cells.append(c)
            c = came[c]
        cells.reverse()
        pts, anchor = [cells[0]], cells[0]  # keep only the corners line of sight needs
        for i in range(1, len(cells) - 1):
            if not self.sight(anchor, cells[i + 1]):
                pts.append(cells[i])
                anchor = cells[i]
        pts.append(cells[-1])
        return [self.point(c) for c in pts[1:]], cost[g] * CELL

    def visit(self, x, y, decay=0.999):
        self.visits *= decay
        r, c = self.cell(x, y)
        self.visits[max(0, r - 6):r + 7, max(0, c - 6):c + 7] += 1.0

    def frontier(self, x, y, rng, tries=40):
        """A reachable spot far from where the player has been lately."""
        best, best_score = None, -1e9
        for _ in range(tries):
            rc = self.sample(x, y, rng)  # only somewhere it can actually get to
            if rc is None:
                return None
            r, c = rc
            px, py = self.point((r, c))
            score = math.hypot(px - x, py - y) / 400.0 - float(self.visits[r, c])
            if score > best_score:
                best, best_score = (px, py), score
        return best
