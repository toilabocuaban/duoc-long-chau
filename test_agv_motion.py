import math
import unittest
from agv_motion import assembly,samples,check_pose,overlaps

class MotionChecks(unittest.TestCase):
    def test_rigid_payload_during_quarter_turn(self):
        start=(5,-9,0,0);end=(5,-9,0,math.pi/2)
        poses=list(samples(start,end))
        self.assertGreater(len(poses),50)
        for base,_ in poses:
            load=assembly(0,base,.2,'pallet')['pallet']
            self.assertAlmostEqual(math.dist(base[:2],load[:2]),1.8)
            self.assertAlmostEqual(load[3]-base[3],-math.pi/2)
        self.assertAlmostEqual(assembly(0,end,.2,'pallet')['pallet'][1],-7.2)

    def test_obstacle_on_path_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'chassis intersects'):
            check_pose(0,(0,0,0,0),{'pallet_5':(.8,0,0,0)})

    def test_parked_vehicle_blocks_route(self):
        with self.assertRaisesRegex(RuntimeError,'parked AGV'):
            check_pose(0,(0,0,0,0),{'agv_1':(1,0,0,0)})

    def test_forks_cannot_hit_adjacent_load(self):
        with self.assertRaisesRegex(RuntimeError,'load/forks'):
            check_pose(0,(0,0,0,0),{'pallet_5':(2,0,0,0)})

    def test_shortest_turn_across_pi(self):
        path=list(samples((0,0,0,3.1),(0,0,0,-3.1)))
        self.assertLess(len(path),10)

if __name__=='__main__':unittest.main()
