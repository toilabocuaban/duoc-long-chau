"""Unit tests for SimulationTracker telemetry and Excel (.xlsx) report exporter."""
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from report_exporter import SimulationTracker, _fmt_vi_hms, export_excel

ROOT = Path(__file__).resolve().parent


class ReportExporterTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_shuttles_agvs_dock_workers_and_excel_sheets(self):
        tracker = SimulationTracker(self.config, total_jobs=10, cycle_duration_s=120.0)

        # Verify initial fleet sizes: 10 shuttles, 2 + 24 + 24 + 56 = 106 AGVs
        self.assertEqual(len(tracker.shuttles), 10)
        self.assertEqual(len(tracker.agvs), 106)

        # Inbound arrival on Dock 4 (even pallet -> Dock AGV -> Kho 01 level 1, Shuttle 1)
        tracker.record_event({"job": 0, "state": "INBOUND_ARRIVAL", "demo_time_s": 0.0, "dock": 4})
        tracker.record_event({"loose": True, "state": "TRUCK_BATCH_READY", "demo_time_s": 1.0, "dock": 4, "classification": "even", "batch": 0, "cartons": 18})
        for c in range(18):
            tracker.record_event({"loose": True, "state": "BARCODE_CLASSIFIED", "demo_time_s": 2.0 + c * 0.5, "dock": 4, "classification": "even", "barcode": f"EVEN-0000-{c:03d}"})
        tracker.record_event({"loose": True, "state": "DOCK_PALLET_READY", "demo_time_s": 12.0, "dock": 4, "classification": "even", "batch": 0})
        tracker.record_event({"loose": True, "state": "EMPTY_PALLET_FROM_POOL", "demo_time_s": 22.0, "dock": 4, "batch": 0})

        # Odd pallet on Dock 5 -> Loose Unpack Tầng 1 -> U1
        tracker.record_event({"job": 1, "state": "INBOUND_ARRIVAL", "demo_time_s": 8.0, "dock": 5})
        tracker.record_event({"loose": True, "state": "TRUCK_BATCH_READY", "demo_time_s": 9.0, "dock": 5, "classification": "odd", "batch": 1, "cartons": 18})
        for c in range(18):
            tracker.record_event({"loose": True, "state": "BARCODE_CLASSIFIED", "demo_time_s": 10.0 + c * 0.5, "dock": 5, "classification": "odd", "barcode": f"ODD-0001-{c:03d}"})
        tracker.record_event({"loose": True, "state": "DOCK_PALLET_READY", "demo_time_s": 20.0, "dock": 5, "classification": "odd", "batch": 1})
        tracker.record_event({"loose": True, "state": "EMPTY_PALLET_RETURNED", "demo_time_s": 32.0, "dock": 5, "batch": 1, "classification": "odd"})

        # Dock AGV task carrying even pallet job=0 from Dock 4 bay into Kho 01
        tracker.start_agv_task(
            "agv_0", "DK-AGV-0001", "Khu Dock -> Kho 01", "Chở pallet chẵn", 23.0,
            pallet_or_item="pallet_0", dock_id=4, pick_location="Ô Pickup 1 (-33.49, -2.77)", drop_location="Cửa nhập Kho 01 - Cổng 1",
        )
        tracker.record_agv_leg("agv_0", 15.0, False, 10.0)
        tracker.record_agv_leg("agv_0", 35.0, True, 20.0)
        tracker.finish_agv_task("agv_0", 53.0)

        # Warehouse AS/RS Lifts & Shuttle 1 inbound & outbound missions
        tracker.start_lift_task("in_lift_0", "Nhập kho", "LF-IN-0001", "pallet_0")
        tracker.record_lift_leg("in_lift_0", 4.2, 6.0)
        tracker.finish_lift_task("in_lift_0")

        tracker.start_shuttle_task(
            "shuttle_0", "Nhập kho", "WH-IN-0001", "pallet_0", 0, "S101", 55.0,
            job_id=0, pick_location="Bàn đệm Nhập 1", drop_location="Ô kệ S101 (T1)", wait_s=1.5,
        )
        tracker.record_shuttle_turn("shuttle_0", 0.6)
        tracker.record_shuttle_leg("shuttle_0", 24.5, 14.0)
        tracker.finish_shuttle_task("shuttle_0", 69.6)

        tracker.start_shuttle_task(
            "shuttle_0", "Xuất kho", "WH-OUT-0001", "pallet_0", 0, "S101", 72.0,
            job_id=0, request_id=0, pick_location="Ô kệ S101 (T1)", drop_location="Bàn đệm Xuất 1",
        )
        tracker.record_shuttle_leg("shuttle_0", 26.5, 15.0)
        tracker.finish_shuttle_task("shuttle_0", 87.0)

        tracker.start_lift_task("out_lift_0", "Xuất kho", "LF-OUT-0001", "pallet_0")
        tracker.record_lift_leg("out_lift_0", 7.8, 9.0)
        tracker.finish_lift_task("out_lift_0")

        # Floor-1 Unpack worker + U1 worker + U1 AGV + Z05 + U2 + P1
        tracker.record_event({"loose": True, "state": "CARTON_ON_BELT", "demo_time_s": 40.0, "operator": 0, "pallet": 1})
        tracker.record_worker_activity("op_0", duration_s=5.5, shared_pick=True)
        tracker.record_event({"loose": True, "state": "CARTON_PLACED_ON_RACK", "demo_time_s": 48.0, "floor": 1, "branch": 0, "med_boxes": ["m0", "m1", "m2"]})
        tracker.record_worker_activity("u1_1_0", duration_s=6.0)
        tracker.record_event({"loose": True, "state": "Z05_CARTON_DELIVERED_TO_U2", "demo_time_s": 92.0, "floor": 1, "worker": 0, "job": 0})
        tracker.record_worker_activity("z05_1_0", duration_s=4.0)
        tracker.record_event({"loose": True, "state": "U2_CARTON_PLACED_ON_RACK", "demo_time_s": 100.0, "floor": 1, "u2_station": 0, "job": 0, "med_boxes": ["m0", "m1", "m2"]})
        tracker.record_event({"loose": True, "state": "P1_PACKED_CARTON_COMPLETED", "demo_time_s": 105.0, "floor": 1, "p1_station": 0})

        u1_code = tracker.next_agv_code("UNP-U1R-T1N1")
        tracker.start_agv_task("u1_agv_1_0", u1_code, "Khu Unpack U1", "Cất rack", 50.0, pallet_or_item="u1_rack_1_0_0")
        tracker.record_agv_leg("u1_agv_1_0", 20.0, True, 18.0, wait_s=2.0)
        tracker.record_agv_leg("u1_agv_1_0", 10.0, False, 10.0)
        tracker.finish_agv_task("u1_agv_1_0", 80.0)

        tracker.record_event({"loose": True, "state": "DOCK_GATE_WAIT", "demo_time_s": 25.0, "dock": 4, "classification": "even", "batch": 0, "wait_s": 2.5})

        summary = tracker.finalize(
            120.0,
            stop_reason="Kết thúc đúng thời lượng chu trình (120.0s)",
            dock_flow_stats={"pallets_to_w1": 1, "pallets_to_w2": 1, "scanned_even": 18, "scanned_odd": 18, "empty_pallet_returns": 1},
            loose_stats={"pallets_unpacked": 1, "cartons_delivered": 1},
            wall_duration_s=40.0,
            traffic_stats={"engine_ticks": 100, "multiple_shuttle_motion_ticks": 25, "inbound_outbound_overlap_ticks": 10, "rail_contact_checks": 500, "dual_agv_motion_ticks": 15, "same_direction_following_ticks": 2, "swept_pose_checks": 400, "agv_traffic_stats": {"trips": 3, "wait_s": 4.0, "max_wait_s": 2.0}},
        )

        self.assertIn("2 phút 00 giây", _fmt_vi_hms(120.0))
        self.assertEqual(summary["wall_duration_s"], 40.0)
        sh0 = tracker.shuttles["shuttle_0"]
        self.assertEqual(sh0["turn_count"], 1)
        self.assertAlmostEqual(sh0["busy_s"], 29.6, places=2)
        self.assertAlmostEqual(sh0["wait_s"], 1.5, places=2)
        self.assertAlmostEqual(sh0["active_pct"] + sh0["idle_pct"], 100.0, places=2)
        self.assertEqual(sh0["fault_count"], 0)

        # Battery math check on agv_0:
        # loaded=35m*0.04=1.40, empty=15m*0.02=0.30, busy=30s -> idle=90s*0.002=0.18 -> used=1.88, rem=98.12%
        agv0 = tracker.agvs["agv_0"]
        self.assertAlmostEqual(agv0["total_distance_m"], 50.0, places=3)
        self.assertAlmostEqual(agv0["battery_used_pct"], 1.88, places=2)
        self.assertAlmostEqual(agv0["battery_remaining_pct"], 98.12, places=2)

        self.assertEqual(summary["warehouse_inbound_tasks"], 1)
        self.assertEqual(summary["warehouse_outbound_tasks"], 1)
        self.assertEqual(summary["agv_dock_tasks"], 1)
        self.assertEqual(summary["agv_unpack_tasks"], 1)
        self.assertEqual(tracker.shuttles["shuttle_9"]["status"], "Dự phòng (Standby)")

        with tempfile.TemporaryDirectory() as tmp:
            paths = export_excel(tracker, tmp, report_summary={"loose_pallets_unpacked": 1, "loose_cartons_delivered": 1})
            latest = Path(paths["latest"])
            stamped = Path(paths["timestamped"])
            self.assertTrue(latest.is_file())
            self.assertTrue(stamped.is_file())

            wb = load_workbook(latest)
            self.assertEqual(
                wb.sheetnames,
                ["Tong_Quan", "Kho_ASRS_Shuttle", "Khu_Unpack_AGV", "Khu_Vuc_Dock", "Cong_Nhan_Unpack", "Chi_Tiet_Nhiem_Vu_AGV_Kho"],
            )
            ws_sh = wb["Kho_ASRS_Shuttle"]
            self.assertEqual(len(ws_sh._charts), 2)
            sh_text = "\n".join(str(c.value) for row in ws_sh.iter_rows(values_only=False) for c in row if c.value is not None)
            self.assertIn("WH-IN-0001", sh_text)
            self.assertIn("WH-OUT-0001", sh_text)
            self.assertIn("LF-IN-0001", sh_text)
            self.assertIn("LF-OUT-0001", sh_text)
            self.assertIn("Thang Nhập 1", sh_text)
            self.assertIn("Thang Nhập 2", sh_text)
            self.assertIn("Thang Xuất 1", sh_text)
            self.assertIn("Thang Xuất 2", sh_text)
            self.assertIn("Shuttle 10", sh_text)
            self.assertIn("Bàn đệm Nhập 1", sh_text)
            self.assertIn("Ô kệ S101 (T1)", sh_text)
            for removed_col in ("Chỉ tiêu Traffic Kho", "ID kỹ thuật", "Tầng kho", "Đổi trục X/Y", "NV gần nhất", "Mã Job / Yêu cầu", "Dock nguồn"):
                self.assertNotIn(removed_col, sh_text)
            ws_agv_kpi = wb["Khu_Unpack_AGV"]
            agv_kpi_text = "\n".join(str(c.value) for row in ws_agv_kpi.iter_rows(max_row=8, values_only=False) for c in row if c.value is not None)
            self.assertIn("400", agv_kpi_text)

            ws_agv = wb["Khu_Unpack_AGV"]
            agv_text = "\n".join(str(c.value) for row in ws_agv.iter_rows(values_only=False) for c in row if c.value is not None)
            self.assertIn("AGV-01", agv_text)
            self.assertIn("DK-AGV-0001", agv_text)
            self.assertIn("U1-AGV-T1-N1", agv_text)
            self.assertIn("UNP-U1R-T1N1-0001", agv_text)

            ws_dock = wb["Khu_Vuc_Dock"]
            dock_text = "\n".join(str(c.value) for row in ws_dock.iter_rows(values_only=False) for c in row if c.value is not None)
            self.assertIn("Dock 04", dock_text)
            self.assertIn("Dock 05", dock_text)
            self.assertIn("DK-AGV-0001", dock_text)

            ws_wrk = wb["Cong_Nhan_Unpack"]
            wrk_text = "\n".join(str(c.value) for row in ws_wrk.iter_rows(values_only=False) for c in row if c.value is not None)
            self.assertIn("OP-T1-01", wrk_text)
            self.assertIn("U1-T1-N1", wrk_text)
            self.assertIn("Z05-T1-CN1", wrk_text)

    def test_empty_cycle_export_and_battery_clamp(self):
        tracker = SimulationTracker(self.config, total_jobs=0, cycle_duration_s=10.0)
        tracker.record_agv_leg("agv_1", 10000.0, True, 100.0)
        snap = tracker.snapshot(50.0, stop_reason="Bản lưu tạm")
        self.assertEqual(snap.agvs["agv_1"]["battery_remaining_pct"], 0.0)
        self.assertEqual(tracker.sim_duration_s, 0.0)
        tracker.finalize(100.0, stop_reason="Dừng kiểm thử")
        self.assertEqual(tracker.agvs["agv_1"]["battery_remaining_pct"], 0.0)
        with tempfile.TemporaryDirectory() as tmp:
            p1 = export_excel(snap, tmp)
            p2 = export_excel(tracker, tmp)
            self.assertEqual(p1["timestamped"], p2["timestamped"])
            wb = load_workbook(p2["latest"])
            self.assertEqual(len(wb.sheetnames), 6)

    def test_staged_prestage_and_pallet_ready_metrics(self):
        tracker = SimulationTracker(self.config, total_jobs=0, continuous_mode=True)
        tracker.record_event({"demo_time_s": 1.0, "state": "INBOUND_ARRIVAL", "job": 0, "dock": 4})
        tracker.record_event({"demo_time_s": 2.0, "state": "TRUCK_BATCH_READY", "dock": 4, "classification": "staged", "batch": 0, "cartons": 18})
        tracker.record_event({"demo_time_s": 10.0, "state": "DOCK_PALLET_READY", "dock": 4, "classification": "even", "batch": 0, "cartons": 18})
        tracker.record_event({"demo_time_s": 22.0, "state": "EMPTY_PALLET_FROM_POOL", "dock": 4, "batch": 0})
        d4 = tracker.docks[4]
        self.assertEqual(d4["even_batches"], 1)
        self.assertEqual(d4["even_cartons"], 18)
        self.assertEqual(d4["even_ready"], 1)
        self.assertEqual(d4["even_pool_delivered"], 1)
        self.assertAlmostEqual(d4["busy_s"], 20.0, places=3)
        w4 = tracker.workers["dock_4"]
        self.assertEqual(w4["cartons"], 18)
        self.assertAlmostEqual(w4["busy_s"], 20.0, places=3)
        self.assertIn("even_0", w4["pallets"])

    def test_agv_traffic_wait_and_pure_idle_decomposition(self):
        tracker = SimulationTracker(self.config, total_jobs=4, cycle_duration_s=120.0)
        tracker.start_agv_task("u1_agv_1_0", "UNP-U1R-T1N1-0001", "Khu Unpack U1", "Cất rack", 0.0)
        tracker.record_agv_leg("u1_agv_1_0", 30.0, True, 24.0, wait_s=36.0)
        tracker.finish_agv_task("u1_agv_1_0", 60.0)
        summary = tracker.finalize(120.0, stop_reason="Kiểm thử tách chờ Traffic")
        agv = tracker.agvs["u1_agv_1_0"]
        self.assertAlmostEqual(agv["active_pct"], 20.0, places=2)
        self.assertAlmostEqual(agv["wait_pct"], 30.0, places=2)
        self.assertAlmostEqual(agv["pure_idle_pct"], 50.0, places=2)
        self.assertAlmostEqual(agv["active_pct"] + agv["wait_pct"] + agv["pure_idle_pct"], 100.0, places=2)
        self.assertIn("agv_avg_wait_pct", summary)
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_excel(tracker, tmp)
            wb = load_workbook(paths["latest"])
            ws3 = wb["Khu_Unpack_AGV"]
            headers = [c.value for row in ws3.iter_rows(min_row=9, max_row=12, values_only=False) for c in row if c.value is not None]
            self.assertIn("Tỉ lệ chờ Traffic (%)", headers)
            self.assertIn("Tỉ lệ đứng rỗi (%)", headers)

    def test_asrs_sheet_charts_hourly_and_four_lifts(self):
        tracker = SimulationTracker(self.config, total_jobs=10, cycle_duration_s=10800.0)
        # Hour 1 (0..3600s): 3 tasks -> Peak hour
        for idx, (sid, start_s) in enumerate([("shuttle_0", 120.0), ("shuttle_1", 900.0), ("shuttle_2", 2400.0)]):
            tracker.start_shuttle_task(sid, "Nhập kho", f"WH-IN-{idx + 1:04d}", f"pallet_{idx}", idx, f"S10{idx + 1}", start_s)
            tracker.record_shuttle_leg(sid, 20.0, 60.0)
            if sid == "shuttle_2":
                tracker.record_shuttle_fault(sid, "Test sensor glitch")
            tracker.finish_shuttle_task(sid, start_s + 60.0)
        # Hour 2 (3600..7200s): 0 tasks -> Quietest hour
        # Hour 3 (7200..10800s): 1 task
        tracker.start_shuttle_task("shuttle_0", "Xuất kho", "WH-OUT-0001", "pallet_0", 0, "S101", 7500.0)
        tracker.record_shuttle_leg("shuttle_0", 18.0, 120.0)
        tracker.finish_shuttle_task("shuttle_0", 7620.0)

        # Exercise all 4 lifts + 1 fault on out_lift_1
        tracker.start_lift_task("in_lift_0", "Nhập kho", "LF-IN-0001", "pallet_0")
        tracker.record_lift_leg("in_lift_0", 5.0, 30.0)
        tracker.finish_lift_task("in_lift_0")

        tracker.start_lift_task("in_lift_1", "Nhập kho", "LF-IN-0002", "pallet_1")
        tracker.record_lift_leg("in_lift_1", 8.5, 45.0)
        tracker.finish_lift_task("in_lift_1")

        tracker.start_lift_task("out_lift_0", "Xuất kho", "LF-OUT-0001", "pallet_0")
        tracker.record_lift_leg("out_lift_0", 6.2, 36.0)
        tracker.finish_lift_task("out_lift_0")

        tracker.start_lift_task("out_lift_1", "Xuất kho", "LF-OUT-0002", "pallet_1")
        tracker.record_lift_leg("out_lift_1", 4.0, 24.0)
        tracker.record_lift_fault("out_lift_1", "Sensor delay")
        tracker.finish_lift_task("out_lift_1")

        tracker.record_lift_wait("in_lift_0", 12.5)
        tracker.record_lift_wait("out_lift_0", 4.0)
        tracker.record_stage_wait("pickup_to_agv", 3.5)
        tracker.record_stage_wait("agv_to_in_lift", 12.5)
        tracker.record_stage_wait("buffer_to_in_shuttle", 6.0)
        tracker.record_stage_wait("out_req_to_lift", 4.0)
        tracker.record_stage_wait("out_port_to_shuttle", 2.0)

        summary = tracker.finalize(10800.0, stop_reason="Kiểm thử 3 giờ và 4 thang nâng")
        self.assertEqual(summary["lift_inbound_tasks"], 2)
        self.assertEqual(summary["lift_outbound_tasks"], 2)
        self.assertEqual(summary["lift_fault_count"], 1)
        self.assertAlmostEqual(summary["lift_total_wait_s"], 16.5, places=2)
        self.assertEqual(tracker.lifts["out_lift_1"]["status"], "Lỗi")
        self.assertEqual(tracker.lifts["in_lift_0"]["status"], "Hoạt động bình thường")

        with tempfile.TemporaryDirectory() as tmp:
            paths = export_excel(tracker, tmp)
            wb = load_workbook(paths["latest"])
            ws1_text = "\n".join(str(c.value) for row in wb["Tong_Quan"].iter_rows(values_only=False) for c in row if c.value is not None)
            self.assertIn("Thông số Shuttle cài đặt", ws1_text)
            ws = wb["Kho_ASRS_Shuttle"]
            self.assertEqual(len(ws._charts), 2)
            chart_map = {type(c).__name__: c for c in ws._charts}
            self.assertEqual(sorted(chart_map.keys()), ["BarChart", "PieChart"])
            self.assertIn("theo giờ", str(chart_map["BarChart"].title.tx.rich.p[0].r[0].t))
            self.assertIn("Shuttle", str(chart_map["PieChart"].title.tx.rich.p[0].r[0].t))

            text = "\n".join(str(c.value) for row in ws.iter_rows(values_only=False) for c in row if c.value is not None)
            for expected in (
                "THÔNG SỐ SHUTTLE CÀI ĐẶT",
                "shuttle_speed", "shuttle_turn_s", "shuttle_positioning_s", "shuttle_lift_s",
                "Thang Nhập 1", "Thang Nhập 2", "Thang Xuất 1", "Thang Xuất 2",
                "TG hàng chờ Thang", "Chờ TB / lượt (s)", "TG hàng chờ Shuttle",
                "1. Hàng chờ Ô Pickup -> AGV nhận hàng (Nhập)",
                "2. Hàng chờ Thang nâng Nhập (Cổng 1 & 2)",
                "3. Hàng chờ Shuttle chiều Nhập kho",
                "4. Hàng chờ Thang nâng Xuất (Cổng 1 & 2)",
                "5. Hàng chờ Shuttle chiều Xuất kho",
                "LF-IN-0001", "LF-IN-0002", "LF-OUT-0001", "LF-OUT-0002",
                "Giờ 1 (00:00-01:00)", "Giờ 2 (01:00-02:00)", "Giờ 3 (02:00-03:00)",
                "Bận rộn nhất (Cao điểm)", "Rảnh nhất (Thấp điểm)",
                "Trạng thái 10 Shuttle", "Đang hoạt động (Chạy nhiệm vụ)",
                "Chờ Traffic / Khóa tầng", "Nghỉ / Chờ lệnh & Dự phòng",
            ):
                self.assertIn(expected, text)

    def test_eight_hour_150_in_300_out_configuration(self):
        self.assertEqual(self.config["cycle_duration_s"], 28800)
        self.assertEqual(self.config["warehouse_inbound_target"], 150)
        self.assertEqual(self.config["warehouse_outbound_target"], 300)
        self.assertGreaterEqual(
            self.config["initial_outbound_pallets"] + self.config["warehouse_inbound_target"],
            self.config["warehouse_outbound_target"],
        )
        self.assertGreater(self.config["max_stored_inflight"], self.config["initial_outbound_pallets"])
        from cad_navigation import LAYOUT
        n_js = len(LAYOUT.job_slots)
        cells = {((i // n_js + 4) % 9, LAYOUT.job_slots[i % n_js]) for i in range(self.config["initial_outbound_pallets"])}
        self.assertEqual(len(cells), self.config["initial_outbound_pallets"])


if __name__ == "__main__":
    unittest.main()

