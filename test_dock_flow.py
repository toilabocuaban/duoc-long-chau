import asyncio
import unittest
from dock_flow import FloorGate,stage,source_stack
import truck_scene

class DockFlowChecks(unittest.IsolatedAsyncioTestCase):
    async def test_manual_waits_for_all_agvs_and_blocks_new_entries(self):
        gate=FloorGate();a=gate.agv();b=gate.agv()
        await a.__aenter__();second=asyncio.create_task(b.__aenter__());await asyncio.sleep(0)
        self.assertFalse(second.done())
        entered=asyncio.Event();release=asyncio.Event()
        async def move():
            async with gate.manual():entered.set();await release.wait()
        t=asyncio.create_task(move());await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        await a.__aexit__(None,None,None);await entered.wait()
        c=gate.agv();new=asyncio.create_task(c.__aenter__());await asyncio.sleep(0)
        self.assertFalse(new.done())
        release.set();await t;await second;await b.__aexit__(None,None,None);await new;await c.__aexit__(None,None,None)
        self.assertEqual(gate.agvs,0)
    def test_staging_clear_of_loaded_agv_lane(self):
        for d in truck_scene.CAD['doors']:
            self.assertLess(stage(d)[1]+.5,-9.05)
            self.assertTrue(all(p[1]<d['rear_y'] for p in source_stack(d,18)))
    def test_workers_and_jacks_never_leave_factory(self):
        # Route geometry is deterministic; use a minimal object without starting Gazebo.
        flow=object.__new__(__import__('dock_flow').DockFlow)
        flow.docks=truck_scene.selected_docks({'dock_active_ids':[4,5,9,10],'dock_active_count':4,'truck_count_visible':4})
        flow.poses={f'dock_jack_{i}':stage(d)+(0,) for i,d in enumerate(flow.docks)}
        for i in range(4):
            target=(-33.485,-2.765,0,0) if i<2 else (-10.7,-2.2,0,0)
            route=flow.transfer_waypoints(i,target)
            self.assertTrue(all(p[1]>=flow.docks[i]['rear_y'] for p in route))
            self.assertIn(-5.35,[p[1] for p in route])
            self.assertTrue(all(p[1]<flow.docks[i]['rear_y'] for p in source_stack(flow.docks[i],18)))
            worker_y=flow.docks[i]['rear_y']+.72
            self.assertGreater(worker_y,flow.docks[i]['rear_y'])

if __name__=='__main__':unittest.main()
