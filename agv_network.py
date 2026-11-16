"""AGV path network of the 4-floor building (CAD agv_paths) with per-floor traffic control.

* Graph: every CAD AGV path segment, split at junctions and every STEP metres. The only addition to the
  CAD lines is K06_LINK, the K06 corridor y=13.795 continued east into Khu 06 (its slots lie beyond the
  drawn line end x=-10.067).
* Off-graph targets (rack cells, station racks, trolley marks, Khu 06 slots) are reached by a short
  perpendicular hop from the nearest path point ("access node").
* Traffic: an AGV reserves every node of its route atomically before moving (requests are served first in
  first out among conflicting requests) and releases nodes progressively once it is CLEAR metres past them.
  Nodes closer than CLEAR conflict, so the paired CAD lanes 1.1 m apart are never used side by side by two
  loaded AGVs. Holding nothing while waiting makes the scheme deadlock-free.
* Spots: rack cells and Khu 06 slots are claimed before an AGV drops a load there, so a load is only ever
  placed into an empty position.
"""
import asyncio
import heapq
import math

import four_floor_scene as FF

STEP = 0.5          # node spacing along a path (m)
CLEAR = 1.35        # minimum centre distance between two AGVs (loaded footprint ~1.2 m)
TURN_COST = 3.0     # metres-equivalent penalty per 90 degree turn when planning
K06_LINK = (-10.067, 13.795, -2.0, 13.795)

def _key(x, y): return (round(x, 3), round(y, 3))

class Network:
    def __init__(self, segments, targets=()):
        self.segments = [tuple(map(float, s)) for s in segments]
        cuts = {i: {0.0, 1.0} for i in range(len(self.segments))}
        # Junctions: any segment endpoint lying on another segment, and H/V crossings.
        for i, a in enumerate(self.segments):
            for j, b in enumerate(self.segments):
                if i == j: continue
                for px, py in ((b[0], b[1]), (b[2], b[3])):
                    t = self._param(a, px, py)
                    if t is not None: cuts[i].add(t)
                p = self._cross(a, b)
                if p is not None: cuts[i].add(self._param(a, *p))
        self.access_of = {}
        for tx, ty in targets:
            i, t, _ = self._project(tx, ty)
            cuts[i].add(t)
        self.pos = {}; self.adj = {}
        for i, s in enumerate(self.segments):
            length = math.dist(s[:2], s[2:])
            ts = sorted(cuts[i])
            dense = []
            for a, b in zip(ts, ts[1:]):
                n = max(1, math.ceil((b - a) * length / STEP))
                dense.extend(a + (b - a) * k / n for k in range(n))
            dense.append(ts[-1])
            keys = []
            for t in dense:
                x, y = s[0] + (s[2] - s[0]) * t, s[1] + (s[3] - s[1]) * t
                k = _key(x, y); self.pos[k] = (x, y); self.adj.setdefault(k, set()); keys.append(k)
            for a, b in zip(keys, keys[1:]):
                if a != b: self.adj[a].add(b); self.adj[b].add(a)
        for tx, ty in targets:
            self.access_of[_key(tx, ty)] = self.access(tx, ty)
        # Conflict sets via a coarse grid hash.
        grid = {}
        for k, (x, y) in self.pos.items():
            grid.setdefault((int(x // CLEAR), int(y // CLEAR)), []).append(k)
        self.conflict = {}
        for k, (x, y) in self.pos.items():
            gx, gy = int(x // CLEAR), int(y // CLEAR)
            self.conflict[k] = {m for dx in (-1, 0, 1) for dy in (-1, 0, 1) for m in grid.get((gx + dx, gy + dy), ())
                                if math.dist(self.pos[m], (x, y)) < CLEAR}
        self._cache = {}

    @staticmethod
    def _param(s, px, py, tol=2e-3):
        (x0, y0, x1, y1) = s; dx, dy = x1 - x0, y1 - y0; l2 = dx * dx + dy * dy
        t = ((px - x0) * dx + (py - y0) * dy) / l2
        if -1e-9 <= t <= 1 + 1e-9 and math.dist((x0 + dx * t, y0 + dy * t), (px, py)) < tol:
            return min(1.0, max(0.0, t))
        return None

    @staticmethod
    def _cross(a, b):
        ah, bh = abs(a[1] - a[3]) < 1e-6, abs(b[1] - b[3]) < 1e-6
        if ah == bh: return None
        h, v = (a, b) if ah else (b, a)
        x, y = v[0], h[1]
        if min(h[0], h[2]) - 1e-6 <= x <= max(h[0], h[2]) + 1e-6 and min(v[1], v[3]) - 1e-6 <= y <= max(v[1], v[3]) + 1e-6:
            return (x, y)
        return None

    def _project(self, px, py):
        """Nearest point on the network, preferring a perpendicular foot inside a segment (straight hop)."""
        best = None
        for i, (x0, y0, x1, y1) in enumerate(self.segments):
            dx, dy = x1 - x0, y1 - y0; l2 = dx * dx + dy * dy
            t_raw = ((px - x0) * dx + (py - y0) * dy) / l2
            t = min(1.0, max(0.0, t_raw))
            d = math.dist((x0 + dx * t, y0 + dy * t), (px, py))
            inside = -0.05 <= t_raw * math.sqrt(l2) <= math.sqrt(l2) + 0.05
            score = d + (0.0 if inside else 5.0)   # diagonal hops only as a last resort
            if best is None or score < best[0]: best = (score, i, t, d)
        return best[1], best[2], best[3]

    def access(self, px, py):
        i, t, _ = self._project(px, py)
        s = self.segments[i]
        x, y = s[0] + (s[2] - s[0]) * t, s[1] + (s[3] - s[1]) * t
        k = _key(x, y)
        if k not in self.pos:   # target not registered at build time: nearest existing node
            k = min(self.pos, key=lambda m: math.dist(self.pos[m], (x, y)))
        return k

    def route(self, a, b):
        """Node keys from a to b, shortest length with a penalty per change of direction."""
        if (a, b) in self._cache: return self._cache[(a, b)]
        def heading(u, v):
            return round(math.atan2(self.pos[v][1] - self.pos[u][1], self.pos[v][0] - self.pos[u][0]), 2)
        start = (a, None); dist = {start: 0.0}; prev = {}; pq = [(0.0, 0, start)]; n = 0; end = None
        while pq:
            d, _, state = heapq.heappop(pq)
            if d > dist.get(state, 1e18): continue
            u, h = state
            if u == b: end = state; break
            for v in self.adj[u]:
                hv = heading(u, v)
                nd = d + math.dist(self.pos[u], self.pos[v]) + (TURN_COST if h is not None and abs(math.remainder(hv - h, 2 * math.pi)) > .05 else 0.)
                ns = (v, hv)
                if nd < dist.get(ns, 1e18):
                    dist[ns] = nd; prev[ns] = state; n += 1; heapq.heappush(pq, (nd, n, ns))
        if end is None: raise RuntimeError(f'No CAD AGV path between {self.pos[a]} and {self.pos[b]}')
        path = [end[0]]; s = end
        while s in prev: s = prev[s]; path.append(s[0])
        path.reverse(); self._cache[(a, b)] = path
        return path

    def plan(self, start_xy, end_xy):
        """(points, node_keys): start, hop to access node, CAD route, hop to the end target.
        node_keys[i] is the network node at points[i] (None for the off-graph start / end)."""
        a, b = self.access(*start_xy), self.access(*end_xy)
        nodes = self.route(a, b)
        def corner(target, node, nxt):
            """Axis-aligned hop between an off-graph target and its access node: if the hop is diagonal,
            first continue along the lane direction, then turn square into the target."""
            (tx, ty), (ax, ay) = target, self.pos[node]
            if abs(tx - ax) < .02 or abs(ty - ay) < .02: return []
            lane_horizontal = nxt is not None and abs(self.pos[nxt][1] - ay) < 1e-3
            return [(tx, ay)] if lane_horizontal else [(ax, ty)]
        nxt_a = nodes[1] if len(nodes) > 1 else None
        nxt_b = nodes[-2] if len(nodes) > 1 else None
        pts = ([tuple(start_xy)] + corner(start_xy, a, nxt_a) + [self.pos[k] for k in nodes]
               + corner(end_xy, b, nxt_b) + [tuple(end_xy)])
        keys = ([None] * (1 + len(corner(start_xy, a, nxt_a))) + list(nodes)
                + [None] * (1 + len(corner(end_xy, b, nxt_b))))
        out_p, out_k = [], []
        for p, k in zip(pts, keys):
            if out_p and math.dist(out_p[-1], p) < 1e-3:
                out_k[-1] = out_k[-1] or k; continue
            out_p.append(p); out_k.append(k)
        return out_p, out_k

class Traffic:
    """Atomic node reservation with progressive release; FIFO among conflicting requests."""
    def __init__(self, net):
        self.net = net; self.held = {}; self.waiting = []; self.cond = None
        self.max_wait = 0.0; self.grants = 0

    def _blocked(self, nodes, owner):
        for n in nodes:
            for m in self.net.conflict[n]:
                o = self.held.get(m)
                if o is not None and o != owner: return True
        return False

    def _grantable(self, ticket):
        idx = self.waiting.index(ticket)
        _, owner, nodes, zone = ticket
        if self._blocked(nodes, owner): return False
        for _, o2, _, zone2 in self.waiting[:idx]:     # an earlier conflicting request goes first
            if o2 != owner and zone & zone2: return False
        return True

    async def acquire(self, owner, nodes):
        if self.cond is None: self.cond = asyncio.Condition()
        nodes = [n for n in dict.fromkeys(nodes) if n is not None]
        zone = set().union(*(self.net.conflict[n] for n in nodes)) if nodes else set()
        ticket = (object(), owner, nodes, zone)
        async with self.cond:
            self.waiting.append(ticket)
            try:
                await self.cond.wait_for(lambda: self._grantable(ticket))
            finally:
                self.waiting.remove(ticket)
            for n in nodes: self.held[n] = owner
            self.grants += 1
            self.cond.notify_all()

    def release(self, owner, nodes):
        changed = False
        for n in nodes:
            if n is not None and self.held.get(n) == owner:
                del self.held[n]; changed = True
        if changed and self.cond is not None:
            asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self.cond: self.cond.notify_all()

class Spots:
    """Exclusive occupancy of drop positions (rack cells, Khu 06 slots) on one floor."""
    def __init__(self):
        self.owner = {}; self.cond = None

    def occupy(self, key, owner):
        if self.owner.get(key) not in (None, owner):
            raise RuntimeError(f'Position {key} already holds {self.owner[key]}')
        self.owner[key] = owner

    async def claim(self, keys, owner):
        """Wait until one of keys (in preference order) is empty, then occupy it and return it."""
        if self.cond is None: self.cond = asyncio.Condition()
        async with self.cond:
            await self.cond.wait_for(lambda: any(self.owner.get(k) is None for k in keys))
            key = next(k for k in keys if self.owner.get(k) is None)
            self.owner[key] = owner
            return key

    def free(self, key):
        self.owner.pop(key, None)
        if self.cond is not None:
            asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self.cond: self.cond.notify_all()

def segments():
    return [tuple(s) for s in FF.L['agv_paths']] + [K06_LINK]
