import asyncio
import math
import unittest

import agv_network as AN


class AgvNetworkChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.net = AN.Network(AN.segments())
        self.traffic = AN.Traffic(self.net)

    def test_paired_vertical_trunks_prefer_right_hand_flow(self):
        north = self.net.route(self.net.access(-10.8, 17.959), self.net.access(-10.8, 39.021))
        south = self.net.route(self.net.access(-10.8, 39.021), self.net.access(-10.8, 17.959))
        north_xs = {round(self.net.pos[k][0], 3) for k in north if 19.0 < self.net.pos[k][1] < 38.0}
        south_xs = {round(self.net.pos[k][0], 3) for k in south if 19.0 < self.net.pos[k][1] < 38.0}
        self.assertEqual(north_xs, {-10.067})
        self.assertEqual(south_xs, {-11.585})

    async def test_acquire_timed_allows_disjoint_windows_and_delays_conflicts(self):
        now = 0.0

        def clock():
            return now

        async def wait_until(t):
            nonlocal now
            if t > now:
                now = t

        n_a = self.net.access(-20.0, 17.959)
        n_b = self.net.access(-26.0, 44.191)

        # Two AGVs crossing shared corridor n_a sequentially vs disjoint n_b simultaneously
        t1 = await self.traffic.acquire_timed("agv_1", [(n_a, 0.0, 3.0), (n_b, 8.0, 10.0)], clock, wait_until)
        self.assertEqual(t1, 0.0)

        # Conflicting request on n_a skips past t=3.0 (wait>0), whereas disjoint n_b at t=0.0..2.0 does not wait
        t_disjoint = await self.traffic.acquire_timed("agv_2", [(n_b, 0.0, 2.0)], clock, wait_until)
        self.assertEqual(t_disjoint, 0.0)

        t_conflict = await self.traffic.acquire_timed("agv_3", [(n_a, 0.0, 2.0)], clock, wait_until)
        self.assertGreaterEqual(t_conflict, 3.0)

        self.traffic.release_timed("agv_1")
        self.traffic.release_timed("agv_2")
        self.traffic.release_timed("agv_3")
        self.assertFalse(self.traffic.timed)

    async def test_concurrent_timed_trips_never_violate_clear_radius(self):
        # Simulate two AGVs sharing the east-west corridor y=17.959 at overlapping start times
        a = self.net.access(-15.0, 17.959)
        b = self.net.access(-30.0, 17.959)
        path1 = self.net.route(a, b)
        path2 = self.net.route(b, a)
        speed = 1.5
        t_step = 0.5 / speed

        def build_windows(path):
            return [(k, max(0.0, i * t_step - 1.2), i * t_step + 1.4) for i, k in enumerate(path)]

        # Book AGV 1 at t=0, then schedule AGV 2
        clock_t = [0.0]
        await self.traffic.acquire_timed("u1_a", build_windows(path1), lambda: clock_t[0], self._noop_wait)
        t2_dep = None
        async def record_wait(t):
            nonlocal t2_dep
            t2_dep = t
        await self.traffic.acquire_timed("u1_b", build_windows(path2), lambda: clock_t[0], record_wait)
        dep2 = 0.0 if t2_dep is None else t2_dep

        def pos_at(path, t0, t):
            u = (t - t0) / t_step
            if u < 0 or u > len(path) - 1:
                return None
            idx = min(len(path) - 2, int(u))
            f = u - idx
            p0, p1 = self.net.pos[path[idx]], self.net.pos[path[idx + 1]]
            return (p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f)

        max_t = dep2 + len(path2) * t_step
        t = 0.0
        while t <= max_t:
            p1, p2 = pos_at(path1, 0.0, t), pos_at(path2, dep2, t)
            if p1 is not None and p2 is not None:
                self.assertGreaterEqual(math.dist(p1, p2), AN.CLEAR - 0.1)
            t += 0.1

    @staticmethod
    async def _noop_wait(t):
        return None


if __name__ == "__main__":
    unittest.main()

