import math
import unittest
from motion_profile import blend,duration,bounds,intersects

class ProfileChecks(unittest.TestCase):
    def test_limits_for_short_and_long_moves(self):
        for d in [.01,.3,2,16,60]:
            v,a,j=1.5,.4,.8
            t=duration(d,v,a,j)
            self.assertLessEqual(1.875*d/t,v+1e-9)
            self.assertLessEqual(10/math.sqrt(3)*d/t**2,a+1e-9)
            self.assertLessEqual(60*d/t**3,j+1e-9)
            self.assertEqual(blend(0),0)
            self.assertEqual(blend(1),1)
            self.assertLess(blend(1e-5),1e-12)
            self.assertLess(1-blend(1-1e-5),1e-12)
    def test_reserved_turn_includes_fork_sweep(self):
        arc=[(0,0,0,k*math.pi/200) for k in range(101)]
        self.assertTrue(intersects(bounds(arc),bounds([(0,2.8,0,0)])))
    def test_parallel_aisles_do_not_block_each_other(self):
        a=bounds([(0,-9,0,0),(20,-9,0,0)])
        b=bounds([(20,-7,0,math.pi),(0,-7,0,math.pi)])
        self.assertFalse(intersects(a,b))
