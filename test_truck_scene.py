import json
import math
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
import truck_scene as T

class TruckLayoutChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c=json.loads((Path(__file__).parent/'config.json').read_text())

    def test_rear_at_cad_datum_and_front_outside(self):
        for d in T.selected_docks(self.c):
            x,y,z,yaw=T.truck_pose(d)
            self.assertAlmostEqual(x,d['x'])
            self.assertAlmostEqual(y,d['rear_y'])
            self.assertLess(y,-10.19)
            self.assertLess(y+math.sin(yaw)*self.c['truck_length_m'],y)
            self.assertAlmostEqual(z+T.DECK,0)

    def test_no_stair_or_duplicate_truck_bay(self):
        docks=T.CAD['doors']
        self.assertEqual(len(docks),11)
        self.assertTrue(all(abs(d['x']+44.534684)>2 for d in docks))
        xs=sorted(d['x'] for d in docks)
        self.assertTrue(all(b-a>self.c['truck_width_m']+.7 for a,b in zip(xs,xs[1:])))

    def test_actual_dwg_positions_match_existing_door_geometry(self):
        plan=json.loads((Path(__file__).parent/'four_floor_layout.json').read_text(encoding='utf-8'))
        doors=plan['plan_lines']['door']
        for d in T.CAD['doors']:
            self.assertTrue(any(abs((s[0]+s[2])/2-d['x'])<.002 and abs(s[1]-d['door_y'])<.002 for s in doors))

    def test_transparency_is_sdf_visual_property(self):
        v=ET.fromstring(T.transparent_panel('wall',0,0,1,2,.02,2,.85))
        self.assertEqual(v.findtext('transparency'),'0.85')
        self.assertIsNone(v.find('material/transparency'))

if __name__=='__main__':unittest.main()
