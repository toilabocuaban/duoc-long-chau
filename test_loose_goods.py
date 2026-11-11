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

    def test_u1_station_and_agv_kho04_routes_clear_of_obstacles(self):
        reserved = G.FF.U1_RESERVED_CELL_XY | G.FF.U2_RESERVED_CELL_XY | G.FF.P1_RESERVED_CELL_XY
        static_racks = [c for c in G.L['rack_cells'] if (round(c['x'], 3), round(c['y'], 3)) not in reserved]
        for f in G.floors():
            z0 = G.LEVELS[f - 1]
            for b in range(len(G.BRANCHES)):
                wx, wy, wyaw, wz = G.u1_station(f, b)
                rx, ry, rz, ryaw = G.u1_rack_pose(f, b)
                self.assertAlmostEqual(wz, z0)
                self.assertAlmostEqual(rz, z0)
                self.assertAlmostEqual(wx, rx, places=2)
                self.assertGreater(ry, wy)                     # U1 rack stands directly behind the sorting worker
                c_pose = G.u1_carton_on_rack((rx, ry, rz, ryaw), 0)
                self.assertAlmostEqual(c_pose[2], z0 + G.U1_RACK_CARTON_Z)
                meds_in = [G.u1_med_in_carton((wx, 11.229, G.zc(f), math.pi / 2), m) for m in range(G.U1_MED_PER_CARTON)]
                meds_rack = [G.u1_med_on_rack((rx, ry, rz, ryaw), m, 0) for m in range(G.U1_MED_PER_CARTON)]
                self.assertEqual(len({round(m[0], 3) for m in meds_in}), G.U1_MED_PER_CARTON)
                self.assertEqual(len({round(m[0], 3) for m in meds_rack}), G.U1_MED_PER_CARTON)
                self.assertTrue(all(abs(m[0] - (rx - .295)) < .20 for m in meds_rack))   # inside the 580mm hollow blue tray
                cells = G.U1_BRANCH_CELLS[b]
                for k in range(len(cells) - 1):
                    store = (cells[k]['x'], cells[k]['y'], z0)
                    spare = (cells[k + 1]['x'], cells[k + 1]['y'], z0)
                    u1 = (rx, ry, z0)
                    for r in (G.u1_agv_route(u1, store, b), G.u1_agv_route(store, spare, b), G.u1_agv_route(spare, u1, b)):
                        for p0, p1 in zip(r, r[1:]):
                            xmin, xmax = min(p0[0], p1[0]), max(p0[0], p1[0])
                            ymin, ymax = min(p0[1], p1[1]), max(p0[1], p1[1])
                            for sr in static_racks:
                                self.assertFalse((xmin - .8 <= sr['x'] <= xmax + .8) and (ymin - .6 <= sr['y'] <= ymax + .6))
                            for col in G.L['columns']:
                                self.assertTrue(xmax + .5 < col[0] or xmin - .5 > col[2] or ymax + .4 < col[1] or ymin - .4 > col[3])

    def test_z05_unpack_and_u2_stations_to_kho04(self):
        reserved = G.FF.U1_RESERVED_CELL_XY | G.FF.U2_RESERVED_CELL_XY | G.FF.P1_RESERVED_CELL_XY
        static_racks = [c for c in G.L['rack_cells'] if (round(c['x'], 3), round(c['y'], 3)) not in reserved]
        self.assertEqual(len(G.Z05_WORKERS), 2)
        self.assertEqual(len(G.U2_WORKERS), 6)
        for f in G.floors():
            z0 = G.LEVELS[f - 1]
            for z_idx in range(len(G.Z05_WORKERS)):
                spot_xy = (-1.705, 37.19 if z_idx == 0 else 30.27)
                stack = G.z05_stack_positions(spot_xy, f, 18)
                self.assertEqual(len(stack), 18)
                self.assertGreater(stack[0][2], stack[-1][2])
                p0 = G.z05_pallet_side(spot_xy, 0)
                p1 = G.z05_pallet_side(spot_xy, 1)
                self.assertGreater(math.hypot(p0[0] - p1[0], p0[1] - p1[1]), .4)   # two workers at same pallet do not overlap
            for u2_idx in range(len(G.U2_WORKERS)):
                wx, wy, wyaw, wz = G.u2_station(f, u2_idx)
                rx, ry, rz, ryaw = G.u2_rack_pose(f, u2_idx)
                drop = G.u2_carton_drop_pose(f, u2_idx)
                self.assertAlmostEqual(wz, z0)
                self.assertAlmostEqual(rz, z0)
                self.assertGreater(drop[0], wx)                # carton sits on the floor in front of the U2 worker (east)
                self.assertLess(rx, wx)                        # U2 rack sits directly behind the U2 worker (west)
                meds_in = [G.u2_med_in_carton(drop, m) for m in range(G.U1_MED_PER_CARTON)]
                meds_rack = [G.u1_med_on_rack((rx, ry, rz, ryaw), m, 0) for m in range(G.U1_MED_PER_CARTON)]
                self.assertEqual(len({round(m[1], 3) for m in meds_in}), G.U1_MED_PER_CARTON)
                self.assertEqual(len({round(m[1], 3) for m in meds_rack}), G.U1_MED_PER_CARTON)
                self.assertTrue(all(abs(m[1] - (ry - .295)) < .20 for m in meds_rack))
                cells = G.U2_STATION_CELLS[u2_idx]
                for k in range(len(cells) - 1):
                    store = (cells[k]['x'], cells[k]['y'], z0)
                    spare = (cells[k + 1]['x'], cells[k + 1]['y'], z0)
                    u2 = (rx, ry, z0)
                    for r in (G.u2_agv_route(u2, store, u2_idx), G.u2_agv_route(store, spare, u2_idx), G.u2_agv_route(spare, u2, u2_idx)):
                        for p0, p1 in zip(r, r[1:]):
                            xmin, xmax = min(p0[0], p1[0]), max(p0[0], p1[0])
                            ymin, ymax = min(p0[1], p1[1]), max(p0[1], p1[1])
                            for sr in static_racks:
                                self.assertFalse((xmin - .8 <= sr['x'] <= xmax + .8) and (ymin - .6 <= sr['y'] <= ymax + .6))
                            for col in G.L['columns']:
                                self.assertTrue(xmax + .5 < col[0] or xmin - .5 > col[2] or ymax + .4 < col[1] or ymin - .4 > col[3])

    def test_k06_trolleys_and_p1_stations_north_and_west(self):
        reserved = G.FF.U1_RESERVED_CELL_XY | G.FF.U2_RESERVED_CELL_XY | G.FF.P1_RESERVED_CELL_XY
        static_racks = [c for c in G.L['rack_cells'] if (round(c['x'], 3), round(c['y'], 3)) not in reserved]
        self.assertEqual(len(G.P1_STATIONS), 14)   # 7 North (0..6) + 7 West (7..13)
        self.assertEqual(len(G.K06_SLOTS_XY), 8)   # 4 Empty + 4 Full staging bays in Khu 06
        for f in G.floors():
            z0 = G.LEVELS[f - 1]
            # Verify U1 and U2 trolley positions and Khu 06 routes
            for b in range(len(G.BRANCHES)):
                tp = G.u1_trolley_pose(f, b)
                slots = [G.trolley_carton_pose(tp, s) for s in range(G.TROLLEY_CAPACITY)]
                self.assertEqual(len(slots), 5)
                k06 = G.k06_slot_pose(f, b % 4)[:3]
                pts = G.u1_k06_route(tp[:3], k06)
                self.assertGreaterEqual(len(pts), 2)
            for u2_idx in range(len(G.U2_WORKERS)):
                tp = G.u2_trolley_pose(f, u2_idx)
                k06 = G.k06_slot_pose(f, u2_idx % 4)[:3]
                pts = G.u2_k06_route(tp[:3], k06, u2_idx)
                self.assertGreaterEqual(len(pts), 2)
            # Verify all 14 P1 stations (North 0..6 and West 7..13)
            for st_idx in range(len(G.P1_STATIONS)):
                wx, wy, wyaw, wz = G.p1_station(f, st_idx)
                rx, ry, rz, ryaw = G.p1_rack_pose(f, st_idx)
                tx, ty, tz, tyaw = G.p1_trolley_pose(f, st_idx)
                c_act = G.p1_active_carton_pose(f, st_idx)
                c_aside = G.p1_aside_carton_pose(f, st_idx)
                self.assertAlmostEqual(wz, z0)
                self.assertAlmostEqual(rz, z0)
                self.assertAlmostEqual(tz, z0)
                self.assertGreater(math.hypot(wx - tx, wy - ty), .45)   # P1 worker and P1 trolley do not overlap
                self.assertGreater(math.hypot(c_act[0] - c_aside[0], c_act[1] - c_aside[1]), .4)
                cells = G.P1_STATION_CELLS[st_idx]
                self.assertEqual(len(cells), 2)
                store = (cells[0]['x'], cells[0]['y'], z0)
                spare = (cells[1]['x'], cells[1]['y'], z0)
                p1_r = (rx, ry, z0)
                for r in (G.p1_agv_route(p1_r, store, st_idx), G.p1_agv_route(store, spare, st_idx), G.p1_agv_route(spare, p1_r, st_idx)):
                    for p0, p1 in zip(r, r[1:]):
                        xmin, xmax = min(p0[0], p1[0]), max(p0[0], p1[0])
                        ymin, ymax = min(p0[1], p1[1]), max(p0[1], p1[1])
                        for sr in static_racks:
                            self.assertFalse((xmin - .8 <= sr['x'] <= xmax + .8) and (ymin - .6 <= sr['y'] <= ymax + .6))
                        for col in G.L['columns']:
                            self.assertTrue(xmax + .5 < col[0] or xmin - .5 > col[2] or ymax + .4 < col[1] or ymin - .4 > col[3])
                k06_full = G.k06_slot_pose(f, 4 + (st_idx % 4))[:3]
                pts_k06 = G.p1_k06_route((tx, ty, z0), k06_full, st_idx)
                self.assertGreaterEqual(len(pts_k06), 2)

if __name__ == '__main__':
    unittest.main()
