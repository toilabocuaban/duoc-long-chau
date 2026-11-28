import json
import unittest
from pathlib import Path

import truck_plan as TP

C = json.loads((Path(__file__).resolve().parent / 'config.json').read_text())


class TruckPlanTest(unittest.TestCase):
    def test_day_totals_match_excel(self):
        plan = TP.build_day_plan(C)
        pallets = [p for tr in plan for p in tr['pallets']]
        self.assertEqual(len(plan), 60)
        self.assertEqual(sum(tr['cartons'] for tr in plan), 12000)
        self.assertEqual(sum(p['cartons'] for p in pallets), 12000)
        self.assertEqual(sum(p['kind'] == 'even' for p in pallets), 150)
        self.assertEqual(sum(p['odd_type'] == 'gt10' for p in pallets), 130)
        self.assertEqual(sum(p['odd_type'] == 'lt10' for p in pallets), 120)

    def test_every_truck_carries_200_mixed_cartons(self):
        for tr in TP.build_day_plan(C):
            self.assertEqual(sum(p['cartons'] for p in tr['pallets']), 200)
            kinds = {p['kind'] for p in tr['pallets']}
            self.assertEqual(kinds, {'even', 'odd'})

    def test_arrivals_spread_over_the_shift(self):
        plan = TP.build_day_plan(C, day=2, first_truck_id=120)
        self.assertEqual(plan[0]['truck_id'], 120)
        self.assertAlmostEqual(plan[0]['arrival_s'], 2 * 28800.0)
        self.assertLess(plan[-1]['arrival_s'], 3 * 28800.0)

    def test_dock_has_enough_carton_models(self):
        self.assertLessEqual(TP.max_cartons_per_pallet(C), int(C['dock_max_cartons_per_pallet']))


if __name__ == '__main__':
    unittest.main()
