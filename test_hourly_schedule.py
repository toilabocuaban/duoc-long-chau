import sys
import unittest

sys.argv = ['test_hourly_schedule.py', '--no-gz', '--fast']
import controller  # noqa: E402


class TestHourlySchedule(unittest.TestCase):
    def test_release_times_sum_and_bounds(self):
        in_sched = [28, 30, 26, 18, 14, 12, 12, 10]
        out_sched = [18, 22, 30, 35, 50, 55, 55, 35]
        in_t = controller.build_hourly_release_times(in_sched)
        out_t = controller.build_hourly_release_times(out_sched)

        self.assertEqual(len(in_t), 150)
        self.assertEqual(len(out_t), 300)
        self.assertEqual(in_t, sorted(in_t))
        self.assertEqual(out_t, sorted(out_t))
        self.assertEqual(in_t[0], 0.0)
        self.assertEqual(out_t[0], 0.0)
        self.assertLess(in_t[-1], 28800.0)
        self.assertLess(out_t[-1], 28800.0)

        for h, cnt in enumerate(in_sched):
            in_hour = [t for t in in_t if h * 3600.0 <= t < (h + 1) * 3600.0]
            self.assertEqual(len(in_hour), cnt)
        for h, cnt in enumerate(out_sched):
            in_hour = [t for t in out_t if h * 3600.0 <= t < (h + 1) * 3600.0]
            self.assertEqual(len(in_hour), cnt)

    def test_zero_and_empty_hours(self):
        self.assertEqual(controller.build_hourly_release_times([]), [])
        times = controller.build_hourly_release_times([0, 2, 0, 1], window_s=100.0)
        self.assertEqual(times, [100.0, 150.0, 300.0])


if __name__ == '__main__':
    unittest.main()

