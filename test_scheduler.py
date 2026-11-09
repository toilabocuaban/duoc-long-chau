import unittest,math
from motion_scheduler import Scheduler,Motion,InboundInterlock,forks_clear

class SchedulerChecks(unittest.TestCase):
    def test_two_devices_move_in_the_same_frame(self):
        s=Scheduler();poses={'agv':(0,0,0),'pallet':(0,0,0)}
        s.add('return',[Motion({'agv':(-10,0,0)},10)])
        s.add('chain',[Motion({'pallet':(0,4,0)},4)])
        update=s.advance(2,poses)
        self.assertEqual(update['agv'],(-2,0,0))
        self.assertEqual(update['pallet'],(0,2,0))
        poses.update(update);s.advance(2,poses)
        self.assertTrue(s.active('return'))

    def test_same_entity_cannot_have_two_owners(self):
        s=Scheduler();s.add('a',[Motion({'p':(1,0,0)},1)]);s.add('b',[Motion({'p':(2,0,0)},1)])
        with self.assertRaisesRegex(RuntimeError,'Concurrent writers'):s.advance(.2,{'p':(0,0,0)})

    def test_no_start_while_forks_engaged(self):
        pallet=(10,-1.8,.3,0)
        self.assertFalse(forks_clear((10,-3.6,0,math.pi/2),pallet))
        self.assertTrue(forks_clear((10,-4.82,0,math.pi/2),pallet))

    def test_buffer_reservation_and_ready_checks(self):
        for field in ['placed','clear','lift_ready','buffer_free']:
            values=dict(placed=True,clear=True,lift_ready=True,buffer_free=True);values[field]=False
            with self.assertRaises(RuntimeError):InboundInterlock().reserve('p',**values)
        i=InboundInterlock();i.reserve('p',placed=True,clear=True,lift_ready=True,buffer_free=True)
        with self.assertRaises(RuntimeError):i.reserve('q',placed=True,clear=True,lift_ready=True,buffer_free=True)
        i.received('p');i.picked('p');self.assertIsNone(i.owner)

if __name__=='__main__':unittest.main()
