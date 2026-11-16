import unittest
from agv_traffic import Plan,compatible

class TrafficChecks(unittest.TestCase):
    def test_same_lane_following_with_overlapping_route_extents(self):
        lead=Plan((10,-9,0,0),(60,-9,0,0),0,60)
        follower=Plan((0,-9,0,0),(50,-9,0,0),0,60)
        self.assertTrue(compatible(follower,lead,0,1))
    def test_cannot_run_into_stopped_leader(self):
        lead=Plan((10,-9,0,0),(20,-9,0,0),0,20)
        follower=Plan((0,-9,0,0),(50,-9,0,0),0,60)
        self.assertFalse(compatible(follower,lead,0,1))
    def test_parallel_lanes_can_run_together(self):
        a=Plan((0,-9,0,0),(50,-9,0,0),0,60)
        b=Plan((0,-7,0,0),(50,-7,0,0),0,60)
        self.assertTrue(compatible(a,b,0,1))
    def test_conflicting_crossing_waits(self):
        a=Plan((-10,0,0,0),(10,0,0,0),0,20)
        b=Plan((0,-10,0,1.570796),(0,10,0,1.570796),0,20)
        self.assertFalse(compatible(a,b,0,1))

    def test_turn_near_parked_peer_uses_actual_parts(self):
        import math
        turning=Plan((-33,-7,0,math.pi),(-33,-7,0,math.pi/2),0,7)
        parked=Plan((-38,-5,0,0),(-38,-5,0,0),0,.05)
        self.assertTrue(compatible(turning,parked,0))

    def test_departing_agv_does_not_graze_returning_agv_between_ticks(self):
        import math
        from motion_profile import SCurve
        p1=SCurve(44.30108,1.8,0.5,1.0)
        p2=SCurve(1.9,1.8,0.5,1.0)
        returning=Plan((20.30108,-6.4,0,math.pi),(-24,-6.4,0,math.pi),603.433333,p1.duration,False,p1.shape)
        departing=Plan((-19.0,-4.5,0,math.pi/2),(-19,-6.4,0,math.pi/2),627.2,p2.duration,False,p2.shape)
        self.assertFalse(compatible(departing,returning,627.2,1.0))
