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
            self.assertAlmostEqual(load[3]-base[3],0)   # forks along the pallet's 1.2 m side
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

    def test_pick_between_occupied_cad_spots(self):
        # The 4 CAD spots are 1.045 m apart for 1.0 m pallets: forks and load must clear both neighbours.
        from agv_motion import pickup_position,route_to_pickup,route_to_gate,OFFSET
        spots=[pickup_position(j) for j in range(4)]
        pallets={f'pallet_{j}':s for j,s in enumerate(spots)}
        for j,(x,y,_,_) in enumerate(spots):
            insert=route_to_pickup(0,j,spots[j])[-1][0]
            self.assertAlmostEqual(insert[1],y-OFFSET)
            for base,_ in samples((x,y-OFFSET-1.5,0,math.pi/2),insert):
                check_pose(0,base,pallets,None,f'pallet_{j}')
            back=route_to_gate(0,20.30108)[0][0]
            for base,_ in samples(insert,(x,back[1],0,math.pi/2)):
                check_pose(0,base,pallets,f'pallet_{j}',f'pallet_{j}')

    def test_shortest_turn_across_pi(self):
        path=list(samples((0,0,0,3.1),(0,0,0,-3.1)))
        self.assertLess(len(path),10)

if __name__=='__main__':unittest.main()
