import unittest
import xml.etree.ElementTree as ET
import equipment_visuals as EQ

def box(name,x,y,z,sx,sy,sz,color,yaw=0):
    return f'<visual name="{name}"><pose>{x} {y} {z} 0 0 {yaw}</pose><geometry><box><size>{sx} {sy} {sz}</size></box></geometry></visual>'

def cylinder(name,x,y,z,r,length,color,roll=0,pitch=0):
    return f'<visual name="{name}"><pose>{x} {y} {z} {roll} {pitch} 0</pose><geometry><cylinder><radius>{r}</radius><length>{length}</length></cylinder></geometry></visual>'

class ChainChecks(unittest.TestCase):
    def test_support_surface_matches_transfer_height(self):
        for axis in ['x','y']:
            items=EQ.chain_conveyor(box,cylinder,3,axis=axis)
            names=[ET.fromstring(s).attrib['name'] for s in items]
            self.assertEqual(len(names),len(set(names)))
            links=[ET.fromstring(s) for s in items if ET.fromstring(s).attrib['name'].startswith('chain_link')]
            self.assertGreater(len(links),80)
            for link in links:
                pose=list(map(float,link.find('pose').text.split()))
                height=float(link.find('geometry/box/size').text.split()[2])
                self.assertAlmostEqual(pose[2]+height/2,0)
            channels=[ET.fromstring(s) for s in items if ET.fromstring(s).attrib['name'].startswith('chain_channel')]
            gauge_axis=0 if axis=='y' else 1
            offsets=[abs(float(s.find('pose').text.split()[gauge_axis])) for s in channels]
            self.assertTrue(all(abs(a-.42)<1e-6 for a in offsets))

if __name__=='__main__':unittest.main()
