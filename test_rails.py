import unittest
from cad_navigation import LAYOUT
from rail_geometry import WHEEL_CENTRE_Z,WHEEL_RADIUS,RAIL_TOP_OFFSET,supported

class RailChecks(unittest.TestCase):
    def test_wheel_tangent(self):
        for z in [.3,2.3,4.3,6.3,8.3,10.3,12.3,14.3,16.3]:
            self.assertAlmostEqual(z+WHEEL_CENTRE_Z-WHEEL_RADIUS,z+RAIL_TOP_OFFSET)
    def test_all_jobs_respect_cad_frames(self):
        for j,sid in enumerate(LAYOUT.job_slots):
            inp=LAYOUT.data['ports']['in'][j%2];out=LAYOUT.data['ports']['out'][j%2]
            for start,end in [(inp['buffer'],LAYOUT.slot_point(sid)),(LAYOUT.slot_point(sid),out['buffer'])]:
                route=LAYOUT.path(start,end)
                for a,b in zip(route,route[1:]):
                    self.assertTrue(LAYOUT.clear(a,b))
                    axis='x' if abs(a[0]-b[0])>1e-4 else 'y'
                    for i in range(51):
                        t=i/50
                        self.assertTrue(supported(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t,axis))
    def test_cross_rack_shortcut_rejected(self):
        self.assertFalse(LAYOUT.clear((.80054,6),(2.30054,6)))
    def test_six_factory_columns(self):
        self.assertEqual(len(LAYOUT.columns),6)
        for r in LAYOUT.columns:
            x,y=(r[0]+r[2])/2,(r[1]+r[3])/2
            self.assertFalse(LAYOUT.clear((x-1,y),(x+1,y)))
    def test_not_a_free_grid(self):
        self.assertFalse(supported(10,10,'x'))
if __name__=='__main__':unittest.main()
