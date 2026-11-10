import math
import unittest
import loose_goods as G

class LooseGoodsChecks(unittest.TestCase):
    def test_routes_reach_every_floor_on_belt_height(self):
        for f in G.floors():
            pts = G.route(f, -8.5, 0)
            self.assertAlmostEqual(pts[0][2], G.zc(1))
            self.assertAlmostEqual(pts[-1][2], G.zc(f))           # ends on the destination floor's belt
            self.assertLess(pts[-1][1], G.BRANCHES[0][3])          # inside the sorting branch
            for a, b in zip(pts, pts[1:]):                         # never jumps vertically between points
                self.assertLess(abs(b[2] - a[2]), 2.5)

    def test_spiral_leg_rises_one_floor_per_spiral(self):
        pts = G.route(4, -8.5, 0)
        zs = [p[2] for p in pts]
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(zs, zs[1:]) if a > G.zc(1) + 2.6))   # monotone once past the incline
        self.assertAlmostEqual(max(zs), G.zc(4))

    def test_branch_must_be_west_of_loop_end(self):
        for f in G.floors():
            for b in range(len(G.BRANCHES)):
                G.route(f, -8.5, b)   # every branch is upstream of every floor's loop end

    def test_stack_unpacks_top_layer_first(self):
        pos = G.stack_positions((-10.7, -2.2, 0), 18)
        self.assertEqual(len(pos), 18)
        self.assertGreater(pos[0][2], pos[-1][2])
        self.assertEqual(len({(round(p[0], 3), round(p[1], 3), round(p[2], 3)) for p in pos}), 18)

    def test_operator_reaches_pallet_and_carries_at_chest_height(self):
        self.assertEqual(len(G.OPERATORS), 2)
        spots = [(-10.7, -2.2, 0), (-4.7, -2.2, 0)]
        for op, spot in zip(G.OPERATORS, spots):
            px, py, pyaw = G.pallet_side(op, spot)
            self.assertGreaterEqual(abs(px - spot[0]) - .6, .4)              # stands clear of the pallet edge
            for z in (.28, .53):                                              # bottom and top layer
                bend, arm = G.reach_for(z)
                hand = G.held_carton(px, py, pyaw, bend, arm)
                self.assertLess(abs(hand[0] - spot[0]), 1.0)                  # hands over the pallet
                self.assertLess(hand[2], 1.0)                                 # bent down towards the stack
            carry = G.held_carton(*G.station(op)[:3], .1, 1.0)
            self.assertTrue(1.0 < carry[2] < 1.3)                             # carried at chest height
            parts = G.operator_pose(0, *G.station(op))
            self.assertEqual(len(parts), 4)
            self.assertTrue(all(len(p) == 6 for p in parts.values()))

    def test_along_is_continuous(self):
        pts = G.route(2, -7.0, 3); total = G.length(pts)
        prev = G.along(pts, 0)
        for k in range(1, 400):
            cur = G.along(pts, total * k / 399)
            self.assertLess(math.dist(prev[:3], cur[:3]), total / 399 + 1e-6)
            prev = cur

if __name__ == '__main__':
    unittest.main()
