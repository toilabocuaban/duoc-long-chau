import unittest
from stock_layout import stock_positions
from cad_navigation import LAYOUT,hit
class StockChecks(unittest.TestCase):
    def test_stock_only_on_cad_x(self):
        positions=stock_positions([.3]);valid={(s['x'],s['y']) for s in LAYOUT.slots.values() if s['id'] not in LAYOUT.reserved}
        self.assertGreater(len(positions),200)
        for x,y,z in positions:
            self.assertIn((x,y),valid)
            self.assertFalse(any(hit([x-.6,y-.5,x+.6,y+.5],c) for c in LAYOUT.columns))
    def test_no_stock_on_loaded_paths(self):
        stock=stock_positions([.3])
        for j,sid in enumerate(LAYOUT.job_slots):
            paths=[LAYOUT.path(LAYOUT.data['ports']['in'][j%2]['buffer'],LAYOUT.slot_point(sid)),LAYOUT.path(LAYOUT.slot_point(sid),LAYOUT.data['ports']['out'][j%2]['buffer'])]
            for path in paths:
                for a,b in zip(path,path[1:]):
                    envelope=[min(a[0],b[0])-.6,min(a[1],b[1])-.5,max(a[0],b[0])+.6,max(a[1],b[1])+.5]
                    for x,y,_ in stock:self.assertFalse(hit(envelope,[x-.6,y-.5,x+.6,y+.5]),(a,b,x,y))
if __name__=='__main__':unittest.main()
