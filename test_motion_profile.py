import math
import unittest
from motion_profile import SCurve,duration,bounds,intersects

def derivatives(p,n=4000):
    h=p.T/n;s=[p.position(i*h) for i in range(n+1)]
    v=[(s[i+1]-s[i])/h for i in range(n)];a=[(v[i+1]-v[i])/h for i in range(n-1)];j=[(a[i+1]-a[i])/h for i in range(n-2)]
    return s,v,a,j

class ProfileChecks(unittest.TestCase):
    def test_limits_for_short_and_long_moves(self):
        for v,a,j in [(1.5,.4,.8),(2,.5,1),(.3,.25,.5)]:
            for d in [.01,.04,.3,2,16,60]:
                p=SCurve(d,v,a,j);s,vel,acc,jer=derivatives(p)
                self.assertAlmostEqual(s[-1],d,9)
                self.assertTrue(all(b>=a_-1e-12 for a_,b in zip(s,s[1:])))
                self.assertLessEqual(max(vel),v*1.001)
                self.assertLessEqual(max(map(abs,acc)),a*1.01)
                self.assertLessEqual(max(map(abs,jer)),j*1.05)
                self.assertAlmostEqual(p.shape(0),0);self.assertAlmostEqual(p.shape(1),1)
                self.assertGreaterEqual(duration(d,v,a,j),p.T)
    def test_long_move_cruises_at_configured_speed(self):
        # 7-segment law: a long move reaches and holds the configured speed (quintic only touched it).
        p=SCurve(30,1,.6,1.2);_,vel,_,_=derivatives(p)
        self.assertGreater(p.tc,20)
        self.assertAlmostEqual(max(vel),1,3)
        self.assertAlmostEqual(p.T,30/1+1/.6+.6/1.2,6)
    def test_stretched_shape_stays_within_limits(self):
        p=SCurve(5,1,.6,1.2);T=2*p.T
        n=4000;s=[5*p.shape(i/n) for i in range(n+1)];v=[(s[i+1]-s[i])*n/T for i in range(n)]
        self.assertLessEqual(max(v),.5*1.001)
    def test_reserved_turn_includes_fork_sweep(self):
        arc=[(0,0,0,k*math.pi/200) for k in range(101)]
        self.assertTrue(intersects(bounds(arc),bounds([(0,2.8,0,0)])))
    def test_parallel_aisles_do_not_block_each_other(self):
        a=bounds([(0,-9,0,0),(20,-9,0,0)])
        b=bounds([(20,-7,0,math.pi),(0,-7,0,math.pi)])
        self.assertFalse(intersects(a,b))
