import math, random
from collections import deque

C = 32.0


def _log(*a):
    try:
        log(*a)
    except Exception:
        pass


def _norm(a):
    while a > 180:
        a -= 360
    while a < -180:
        a += 360
    return a


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _ccw(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _seg_inter(p, q, l):
    x1, y1, x2, y2 = l
    d1 = _ccw(x1, y1, x2, y2, p[0], p[1])
    d2 = _ccw(x1, y1, x2, y2, q[0], q[1])
    d3 = _ccw(p[0], p[1], q[0], q[1], x1, y1)
    d4 = _ccw(p[0], p[1], q[0], q[1], x2, y2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _pt_seg_dist(px, py, l):
    x1, y1, x2, y2 = l
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    if L <= 0:
        return math.hypot(px - x1, py - y1)
    t = _clamp(((px - x1) * dx + (py - y1) * dy) / L, 0.0, 1.0)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


class Agent:
    def __init__(self):
        self.layout = None
        self.last_tic = -1

    def setup(self, layout):
        self.layout = layout
        lines = set()
        for s in layout:
            for ln in s.lines:
                if ln.is_blocking:
                    lines.add((float(ln.x1), float(ln.y1), float(ln.x2), float(ln.y2)))
        self.lines = list(lines)
        xs = [l[0] for l in self.lines] + [l[2] for l in self.lines] or [0]
        ys = [l[1] for l in self.lines] + [l[3] for l in self.lines] or [0]
        self.minc = (int(min(xs) // C) - 1, int(min(ys) // C) - 1)
        self.maxc = (int(max(xs) // C) + 1, int(max(ys) // C) + 1)
        self.buckets = {}
        for l in self.lines:
            L = math.hypot(l[2] - l[0], l[3] - l[1])
            n = int(L / 6) + 1
            for i in range(n + 1):
                t = i / n
                c = (int((l[0] + t * (l[2] - l[0])) // C), int((l[1] + t * (l[3] - l[1])) // C))
                self.buckets.setdefault(c, []).append(l)
        self.cache = {}
        self.visited = set()
        self.bad = set()
        self.path = []
        self.target = None
        self.plan_tic = -999
        self.hist = deque(maxlen=36)
        self.stuck_n = 0
        self.unstick_until = -1
        self.strafe_dir = 0
        self.ignore = {}
        self.aim_id = None
        self.aim_start = 0
        self.aim_dealt = 0
        self.item_ignore = {}
        self.item_id = None
        self.logs = 0
        self.start_cell = None
        self.prev_damage = None
        self.look_until = -1
        self.look_abs = None
        self.last_seen_abs = None
        self.last_seen_tic = -9999
        self.look_logs = 0
        self.mem = {}
        self.look_found = True
        self.look_end = -9999
        self.trail = deque(maxlen=900)
        self.haz = set()
        self.last_hit = None
        self.streak_start = -9999
        self.last_haz_tic = -9999
        self.esc_route = []
        self.esc_until = -1
        self.esc_chk = None
        self.esc_stuck = 0
        self.esc_logs = 0
        self.cover_tic = -9999
        self.hold_pending = False
        self.hold_until = -1
        self.hs_seen_tic = -9999
        self.cover_logs = 0
        # sectors
        self.sec_lines = []
        self.sec_floor = []
        self.sec_ceil = []
        self.abuckets = {}
        for i, s in enumerate(layout):
            ls = [(float(ln.x1), float(ln.y1), float(ln.x2), float(ln.y2)) for ln in s.lines]
            self.sec_lines.append(ls)
            self.sec_floor.append(float(s.floor_height))
            self.sec_ceil.append(float(s.ceiling_height))
            for l in ls:
                L = math.hypot(l[2] - l[0], l[3] - l[1])
                n = int(L / 16) + 1
                for k in range(n + 1):
                    t = k / n
                    c = (int((l[0] + t * (l[2] - l[0])) // C), int((l[1] + t * (l[3] - l[1])) // C))
                    self.abuckets.setdefault(c, set()).add(i)
        self.sec_cache = {}
        self.hz_cnt = {}
        self.safe_sec = set()
        self.haz_phase = None
        self.esc_fail = set()
        self.esc_goal = None
        _log("setup lines=%d bounds=%s %s" % (len(self.lines), self.minc, self.maxc))

    def _inside(self, s, x, y):
        ins = False
        for (x1, y1, x2, y2) in self.sec_lines[s]:
            if (y1 > y) != (y2 > y):
                xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if xi > x:
                    ins = not ins
        return ins

    def sec_at(self, x, y):
        c = self.cell(x, y)
        tested = set()
        for r in range(0, 13):
            new = set()
            for i in range(-r, r + 1):
                for j in range(-r, r + 1):
                    if max(abs(i), abs(j)) != r:
                        continue
                    b = self.abuckets.get((c[0] + i, c[1] + j))
                    if b:
                        new |= b
            new -= tested
            for s in new:
                if self._inside(s, x, y):
                    return s
            tested |= new
        return None

    def sec_cell(self, c):
        r = self.sec_cache.get(c, -2)
        if r == -2:
            p = self.center(c)
            r = self.sec_at(p[0], p[1])
            self.sec_cache[c] = r
        return r

    def bfs_escape(self, tic, x, y):
        cur = self.cell(x, y)
        cs = self.sec_at(x, y)
        cf = self.sec_floor[cs] if cs is not None else None
        par = {cur: None}
        dist = {cur: 0}
        dq = deque([cur])
        n = 0
        b_safe = b_up = b_any = None
        first_d = None
        while dq and n < 1000:
            c = dq.popleft()
            n += 1
            s = self.sec_cell(c)
            if first_d is not None and dist[c] > first_d + 8:
                break
            if c != cur and s is not None and s != cs and s not in self.hz_cnt \
                    and c not in self.haz and c not in self.esc_fail:
                if first_d is None:
                    first_d = dist[c]
                if s in self.safe_sec and b_safe is None:
                    b_safe = c
                if cf is not None and self.sec_floor[s] > cf and b_up is None:
                    b_up = c
                if b_any is None:
                    b_any = c
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (c[0] + d[0], c[1] + d[1])
                if nb in par or not self.passable(c, nb):
                    continue
                sb = self.sec_cell(nb)
                if sb is not None:
                    if self.sec_ceil[sb] - self.sec_floor[sb] < 56:
                        continue
                    if s is not None and self.sec_floor[sb] - self.sec_floor[s] > 24:
                        continue
                par[nb] = c
                dist[nb] = dist[c] + 1
                dq.append(nb)
        goal = b_safe or b_up or b_any
        if goal is None:
            self.esc_route = []
            self.esc_until = -1
            return
        path = []
        c = goal
        while c is not None and c != cur:
            path.append(self.center(c))
            c = par[c]
        path.reverse()
        self.esc_route = path
        self.esc_goal = goal
        self.esc_until = tic + 5 * len(path) + 70
        self.esc_chk = None
        self.esc_stuck = 0
        if self.esc_logs < 6:
            self.esc_logs += 1
            _log("bfs escape t=%d sec %s -> %s len %d n=%d" % (tic, cs, self.sec_cell(goal), len(path), n))

    def los_blocked(self, ax, ay, bx, by):
        L = math.hypot(bx - ax, by - ay)
        n = int(L / 16) + 1
        cells = set()
        for i in range(n + 1):
            t = i / n
            cells.add(self.cell(ax + t * (bx - ax), ay + t * (by - ay)))
        seen = set()
        p, q = (ax, ay), (bx, by)
        for c in cells:
            for l in self.buckets.get(c, ()):
                if l in seen:
                    continue
                seen.add(l)
                if _seg_inter(p, q, l):
                    return True
        return False

    def find_cover(self, tic, x, y, mx, my):
        route = []
        k = 0
        for (t, px, py) in reversed(self.trail):
            if tic - t < 4:
                continue
            k += 1
            if k > 100:
                break
            route.append((px, py))
            if k % 2 == 0 and math.hypot(px - x, py - y) > 40 \
                    and self.cell(px, py) not in self.haz:
                if self.los_blocked(px, py, mx, my):
                    return route
        return None

    def cell(self, x, y):
        return (int(x // C), int(y // C))

    def center(self, c):
        return ((c[0] + 0.5) * C, (c[1] + 0.5) * C)

    def passable(self, a, b):
        k = (a, b)
        r = self.cache.get(k)
        if r is not None:
            return r
        r = True
        if not (self.minc[0] <= b[0] <= self.maxc[0] and self.minc[1] <= b[1] <= self.maxc[1]):
            r = False
        else:
            p, q = self.center(a), self.center(b)
            ls = self.buckets.get(a, []) + self.buckets.get(b, [])
            for l in ls:
                if _seg_inter(p, q, l):
                    r = False
                    break
            if r and b != self.start_cell:
                for l in self.buckets.get(b, []):
                    if _pt_seg_dist(q[0], q[1], l) < 12:
                        r = False
                        break
        self.cache[k] = r
        return r

    def plan(self, start):
        par = {start: None}
        dq = deque([start])
        n = 0
        goal = None
        while dq and n < 5000:
            c = dq.popleft()
            n += 1
            if c not in self.visited and c not in self.bad:
                if self.hz_cnt:
                    sc = self.sec_cell(c)
                    if sc is not None and self.hz_cnt.get(sc, 0) >= 2:
                        self.bad.add(c)
                        continue
                goal = c
                break
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (c[0] + d[0], c[1] + d[1])
                if nb in par:
                    continue
                if self.passable(c, nb):
                    par[nb] = c
                    dq.append(nb)
        if goal is None:
            if n < 5000:
                # whole reachable area explored: start over
                self.visited = set()
                self.bad = set()
            return None, []
        path = []
        c = goal
        while c is not None and c != start:
            path.append(c)
            c = par[c]
        path.reverse()
        return goal, path

    def steer(self, state, tx, ty):
        dx, dy = tx - state['x'], ty - state['y']
        des = math.degrees(math.atan2(dy, dx))
        diff = _norm(des - state['angle'])
        turn = _clamp(diff, -30, 30)
        fwd = 1 if abs(diff) < 50 else 0
        return turn, fwd

    def start_escape(self, tic, x, y):
        route = []
        found = False
        for (t, px, py) in reversed(self.trail):
            if tic - t < 4:
                continue
            route.append((px, py))
            if t < self.streak_start - 36 and self.cell(px, py) not in self.haz:
                found = True
                break
        if found and route:
            s = self.sec_at(route[-1][0], route[-1][1])
            if s is not None and s in self.hz_cnt:
                found = False
        if not found or not route:
            self.bfs_escape(tic, x, y)
            return
        self.esc_goal = None
        self.esc_route = route
        self.esc_until = tic + 4 * len(route) + 70
        self.esc_chk = None
        self.esc_stuck = 0
        if self.esc_logs < 4:
            self.esc_logs += 1
            _log("hazard escape t=%d route %d to %s" % (tic, len(route), route[-1]))

    def act(self, state, tic):
        layout = state.get('layout')
        if layout is not self.layout or tic < self.last_tic:
            self.setup(layout)
        self.last_tic = tic
        if state.get('dead'):
            return [0, 0, 0, 0, 0, 0, 0.0, 0]
        x, y = state['x'], state['y']
        cur = self.cell(x, y)
        if self.start_cell is None:
            self.start_cell = cur
        for i in range(-2, 3):
            for j in range(-2, 3):
                if abs(i) + abs(j) <= 3:
                    self.visited.add((cur[0] + i, cur[1] + j))
        self.hist.append((x, y))

        attack = use = left = right = back = fwd = 0
        turn = 0.0

        allm = state.get('monsters', [])
        dmg = state.get('damage', 0)
        if self.prev_damage is None:
            self.prev_damage = dmg
        hit = dmg - self.prev_damage
        self.prev_damage = dmg
        if tic % 4 == 0:
            self.trail.append((tic, x, y))
        HS = ('Zombieman', 'ShotgunGuy', 'ChaingunGuy', 'WolfensteinSS', 'SpiderMastermind')
        for mm in allm:
            if mm['name'] in HS:
                self.hs_seen_tic = tic
        haz = False
        if hit > 0:
            near = min([mm['distance'] for mm in allm] + [9999])
            pv = self.last_hit
            if pv is not None and pv[1] == hit and abs(tic - pv[0] - 32) <= 1 and hit <= 20:
                haz = True
            elif hit in (4, 5, 10, 20) and near > 300 and tic - self.hs_seen_tic > 70:
                haz = True
            self.last_hit = (tic, hit)
        elif self.haz_phase is not None and tic % 32 == self.haz_phase and tic > 0:
            s = self.sec_at(x, y)
            if s is not None:
                self.safe_sec.add(s)
                self.hz_cnt.pop(s, None)
        # take cover from far hitscanners when hurt
        if hit > 0 and not haz and state.get('health', 100) < 50 and tic - self.cover_tic > 175:
            near = min([mm['distance'] for mm in allm] + [9999])
            src = None
            if near > 300:
                for mm in allm:
                    if mm['name'] in HS and mm['distance'] > 450:
                        src = (mm['x'], mm['y'])
                        break
                if src is None and not allm:
                    bt = None
                    for (mx, my, mt, nm) in self.mem.values():
                        if nm in HS and tic - mt < 70:
                            d = math.hypot(mx - x, my - y)
                            if d > 450 and (bt is None or mt > bt):
                                bt = mt
                                src = (mx, my)
            if src is not None:
                self.cover_tic = tic
                route = self.find_cover(tic, x, y, src[0], src[1])
                if route:
                    self.esc_route = route
                    self.esc_until = tic + 4 * len(route) + 70
                    self.esc_chk = None
                    self.esc_stuck = 0
                    self.hold_pending = True
                    self.path = []
                    if self.cover_logs < 4:
                        self.cover_logs += 1
                        _log("cover t=%d hp=%d from %s route %d" % (tic, state.get('health', 0), src, len(route)))
        if haz:
            if tic - self.last_haz_tic > 70:
                self.streak_start = tic
            self.last_haz_tic = tic
            self.haz_phase = tic % 32
            s = self.sec_at(x, y)
            if s is not None:
                self.hz_cnt[s] = self.hz_cnt.get(s, 0) + 1
                self.safe_sec.discard(s)
            for i in range(-1, 2):
                for j in range(-1, 2):
                    c = (cur[0] + i, cur[1] + j)
                    self.haz.add(c)
                    self.bad.add(c)
            if not self.esc_route or tic >= self.esc_until:
                self.hold_pending = False
                self.start_escape(tic, x, y)
            self.path = []
        for mm in allm:
            self.mem[mm['id']] = (mm['x'], mm['y'], tic, mm['name'])
            if hit > 0:
                self.ignore.pop(mm['id'], None)
        if allm:
            m0 = allm[0]
            self.last_seen_abs = state['angle'] + m0['bearing']
            self.last_seen_tic = tic
        elif hit > 0 and not haz and hit not in (5, 10, 20) and tic >= self.look_until:
            failed = (not self.look_found) and tic - self.look_end < 105
            self.look_until = tic + 20
            self.look_end = tic + 20
            self.look_found = False
            self.look_abs = None
            if not failed:
                hs = ('Zombieman', 'ShotgunGuy', 'ChaingunGuy', 'WolfensteinSS', 'SpiderMastermind')
                best = None
                for (mx, my, mt, nm) in self.mem.values():
                    if tic - mt > 175:
                        continue
                    d = math.hypot(mx - x, my - y)
                    if hit % 3 == 0 and nm in hs:
                        d *= 0.5
                    if best is None or d < best[0]:
                        best = (d, mx, my)
                if best is not None and best[0] > 1:
                    self.look_abs = math.degrees(math.atan2(best[2] - y, best[1] - x))
                elif tic - self.last_seen_tic < 105 and self.last_seen_abs is not None:
                    self.look_abs = self.last_seen_abs
            if self.look_logs < 4:
                self.look_logs += 1
                _log("unseen hit %d at t=%d, look %s failed=%s" % (hit, tic, self.look_abs, failed))
        ammo = state.get('ammo', 0)
        # hazard escape along own trail
        if self.esc_route and tic < self.esc_until:
            while self.esc_route:
                px, py = self.esc_route[0]
                if math.hypot(px - x, py - y) < 24:
                    self.esc_route.pop(0)
                else:
                    break
            if self.esc_route:
                px, py = self.esc_route[0]
                dx, dy = px - x, py - y
                diff = _norm(math.degrees(math.atan2(dy, dx)) - state['angle'])
                t = _clamp(diff, -30, 30)
                f = 1 if abs(diff) < 70 else 0
                a = 0
                u = 0
                for mm in allm:
                    tol = max(2.0, math.degrees(math.atan2(16, max(mm['distance'], 1))))
                    if ammo > 0 and abs(mm['bearing']) < tol + 1:
                        a = 1
                        break
                if self.esc_chk is None or tic - self.esc_chk[0] >= 35:
                    if self.esc_chk is not None and math.hypot(x - self.esc_chk[1], y - self.esc_chk[2]) < 12:
                        self.esc_stuck += 1
                        if self.esc_stuck == 1:
                            u = 1
                        else:
                            if self.esc_goal is not None:
                                self.esc_fail.add(self.esc_goal)
                            self.esc_route = []
                            self.esc_until = -1
                            self.hold_pending = False
                    self.esc_chk = (tic, x, y)
                self.hist.clear()
                return [a, 1, f, 0, 0, 0, float(t), u]
            self.esc_until = -1
            if self.hold_pending:
                self.hold_pending = False
                self.hold_until = tic + 105
        mons = [m for m in allm if self.ignore.get(m['id'], -1) < tic
                and (ammo > 0 or m['distance'] < 90)]
        if mons:
            if self.look_until > tic:
                self.look_found = True
            self.look_until = -1
            m = mons[0]
            if m['id'] != self.aim_id:
                self.aim_id = m['id']
                self.aim_start = tic
                self.aim_dealt = state.get('dealt', 0)
            elif tic - self.aim_start > 140:
                if state.get('dealt', 0) <= self.aim_dealt:
                    self.ignore[m['id']] = tic + 350
                    _log("ignore monster %s %s" % (m['name'], m['id']))
                self.aim_start = tic
                self.aim_dealt = state.get('dealt', 0)
            b = m['bearing']
            turn = _clamp(b, -30, 30)
            tol = max(2.0, math.degrees(math.atan2(16, max(m['distance'], 1))))
            if abs(b) < tol + 2:
                attack = 1
            if (tic // 25) % 2:
                left = 1
            else:
                right = 1
            if ammo == 0 and m['distance'] > 60:
                fwd = 1
            if m['name'] in ('DoomImp', 'Demon', 'Spectre', 'LostSoul', 'Revenant') \
                    and m['distance'] < 200 and ammo > 0:
                back = 1
                fwd = 0
            self.hist.clear()
            return [attack, 1, fwd, back, left, right, float(turn), use]

        if tic < self.hold_until:
            self.hist.clear()
            return [0, 0, 0, 0, 0, 0, 0.0, 0]

        if tic < self.look_until:
            if self.look_abs is not None:
                d = _norm(self.look_abs - state['angle'])
                t = _clamp(d, -30, 30) if abs(d) > 5 else 30.0
            else:
                t = 26.0
            if (tic // 25) % 2:
                left = 1
            else:
                right = 1
            self.hist.clear()
            return [0, 1, 0, 0, left, right, float(t), 0]

        if tic < self.unstick_until:
            if self.strafe_dir > 0:
                left = 1
            else:
                right = 1
            back = 1 if (self.unstick_until - tic) > 8 else 0
            return [0, 1, 0, back, left, right, 0.0, 0]

        # items
        goal_xy = None
        items = [it for it in state.get('items', [])
                 if self.item_ignore.get(it['id'], -1) < tic and it['distance'] < 400]
        if ammo == 0:
            amm = [it for it in state.get('items', [])
                   if self.item_ignore.get(it['id'], -1) < tic and it['distance'] < 1200
                   and it.get('kind') in ('ammo', 'weapon')]
            if amm:
                items = amm
        if items:
            it = items[0]
            if it['id'] != self.item_id:
                self.item_id = it['id']
                self.item_start = tic
            elif tic - self.item_start > 140:
                self.item_ignore[it['id']] = tic + 3500
            goal_xy = (it['x'], it['y'])
        else:
            if (not self.path) or tic - self.plan_tic > 20:
                self.target, self.path = self.plan(cur)
                self.plan_tic = tic
                if self.logs < 6 and self.target is not None and tic % 700 < 21:
                    self.logs += 1
                    _log("t=%d at %s target %s len %d" % (tic, cur, self.target, len(self.path)))
            while self.path:
                cx, cy = self.center(self.path[0])
                if math.hypot(cx - x, cy - y) < 20:
                    self.path.pop(0)
                else:
                    break
            if self.path:
                k = self.path[min(1, len(self.path) - 1)]
                # look ahead if straight line is clear of blocking line
                wp = self.path[0]
                if len(self.path) > 1:
                    p = (x, y)
                    q = self.center(k)
                    ok = True
                    for c in (cur, wp, k):
                        for l in self.buckets.get(c, []):
                            if _seg_inter(p, q, l):
                                ok = False
                                break
                        if not ok:
                            break
                    if ok:
                        wp = k
                goal_xy = self.center(wp)
            else:
                # nothing to go to: wander forward
                turn = 3.0
                fwd = 1

        if goal_xy is not None:
            turn, fwd = self.steer(state, goal_xy[0], goal_xy[1])

        # stuck detection
        if fwd and len(self.hist) == self.hist.maxlen:
            ox, oy = self.hist[0]
            if math.hypot(x - ox, y - oy) < 12:
                self.stuck_n += 1
                self.hist.clear()
                if self.stuck_n % 2 == 1:
                    use = 1
                    if self.logs < 12:
                        self.logs += 1
                        _log("stuck at %d,%d: use" % (x, y))
                else:
                    if self.path:
                        self.cache[(cur, self.path[0])] = False
                        self.cache[(self.path[0], cur)] = False
                    if self.target is not None:
                        self.bad.add(self.target)
                    if items:
                        self.item_ignore[items[0]['id']] = tic + 3500
                    self.path = []
                    self.strafe_dir = random.choice((-1, 1))
                    self.unstick_until = tic + 15
        if fwd and len(self.hist) > 4:
            ox, oy = self.hist[-5]
            if math.hypot(x - ox, y - oy) > 20:
                self.stuck_n = 0
        return [attack, 1, fwd, back, left, right, float(turn), use]
