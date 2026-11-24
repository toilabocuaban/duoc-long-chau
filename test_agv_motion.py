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

    def test_two_forklifts_serve_six_pickup_bays_cleanly(self):
        from agv_motion import PARKS,pickup_position,route_to_pickup,route_to_gate,OFFSET
        self.assertEqual(len(PARKS),2)
        spots=[pickup_position(j) for j in range(6)]
        pallets={f'pallet_{j}':s for j,s in enumerate(spots)}
        for agv_i in range(len(PARKS)):
            for j,(x,y,_,_) in enumerate(spots):
                insert=route_to_pickup(agv_i,j,spots[j])[-1][0]
                self.assertAlmostEqual(insert[1],y-OFFSET)
                for base,_ in samples((x,y-OFFSET-1.5,0,math.pi/2),insert):
                    check_pose(agv_i,base,pallets,None,f'pallet_{j}')
                for port_idx in (0,1):
                    gate=route_to_gate(agv_i,20.30108 if port_idx==0 else 38.30108,port_idx=port_idx)
                    self.assertEqual(gate[-1][0][0],20.30108 if port_idx==0 else 38.30108)
                back=route_to_gate(agv_i,20.30108)[0][0]
                for base,_ in samples(insert,(x,back[1],0,math.pi/2)):
                    check_pose(agv_i,base,pallets,f'pallet_{j}',f'pallet_{j}')
        from agv_traffic import Plan,compatible
        for agv_i in range(len(PARKS)):
            for peer_j in range(len(PARKS)):
                if agv_i==peer_j:continue
                peer=Plan(PARKS[peer_j],PARKS[peer_j],0.0,0.05,False)
                cur=PARKS[agv_i]
                for nxt,_ in route_to_pickup(agv_i,0,spots[0]):
                    self.assertTrue(compatible(Plan(cur,nxt,0.0,5.0,False),peer,0.0,1.0))
                    cur=nxt

    def test_shortest_turn_across_pi(self):
        path=list(samples((0,0,0,3.1),(0,0,0,-3.1)))
        self.assertLess(len(path),10)

if __name__=='__main__':unittest.main()
