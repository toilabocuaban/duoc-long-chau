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
        cfg = {
            **self.config,
            "agv_drain_loaded_pct_per_m": 0.04,
            "agv_drain_empty_pct_per_m": 0.02,
            "agv_drain_idle_pct_per_s": 0.002,
        }
        tracker = SimulationTracker(cfg, total_jobs=10, cycle_duration_s=120.0)

        # Verify initial fleet sizes: 10 shuttles, 2 + 4*(2 + 2 + 6) = 42 pooled AGVs
        self.assertEqual(len(tracker.shuttles), 10)
        self.assertEqual(len(tracker.agvs), 42)
        self.assertIn("u1_agv_1_0", tracker.agvs)
        self.assertIn("u1_agv_4_1", tracker.agvs)
        self.assertNotIn("u1_agv_1_2", tracker.agvs)
        self.assertIn("u2_agv_1_0", tracker.agvs)
        self.assertIn("u2_agv_4_1", tracker.agvs)
        self.assertNotIn("u2_agv_1_2", tracker.agvs)
        self.assertIn("p1_agv_1_0", tracker.agvs)
        self.assertIn("p1_agv_4_5", tracker.agvs)
        self.assertNotIn("p1_agv_1_6", tracker.agvs)

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
            self.assertEqual(len(ws_sh._charts), 3)
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
            agv_sec1_r = next(r for r in range(1, ws_agv_kpi.max_row + 1) if str(ws_agv_kpi.cell(r, 1).value or "").startswith("1. "))
            agv_kpi_text = "\n".join(
                str(c.value)
                for row in ws_agv_kpi.iter_rows(min_row=agv_sec1_r, max_row=agv_sec1_r + 5, values_only=False)
                for c in row if c.value is not None
            )
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
        cfg = {**self.config, "agv_drain_loaded_pct_per_m": 0.04}
        tracker = SimulationTracker(cfg, total_jobs=0, cycle_duration_s=10.0)
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
            headers = {c.value for row in ws3.iter_rows(values_only=False) for c in row if c.value is not None}
            self.assertIn("Tỉ lệ chờ Traffic (%)", headers)
            self.assertIn("Tỉ lệ đứng rỗi (%)", headers)

    def test_asrs_sheet_charts_hourly_and_four_lifts(self):
        tracker = SimulationTracker(self.config, total_jobs=10, cycle_duration_s=10800.0)
        # Hour 1 (0..3600s): 3 tasks -> Peak hour
        for idx, (sid, start_s) in enumerate([("shuttle_0", 120.0), ("shuttle_1", 900.0), ("shuttle_2", 2400.0)]):
            tracker.start_shuttle_task(
                sid, "Nhập kho", f"WH-IN-{idx + 1:04d}", f"pallet_{idx}", idx, f"S10{idx + 1}", start_s,
                lift_id=f"in_lift_{idx % 2}", lift_wait_s=4.5, process_start_s=start_s - 4.5,
            )
            tracker.record_shuttle_leg(sid, 20.0, 60.0)
            if sid == "shuttle_2":
                tracker.record_shuttle_fault(sid, "Test sensor glitch")
            tracker.finish_shuttle_task(sid, start_s + 60.0)
        # Hour 2 (3600..7200s): 0 tasks -> Quietest hour
        # Hour 3 (7200..10800s): 1 task (end-to-end outbound process test)
        out_rec = tracker.start_shuttle_task(
            "shuttle_0", "Xuất kho", "WH-OUT-0001", "pallet_0", 0, "S101", 7520.0,
            lift_id="out_lift_0", lift_wait_s=20.0, process_start_s=7500.0, keep_process_open=True,
        )
        tracker.record_shuttle_leg("shuttle_0", 18.0, 120.0)
        tracker.finish_shuttle_task("shuttle_0", 7640.0)
        tracker.finish_warehouse_process(out_rec, 7690.0, extra_lift_wait_s=5.0)
        self.assertAlmostEqual(out_rec["lift_wait_s"], 25.0, places=2)
        self.assertAlmostEqual(out_rec["duration_s"], 190.0, places=2)

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
            self.assertIn("Thông số Thang nâng cài đặt", ws1_text)
            ws = wb["Kho_ASRS_Shuttle"]
            self.assertEqual(len(ws._charts), 3)
            chart_types = sorted(type(c).__name__ for c in ws._charts)
            self.assertEqual(chart_types, ["BarChart", "BarChart", "PieChart"])
            bar_charts = [c for c in ws._charts if type(c).__name__ == "BarChart"]
            pie_chart = next(c for c in ws._charts if type(c).__name__ == "PieChart")
            self.assertIn("theo giờ", str(bar_charts[0].title.tx.rich.p[0].r[0].t))
            self.assertIn("Đang sạc", str(bar_charts[1].title.tx.rich.p[0].r[0].t))
            self.assertEqual(len(bar_charts[1].series), 4)
            self.assertEqual(bar_charts[1].y_axis.scaling.min, 0)
            self.assertEqual(bar_charts[1].y_axis.scaling.max, 100)
            self.assertIn("9 Shuttle", str(pie_chart.title.tx.rich.p[0].r[0].t))
            self.assertEqual(pie_chart.legend.position, "b")

            # Verify merged Section 0 input table starts at Row 4 with 3 charts at top (E5, H5, E{sec1_row})
            sec0_row = next(
                r for r in range(1, ws.max_row + 1)
                if str(ws.cell(r, 1).value or "").startswith("0. TỔNG HỢP THÔNG SỐ ĐẦU VÀO")
            )
            sec1_row = next(
                r for r in range(1, ws.max_row + 1)
                if str(ws.cell(r, 1).value or "").startswith("1. TỔNG HỢP HOẠT ĐỘNG")
            )
            sec2_row = next(
                r for r in range(1, ws.max_row + 1)
                if str(ws.cell(r, 1).value or "").startswith("2. THÔNG SỐ CÀI ĐẶT")
            )
            self.assertEqual(sec0_row, 4)
            self.assertLess(sec0_row, sec1_row)
            self.assertLess(sec1_row, sec2_row)
            self.assertEqual((bar_charts[0].anchor._from.col, bar_charts[0].anchor._from.row), (5, 4))
            self.assertEqual((pie_chart.anchor._from.col, pie_chart.anchor._from.row), (8, 4))
            self.assertEqual((bar_charts[1].anchor._from.col, bar_charts[1].anchor._from.row), (5, sec1_row - 1))

            # Verify merged Section 0 input rows (5 cols: Nhóm, Chỉ tiêu, Giá trị, Đơn vị, Thực tế & Diễn giải)
            self.assertEqual(
                [ws.cell(sec0_row + 1, c).value for c in range(1, 6)],
                ["Nhóm thông số", "Chỉ tiêu đầu vào", "Giá trị", "Đơn vị", "Thực tế & Diễn giải"],
            )
            sec0_rows_by_lbl = {
                ws.cell(r, 2).value: r
                for r in range(sec0_row + 2, sec1_row)
                if ws.cell(r, 2).value
            }
            self.assertEqual(ws.cell(sec0_rows_by_lbl["Số lượng hàng nhập kế hoạch / ca"], 3).value, 150)
            self.assertAlmostEqual(ws.cell(sec0_rows_by_lbl["Trung bình tần suất nhập trong 1 giờ"], 3).value, 18.75, places=2)
            self.assertEqual(ws.cell(sec0_rows_by_lbl["Số lượng hàng xuất kế hoạch / ca"], 3).value, 300)
            self.assertAlmostEqual(ws.cell(sec0_rows_by_lbl["Trung bình tần suất xuất trong 1 giờ"], 3).value, 37.5, places=2)
            r_shuttle_start = sec0_rows_by_lbl["Số lượng Shuttle (9 chính + 1 Standby)"]
            self.assertEqual(ws.cell(r_shuttle_start, 3).value, 10)
            self.assertEqual(ws.cell(r_shuttle_start, 4).value, "xe")
            for c_hl in range(1, 6):
                self.assertEqual(ws.cell(r_shuttle_start, c_hl).fill.start_color.rgb, "00FFE082")
            self.assertIn("Thời gian hoạt động liên tục (cấu hình pin)", sec0_rows_by_lbl)
            self.assertAlmostEqual(ws.cell(sec0_rows_by_lbl["Thời gian hoạt động liên tục (cấu hình pin)"], 3).value, 7.0, places=2)
            self.assertIn("Thời gian sạc Shuttle (cấu hình)", sec0_rows_by_lbl)
            r_sh_chg = sec0_rows_by_lbl["Thời gian sạc Shuttle (cấu hình)"]
            self.assertAlmostEqual(ws.cell(r_sh_chg, 3).value, 2.0, places=2)
            self.assertEqual(ws.cell(r_sh_chg, 4).value, "giờ")
            self.assertIn("120 phút", str(ws.cell(r_sh_chg, 5).value))
            r_lift_start = sec0_rows_by_lbl["Số lượng Thang Nhập"]
            self.assertEqual(ws.cell(r_lift_start, 3).value, 2)
            self.assertEqual(ws.cell(r_lift_start, 4).value, "thang")
            for c_hl in range(1, 6):
                self.assertEqual(ws.cell(r_lift_start, c_hl).fill.start_color.rgb, "00A5D6A7")
            self.assertEqual(ws.cell(sec0_rows_by_lbl["Số lượng Thang Xuất"], 3).value, 2)
            self.assertAlmostEqual(ws.cell(sec0_rows_by_lbl["Tần suất hoạt động TB (4 thang / giờ)"], 3).value, 56.25, places=2)
            self.assertIn("Thời gian chờ nhiệm vụ (Nghỉ rảnh)", sec0_rows_by_lbl)

            # Verify Section 1 Cách A headers, separated metric rows, and 3-state breakdown
            self.assertEqual(
                [ws.cell(sec1_row + 1, c).value for c in range(1, 5)],
                ["Nhóm thiết bị", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
            )
            sec1_metrics = {ws.cell(r, 2).value: ws.cell(r, 3).value for r in range(sec1_row + 2, sec2_row - 1)}
            for required_label in (
                "Tổng thời gian vận hành kho (1x chuẩn)",
                "Tổng nhiệm vụ Shuttle - Nhập kho",
                "Tổng nhiệm vụ Shuttle - Xuất kho",
                "Tổng quãng đường 10 Shuttle",
                "Tỉ lệ TB Shuttle hoạt động",
                "Tỉ lệ TB Shuttle có lệnh chờ hàng",
                "Tỉ lệ TB Shuttle nghỉ rảnh",
                "Tổng thời gian hàng chờ Shuttle",
                "Tổng số lỗi Shuttle",
                "Tổng hàng qua 4 Thang nâng - Nhập kho",
                "Tổng hàng qua 4 Thang nâng - Xuất kho",
                "Tỉ lệ TB 4 Thang nâng hoạt động",
                "Tỉ lệ TB 4 Thang nâng có lệnh chờ hàng",
                "Tỉ lệ TB 4 Thang nâng nghỉ rảnh",
                "Tổng thời gian hàng chờ Thang nâng",
                "Tổng số lỗi Thang nâng",
            ):
                self.assertIn(required_label, sec1_metrics)
            self.assertEqual(sec1_metrics["Tổng nhiệm vụ Shuttle - Nhập kho"], 3)
            self.assertEqual(sec1_metrics["Tổng nhiệm vụ Shuttle - Xuất kho"], 1)
            self.assertEqual(sec1_metrics["Tổng hàng qua 4 Thang nâng - Nhập kho"], 2)
            self.assertEqual(sec1_metrics["Tổng hàng qua 4 Thang nâng - Xuất kho"], 2)
            self.assertEqual(sec1_metrics["Tổng số lỗi Shuttle"], 1)
            self.assertEqual(sec1_metrics["Tổng số lỗi Thang nâng"], 1)
            for k_lbl, val in sec1_metrics.items():
                self.assertNotIn("|", str(val))
                if k_lbl != "Tổng thời gian vận hành kho (1x chuẩn)":
                    self.assertNotIn("/", str(val))

            # Verify Section 2 banner stops at D (no merge across E..J behind charts) and Section 3 starts below Chart 3
            sec2_merges = [rng for rng in ws.merged_cells.ranges if rng.min_row == sec2_row and rng.max_row == sec2_row]
            self.assertEqual(len(sec2_merges), 1)
            self.assertEqual((sec2_merges[0].min_col, sec2_merges[0].max_col), (1, 4))
            sec3_row = next(
                r for r in range(1, ws.max_row + 1)
                if str(ws.cell(r, 1).value or "").startswith("3. BẢNG TỔNG HỢP")
            )
            self.assertGreaterEqual(sec3_row, sec2_row + 13)

            text = "\n".join(str(c.value) for row in ws.iter_rows(values_only=False) for c in row if c.value is not None)
            for expected in (
                "THÔNG SỐ CÀI ĐẶT SHUTTLE & THANG NÂNG",
                "shuttle_speed", "lift_speed", "lift_settle_s", "80.0% / 80.0%",
                "Thang Nhập 1", "Thang Nhập 2", "Thang Xuất 1", "Thang Xuất 2",
                "TG hàng chờ (cộng dồn)", "Chờ TB / lượt (s)",
                "TG có lệnh chờ hàng", "TG nghỉ rảnh",
                "1. Hàng chờ Ô Pickup -> AGV nhận hàng (Nhập)",
                "2. Hàng chờ Thang nâng Nhập (Cổng 1 & 2)",
                "3. Hàng chờ Shuttle chiều Nhập kho",
                "4. Hàng chờ Thang nâng Xuất (Cổng 1 & 2)",
                "5. Hàng chờ Shuttle chiều Xuất kho",
                "LF-IN-0001", "LF-IN-0002", "LF-OUT-0001", "LF-OUT-0002",
                "Giờ 1 (00:00-01:00)", "Giờ 2 (01:00-02:00)", "Giờ 3 (02:00-03:00)",
                "Bận rộn nhất (Cao điểm)", "Rảnh nhất (Thấp điểm)",
                "Trạng thái 9 Shuttle chính", "Đang hoạt động (Chạy nhiệm vụ)",
                "Có lệnh - Chờ hàng / Bàn đệm", "Nghỉ rảnh (Chưa có lệnh)",
                "Thiết bị Kho AS/RS", "Đang hoạt động (%)", "Tỉ lệ chờ hàng/lệnh (%)", "Tỉ lệ nghỉ rảnh (%)",
                "Mã quy trình", "Mã Thang nâng", "Mã Shuttle",
                "Thời điểm bắt đầu", "TG hàng đợi Thang nâng", "TG hàng đợi Shuttle",
                "Thời điểm kết thúc", "Tổng TG quy trình",
                "Đang sạc (%)", "TG sạc (phút)", "Pin còn lại (%)",
            ):
                self.assertIn(expected, text)

            # Verify removed columns from Section 4 (4 Lifts)
            for row_vals in ws.iter_rows(values_only=True):
                if row_vals and row_vals[0] == "Mã Thang nâng":
                    hdrs = [v for v in row_vals if v is not None]
                    self.assertNotIn("Số hàng nhập", hdrs)
                    self.assertNotIn("Số hàng xuất", hdrs)
                    break

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
        cells = {((i + 4) % 9, LAYOUT.job_slots[(i // 9) % n_js]) for i in range(self.config["initial_outbound_pallets"])}
        self.assertEqual(len(cells), self.config["initial_outbound_pallets"])

    def test_all_remaining_sheets_match_asrs_dashboard_style(self):
        tracker = SimulationTracker(self.config, total_jobs=10, cycle_duration_s=10800.0)
        tracker.record_event({"job": 0, "state": "INBOUND_ARRIVAL", "demo_time_s": 0.0, "dock": 4})
        tracker.record_event({"loose": True, "state": "TRUCK_BATCH_READY", "demo_time_s": 1.0, "dock": 4, "classification": "even", "batch": 0, "cartons": 18})
        tracker.record_event({"loose": True, "state": "DOCK_PALLET_READY", "demo_time_s": 15.0, "dock": 4, "classification": "even", "batch": 0})
        tracker.record_event({"loose": True, "state": "DOCK_PALLET_READY", "demo_time_s": 25.0, "dock": 5, "classification": "odd", "batch": 1})
        tracker.record_event({"loose": True, "state": "CARTON_PLACED_ON_RACK", "demo_time_s": 40.0, "floor": 1, "branch": 0, "med_boxes": ["m0", "m1"]})
        tracker.record_worker_activity("u1_1_0", duration_s=12.0)

        tracker.start_agv_task("agv_0", "DK-AGV-0001", "Khu Dock -> Kho 01", "Chở pallet", 20.0, pallet_or_item="pallet_0", dock_id=4)
        tracker.record_agv_leg("agv_0", 40.0, True, 25.0, wait_s=5.0)
        tracker.finish_agv_task("agv_0", 50.0)
        tracker.start_agv_task("u1_agv_1_0", "UNP-U1R-T1N1-0001", "Khu Unpack U1", "Cất rack", 3700.0, pallet_or_item="u1_rack_1_0_0")
        tracker.record_agv_leg("u1_agv_1_0", 30.0, True, 20.0, wait_s=4.0)
        tracker.finish_agv_task("u1_agv_1_0", 3724.0)

        tracker.finalize(10800.0, stop_reason="Kiểm thử đồng bộ 6 sheet")
        with tempfile.TemporaryDirectory() as tmp:
            wb = load_workbook(export_excel(tracker, tmp)["latest"])
            expected_charts = {
                "Tong_Quan": 4,
                "Kho_ASRS_Shuttle": 3,
                "Khu_Unpack_AGV": 4,
                "Khu_Vuc_Dock": 3,
                "Cong_Nhan_Unpack": 3,
                "Chi_Tiet_Nhiem_Vu_AGV_Kho": 2,
            }
            for sname, chart_cnt in expected_charts.items():
                ws = wb[sname]
                self.assertEqual(len(ws._charts), chart_cnt, f"{sname} chart count mismatch")
                # All 6 sheets use unified 5-column Section 0 + right chart panel banner
                sec0_cols = 5
                expected_r4 = [(1, 5), (6, 11)]
                r4_merges = sorted((m.min_col, m.max_col) for m in ws.merged_cells.ranges if m.min_row == 4 and m.max_row == 4)
                self.assertEqual(r4_merges, expected_r4, f"{sname} row 4 banner mismatch")
                self.assertTrue(str(ws.cell(4, 1).value or "").startswith("0. "), f"{sname} row 4 should start with '0. '")
                # Row 5 has Section 0 input headers (Giá trị at Col 3, Đơn vị at Col 4, Thực tế & Diễn giải at Col 5)
                self.assertEqual(ws.cell(5, 2).value, "Chỉ tiêu đầu vào", f"{sname} row 5 col 2 header mismatch")
                self.assertEqual(ws.cell(5, 3).value, "Giá trị")
                self.assertEqual(ws.cell(5, 4).value, "Đơn vị")
                self.assertEqual(ws.cell(5, 5).value, "Thực tế & Diễn giải")
                top_anchors = [(ch.anchor._from.col, ch.anchor._from.row) for ch in ws._charts[:2]]
                self.assertEqual(top_anchors, [(5, 4), (8, 4)], f"{sname} top charts anchor mismatch")
                # All stacked BarCharts must be bounded 0..100%
                for ch in ws._charts:
                    if type(ch).__name__ == "BarChart" and ch.grouping == "stacked":
                        self.assertEqual(ch.y_axis.scaling.min, 0, f"{sname} stacked min != 0")
                        self.assertEqual(ch.y_axis.scaling.max, 100, f"{sname} stacked max != 100")
                # Section 0 must include both yellow (#FFE082) and green (#A5D6A7) sub-block start rows, with no '|' in Col 3
                sec0_fills = set()
                r_cur = 6
                while ws.cell(r_cur, 1).value not in (None, ""):
                    val = ws.cell(r_cur, 3).value
                    self.assertNotIn("|", str(val), f"{sname} Section 0 row {r_cur} packed '|' in value")
                    rgb = ws.cell(r_cur, 1).fill.start_color.rgb
                    if rgb in ("00FFE082", "00A5D6A7"):
                        sec0_fills.add(rgb)
                        for c_hl in range(1, sec0_cols + 1):
                            self.assertEqual(ws.cell(r_cur, c_hl).fill.start_color.rgb, rgb)
                    r_cur += 1
                self.assertEqual(sec0_fills, {"00FFE082", "00A5D6A7"}, f"{sname} missing yellow/green Section 0 start rows")
                # Section 1 KPI rows keep clean single values without "|" packing
                sec1_r = next(r for r in range(1, ws.max_row + 1) if str(ws.cell(r, 1).value or "").startswith("1. "))
                self.assertEqual(ws.cell(sec1_r + 1, 2).value, "Chỉ tiêu thống kê", f"{sname} Section 1 header mismatch")
                r_kpi = sec1_r + 2
                while ws.cell(r_kpi, 1).value not in (None, ""):
                    val = ws.cell(r_kpi, 3).value
                    self.assertNotIn("|", str(val), f"{sname} Section 1 row {r_kpi} packed '|' in KPI value")
                    r_kpi += 1

            # Verify AGV log sheet includes hourly buckets up to Giờ 3
            ws_log_text = "\n".join(str(c.value) for r in wb["Chi_Tiet_Nhiem_Vu_AGV_Kho"].iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("Giờ 1 (00:00-01:00)", ws_log_text)
            self.assertIn("Giờ 3 (02:00-03:00)", ws_log_text)

            # Verify 3-state headers across Dock & Worker sheets, dynamic 12-carton trolley capacity, and Chart 3 alignment on Sheets 1..5
            ws_dock_hdrs = {c.value for r in wb["Khu_Vuc_Dock"].iter_rows(values_only=False) for c in r if c.value is not None}
            self.assertIn("Tỉ lệ chờ ô & AGV (%)", ws_dock_hdrs)
            self.assertIn("Tỉ lệ nghỉ rảnh (%)", ws_dock_hdrs)
            ws_wrk_text = "\n".join(str(c.value) for r in wb["Cong_Nhan_Unpack"].iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("Tỉ lệ chờ hàng/AGV (%)", ws_wrk_text)
            self.assertIn("Tỉ lệ nghỉ rảnh (%)", ws_wrk_text)
            self.assertIn("20 thùng/Trolley", ws_wrk_text)
            self.assertIn("8 khay/Rack", ws_wrk_text)
            self.assertNotIn("5 thùng/Trolley", ws_wrk_text)
            for sname in ("Tong_Quan", "Kho_ASRS_Shuttle", "Khu_Unpack_AGV", "Khu_Vuc_Dock", "Cong_Nhan_Unpack"):
                ws_s = wb[sname]
                sec1_r = next(r for r in range(1, ws_s.max_row + 1) if str(ws_s.cell(r, 1).value or "").startswith("1. "))
                sec2_r = next(r for r in range(1, ws_s.max_row + 1) if str(ws_s.cell(r, 1).value or "").startswith("2. "))
                sec3_r = next(r for r in range(1, ws_s.max_row + 1) if str(ws_s.cell(r, 1).value or "").startswith("3. "))
                self.assertGreater(sec2_r, sec1_r, f"{sname} sec2 should follow sec1")
                self.assertGreater(sec3_r, sec2_r, f"{sname} sec3 should follow sec2")
                self.assertEqual((ws_s._charts[2].anchor._from.col, ws_s._charts[2].anchor._from.row), (5, sec1_r - 1))
                if len(ws_s._charts) == 4:
                    self.assertEqual((ws_s._charts[3].anchor._from.col, ws_s._charts[3].anchor._from.row), (8, sec1_r - 1))

    def test_three_state_interval_separation_and_dock_waits(self):
        tracker = SimulationTracker(self.config, total_jobs=4, cycle_duration_s=100.0)
        # Concurrent overlapping waits across two pallets on out_lift_0 (cumulative wait = 80s, merged = 50s):
        # Active leg spans [20, 30] and [60, 70] -> busy = 20s (20%), cmd_wait = 50s (50% = [0..20]U[30..60]), pure_idle = 30s (30%).
        tracker.start_lift_task("out_lift_0", "Xuất kho", "LF-OUT-0001", "pallet_0")
        tracker.record_lift_leg("out_lift_0", 5.0, 10.0, t_start=20.0, t_end=30.0)
        tracker.record_lift_leg("out_lift_0", 5.0, 10.0, t_start=60.0, t_end=70.0)
        tracker.record_lift_wait("out_lift_0", 40.0)
        tracker.record_lift_wait("out_lift_0", 40.0)
        tracker.record_lift_wait_span("out_lift_0", 0.0, 40.0)
        tracker.record_lift_wait_span("out_lift_0", 20.0, 60.0)
        tracker.finish_lift_task("out_lift_0")

        # Shuttle overlapping wait spans + active leg:
        tracker.start_shuttle_task("shuttle_0", "Xuất kho", "WH-OUT-0001", "pallet_0", 0, "S101", 25.0)
        tracker.record_shuttle_leg("shuttle_0", 12.0, 15.0, t_start=25.0, t_end=40.0)
        tracker.record_shuttle_wait_span("shuttle_0", 10.0, 30.0)  # overlaps active [25..40] -> wait only [10..25] = 15s
        tracker.record_shuttle_wait_span("shuttle_0", 70.0, 85.0)  # +15s -> total cmd_wait = 30s
        tracker.record_shuttle_wait("shuttle_0", 35.0)
        tracker.finish_shuttle_task("shuttle_0", 40.0)

        # Worker (P1) active + rack swap wait spans:
        tracker.record_worker_activity("p1_1_0", duration_s=25.0, cartons=2, med_boxes=6, t_start=0.0, t_end=25.0)
        tracker.record_worker_wait_span("p1_1_0", 25.0, 65.0)

        # Dock with batch [10, 60] containing DOCK_SLOT_WAIT [20, 35] and DOCK_GATE_WAIT [40, 50] -> busy = 25s, wait = 25s, idle = 50s
        tracker.record_event({"demo_time_s": 5.0, "state": "INBOUND_ARRIVAL", "job": 0, "dock": 4})
        tracker.record_event({"loose": True, "demo_time_s": 10.0, "state": "TRUCK_BATCH_READY", "dock": 4, "classification": "staged", "batch": 0, "cartons": 18})
        tracker.record_event({"loose": True, "demo_time_s": 35.0, "state": "DOCK_SLOT_WAIT", "dock": 4, "classification": "staged", "batch": 0, "wait_s": 15.0, "start_s": 20.0, "end_s": 35.0})
        tracker.record_event({"loose": True, "demo_time_s": 35.0, "state": "DOCK_PALLET_READY", "dock": 4, "classification": "even", "batch": 0, "cartons": 18})
        tracker.record_event({"loose": True, "demo_time_s": 50.0, "state": "DOCK_GATE_WAIT", "dock": 4, "classification": "even", "batch": 0, "wait_s": 10.0, "start_s": 40.0, "end_s": 50.0})
        tracker.record_event({"loose": True, "demo_time_s": 60.0, "state": "EMPTY_PALLET_FROM_POOL", "dock": 4, "batch": 0})

        summary = tracker.finalize(100.0, stop_reason="Kiểm thử tách 3 trạng thái")

        lf0 = tracker.lifts["out_lift_0"]
        self.assertAlmostEqual(lf0["wait_s"], 80.0, places=2)
        self.assertAlmostEqual(lf0["busy_s"], 20.0, places=2)
        self.assertAlmostEqual(lf0["cmd_wait_s"], 50.0, places=2)
        self.assertAlmostEqual(lf0["pure_idle_s"], 30.0, places=2)
        self.assertAlmostEqual(lf0["active_pct"] + lf0["cmd_wait_pct"] + lf0["pure_idle_pct"], 100.0, places=2)

        sh0 = tracker.shuttles["shuttle_0"]
        self.assertAlmostEqual(sh0["busy_s"], 15.0, places=2)
        self.assertAlmostEqual(sh0["cmd_wait_s"], 30.0, places=2)
        self.assertAlmostEqual(sh0["pure_idle_s"], 55.0, places=2)
        self.assertAlmostEqual(sh0["active_pct"] + sh0["cmd_wait_pct"] + sh0["pure_idle_pct"], 100.0, places=2)

        w_p1 = tracker.workers["p1_1_0"]
        self.assertAlmostEqual(w_p1["active_pct"], 25.0, places=2)
        self.assertAlmostEqual(w_p1["cmd_wait_pct"], 40.0, places=2)
        self.assertAlmostEqual(w_p1["pure_idle_pct"], 35.0, places=2)

        d4 = tracker.docks[4]
        w_d4 = tracker.workers["dock_4"]
        for rec in (d4, w_d4):
            self.assertAlmostEqual(rec["busy_s"], 25.0, places=2)
            self.assertAlmostEqual(rec["cmd_wait_s"], 25.0, places=2)
            self.assertAlmostEqual(rec["pure_idle_s"], 50.0, places=2)
            self.assertAlmostEqual(rec["active_pct"] + rec["cmd_wait_pct"] + rec["pure_idle_pct"], 100.0, places=2)

        self.assertIn("shuttle_avg_cmd_wait_pct", summary)
        self.assertIn("worker_avg_cmd_wait_pct", summary)
        with tempfile.TemporaryDirectory() as tmp:
            wb = load_workbook(export_excel(tracker, tmp)["latest"])
            ws_ov = wb["Tong_Quan"]
            ov_text = "\n".join(str(c.value) for r in ws_ov.iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("Có lệnh - Chờ hàng / Traffic (%)", ov_text)
            self.assertIn("Nghỉ rảnh (%)", ov_text)

    def test_pooled_42_agvs_and_z05_partial_return_excel(self):
        tracker = SimulationTracker(self.config, total_jobs=10, cycle_duration_s=120.0)
        self.assertEqual(len(tracker.agvs), 42)

        # 2 full-unpack outbound pulls + 1 partial-return outbound pull + 1 return push-back = 3 total outbound
        for idx, (pid, lcode, wcode) in enumerate((
            ("pallet_0", "LF-OUT-0001", "WH-OUT-0001"),
            ("pallet_2", "LF-OUT-0002", "WH-OUT-0002"),
        )):
            tracker.start_lift_task("out_lift_0", "Xuất kho", lcode, pid)
            tracker.record_lift_leg("out_lift_0", 6.0, 5.0)
            tracker.finish_lift_task("out_lift_0")
            tracker.start_shuttle_task("shuttle_0", "Xuất kho", wcode, pid, 0, f"S10{idx+1}", 10.0 + idx * 10.0)
            tracker.record_shuttle_leg("shuttle_0", 12.0, 8.0)
            tracker.finish_shuttle_task("shuttle_0", 18.0 + idx * 10.0)

        tracker.start_lift_task("out_lift_1", "Xuất tạm hoàn Z05", "LF-OUT-0003", "pallet_1")
        tracker.record_lift_leg("out_lift_1", 6.0, 5.0)
        tracker.finish_lift_task("out_lift_1")
        tracker.start_shuttle_task("shuttle_1", "Xuất tạm hoàn Z05", "WH-OUT-0003", "pallet_1", 1, "S205", 20.0)
        tracker.record_shuttle_leg("shuttle_1", 12.0, 8.0)
        tracker.finish_shuttle_task("shuttle_1", 28.0)

        tracker.record_event({"loose": True, "state": "Z05_JACK_PULL_FROM_GATE", "demo_time_s": 5.0, "floor": 1, "worker": 0, "job": 1})
        tracker.record_event({"loose": True, "state": "Z05_PALLET_PARTIAL_RETURN", "demo_time_s": 18.0, "floor": 1, "worker": 0, "job": 1, "unpacked_cartons": 12, "remaining_cartons": 6})
        tracker.record_event({"loose": True, "state": "PALLET_RETURNED_TO_STORAGE", "demo_time_s": 40.0, "floor": 1, "level": 2, "slot": "S205", "pallet_id": "pallet_1", "remaining_cartons": 6})

        tracker.start_lift_task("out_lift_0", "Hoàn kho (Hàng dư Z05)", "LF-RET-0001", "pallet_1")
        tracker.record_lift_leg("out_lift_0", 6.0, 5.0)
        tracker.finish_lift_task("out_lift_0")

        tracker.start_shuttle_task(
            "shuttle_1", "Hoàn kho (Hàng dư Z05)", "WH-RET-0001", "pallet_1", 1, "S205", 25.0,
            job_id=1, pick_location="Bàn đệm Xuất 1 (T1)", drop_location="Ô kệ S205 (T2)",
        )
        tracker.record_shuttle_leg("shuttle_1", 18.0, 12.0)
        tracker.finish_shuttle_task("shuttle_1", 37.0)

        p1_code = tracker.next_agv_code("UNP-P1R-T2S01")
        tracker.start_agv_task("p1_agv_2_3", p1_code, "Khu Đóng Gói P1", "Cấp rack 4 khay đầy", 45.0, pallet_or_item="p1_rack_2_0")
        tracker.record_agv_leg("p1_agv_2_3", 12.0, True, 8.0)
        tracker.finish_agv_task("p1_agv_2_3", 53.0)

        summary = tracker.finalize(120.0, stop_reason="Kiểm thử 42 AGV + hoàn kho Z05")
        self.assertEqual(len(tracker.agvs), 42)
        self.assertEqual(summary["agv_p1_tasks"], 1)
        self.assertEqual(summary["z05_partial_returns"], 1)
        self.assertEqual(summary["warehouse_outbound_tasks"], 3)
        self.assertEqual(summary["warehouse_outbound_full_tasks"], 2)
        self.assertEqual(summary["warehouse_outbound_partial_tasks"], 1)
        self.assertEqual(summary["lift_outbound_tasks"], 3)
        self.assertEqual(summary["lift_outbound_full_tasks"], 2)
        self.assertEqual(summary["lift_outbound_partial_tasks"], 1)
        self.assertEqual(summary["warehouse_return_tasks"], 1)
        self.assertEqual(summary["lift_return_tasks"], 1)
        self.assertEqual(tracker.shuttles["shuttle_1"]["partial_out_count"], 1)
        self.assertEqual(tracker.shuttles["shuttle_1"]["return_count"], 1)
        self.assertEqual(tracker.lifts["out_lift_1"]["partial_out_count"], 1)
        self.assertEqual(tracker.lifts["out_lift_0"]["return_count"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            wb = load_workbook(export_excel(tracker, tmp)["latest"])
            ov_text = "\n".join(str(c.value) for r in wb["Tong_Quan"].iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("2 unpack hết", ov_text)
            self.assertIn("1 hoàn kho", ov_text)

            ws_sh = wb["Kho_ASRS_Shuttle"]
            sh_text = "\n".join(str(c.value) for r in ws_sh.iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("Hoàn kho (Hàng dư Z05)", sh_text)
            self.assertIn("2 unpack hết & 1 hoàn kho", sh_text)
            sh_sec1_vals = {
                ws_sh.cell(r, 2).value: ws_sh.cell(r, 3).value
                for r in range(1, ws_sh.max_row + 1)
                if ws_sh.cell(r, 2).value in ("Tổng nhiệm vụ Shuttle - Xuất kho", "Tổng hàng qua 4 Thang nâng - Xuất kho")
            }
            self.assertEqual(sh_sec1_vals["Tổng nhiệm vụ Shuttle - Xuất kho"], 3)
            self.assertEqual(sh_sec1_vals["Tổng hàng qua 4 Thang nâng - Xuất kho"], 3)

            agv_text = "\n".join(str(c.value) for r in wb["Khu_Unpack_AGV"].iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("P1-AGV-T2-S04", agv_text)
            self.assertIn("Đội P1-Tây-1", agv_text)
            wrk_text = "\n".join(str(c.value) for r in wb["Cong_Nhan_Unpack"].iter_rows(values_only=False) for c in r if c.value is not None)
            self.assertIn("Kéo pallet từ Thang Xuất về Z05", wrk_text)
            self.assertIn("2 unpack hết & 1 hoàn kho", wrk_text)
            self.assertNotIn("F4", wrk_text)

    def test_agv_and_forklift_duty_and_charge_specs(self):
        self.assertEqual(self.config["agv_continuous_hours_min"], 6.0)
        self.assertEqual(self.config["agv_continuous_hours_max"], 8.0)
        self.assertEqual(self.config["agv_charge_hours_min"], 1.5)
        self.assertEqual(self.config["agv_charge_hours_max"], 2.0)
        self.assertEqual(self.config["agv_forklift_continuous_hours_min"], 6.0)
        self.assertEqual(self.config["agv_forklift_continuous_hours_max"], 8.0)
        self.assertEqual(self.config["agv_forklift_charge_hours_min"], 2.0)
        self.assertEqual(self.config["agv_forklift_charge_hours_max"], 3.0)

        tracker = SimulationTracker(self.config, total_jobs=4, cycle_duration_s=28800.0)
        # Simulate agv_0 (Forklift) actively charging at stop, and u1_agv_1_0 (Standard AGV) finishing a 1.75h charge
        tracker.start_agv_task("u1_agv_1_0", "UNP-U1R-T1N1-0001", "Khu Unpack U1", "Cất rack", 10.0)
        tracker.record_agv_leg("u1_agv_1_0", 500.0, True, 200.0)
        tracker.finish_agv_task("u1_agv_1_0", 210.0)
        tracker.start_agv_charge("u1_agv_1_0", 21600.0, 27900.0, is_forklift=False)  # 1.75h charge
        tracker.finish_agv_charge("u1_agv_1_0", 27900.0)

        tracker.start_agv_charge("agv_0", 23400.0, 31500.0, is_forklift=True)  # 2.25h forklift charge still active at 28800s

        summary = tracker.finalize(28800.0, stop_reason="Kiểm thử pin & sạc AGV + AGV Forklift")
        self.assertEqual(summary["agv_total_charges"], 2)
        self.assertEqual(tracker.agvs["agv_0"]["status"], "Đang sạc (2.2h)")
        self.assertGreater(tracker.agvs["u1_agv_1_0"]["battery_remaining_pct"], 95.0)

        with tempfile.TemporaryDirectory() as tmp:
            wb = load_workbook(export_excel(tracker, tmp)["latest"])
            for sname, expected_snippets in (
                ("Tong_Quan", ("6 - 8h / Sạc 1.5 - 2h", "6 - 8h / Sạc 2 - 3h")),
                ("Khu_Unpack_AGV", ("6 - 8h / Sạc 1.5 - 2h", "6 - 8h / Sạc 2 - 3h", "6 - 8 giờ / Sạc 1.5 - 2 giờ", "6 - 8 giờ / Sạc 2 - 3 giờ", "Đang sạc (2.2h)")),
                ("Khu_Vuc_Dock", ("Chạy 6 - 8h / Sạc 2 - 3h", "6 - 8 giờ / Sạc 2 - 3 giờ")),
                ("Chi_Tiet_Nhiem_Vu_AGV_Kho", ("6 - 8h (Sạc 1.5 - 2h / 2 - 3h)",)),
            ):
                txt = "\n".join(str(c.value) for r in wb[sname].iter_rows(values_only=False) for c in r if c.value is not None)
                for snippet in expected_snippets:
                    self.assertIn(snippet, txt, f"{sname} missing '{snippet}'")


if __name__ == "__main__":
    unittest.main()

