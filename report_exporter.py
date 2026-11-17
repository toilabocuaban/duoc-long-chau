"""Simulation KPI telemetry tracker and formatted Excel (.xlsx) report exporter."""
from __future__ import annotations

import copy
import os
from datetime import datetime
import math
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.drawing.text import CharacterProperties
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import loose_goods as LG
import truck_scene

THIN_BORDER = Border(
    left=Side(style="thin", color="D5DCE4"),
    right=Side(style="thin", color="D5DCE4"),
    top=Side(style="thin", color="D5DCE4"),
    bottom=Side(style="thin", color="D5DCE4"),
)
FILL_TITLE = PatternFill("solid", fgColor="102A43")
FILL_SECTION = PatternFill("solid", fgColor="243B53")
FILL_HEADER = PatternFill("solid", fgColor="1B4F72")
FILL_ZEBRA = PatternFill("solid", fgColor="F4F7FA")
FILL_TOTAL = PatternFill("solid", fgColor="E6EFF8")
FILL_OK = PatternFill("solid", fgColor="E3F7EB")
FILL_WARN = PatternFill("solid", fgColor="FFF4DB")
FILL_ERR = PatternFill("solid", fgColor="FADBD8")
FILL_STANDBY = PatternFill("solid", fgColor="EEF2F6")

FONT_TITLE = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
FONT_SUBTITLE = Font(name="Calibri", size=10, italic=True, color="334E68")
FONT_SECTION = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
FONT_HEADER = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
FONT_BODY = Font(name="Calibri", size=10, color="102A43")
FONT_BOLD = Font(name="Calibri", size=10, bold=True, color="102A43")

ALIGN_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
ALIGN_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")

FMT_HMS = "[h]:mm:ss"
FMT_HMS_TENTH = "[h]:mm:ss.0"


def _excel_days(seconds: float | None) -> float | None:
    """Convert non-negative seconds into Excel serial day fraction for [h]:mm:ss formatting."""
    if seconds is None:
        return None
    return max(0.0, float(seconds)) / 86400.0


def _fmt_vi_hms(seconds: float | None) -> str:
    """Human-readable Vietnamese H giờ M phút S giây string."""
    if seconds is None:
        return "-"
    s = max(0.0, float(seconds))
    if s < 60.0:
        return f"{s:.1f} giây"
    total_sec = int(round(s))
    h, rem = divmod(total_sec, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h} giờ {m:02d} phút {sec:02d} giây ({h:02d}:{m:02d}:{sec:02d})"
    return f"{m} phút {sec:02d} giây (00:{m:02d}:{sec:02d})"


def _pct(part: float, whole: float) -> float:
    if whole <= 1e-9:
        return 0.0
    return round(min(100.0, max(0.0, (part / whole) * 100.0)), 2)


def _agv_display_meta(agv_id: str) -> tuple[str, str, int, str, int]:
    """Return (display_code, area_label, floor, station_label, sort_group_order)."""
    if agv_id.startswith("u1_agv_"):
        _, _, fl, idx = agv_id.split("_")
        f, b = int(fl), int(idx)
        return f"U1-AGV-T{f}-N{b + 1}", "Khu Unpack U1", f, f"Nhánh U1-{b + 1}", 0
    if agv_id.startswith("u2_agv_"):
        _, _, fl, idx = agv_id.split("_")
        f, u = int(fl), int(idx)
        return f"U2-AGV-T{f}-S{u + 1}", "Khu Unpack U2 (Zone 05)", f, f"Trạm U2-{u + 1}", 1
    if agv_id.startswith("agv_"):
        lane = int(agv_id.split("_")[1])
        return f"AGV-{lane + 1:02d}", "Khu Dock -> Cửa nhập Kho 01", 1, f"Làn nhập {lane + 1}", 2
    if agv_id.startswith("p1_agv_"):
        _, _, fl, idx = agv_id.split("_")
        f, s = int(fl), int(idx)
        return f"P1-AGV-T{f}-S{s + 1:02d}", "Khu Đóng Gói P1", f, f"Trạm P1-{s + 1:02d}", 3
    return agv_id, "Khác", 1, agv_id, 9


class SimulationTracker:
    """Collects per-equipment, per-mission, traffic, dock, and worker metrics."""

    def __init__(
        self,
        config: dict[str, Any],
        total_jobs: int | None = None,
        cycle_duration_s: float = 0.0,
        continuous_mode: bool = False,
    ):
        self.config = dict(config)
        self.total_jobs = int(config.get("jobs", 0) if total_jobs is None else total_jobs)
        self.cycle_duration_s = float(cycle_duration_s or 0.0)
        self.continuous_mode = bool(continuous_mode)
        self.stop_reason = "Hoàn thành chu trình"
        self.sim_duration_s = 0.0
        self.wall_duration_s = 0.0
        self.started_at = datetime.now()
        self.session_stamp = self.started_at.strftime("%Y%m%d_%H%M%S")

        self.init_battery_pct = float(config.get("agv_initial_battery_pct", 100.0))
        self.drain_loaded_per_m = float(config.get("agv_drain_loaded_pct_per_m", 0.04))
        self.drain_empty_per_m = float(config.get("agv_drain_empty_pct_per_m", 0.02))
        self.drain_idle_per_s = float(config.get("agv_drain_idle_pct_per_s", 0.002))

        self.shuttles: dict[str, dict[str, Any]] = {}
        for i in range(10):
            sid = f"shuttle_{i}"
            level = (i % 9) + 1
            role = f"Hoạt động chính - Tầng {level}" if i < 9 else "Dự phòng (Standby - Đỗ tầng 1)"
            self.shuttles[sid] = {
                "id": sid,
                "code": f"Shuttle {i + 1}",
                "index": i + 1,
                "level": level,
                "role": role,
                "inbound_count": 0,
                "outbound_count": 0,
                "turn_count": 0,
                "distance_m": 0.0,
                "busy_s": 0.0,
                "wait_s": 0.0,
                "idle_s": 0.0,
                "active_pct": 0.0,
                "idle_pct": 0.0,
                "fault_count": 0,
                "fault_notes": [],
                "active_task": None,
                "last_task": "",
                "task_codes": [],
                "status": "Dự phòng (Standby)" if i == 9 else "Chờ lệnh",
            }

        self.warehouse_tasks: list[dict[str, Any]] = []
        self._active_wh_tasks: dict[str, dict[str, Any]] = {}
        self.job_dock_map: dict[int, int] = {}

        self.lifts: dict[str, dict[str, Any]] = {}
        for lid, code, direction, station, order in (
            ("in_lift_0", "Thang Nhập 1", "Nhập kho", "Cửa Nhập Kho 01 - Cổng 1", 1),
            ("in_lift_1", "Thang Nhập 2", "Nhập kho", "Cửa Nhập Kho 01 - Cổng 2", 2),
            ("out_lift_0", "Thang Xuất 1", "Xuất kho", "Cửa Xuất Kho 01 - Cổng 1", 3),
            ("out_lift_1", "Thang Xuất 2", "Xuất kho", "Cửa Xuất Kho 01 - Cổng 2", 4),
        ):
            self.lifts[lid] = {
                "id": lid,
                "code": code,
                "direction": direction,
                "station": station,
                "order": order,
                "inbound_count": 0,
                "outbound_count": 0,
                "distance_m": 0.0,
                "busy_s": 0.0,
                "wait_s": 0.0,
                "wait_count": 0,
                "max_wait_s": 0.0,
                "idle_s": 0.0,
                "active_pct": 0.0,
                "idle_pct": 0.0,
                "fault_count": 0,
                "fault_notes": [],
                "active_task": None,
                "last_task": "",
                "task_codes": [],
                "status": "Chờ lệnh",
            }

        self.stage_waits: dict[str, dict[str, Any]] = {}
        for idx, (sk, lbl, desc) in enumerate((
            ("pickup_to_agv", "1. Hàng chờ Ô Pickup -> AGV nhận hàng (Nhập)", "Pallet chẵn nằm tại Ô Pickup chờ AGV tầng 1 tới nhận"),
            ("agv_to_in_lift", "2. Hàng chờ Thang nâng Nhập (Cổng 1 & 2)", "AGV mang pallet chờ cổng & thang nâng nhập trống để lên tầng"),
            ("buffer_to_in_shuttle", "3. Hàng chờ Shuttle chiều Nhập kho", "Pallet trên bàn đệm nhập tầng chờ Shuttle tầng đó rảnh"),
            ("out_req_to_lift", "4. Hàng chờ Thang nâng Xuất (Cổng 1 & 2)", "Yêu cầu xuất kho chờ Z05 trống, hàng sẵn sàng & thang xuất rảnh"),
            ("out_port_to_shuttle", "5. Hàng chờ Shuttle chiều Xuất kho", "Thang Xuất sẵn sàng nhưng chờ Shuttle tầng lấy hàng từ kệ rảnh"),
        ), start=1):
            self.stage_waits[sk] = {
                "id": sk,
                "order": idx,
                "label": lbl,
                "desc": desc,
                "total_events": 0,
                "waited_events": 0,
                "total_wait_s": 0.0,
                "max_wait_s": 0.0,
            }

        self.agvs: dict[str, dict[str, Any]] = {}
        for i in range(2):
            self._register_agv(f"agv_{i}")
        for f in LG.floors():
            for b in range(len(LG.BRANCHES)):
                self._register_agv(f"u1_agv_{f}_{b}")
            for u in range(len(LG.U2_WORKERS)):
                self._register_agv(f"u2_agv_{f}_{u}")
            for st in range(len(LG.P1_STATIONS)):
                self._register_agv(f"p1_agv_{f}_{st}")

        self.agv_tasks: list[dict[str, Any]] = []
        self._active_agv_tasks: dict[str, dict[str, Any]] = {}
        self._agv_seq: dict[str, int] = {}

        docks_cfg = {**config, "truck_count_visible": config.get("dock_active_count", 4)}
        selected_docks = truck_scene.selected_docks(docks_cfg)
        self.dock_order = [d["id"] for d in selected_docks]
        self.docks: dict[int, dict[str, Any]] = {
            did: self._blank_dock(did) for did in self.dock_order
        }
        self.dock_events: list[dict[str, Any]] = []
        self._dock_batch_t0: dict[tuple[int, str, Any], float] = {}
        self.dock_flow_stats: dict[str, int] = {}
        self.loose_stats: dict[str, Any] = {}
        self.traffic_stats: dict[str, Any] = {}

        self.workers: dict[str, dict[str, Any]] = {}
        self._init_workers()

    def _register_agv(self, agv_id: str) -> dict[str, Any]:
        if agv_id not in self.agvs:
            code, area, floor, station, group_order = _agv_display_meta(agv_id)
            self.agvs[agv_id] = {
                "id": agv_id,
                "code": code,
                "area": area,
                "floor": floor,
                "station": station,
                "group_order": group_order,
                "missions_started": 0,
                "missions_completed": 0,
                "loaded_distance_m": 0.0,
                "empty_distance_m": 0.0,
                "total_distance_m": 0.0,
                "busy_s": 0.0,
                "wait_s": 0.0,
                "idle_s": 0.0,
                "pure_idle_s": 0.0,
                "active_pct": 0.0,
                "idle_pct": 0.0,
                "wait_pct": 0.0,
                "pure_idle_pct": 0.0,
                "fault_count": 0,
                "battery_used_pct": 0.0,
                "battery_remaining_pct": self.init_battery_pct,
                "active_task": None,
                "last_task": "",
                "task_codes": [],
                "status": "Chờ nhiệm vụ",
            }
        return self.agvs[agv_id]

    @staticmethod
    def _blank_dock(dock_id: int) -> dict[str, Any]:
        return {
            "dock_id": dock_id,
            "worker_code": f"DOCK-{dock_id:02d}",
            "even_arrivals": 0,
            "odd_arrivals": 0,
            "even_batches": 0,
            "odd_batches": 0,
            "even_cartons": 0,
            "odd_cartons": 0,
            "even_ready": 0,
            "odd_ready": 0,
            "even_pool_delivered": 0,
            "empty_returns": 0,
            "agv_tasks": 0,
            "agv_task_codes": [],
            "gate_wait_s": 0.0,
            "busy_s": 0.0,
        }

    def _dock_entry(self, dock_id: int) -> dict[str, Any]:
        if dock_id not in self.docks:
            self.docks[dock_id] = self._blank_dock(dock_id)
            self.dock_order.append(dock_id)
        return self.docks[dock_id]

    def _register_worker(
        self, key: str, code: str, area: str, group_order: int, floor: Any, station: str, route_desc: str = ""
    ) -> dict[str, Any]:
        if key not in self.workers:
            self.workers[key] = {
                "key": key,
                "code": code,
                "area": area,
                "group_order": group_order,
                "floor": floor,
                "station": station,
                "route_desc": route_desc,
                "pallets": set(),
                "cartons": 0,
                "med_boxes": 0,
                "shared_picks": 0,
                "busy_s": 0.0,
                "idle_s": 0.0,
                "active_pct": 0.0,
                "idle_pct": 0.0,
            }
        return self.workers[key]

    def _init_workers(self) -> None:
        for did in self.dock_order:
            self._register_worker(
                f"dock_{did}", f"DOCK-{did:02d}", "Khu Vực Dock", 0, 1, f"Cửa Dock {did:02d}",
                f"Xe tải Dock {did:02d} -> Ô Pickup / Bàn Unpack T1",
            )
        for k, _ in enumerate(LG.OPERATORS):
            self._register_worker(
                f"op_{k}", f"OP-T1-{k + 1:02d}", "Unpack Hàng Lẻ (Tầng 1)", 1, 1, f"Vị trí Unpack {k + 1}",
                f"Bàn Unpack {k + 1} -> Băng tải lên T1..T4",
            )
        for f in LG.floors():
            for b in range(len(LG.BRANCHES)):
                self._register_worker(
                    f"u1_{f}_{b}", f"U1-T{f}-N{b + 1}", "Chia Hàng Unpack U1", 2, f, f"Nhánh U1-{b + 1}",
                    f"Băng nhánh U1-{b + 1} -> Rack U1 & Trolley",
                )
            for z in range(len(LG.Z05_WORKERS)):
                self._register_worker(
                    f"z05_{f}_{z}", f"Z05-T{f}-CN{z + 1}", "Phân Bổ Zone 05 -> U2", 3, f, f"Công nhân Z05-{z + 1}",
                    f"Zone 05 T{f} -> Các trạm U2 T{f}",
                )
            for u in range(len(LG.U2_WORKERS)):
                self._register_worker(
                    f"u2_{f}_{u}", f"U2-T{f}-S{u + 1}", "Đưa Hàng Lên Rack U2", 4, f, f"Trạm U2-{u + 1}",
                    f"Sàn Trạm U2-{u + 1} -> Rack U2 & Trolley",
                )
            for st in range(len(LG.P1_STATIONS)):
                self._register_worker(
                    f"p1_{f}_{st}", f"P1-T{f}-S{st + 1:02d}", "Đóng Gói P1", 5, f, f"Trạm P1-{st + 1:02d}",
                    f"Rack P1-{st + 1:02d} & Trolley -> Thùng đóng gói P1",
                )

    def start_shuttle_task(
        self,
        shuttle_id: str,
        direction: str,
        task_code: str,
        pallet_id: str,
        level: int,
        slot_id: Any,
        t_start: float,
        job_id: int | None = None,
        request_id: int | None = None,
        pick_location: str = "",
        drop_location: str = "",
        wait_s: float = 0.0,
        lift_id: str = "",
        lift_code: str = "",
        lift_wait_s: float = 0.0,
        process_start_s: float | None = None,
        process_shuttle_wait_s: float | None = None,
        keep_process_open: bool = False,
    ) -> dict[str, Any]:
        sh = self.shuttles[shuttle_id]
        dock_id = self.job_dock_map.get(job_id) if job_id is not None else None
        resolved_lift_code = lift_code or (self.lifts[lift_id]["code"] if lift_id in self.lifts else "-")
        eff_start = t_start if process_start_s is None else process_start_s
        rec_sh_wait = max(0.0, float(wait_s if process_shuttle_wait_s is None else process_shuttle_wait_s))
        rec = {
            "task_code": task_code,
            "direction": direction,
            "pallet_id": pallet_id,
            "job_id": job_id,
            "request_id": request_id,
            "dock_id": dock_id,
            "level": level + 1,
            "slot_id": str(slot_id),
            "pick_location": pick_location,
            "drop_location": drop_location,
            "shuttle_id": shuttle_id,
            "shuttle_code": sh["code"],
            "lift_id": lift_id,
            "lift_code": resolved_lift_code,
            "start_s": round(float(eff_start), 3),
            "end_s": None,
            "duration_s": None,
            "lift_wait_s": round(max(0.0, float(lift_wait_s)), 3),
            "wait_s": round(rec_sh_wait, 3),
            "distance_m": 0.0,
            "fault_count": 0,
            "fault_reason": "",
            "keep_process_open": keep_process_open,
            "status": "Đang thực hiện",
        }
        self.warehouse_tasks.append(rec)
        self._active_wh_tasks[shuttle_id] = rec
        sh["active_task"] = task_code
        sh["last_task"] = task_code
        sh["task_codes"].append(task_code)
        sh["wait_s"] += max(0.0, float(wait_s))
        if direction == "Nhập kho":
            sh["inbound_count"] += 1
        else:
            sh["outbound_count"] += 1
        return rec

    def record_shuttle_leg(self, shuttle_id: str, distance_m: float, duration_s: float) -> None:
        sh = self.shuttles[shuttle_id]
        sh["distance_m"] += max(0.0, float(distance_m))
        sh["busy_s"] += max(0.0, float(duration_s))
        rec = self._active_wh_tasks.get(shuttle_id)
        if rec is not None:
            rec["distance_m"] += max(0.0, float(distance_m))

    def record_shuttle_busy(self, shuttle_id: str, duration_s: float) -> None:
        self.shuttles[shuttle_id]["busy_s"] += max(0.0, float(duration_s))

    def record_shuttle_turn(self, shuttle_id: str, duration_s: float) -> None:
        sh = self.shuttles[shuttle_id]
        sh["turn_count"] += 1
        sh["busy_s"] += max(0.0, float(duration_s))

    def record_shuttle_wait(self, shuttle_id: str, wait_s: float) -> None:
        w = max(0.0, float(wait_s))
        self.shuttles[shuttle_id]["wait_s"] += w
        rec = self._active_wh_tasks.get(shuttle_id)
        if rec is not None:
            rec["wait_s"] += w

    def record_shuttle_fault(self, shuttle_id: str, reason: str) -> None:
        if shuttle_id not in self.shuttles:
            return
        sh = self.shuttles[shuttle_id]
        sh["fault_count"] += 1
        sh["fault_notes"].append(reason)
        rec = self._active_wh_tasks.get(shuttle_id)
        if rec is not None:
            rec["fault_count"] += 1
            rec["fault_reason"] = reason
            rec["status"] = "Lỗi"

    def finish_shuttle_task(self, shuttle_id: str, t_end: float) -> None:
        sh = self.shuttles[shuttle_id]
        rec = self._active_wh_tasks.pop(shuttle_id, None)
        if rec is not None and not rec.get("keep_process_open"):
            rec["end_s"] = round(t_end, 3)
            rec["duration_s"] = round(max(0.0, t_end - rec["start_s"]), 3)
            rec["distance_m"] = round(rec["distance_m"], 3)
            rec["status"] = "Lỗi" if rec["fault_count"] > 0 else "Hoàn thành"
        elif rec is not None:
            rec["distance_m"] = round(rec["distance_m"], 3)
        sh["active_task"] = None

    def finish_warehouse_process(
        self,
        rec: dict[str, Any] | None,
        t_end: float,
        extra_lift_wait_s: float = 0.0,
        fault_count_add: int = 0,
    ) -> None:
        if rec is None:
            return
        if extra_lift_wait_s > 0.0:
            rec["lift_wait_s"] = round(rec.get("lift_wait_s", 0.0) + float(extra_lift_wait_s), 3)
        if fault_count_add > 0:
            rec["fault_count"] = rec.get("fault_count", 0) + int(fault_count_add)
        rec["end_s"] = round(float(t_end), 3)
        rec["duration_s"] = round(max(0.0, float(t_end) - rec["start_s"]), 3)
        rec["keep_process_open"] = False
        rec["status"] = "Lỗi" if rec.get("fault_count", 0) > 0 else "Hoàn thành"

    def start_lift_task(
        self,
        lift_id: str,
        direction: str,
        task_code: str,
        pallet_id: str = "",
    ) -> None:
        if lift_id not in self.lifts:
            return
        lf = self.lifts[lift_id]
        lf["active_task"] = task_code or pallet_id
        lf["last_task"] = task_code or pallet_id
        if task_code:
            lf["task_codes"].append(task_code)
        if direction == "Nhập kho":
            lf["inbound_count"] += 1
        else:
            lf["outbound_count"] += 1

    def record_lift_leg(self, lift_id: str, height_m: float, duration_s: float) -> None:
        if lift_id not in self.lifts:
            return
        lf = self.lifts[lift_id]
        lf["distance_m"] += max(0.0, float(height_m))
        lf["busy_s"] += max(0.0, float(duration_s))

    def finish_lift_task(self, lift_id: str) -> None:
        if lift_id not in self.lifts:
            return
        self.lifts[lift_id]["active_task"] = None

    def record_lift_fault(self, lift_id: str, note: str = "") -> None:
        if lift_id not in self.lifts:
            return
        lf = self.lifts[lift_id]
        lf["fault_count"] += 1
        if note:
            lf["fault_notes"].append(note)

    def record_lift_wait(self, lift_id: str, wait_s: float) -> None:
        if lift_id not in self.lifts:
            return
        lf = self.lifts[lift_id]
        w = max(0.0, float(wait_s))
        lf["wait_s"] += w
        lf["wait_count"] += 1
        if w > lf["max_wait_s"]:
            lf["max_wait_s"] = w

    def record_stage_wait(self, stage_id: str, wait_s: float) -> None:
        st = self.stage_waits.get(stage_id)
        if st is None:
            return
        w = max(0.0, float(wait_s))
        st["total_events"] += 1
        st["total_wait_s"] += w
        if w > 1e-6:
            st["waited_events"] += 1
        if w > st["max_wait_s"]:
            st["max_wait_s"] = w

    def next_agv_code(self, prefix: str) -> str:
        seq = self._agv_seq.get(prefix, 0) + 1
        self._agv_seq[prefix] = seq
        return f"{prefix}-{seq:04d}"

    def start_agv_task(
        self,
        agv_id: str,
        task_code: str,
        area: str,
        task_type: str,
        t_start: float,
        pallet_or_item: str = "",
        dock_id: int | None = None,
        pick_location: str = "",
        drop_location: str = "",
    ) -> None:
        agv = self._register_agv(agv_id)
        rec = {
            "task_code": task_code,
            "area": area,
            "task_type": task_type,
            "agv_id": agv_id,
            "agv_code": agv["code"],
            "floor": agv["floor"],
            "station": agv["station"],
            "dock_id": dock_id,
            "pallet_or_item": pallet_or_item,
            "pick_location": pick_location,
            "drop_location": drop_location,
            "start_s": round(t_start, 3),
            "end_s": None,
            "duration_s": None,
            "wait_s": 0.0,
            "loaded_m": 0.0,
            "empty_m": 0.0,
            "total_m": 0.0,
            "fault_count": 0,
            "status": "Đang thực hiện",
        }
        self.agv_tasks.append(rec)
        self._active_agv_tasks[agv_id] = rec
        agv["missions_started"] += 1
        agv["active_task"] = task_code
        agv["last_task"] = task_code
        agv["task_codes"].append(task_code)
        if dock_id is not None and area == "Khu Dock -> Kho 01":
            d = self._dock_entry(dock_id)
            d["agv_tasks"] += 1
            d["agv_task_codes"].append(task_code)

    def record_agv_leg(self, agv_id: str, distance_m: float, loaded: bool, move_s: float, wait_s: float = 0.0) -> None:
        agv = self._register_agv(agv_id)
        d = max(0.0, float(distance_m))
        w = max(0.0, float(wait_s))
        if loaded:
            agv["loaded_distance_m"] += d
        else:
            agv["empty_distance_m"] += d
        agv["busy_s"] += max(0.0, float(move_s))
        agv["wait_s"] += w
        rec = self._active_agv_tasks.get(agv_id)
        if rec is not None:
            if loaded:
                rec["loaded_m"] += d
            else:
                rec["empty_m"] += d
            rec["total_m"] = rec["loaded_m"] + rec["empty_m"]
            rec["wait_s"] += w

    def record_agv_fault(self, agv_id: str) -> None:
        agv = self._register_agv(agv_id)
        agv["fault_count"] += 1
        rec = self._active_agv_tasks.get(agv_id)
        if rec is not None:
            rec["fault_count"] += 1
            rec["status"] = "Lỗi"

    def finish_agv_task(self, agv_id: str, t_end: float) -> None:
        agv = self._register_agv(agv_id)
        rec = self._active_agv_tasks.pop(agv_id, None)
        if rec is not None:
            rec["end_s"] = round(t_end, 3)
            rec["duration_s"] = round(max(0.0, t_end - rec["start_s"]), 3)
            rec["loaded_m"] = round(rec["loaded_m"], 3)
            rec["empty_m"] = round(rec["empty_m"], 3)
            rec["total_m"] = round(rec["total_m"], 3)
            rec["status"] = "Lỗi" if rec["fault_count"] > 0 else "Hoàn thành"
            agv["missions_completed"] += 1
        agv["active_task"] = None

    def record_worker_activity(
        self,
        worker_key: str,
        duration_s: float = 0.0,
        cartons: int = 0,
        med_boxes: int = 0,
        pallet_id: Any = None,
        shared_pick: bool = False,
    ) -> None:
        w = self.workers.get(worker_key)
        if w is None:
            return
        w["busy_s"] += max(0.0, float(duration_s))
        w["cartons"] += int(cartons)
        w["med_boxes"] += int(med_boxes)
        if pallet_id is not None:
            w["pallets"].add(str(pallet_id))
        if shared_pick:
            w["shared_picks"] += 1

    def record_event(self, record: dict[str, Any]) -> None:
        state = record.get("state", "")
        t = record.get("demo_time_s", 0.0)
        job = record.get("job")
        dock_id = record.get("dock")

        if state == "INBOUND_ARRIVAL" and job is not None and dock_id is not None:
            self.job_dock_map[int(job)] = int(dock_id)

        if state == "TRUCK_BATCH_READY" and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            kind = record.get("classification", "")
            cnt = int(record.get("cartons", 0) or 0)
            batch = record.get("batch")
            self._dock_batch_t0[(did, kind, batch)] = float(t)
            if kind == "even":
                d["even_batches"] += 1
                self.dock_events.append({
                    "time_s": t, "dock_id": did, "kind": "Pallet chẵn (Even)",
                    "batch": batch, "state": state, "detail": f"Mở batch {cnt} kiện trên xe tải",
                })
            elif kind == "odd":
                d["odd_batches"] += 1
                self.dock_events.append({
                    "time_s": t, "dock_id": did, "kind": "Pallet lẻ (Odd)",
                    "batch": batch, "state": state, "detail": f"Mở batch {cnt} kiện trên xe tải",
                })
            elif kind == "staged":
                self.dock_events.append({
                    "time_s": t, "dock_id": did, "kind": "Pre-stage (Chờ slot trống)",
                    "batch": batch, "state": state, "detail": f"Dỡ {cnt} kiện lên bàn Dock (chờ phân bổ ô trống)",
                })
            return

        if state == "BARCODE_CLASSIFIED" and dock_id is not None:
            d = self._dock_entry(int(dock_id))
            kind = record.get("classification", "")
            if kind == "even":
                d["even_cartons"] += 1
            elif kind == "odd":
                d["odd_cartons"] += 1
            self.record_worker_activity(f"dock_{int(dock_id)}", cartons=1)
            return

        if state == "DOCK_PALLET_READY" and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            kind = record.get("classification", "")
            batch = record.get("batch")
            cartons = int(record.get("cartons", 0) or 0)
            staged_t0 = self._dock_batch_t0.pop((did, "staged", batch), None)
            if staged_t0 is not None and (did, kind, batch) not in self._dock_batch_t0:
                self._dock_batch_t0[(did, kind, batch)] = staged_t0
            if cartons > 0:
                if kind == "even":
                    d["even_batches"] += 1
                    d["even_cartons"] += cartons
                elif kind == "odd":
                    d["odd_batches"] += 1
                    d["odd_cartons"] += cartons
                self.record_worker_activity(f"dock_{did}", cartons=cartons)
            if kind == "even":
                d["even_ready"] += 1
                self.dock_events.append({
                    "time_s": t, "dock_id": did, "kind": "Pallet chẵn (Even)",
                    "batch": batch, "state": state, "detail": "Chất đầy pallet tại Dock, chuyển ra 4 Ô Pickup (chờ AGV Dock)",
                })
            elif kind == "odd":
                d["odd_ready"] += 1
                self.dock_events.append({
                    "time_s": t, "dock_id": did, "kind": "Pallet lẻ (Odd)",
                    "batch": batch, "state": state, "detail": "Chất đầy pallet tại Dock, chuyển sang Bàn Unpack T1",
                })
            w = self.workers.get(f"dock_{did}")
            if w is not None:
                w["pallets"].add(f"{kind}_{batch}")
            return

        if state == "DOCK_GATE_WAIT" and dock_id is not None:
            d = self._dock_entry(int(dock_id))
            d["gate_wait_s"] += max(0.0, float(record.get("wait_s", 0.0) or 0.0))
            return

        if state == "EMPTY_PALLET_FROM_POOL" and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            d["even_pool_delivered"] += 1
            t0 = self._dock_batch_t0.pop((did, "even", record.get("batch")), None)
            if t0 is not None:
                dur = max(0.0, float(t) - t0)
                d["busy_s"] += dur
                self.record_worker_activity(f"dock_{did}", duration_s=dur)
            return

        if state == "EMPTY_PALLET_RETURNED" and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            d["empty_returns"] += 1
            kind = record.get("classification", "odd")
            t0 = self._dock_batch_t0.pop((did, kind, record.get("batch")), None)
            if t0 is not None:
                dur = max(0.0, float(t) - t0)
                d["busy_s"] += dur
                self.record_worker_activity(f"dock_{did}", duration_s=dur)
            return

        if state == "CARTON_ON_BELT":
            op = record.get("operator")
            if op is not None:
                self.record_worker_activity(f"op_{int(op)}", cartons=1, pallet_id=record.get("pallet"))
            return

        if state == "CARTON_PLACED_ON_RACK":
            fl, br = record.get("floor"), record.get("branch")
            if fl is not None and br is not None:
                meds = len(record.get("med_boxes") or [])
                self.record_worker_activity(f"u1_{int(fl)}_{int(br)}", cartons=1, med_boxes=meds)
            return

        if state == "Z05_CARTON_DELIVERED_TO_U2":
            fl, wk = record.get("floor"), record.get("worker")
            if fl is not None and wk is not None:
                self.record_worker_activity(f"z05_{int(fl)}_{int(wk)}", cartons=1, pallet_id=record.get("job"))
            return

        if state == "U2_CARTON_PLACED_ON_RACK":
            fl, u2 = record.get("floor"), record.get("u2_station")
            if fl is not None and u2 is not None:
                meds = len(record.get("med_boxes") or [])
                self.record_worker_activity(f"u2_{int(fl)}_{int(u2)}", cartons=1, med_boxes=meds, pallet_id=record.get("job"))
            return

        if state == "P1_PACKED_CARTON_COMPLETED":
            fl, st = record.get("floor"), record.get("p1_station")
            if fl is not None and st is not None:
                meds = int(self.config.get("p1_med_boxes_per_carton", LG.U1_MED_PER_CARTON))
                self.record_worker_activity(f"p1_{int(fl)}_{int(st)}", cartons=1, med_boxes=meds)

    def finalize(
        self,
        sim_duration_s: float,
        stop_reason: str | None = None,
        dock_flow_stats: dict[str, int] | None = None,
        loose_stats: dict[str, Any] | None = None,
        wall_duration_s: float | None = None,
        traffic_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.sim_duration_s = max(0.0, float(sim_duration_s))
        if wall_duration_s is not None:
            self.wall_duration_s = max(0.0, float(wall_duration_s))
        if stop_reason:
            self.stop_reason = stop_reason
        if dock_flow_stats is not None:
            self.dock_flow_stats = dict(dock_flow_stats)
        if loose_stats is not None:
            self.loose_stats = dict(loose_stats)
        if traffic_stats is not None:
            self.traffic_stats = dict(traffic_stats)

        for (did, _, _), t0 in list(self._dock_batch_t0.items()):
            dur = max(0.0, self.sim_duration_s - t0)
            self._dock_entry(did)["busy_s"] += dur
            self.record_worker_activity(f"dock_{did}", duration_s=dur)
        self._dock_batch_t0.clear()

        for sh in self.shuttles.values():
            sh["distance_m"] = round(sh["distance_m"], 3)
            sh["busy_s"] = round(min(sh["busy_s"], self.sim_duration_s) if self.sim_duration_s > 0 else sh["busy_s"], 3)
            sh["wait_s"] = round(sh["wait_s"], 3)
            sh["idle_s"] = round(max(0.0, self.sim_duration_s - sh["busy_s"]), 3)
            sh["active_pct"] = _pct(sh["busy_s"], self.sim_duration_s)
            sh["idle_pct"] = _pct(sh["idle_s"], self.sim_duration_s)
            if sh["fault_count"] > 0:
                sh["status"] = "Lỗi"
            elif sh["index"] == 10 and sh["inbound_count"] == 0 and sh["outbound_count"] == 0:
                sh["status"] = "Dự phòng (Standby)"
            elif sh["active_task"]:
                sh["status"] = "Đang chạy khi dừng"
            elif sh["inbound_count"] + sh["outbound_count"] > 0:
                sh["status"] = "Hoạt động bình thường"
            else:
                sh["status"] = "Chờ lệnh"

        for lf in self.lifts.values():
            lf["distance_m"] = round(lf["distance_m"], 2)
            lf["busy_s"] = round(min(lf["busy_s"], self.sim_duration_s) if self.sim_duration_s > 0 else lf["busy_s"], 3)
            lf["wait_s"] = round(lf["wait_s"], 3)
            lf["max_wait_s"] = round(lf["max_wait_s"], 3)
            lf["idle_s"] = round(max(0.0, self.sim_duration_s - lf["busy_s"]), 3)
            lf["active_pct"] = _pct(lf["busy_s"], self.sim_duration_s)
            lf["idle_pct"] = _pct(lf["idle_s"], self.sim_duration_s)
            if lf["fault_count"] > 0:
                lf["status"] = "Lỗi"
            elif lf["active_task"] is not None:
                lf["status"] = "Đang chạy khi dừng"
            elif lf["inbound_count"] + lf["outbound_count"] > 0:
                lf["status"] = "Hoạt động bình thường"
            else:
                lf["status"] = "Chờ lệnh"

        for agv in self.agvs.values():
            loaded_m = agv["loaded_distance_m"]
            empty_m = agv["empty_distance_m"]
            total_m = loaded_m + empty_m
            busy_s = min(agv["busy_s"], self.sim_duration_s) if self.sim_duration_s > 0 else agv["busy_s"]
            idle_s = max(0.0, self.sim_duration_s - busy_s)
            eff_wait_s = min(agv["wait_s"], idle_s) if self.sim_duration_s > 0 else agv["wait_s"]
            pure_idle_s = max(0.0, idle_s - eff_wait_s)
            used_pct = (
                loaded_m * self.drain_loaded_per_m
                + empty_m * self.drain_empty_per_m
                + idle_s * self.drain_idle_per_s
            )
            rem_pct = max(0.0, self.init_battery_pct - used_pct)
            agv["loaded_distance_m"] = round(loaded_m, 3)
            agv["empty_distance_m"] = round(empty_m, 3)
            agv["total_distance_m"] = round(total_m, 3)
            agv["busy_s"] = round(busy_s, 3)
            agv["wait_s"] = round(agv["wait_s"], 3)
            agv["idle_s"] = round(idle_s, 3)
            agv["pure_idle_s"] = round(pure_idle_s, 3)
            agv["active_pct"] = _pct(busy_s, self.sim_duration_s)
            agv["idle_pct"] = _pct(idle_s, self.sim_duration_s)
            agv["wait_pct"] = _pct(eff_wait_s, self.sim_duration_s)
            agv["pure_idle_pct"] = _pct(pure_idle_s, self.sim_duration_s)
            agv["battery_used_pct"] = round(min(self.init_battery_pct, used_pct), 2)
            agv["battery_remaining_pct"] = round(rem_pct, 2)
            if agv["fault_count"] > 0:
                agv["status"] = "Lỗi"
            elif rem_pct <= 0.0:
                agv["status"] = "Cạn pin"
            elif agv["active_task"]:
                agv["status"] = "Đang chạy khi dừng"
            elif agv["missions_started"] > 0:
                agv["status"] = "Sẵn sàng (Đã chạy)"
            else:
                agv["status"] = "Chờ nhiệm vụ"

        for w in self.workers.values():
            busy_s = min(w["busy_s"], self.sim_duration_s) if self.sim_duration_s > 0 else w["busy_s"]
            idle_s = max(0.0, self.sim_duration_s - busy_s)
            w["busy_s"] = round(busy_s, 3)
            w["idle_s"] = round(idle_s, 3)
            w["active_pct"] = _pct(busy_s, self.sim_duration_s)
            w["idle_pct"] = _pct(idle_s, self.sim_duration_s)

        return self.summary()

    def snapshot(
        self,
        sim_duration_s: float,
        stop_reason: str | None = None,
        dock_flow_stats: dict[str, int] | None = None,
        loose_stats: dict[str, Any] | None = None,
        wall_duration_s: float | None = None,
        traffic_stats: dict[str, Any] | None = None,
    ) -> SimulationTracker:
        """Return a deep-copied finalized snapshot safe for background thread export."""
        snap = copy.deepcopy(self)
        snap.finalize(
            sim_duration_s, stop_reason, dock_flow_stats, loose_stats,
            wall_duration_s=wall_duration_s, traffic_stats=traffic_stats,
        )
        return snap

    def summary(self) -> dict[str, Any]:
        wh_in = sum(1 for t in self.warehouse_tasks if t["direction"] == "Nhập kho")
        wh_out = sum(1 for t in self.warehouse_tasks if t["direction"] == "Xuất kho")
        dock_agv_tasks = [t for t in self.agv_tasks if t["area"] == "Khu Dock -> Kho 01"]
        unpack_agv_tasks = [t for t in self.agv_tasks if t["area"] in ("Khu Unpack U1", "Khu Unpack U2 (Zone 05)")]
        p1_agv_tasks = [t for t in self.agv_tasks if t["area"] == "Khu Đóng Gói P1"]
        avg_bat = sum(a["battery_remaining_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        avg_agv_act = sum(a["active_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        avg_agv_wait = sum(a["wait_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        avg_agv_pure_idle = sum(a["pure_idle_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        active_shuttles = [s for s in self.shuttles.values() if s["index"] <= 9]
        avg_sh_active = sum(s["active_pct"] for s in active_shuttles) / max(1, len(active_shuttles))
        avg_sh_idle = sum(s["idle_pct"] for s in active_shuttles) / max(1, len(active_shuttles))
        lifts_list = list(self.lifts.values())
        avg_lf_active = sum(lf["active_pct"] for lf in lifts_list) / max(1, len(lifts_list))
        avg_lf_idle = sum(lf["idle_pct"] for lf in lifts_list) / max(1, len(lifts_list))
        return {
            "sim_duration_s": round(self.sim_duration_s, 3),
            "wall_duration_s": round(self.wall_duration_s, 3),
            "sim_duration_hms": _fmt_vi_hms(self.sim_duration_s),
            "wall_duration_hms": _fmt_vi_hms(self.wall_duration_s),
            "stop_reason": self.stop_reason,
            "warehouse_inbound_tasks": wh_in,
            "warehouse_outbound_tasks": wh_out,
            "shuttle_total_distance_m": round(sum(s["distance_m"] for s in self.shuttles.values()), 3),
            "shuttle_total_busy_s": round(sum(s["busy_s"] for s in self.shuttles.values()), 3),
            "shuttle_total_wait_s": round(sum(s["wait_s"] for s in self.shuttles.values()), 3),
            "shuttle_total_turns": sum(s["turn_count"] for s in self.shuttles.values()),
            "shuttle_fault_count": sum(s["fault_count"] for s in self.shuttles.values()),
            "shuttle_avg_active_pct": round(avg_sh_active, 2),
            "shuttle_avg_idle_pct": round(avg_sh_idle, 2),
            "lift_inbound_tasks": sum(lf["inbound_count"] for lf in self.lifts.values()),
            "lift_outbound_tasks": sum(lf["outbound_count"] for lf in self.lifts.values()),
            "lift_total_busy_s": round(sum(lf["busy_s"] for lf in self.lifts.values()), 3),
            "lift_total_wait_s": round(sum(lf["wait_s"] for lf in self.lifts.values()), 3),
            "lift_fault_count": sum(lf["fault_count"] for lf in self.lifts.values()),
            "lift_avg_active_pct": round(avg_lf_active, 2),
            "lift_avg_idle_pct": round(avg_lf_idle, 2),
            "agv_total_tasks": len(self.agv_tasks),
            "agv_unpack_tasks": len(unpack_agv_tasks),
            "agv_dock_tasks": len(dock_agv_tasks),
            "agv_p1_tasks": len(p1_agv_tasks),
            "agv_total_distance_m": round(sum(a["total_distance_m"] for a in self.agvs.values()), 3),
            "agv_avg_battery_pct": round(avg_bat, 2),
            "agv_avg_active_pct": round(avg_agv_act, 2),
            "agv_avg_wait_pct": round(avg_agv_wait, 2),
            "agv_avg_pure_idle_pct": round(avg_agv_pure_idle, 2),
            "agv_fault_count": sum(a["fault_count"] for a in self.agvs.values()),
            "dock_even_pallets": sum(d["even_ready"] for d in self.docks.values()),
            "dock_odd_pallets": sum(d["odd_ready"] for d in self.docks.values()),
            "dock_scanned_even": self.dock_flow_stats.get("scanned_even", sum(d["even_cartons"] for d in self.docks.values())),
            "dock_scanned_odd": self.dock_flow_stats.get("scanned_odd", sum(d["odd_cartons"] for d in self.docks.values())),
        }


def _style_banner(ws, row: int, col_count: int, title: str, subtitle: str = "") -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_count)
    c = ws.cell(row=row, column=1, value=title)
    c.font = FONT_TITLE
    c.fill = FILL_TITLE
    c.alignment = ALIGN_LEFT
    ws.row_dimensions[row].height = 28
    if subtitle:
        row += 1
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_count)
        sub = ws.cell(row=row, column=1, value=subtitle)
        sub.font = FONT_SUBTITLE
        sub.alignment = ALIGN_LEFT
        ws.row_dimensions[row].height = 22
    return row + 2


def _style_section(
    ws,
    row: int,
    col_count: int,
    title: str,
    start_col: int = 1,
    left_divider: bool = False,
) -> int:
    end_col = start_col + col_count - 1
    ws.merge_cells(start_row=row, start_column=start_col, end_row=row, end_column=end_col)
    c = ws.cell(row=row, column=start_col, value=title)
    c.font = FONT_SECTION
    c.fill = FILL_SECTION
    c.alignment = ALIGN_LEFT
    if left_divider:
        c.border = Border(left=Side(style="medium", color="FFFFFF"))
    ws.row_dimensions[row].height = 22
    return row + 1


def _write_table(
    ws,
    start_row: int,
    headers: list[str],
    rows: list[list[Any]],
    col_formats: dict[int, str] | None = None,
    right_cols: set[int] | None = None,
    center_cols: set[int] | None = None,
    total_row: list[Any] | None = None,
    status_col: int | None = None,
    auto_filter: bool = True,
) -> int:
    col_formats = col_formats or {}
    right_cols = right_cols or set()
    center_cols = center_cols or set()

    for c_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=c_idx, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER
        cell.border = THIN_BORDER
    ws.row_dimensions[start_row].height = 26

    cur = start_row + 1
    for r_i, row_vals in enumerate(rows):
        zebra = r_i % 2 == 1
        for c_idx, val in enumerate(row_vals, 1):
            cell = ws.cell(row=cur, column=c_idx, value=val)
            cell.font = FONT_BODY
            cell.border = THIN_BORDER
            cell.alignment = (
                ALIGN_RIGHT if c_idx in right_cols else ALIGN_CENTER if c_idx in center_cols else ALIGN_LEFT
            )
            if c_idx in col_formats and val not in (None, "-"):
                cell.number_format = col_formats[c_idx]
            if status_col == c_idx:
                sv = str(val)
                if "Lỗi" in sv or "Cạn" in sv:
                    cell.fill = FILL_ERR
                elif "Hoàn thành" in sv or "bình thường" in sv or "Đã chạy" in sv:
                    cell.fill = FILL_OK
                elif "Đang" in sv:
                    cell.fill = FILL_WARN
                else:
                    cell.fill = FILL_STANDBY
            elif zebra:
                cell.fill = FILL_ZEBRA
        ws.row_dimensions[cur].height = 21
        cur += 1

    if total_row is not None:
        for c_idx, val in enumerate(total_row, 1):
            cell = ws.cell(row=cur, column=c_idx, value=val)
            cell.font = FONT_BOLD
            cell.fill = FILL_TOTAL
            cell.border = THIN_BORDER
            cell.alignment = ALIGN_RIGHT if c_idx in right_cols else ALIGN_CENTER if c_idx in center_cols else ALIGN_LEFT
            if c_idx in col_formats and isinstance(val, (int, float)):
                cell.number_format = col_formats[c_idx]
        ws.row_dimensions[cur].height = 23
        cur += 1

    if auto_filter and rows:
        end_col = get_column_letter(len(headers))
        ws.auto_filter.ref = f"A{start_row}:{end_col}{start_row + len(rows)}"
    return cur + 1


def _fit_columns(ws, max_width: int = 44, min_width: int = 11) -> None:
    merged_cells = {coord for rng in ws.merged_cells.ranges for coord in rng.cells}
    for col in ws.columns:
        idx = col[0].column
        width = min_width
        for cell in col:
            if (cell.row, cell.column) in merged_cells:
                continue
            fmt = str(cell.number_format or "")
            if "mm:ss" in fmt:
                width = max(width, 13)
                continue
            txt = str(cell.value or "")
            longest = max((len(line) for line in txt.splitlines()), default=0)
            width = max(width, min(max_width, longest + 3))
        ws.column_dimensions[get_column_letter(idx)].width = width


def _style_chart_title(chart, title: str, font_pt: float = 11.5) -> None:
    chart.title = title
    rich = getattr(getattr(getattr(chart, "title", None), "tx", None), "rich", None)
    if rich and rich.p and rich.p[0].r:
        rich.p[0].r[0].rPr = CharacterProperties(sz=int(round(font_pt * 100)), b=True)


def _fit_wrapped_row_heights(ws, start_row: int, end_row: int, max_col: int = 15, min_line_h: float = 14.5) -> None:
    merged_cells = {coord for rng in ws.merged_cells.ranges for coord in rng.cells}
    for r in range(start_row, end_row + 1):
        max_lines = 1
        for c in range(1, max_col + 1):
            if (r, c) in merged_cells:
                continue
            cell = ws.cell(row=r, column=c)
            if not (cell.alignment and cell.alignment.wrap_text) or cell.value is None:
                continue
            txt = str(cell.value)
            if not txt:
                continue
            col_w = max(8, int((ws.column_dimensions[get_column_letter(c)].width or 14) - 2))
            lines = sum(max(1, math.ceil(len(line) / col_w)) for line in txt.splitlines())
            max_lines = max(max_lines, lines)
        if max_lines > 1:
            cur_h = ws.row_dimensions[r].height or 21
            ws.row_dimensions[r].height = max(cur_h, max_lines * min_line_h + 6)


def _fmt_codes(codes: list[str], limit: int = 15) -> str:
    if not codes:
        return "-"
    if len(codes) <= limit:
        return ", ".join(codes)
    return f"{', '.join(codes[:limit])} ... (+{len(codes) - limit} mã)"


def _build_worker_groups(tracker: SimulationTracker, sim_dur: float) -> list[list[Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for w in tracker.workers.values():
        g = grouped.setdefault(w["area"], {
            "area": w["area"], "order": w["group_order"], "headcount": 0, "active": 0,
            "pallets": 0, "cartons": 0, "med_boxes": 0, "busy_s": 0.0, "idle_s": 0.0,
        })
        g["headcount"] += 1
        if w["cartons"] > 0 or w["med_boxes"] > 0 or w["busy_s"] > 0:
            g["active"] += 1
        g["pallets"] += len(w["pallets"])
        g["cartons"] += w["cartons"]
        g["med_boxes"] += w["med_boxes"]
        g["busy_s"] += w["busy_s"]
        g["idle_s"] += w["idle_s"]
    out = []
    for g in sorted(grouped.values(), key=lambda x: x["order"]):
        cap = g["headcount"] * sim_dur
        act_pct = _pct(g["busy_s"], cap)
        idle_pct = round(max(0.0, 100.0 - act_pct), 2) if cap > 0 else 0.0
        out.append([
            g["area"], g["headcount"], g["active"], g["pallets"], g["cartons"], g["med_boxes"],
            _excel_days(g["busy_s"]), _excel_days(g["idle_s"]), act_pct, idle_pct,
        ])
    return out


def export_excel(
    tracker: SimulationTracker,
    output_dir: str | Path,
    report_summary: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Generate a formatted 6-sheet Excel report in output_dir atomically."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = tracker.summary()
    extra = report_summary or {}
    stamp = getattr(tracker, "session_stamp", None) or datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_path = out_dir / f"Bao_Cao_Kho_Long_Chau_{stamp}.xlsx"
    latest_path = out_dir / "Bao_Cao_Moi_Nhat.xlsx"
    sim_dur = max(0.0, tracker.sim_duration_s)
    speed_x = float(tracker.config.get("speed_multiplier", 1.0) or 1.0)
    tf = tracker.traffic_stats
    engine_ticks = max(1, int(tf.get("engine_ticks", 0) or 0))

    wb = Workbook()

    # ---------------------------------------------------------------- Sheet 1: Tong_Quan
    ws1 = wb.active
    ws1.title = "Tong_Quan"
    ws1.views.sheetView[0].showGridLines = True
    subtitle = (
        f"Ngày xuất: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"Thời gian vận hành thực tế chuẩn 1x: {summary['sim_duration_hms']} | "
        f"Thời gian chạy trên đồng hồ (Wall-clock): {summary['wall_duration_hms']} | "
        f"Kết thúc: {summary['stop_reason']}"
    )
    row = _style_banner(ws1, 1, 4, "BÁO CÁO VẬN HÀNH MÔ PHỎNG KHO DƯỢC LONG CHÂU", subtitle)

    cfg = tracker.config
    row = _style_section(ws1, row, 4, "1. THÔNG SỐ THỜI GIAN THỰC TẾ & CẤU HÌNH MÔ PHỎNG")
    mode_desc = (
        "Chạy liên tục không giới hạn tới khi đóng Gazebo"
        if tracker.continuous_mode and tracker.cycle_duration_s <= 0
        else (f"Dừng theo chu trình {tracker.cycle_duration_s:.1f}s" if tracker.cycle_duration_s > 0 else f"Chạy đủ {tracker.total_jobs} jobs")
    )
    cfg_rows = [
        ["Tổng thời gian vận hành thực tế chuẩn 1x (Giờ:Phút:Giây)", summary["sim_duration_hms"], "1x chuẩn nhà máy", "Thời gian thực tế thiết bị chạy ngoài đời theo vận tốc thật (không tính hệ số tua x2/x3)"],
        ["Tổng thời gian mở mô phỏng ngoài đời (Wall-clock)", summary["wall_duration_hms"], "Đồng hồ thật", f"Thời gian thực bạn theo dõi màn hình Gazebo ở tốc độ tua {speed_x:.2f}x"],
        ["Chế độ vận hành & Lý do dừng", mode_desc, "-", summary["stop_reason"]],
        ["Số pallet cấu hình (khi tắt chế độ liên tục)", tracker.total_jobs if not tracker.continuous_mode else "Không giới hạn (Liên tục)", "pallet", "Tự động tái chế pallet model khi bật continuous_mode"],
        ["Giới hạn thời lượng chu trình (cycle_duration_s)", _fmt_vi_hms(tracker.cycle_duration_s) if tracker.cycle_duration_s > 0 else "0 (Không giới hạn)", "giờ:phút:giây", "0 = chạy liên tục; > 0 = tự động dừng đúng mốc thời gian chuẩn 1x"],
        ["Chu kỳ nhập / xuất (inbound / outbound interval)", f"{cfg.get('inbound_interval_s')}s / {cfg.get('outbound_interval_s')}s", "giây (1x)", f"Mục tiêu ca: {cfg.get('warehouse_inbound_target', '-')} nhập / {cfg.get('warehouse_outbound_target', '-')} xuất | Tồn đầu: {cfg.get('initial_outbound_pallets', 0)} | Dock mở: {cfg.get('dock_active_count')}"],
        ["Thông số Shuttle cài đặt (tốc độ / gia tốc / jerk)", f"{cfg.get('shuttle_speed', 1.0):.2f} m/s | {cfg.get('shuttle_acceleration', 0.3):.2f} m/s² | {cfg.get('shuttle_jerk', 0.6):.2f} m/s³", "9 tầng + 1 dự phòng", f"Đổi bánh: {cfg.get('shuttle_turn_s', 5.0):.1f}s | Định vị: {cfg.get('shuttle_positioning_s', 5.0):.1f}s | Nâng/hạ: {cfg.get('shuttle_lift_s', 2.5):.1f}s | Avail/Util: {float(cfg.get('shuttle_availability', 0.7))*100:.0f}%/{float(cfg.get('shuttle_utilization', 0.7))*100:.0f}%"],
        ["Thông số Thang nâng cài đặt (tốc độ / gia tốc / jerk)", f"{float(cfg.get('lift_speed', 0.6)):.2f} m/s | {float(cfg.get('lift_acceleration', 0.3)):.2f} m/s² | {float(cfg.get('lift_jerk', 0.8)):.2f} m/s³", "4 thang (2 Nhập + 2 Xuất)", f"Ổn định dừng: {float(cfg.get('lift_settle_s', 2.0)):.1f}s | Avail/Util: {float(cfg.get('lift_availability', 0.8))*100:.0f}%/{float(cfg.get('lift_utilization', 0.8))*100:.0f}% | Băng tải: {float(cfg.get('conveyor_speed', 0.6)):.2f} m/s"],
        ["Pin AGV (Ban đầu / Có tải / Không tải / Chờ)", f"{tracker.init_battery_pct:.0f}% / {tracker.drain_loaded_per_m}%/m", f"{tracker.drain_empty_per_m}%/m", f"Đứng chờ: {tracker.drain_idle_per_s}%/s"],
    ]
    row = _write_table(
        ws1, row,
        ["Thông số", "Giá trị", "Đơn vị / Quy chuẩn", "Diễn giải chi tiết"],
        cfg_rows,
        center_cols={3},
        auto_filter=False,
    )

    row = _style_section(ws1, row, 4, "2. BẢNG CHỈ SỐ KPI TỪNG KHU VỰC")
    kpi_rows = [
        ["Kho AS/RS - Nhập kho / Xuất kho", f"{summary['warehouse_inbound_tasks']} nhập / {summary['warehouse_outbound_tasks']} xuất", "nhiệm vụ", f"Quãng đường 10 Shuttle: {summary['shuttle_total_distance_m']:,.2f} m | Lỗi: {summary['shuttle_fault_count']}"],
        ["Kho AS/RS - Hiệu suất & Hàng chờ 9 Shuttle chính", f"{summary['shuttle_avg_active_pct']:.2f}% chạy / {summary['shuttle_avg_idle_pct']:.2f}% đứng chơi", _fmt_vi_hms(summary['shuttle_total_busy_s']), f"Tổng hàng chờ Shuttle: {_fmt_vi_hms(summary['shuttle_total_wait_s'])} | Đổi bánh X/Y: {summary['shuttle_total_turns']} lượt"],
        ["Kho AS/RS - 4 Thang nâng (Nhập / Xuất / Hàng chờ)", f"{summary['lift_inbound_tasks']} nhập / {summary['lift_outbound_tasks']} xuất", f"{summary['lift_avg_active_pct']:.2f}% chạy", f"Tổng hàng chờ 4 thang: {_fmt_vi_hms(summary['lift_total_wait_s'])} | Lỗi: {summary['lift_fault_count']}"],
        ["Khu Unpack - Nhiệm vụ AGV (U1 & U2)", summary["agv_unpack_tasks"], "nhiệm vụ", "Đổi rack kho 04 & đổi trolley khu 06 tại bàn Unpack U1/U2"],
        ["Khu Đóng Gói P1 - Nhiệm vụ AGV", summary["agv_p1_tasks"], "nhiệm vụ", "Cấp rack hàng & đổi trolley đóng gói tại 56 trạm P1"],
        ["Khu Dock -> Kho 01 - Nhiệm vụ AGV", summary["agv_dock_tasks"], "nhiệm vụ", "AGV tầng 1 chở pallet chẵn từ 4 Ô Pickup vào cửa nhập Kho 01"],
        ["Toàn đội AGV - Quãng đường, Hiệu suất & Pin TB", f"{summary['agv_total_distance_m']:,.2f} m | Pin TB: {summary['agv_avg_battery_pct']:.2f}%", f"{len(tracker.agvs)} AGV", f"Chạy {summary['agv_avg_active_pct']:.1f}% | Chờ Traffic {summary['agv_avg_wait_pct']:.1f}% | Rỗi {summary['agv_avg_pure_idle_pct']:.1f}% | Lỗi: {summary['agv_fault_count']}"],
        ["Khu Dock - Pallet chẵn / Pallet lẻ xong", f"{summary['dock_even_pallets']} chẵn / {summary['dock_odd_pallets']} lẻ", "pallet", f"Đã quét barcode: {summary['dock_scanned_even']} kiện chẵn, {summary['dock_scanned_odd']} kiện lẻ"],
        ["Công nhân Unpack Tầng 1 (Carton Belt)", extra.get("loose_pallets_unpacked", tracker.loose_stats.get("pallets_unpacked", 0)), "pallet lẻ", f"Tổng thùng unpack lên băng tải: {extra.get('loose_cartons_delivered', tracker.loose_stats.get('cartons_delivered', 0))} thùng"],
    ]
    _write_table(
        ws1, row,
        ["Khu vực / Chỉ số", "Kết quả", "Đơn vị / Quy chuẩn", "Diễn giải"],
        kpi_rows,
        center_cols={3},
        auto_filter=False,
    )
    ws1.freeze_panes = "A4"
    _fit_columns(ws1, max_width=56)

    # ---------------------------------------------------------------- Sheet 2: Kho_ASRS_Shuttle
    ws2 = wb.create_sheet("Kho_ASRS_Shuttle")
    row = _style_banner(
        ws2, 1, 15,
        "KHU VỰC KHO AS/RS (9 TẦNG) - THÔNG SỐ CÀI ĐẶT, SHUTTLE, THANG NÂNG, HÀNG CHỜ & BIỂU ĐỒ",
        f"Mọi thời gian tính theo thời gian vận hành thực tế chuẩn 1x ({summary['sim_duration_hms']}), định dạng Giờ:Phút:Giây.",
    )
    top_sec_row = row
    _style_section(ws2, top_sec_row, 4, "1. TỔNG HỢP HOẠT ĐỘNG & TỈ LỆ KHO AS/RS")
    row = _style_section(
        ws2, top_sec_row, 6,
        "BIỂU ĐỒ TRỰC QUAN KHO AS/RS (TẦN SUẤT THEO GIỜ & TỈ LỆ HOẠT ĐỘNG TỐI ĐA 100%)",
        start_col=5, left_divider=True,
    )
    sec1_tbl_start = row
    sh_kpi_rows = [
        ["Kho AS/RS", "Tổng thời gian vận hành kho (1x chuẩn)", summary["sim_duration_hms"], "Thời gian mô phỏng chuẩn 1x"],
        ["Đội Shuttle (1..10)", "Tổng nhiệm vụ Shuttle - Nhập kho", int(summary["warehouse_inbound_tasks"]), "nhiệm vụ nhập kho"],
        ["Đội Shuttle (1..10)", "Tổng nhiệm vụ Shuttle - Xuất kho", int(summary["warehouse_outbound_tasks"]), "nhiệm vụ xuất kho"],
        ["Đội Shuttle (1..10)", "Tổng quãng đường 10 Shuttle", round(summary["shuttle_total_distance_m"], 2), "m (mét di chuyển)"],
        ["Shuttle chính (1..9)", "Tỉ lệ TB Shuttle hoạt động", round(summary["shuttle_avg_active_pct"], 2), "% thời gian chạy (Tối đa 100%)"],
        ["Shuttle chính (1..9)", "Tỉ lệ TB Shuttle đứng chơi", round(summary["shuttle_avg_idle_pct"], 2), "% đứng chờ / nghỉ"],
        ["Đội Shuttle (1..10)", "Tổng thời gian hàng chờ Shuttle", _fmt_vi_hms(summary["shuttle_total_wait_s"]), "Cộng dồn TG hàng chờ 10 Shuttle"],
        ["Đội Shuttle (1..10)", "Tổng số lỗi Shuttle", int(summary["shuttle_fault_count"]), "lỗi toàn đội Shuttle"],
        ["4 Thang nâng", "Tổng hàng qua 4 Thang nâng - Nhập kho", int(summary["lift_inbound_tasks"]), "lượt qua 2 Thang Nhập"],
        ["4 Thang nâng", "Tổng hàng qua 4 Thang nâng - Xuất kho", int(summary["lift_outbound_tasks"]), "lượt qua 2 Thang Xuất"],
        ["4 Thang nâng", "Tỉ lệ TB 4 Thang nâng hoạt động", round(summary["lift_avg_active_pct"], 2), "% thời gian chạy (Tối đa 100%)"],
        ["4 Thang nâng", "Tỉ lệ TB 4 Thang nâng nghỉ", round(summary["lift_avg_idle_pct"], 2), "% đứng chờ / nghỉ"],
        ["4 Thang nâng", "Tổng thời gian hàng chờ Thang nâng", _fmt_vi_hms(summary["lift_total_wait_s"]), "Cộng dồn TG hàng chờ 4 Thang nâng"],
        ["4 Thang nâng", "Tổng số lỗi Thang nâng", int(summary["lift_fault_count"]), "lỗi toàn hệ Thang nâng"],
    ]
    sec1_count_rows = {2, 3, 8, 9, 10, 14}
    sec1_pct_rows = {5, 6, 11, 12}
    row = _write_table(
        ws2, row,
        ["Nhóm thiết bị", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        sh_kpi_rows,
        center_cols={1, 3},
        auto_filter=False,
    )
    for rel_r in range(1, len(sh_kpi_rows) + 1):
        val_cell = ws2.cell(row=sec1_tbl_start + rel_r, column=3)
        if rel_r in sec1_count_rows:
            val_cell.number_format = "#,##0"
        elif rel_r == 4:
            val_cell.number_format = "#,##0.00"
        elif rel_r in sec1_pct_rows:
            val_cell.number_format = '0.00"%"'
    ws2.row_dimensions[row - 1].height = 18

    sec2_start_row = row
    row = _style_section(ws2, row, 4, "2. THÔNG SỐ CÀI ĐẶT SHUTTLE & THANG NÂNG KHO AS/RS")
    sh_cfg_rows = [
        ["Shuttle - Vận tốc di chuyển (shuttle_speed)", f"{float(cfg.get('shuttle_speed', 1.0)):.2f}", "m/s", "Tốc độ chạy dọc/ngang tối đa trên hệ ray 9 tầng"],
        ["Shuttle - Gia tốc / Độ giật (shuttle_acceleration / shuttle_jerk)", f"{float(cfg.get('shuttle_acceleration', 0.3)):.2f} / {float(cfg.get('shuttle_jerk', 0.6)):.2f}", "m/s² | m/s³", "Đồ thị chuyển động S-curve 7 pha tăng/giảm tốc mượt"],
        ["Shuttle - Thời gian chuyển hướng bánh X/Y (shuttle_turn_s)", f"{float(cfg.get('shuttle_turn_s', 5.0)):.2f}", "giây/lần", "Thời gian nâng-hạ bộ bánh xe khi rẽ góc 90° trên ray"],
        ["Shuttle - Thời gian định vị tinh tại ô kệ (shuttle_positioning_s)", f"{float(cfg.get('shuttle_positioning_s', 5.0)):.2f}", "giây/lần", "Thời gian căn chỉnh vị trí trước khi nâng/hạ pallet"],
        ["Shuttle - Thời gian nâng / hạ bàn nâng (shuttle_lift_s)", f"{float(cfg.get('shuttle_lift_s', 2.5)):.2f}", "giây/lần", "Thời gian kích thủy lực nâng/hạ pallet trên thân Shuttle"],
        ["Shuttle - Hệ số sẵn sàng & Sử dụng (availability / utilization)", f"{float(cfg.get('shuttle_availability', 0.7))*100:.1f}% / {float(cfg.get('shuttle_utilization', 0.7))*100:.1f}%", "%", "Hệ số khả dụng & hiệu suất thiết kế theo bảng tính 4-way Shuttle"],
        ["Thang nâng - Vận tốc nâng-hạ (lift_speed)", f"{float(cfg.get('lift_speed', 0.6)):.2f}", "m/s", "Tốc độ nâng/hạ cabin của 2 Thang Nhập & 2 Thang Xuất"],
        ["Thang nâng - Gia tốc / Độ giật (lift_acceleration / lift_jerk)", f"{float(cfg.get('lift_acceleration', 0.3)):.2f} / {float(cfg.get('lift_jerk', 0.8)):.2f}", "m/s² | m/s³", "Đồ thị chuyển động S-curve khi nâng/hạ cabin thang"],
        ["Thang nâng - Thời gian ổn định dừng tầng (lift_settle_s)", f"{float(cfg.get('lift_settle_s', 2.0)):.2f}", "giây/lần", "Thời gian cabin cân bằng chính xác trước khi chuyển pallet"],
        ["Thang nâng - Hệ số sẵn sàng & Sử dụng (availability / utilization)", f"{float(cfg.get('lift_availability', 0.8))*100:.1f}% / {float(cfg.get('lift_utilization', 0.8))*100:.1f}%", "%", "Hệ số khả dụng & hiệu suất khai thác thiết kế của hệ Thang nâng"],
        ["Quy mô Kho AS/RS & Băng tải (conveyor_speed)", f"10 Shuttle | 4 Thang | BT {float(cfg.get('conveyor_speed', 0.6)):.2f} m/s", "9 tầng (0.3m..16.3m)", f"9 Shuttle chính + 1 Standby T1 | Mục tiêu {_fmt_vi_hms(tracker.cycle_duration_s).split(' (')[0] if tracker.cycle_duration_s > 0 else 'Liên tục'}: {cfg.get('warehouse_inbound_target', '-')} nhập / {cfg.get('warehouse_outbound_target', '-')} xuất (Tồn: {cfg.get('initial_outbound_pallets', 0)} | Nhịp: {cfg.get('inbound_even_interval_s', cfg.get('inbound_interval_s'))}s/{cfg.get('outbound_interval_s')}s)"],
    ]
    row = _write_table(
        ws2, row,
        ["Thông số cài đặt Shuttle & Thang nâng", "Giá trị cấu hình", "Đơn vị", "Diễn giải / Tham số vận hành"],
        sh_cfg_rows,
        center_cols={2, 3},
        auto_filter=False,
    )
    sec2_end_row = row - 2
    ws2.row_dimensions[row - 1].height = 22
    ws2.row_dimensions[row].height = 22
    row += 1

    row = _style_section(ws2, row, 15, "3. BẢNG TỔNG HỢP TRẠNG THÁI & HÀNG CHỜ 10 SHUTTLE (TỪ SHUTTLE 1 ĐẾN SHUTTLE 10)")
    shuttle_rows = []
    tot_in = tot_out = tot_faults = 0
    tot_busy = tot_idle = tot_wait = 0.0
    for sid in [f"shuttle_{i}" for i in range(10)]:
        sh = tracker.shuttles[sid]
        cnt = sh["inbound_count"] + sh["outbound_count"]
        tot_in += sh["inbound_count"]
        tot_out += sh["outbound_count"]
        tot_faults += sh["fault_count"]
        tot_busy += sh["busy_s"]
        tot_idle += sh["idle_s"]
        tot_wait += sh["wait_s"]
        avg_wait = round(sh["wait_s"] / cnt, 2) if cnt > 0 else 0.0
        shuttle_rows.append([
            sh["code"], sh["role"],
            sh["inbound_count"], sh["outbound_count"], cnt,
            _excel_days(sh["busy_s"]), _excel_days(sh["idle_s"]), _excel_days(sh["wait_s"]), avg_wait,
            sh["active_pct"], sh["idle_pct"], sh["fault_count"],
            sh["status"], _fmt_codes(sh["task_codes"], 10),
        ])
    tot_sh_cnt = tot_in + tot_out
    row = _write_table(
        ws2, row,
        [
            "Mã Shuttle", "Vai trò vận hành",
            "Lượt nhập", "Lượt xuất", "Tổng NV",
            "TG hoạt động", "TG đứng chơi", "TG hàng chờ Shuttle", "Chờ TB / NV (s)",
            "Tỉ lệ chạy (%)", "Tỉ lệ đứng chơi (%)", "Số lỗi",
            "Trạng thái", "Danh sách mã nhiệm vụ",
        ],
        shuttle_rows,
        col_formats={
            3: "#,##0", 4: "#,##0", 5: "#,##0",
            6: FMT_HMS_TENTH, 7: FMT_HMS_TENTH, 8: FMT_HMS_TENTH, 9: "#,##0.00",
            10: "0.00", 11: "0.00", 12: "#,##0",
        },
        right_cols={3, 4, 5, 6, 7, 8, 9, 10, 11, 12},
        center_cols={1, 13},
        status_col=13,
        total_row=[
            "TỔNG / TB 10 SHUTTLE", "",
            tot_in, tot_out, tot_sh_cnt,
            _excel_days(tot_busy), _excel_days(tot_idle), _excel_days(tot_wait),
            round(tot_wait / tot_sh_cnt, 2) if tot_sh_cnt > 0 else 0.0,
            round(sum(r[9] for r in shuttle_rows[:9]) / 9.0, 2),
            round(sum(r[10] for r in shuttle_rows[:9]) / 9.0, 2),
            tot_faults, "(*) TB hàng % tính trên 9 Shuttle hoạt động chính", "",
        ],
        auto_filter=False,
    )

    row = _style_section(ws2, row, 15, "4. BÁO CÁO CHI TIẾT HOẠT ĐỘNG & HÀNG CHỜ 4 THANG NÂNG (KHO AS/RS)")
    lift_rows = []
    lf_tot_all = lf_tot_faults = lf_tot_wait_cnt = 0
    lf_tot_dist = lf_tot_busy = lf_tot_idle = lf_tot_wait = 0.0
    ordered_lifts = sorted(tracker.lifts.values(), key=lambda item: item["order"])
    for lf in ordered_lifts:
        cnt = lf["inbound_count"] + lf["outbound_count"]
        lf_tot_all += cnt
        lf_tot_faults += lf["fault_count"]
        lf_tot_dist += lf["distance_m"]
        lf_tot_busy += lf["busy_s"]
        lf_tot_idle += lf["idle_s"]
        lf_tot_wait += lf["wait_s"]
        denom = lf["wait_count"] if lf["wait_count"] > 0 else cnt
        lf_tot_wait_cnt += denom
        avg_lf_wait = round(lf["wait_s"] / denom, 2) if denom > 0 else 0.0
        lift_rows.append([
            lf["code"], f"{lf['direction']} ({lf['station']})",
            cnt,
            round(lf["distance_m"], 2),
            _excel_days(lf["busy_s"]), _excel_days(lf["idle_s"]),
            _excel_days(lf["wait_s"]), avg_lf_wait,
            lf["active_pct"], lf["idle_pct"], lf["fault_count"],
            lf["status"], _fmt_codes(lf["task_codes"], 10),
        ])
    lf_cnt = max(1, len(lift_rows))
    row = _write_table(
        ws2, row,
        [
            "Mã Thang nâng", "Cổng / Chức năng",
            "Tổng hàng qua thang",
            "Quãng đường nâng-hạ (m)", "TG hoạt động", "TG nghỉ",
            "TG hàng chờ Thang", "Chờ TB / lượt (s)",
            "Tỉ lệ chạy (%)", "Tỉ lệ nghỉ (%)", "Số lỗi",
            "Trạng thái", "Danh sách mã nhiệm vụ",
        ],
        lift_rows,
        col_formats={
            3: "#,##0",
            4: "#,##0.00", 5: FMT_HMS_TENTH, 6: FMT_HMS_TENTH,
            7: FMT_HMS_TENTH, 8: "#,##0.00",
            9: "0.00", 10: "0.00", 11: "#,##0",
        },
        right_cols={3, 4, 5, 6, 7, 8, 9, 10, 11},
        center_cols={1, 12},
        status_col=12,
        total_row=[
            "TỔNG / TB 4 THANG NÂNG", "",
            lf_tot_all,
            round(lf_tot_dist, 2), _excel_days(lf_tot_busy), _excel_days(lf_tot_idle),
            _excel_days(lf_tot_wait),
            round(lf_tot_wait / lf_tot_wait_cnt, 2) if lf_tot_wait_cnt > 0 else 0.0,
            round(sum(r[8] for r in lift_rows) / lf_cnt, 2),
            round(sum(r[9] for r in lift_rows) / lf_cnt, 2),
            lf_tot_faults, "", "",
        ],
        auto_filter=False,
    )

    sec5_start_row = row
    row = _style_section(ws2, row, 15, "5. THỜI GIAN CHỜ TỪNG CÔNG ĐOẠN (HÀNG CHỜ THANG NÂNG & HÀNG CHỜ SHUTTLE)")
    stage_rows = []
    tot_stg_events = tot_stg_waited = 0
    tot_stg_wait_s = max_stg_wait_s = 0.0
    for st in sorted(tracker.stage_waits.values(), key=lambda item: item["order"]):
        evs = st["total_events"]
        wevs = st["waited_events"]
        ws_val = round(st["total_wait_s"], 3)
        tot_stg_events += evs
        tot_stg_waited += wevs
        tot_stg_wait_s += ws_val
        max_stg_wait_s = max(max_stg_wait_s, st["max_wait_s"])
        avg_s = round(ws_val / evs, 2) if evs > 0 else 0.0
        stage_rows.append([
            st["label"],
            evs,
            f"{wevs} ({_pct(wevs, evs):.1f}%)" if evs > 0 else "0 (0.0%)",
            _excel_days(ws_val),
            avg_s,
            round(st["max_wait_s"], 2),
            st["desc"],
        ])
    row = _write_table(
        ws2, row,
        [
            "Công đoạn / Hàng chờ Kho AS/RS", "Số lượt qua trạm", "Số lượt phải chờ (>0s)",
            "Tổng TG hàng chờ", "Chờ TB / lượt (s)", "Chờ tối đa (s)", "Diễn giải chi tiết",
        ],
        stage_rows,
        col_formats={2: "#,##0", 4: FMT_HMS_TENTH, 5: "#,##0.00", 6: "#,##0.00"},
        right_cols={2, 4, 5, 6},
        center_cols={3},
        total_row=[
            "TỔNG CỘNG CÁC CÔNG ĐOẠN CHỜ AS/RS",
            tot_stg_events, tot_stg_waited, _excel_days(tot_stg_wait_s),
            round(tot_stg_wait_s / tot_stg_events, 2) if tot_stg_events > 0 else 0.0,
            round(max_stg_wait_s, 2), "",
        ],
        auto_filter=False,
    )
    sec5_end_row = row - 2

    row = _style_section(ws2, row, 15, "6. SỐ LIỆU TẦN SUẤT THEO GIỜ & TỈ LỆ HOẠT ĐỘNG KHO AS/RS (NGUỒN 3 BIỂU ĐỒ ĐẦU TRANG)")

    max_task_s = max((float(t.get("start_s") or 0.0) for t in tracker.warehouse_tasks), default=0.0)
    hour_span_s = max(float(tracker.sim_duration_s or 0.0), max_task_s)
    num_hours = max(1, int(math.ceil(max(0.0, hour_span_s - 1e-3) / 3600.0))) if hour_span_s > 0 else 1
    hour_buckets = [{"in": 0, "out": 0} for _ in range(num_hours)]
    for t in tracker.warehouse_tasks:
        h_idx = min(num_hours - 1, max(0, int(float(t.get("start_s") or 0.0) // 3600.0)))
        if t.get("direction") == "Nhập kho":
            hour_buckets[h_idx]["in"] += 1
        else:
            hour_buckets[h_idx]["out"] += 1

    total_hour_tasks = sum(b["in"] + b["out"] for b in hour_buckets)
    hour_totals = [b["in"] + b["out"] for b in hour_buckets]
    max_hr_cnt = max(hour_totals) if hour_totals else 0
    min_hr_cnt = min(hour_totals) if hour_totals else 0
    hourly_rows = []
    for h_idx, b in enumerate(hour_buckets):
        cnt = b["in"] + b["out"]
        share = round((cnt * 100.0 / total_hour_tasks), 2) if total_hour_tasks > 0 else 0.0
        if max_hr_cnt == 0:
            note = "Chưa có nhiệm vụ"
        elif len(hour_buckets) > 1 and max_hr_cnt > min_hr_cnt:
            if cnt == max_hr_cnt:
                note = "Bận rộn nhất (Cao điểm)"
            elif cnt == min_hr_cnt:
                note = "Rảnh nhất (Thấp điểm)"
            else:
                note = "Bình thường"
        else:
            note = "Phân bổ đều"
        hourly_rows.append([
            f"Giờ {h_idx + 1} ({h_idx:02d}:00-{h_idx + 1:02d}:00)",
            b["in"], b["out"], cnt, share, note,
        ])

    h_hdr_row = row
    h_last_data_row = h_hdr_row + len(hourly_rows)
    row = _write_table(
        ws2, row,
        ["Khung giờ (1x)", "Lượt nhập", "Lượt xuất", "Tổng nhiệm vụ", "Tỉ trọng (%)", "Đánh giá cao/thấp điểm"],
        hourly_rows,
        col_formats={2: "#,##0", 3: "#,##0", 4: "#,##0", 5: "0.00"},
        right_cols={2, 3, 4, 5},
        center_cols={1, 6},
        total_row=[
            "TỔNG CỘNG THEO GIỜ",
            sum(b["in"] for b in hour_buckets),
            sum(b["out"] for b in hour_buckets),
            total_hour_tasks,
            100.0 if total_hour_tasks > 0 else 0.0,
            f"Cao nhất: {max_hr_cnt} | Thấp nhất: {min_hr_cnt} NV/giờ",
        ],
        auto_filter=False,
    )

    active9_sh = [tracker.shuttles[f"shuttle_{i}"] for i in range(9)]
    active9_busy = sum(sh["busy_s"] for sh in active9_sh)
    active9_idle = sum(sh["idle_s"] for sh in active9_sh)
    active9_cap = active9_busy + active9_idle
    active_sh_count = sum(1 for i in range(10) if tracker.shuttles[f"shuttle_{i}"]["inbound_count"] + tracker.shuttles[f"shuttle_{i}"]["outbound_count"] > 0)
    pct9_act = _pct(active9_busy, active9_cap)
    pct9_idle = round(max(0.0, 100.0 - pct9_act), 2) if active9_cap > 0 else 0.0
    sh_state_rows = [
        [
            "Đang hoạt động (Chạy nhiệm vụ)",
            round(active9_busy / 60.0, 2),
            pct9_act,
            f"{active_sh_count}/10 Shuttle tham gia | {tot_in} nhập, {tot_out} xuất",
        ],
        [
            "Nghỉ / Chờ lệnh",
            round(active9_idle / 60.0, 2),
            pct9_idle,
            f"Shuttle 10 dự phòng (Standby) | Lỗi toàn đội: {tot_faults}",
        ],
    ]
    s_hdr_row = row
    s_last_data_row = s_hdr_row + len(sh_state_rows)
    row = _write_table(
        ws2, row,
        ["Trạng thái 9 Shuttle chính", "Thời gian (phút)", "Tỉ lệ (%)", "Ghi chú vận hành & Lỗi"],
        sh_state_rows,
        col_formats={2: "#,##0.00", 3: "0.00"},
        right_cols={2, 3},
        center_cols={1},
        total_row=[
            "TỔNG (TỐI ĐA 100%)",
            round(active9_cap / 60.0, 2),
            100.0 if active9_cap > 0 else 0.0,
            f"Tổng TG hàng chờ Shuttle (10 xe): {_fmt_vi_hms(tot_wait)} | Lỗi: {tot_faults}",
        ],
        auto_filter=False,
    )

    util_rows = []
    for i in range(10):
        sh = tracker.shuttles[f"shuttle_{i}"]
        act_v = round(min(100.0, max(0.0, float(sh["active_pct"]))), 2)
        idle_v = round(max(0.0, 100.0 - act_v), 2) if (act_v > 0 or sh["idle_pct"] > 0) else round(min(100.0, max(0.0, float(sh["idle_pct"]))), 2)
        util_rows.append([sh["code"], act_v, idle_v])
    for lf in ordered_lifts:
        act_v = round(min(100.0, max(0.0, float(lf["active_pct"]))), 2)
        idle_v = round(max(0.0, 100.0 - act_v), 2) if (act_v > 0 or lf["idle_pct"] > 0) else round(min(100.0, max(0.0, float(lf["idle_pct"]))), 2)
        util_rows.append([lf["code"], act_v, idle_v])

    u_hdr_row = row
    u_last_data_row = u_hdr_row + len(util_rows)
    left_end_row = _write_table(
        ws2, row,
        ["Thiết bị Kho AS/RS", "Tỉ lệ hoạt động (%)", "Tỉ lệ nghỉ (%)"],
        util_rows,
        col_formats={2: "0.00", 3: "0.00"},
        right_cols={2, 3},
        center_cols={1},
        auto_filter=False,
    )

    hour_bar = BarChart()
    hour_bar.type = "col"
    hour_bar.grouping = "clustered"
    hour_bar.style = 10
    _style_chart_title(hour_bar, "Tần suất Nhập / Xuất theo giờ (Kho AS/RS)")
    hour_bar.x_axis.title = "Khung giờ (1x)"
    hour_bar.y_axis.title = "Số nhiệm vụ"
    hour_bar.width = 21.4
    hour_bar.height = 11.0
    hr_data = Reference(ws2, min_col=2, max_col=4, min_row=h_hdr_row, max_row=h_last_data_row)
    hr_cats = Reference(ws2, min_col=1, min_row=h_hdr_row + 1, max_row=h_last_data_row)
    hour_bar.add_data(hr_data, titles_from_data=True)
    hour_bar.set_categories(hr_cats)
    hour_bar.legend.position = "b"
    ws2.add_chart(hour_bar, f"E{sec1_tbl_start}")

    sh_pie = PieChart()
    _style_chart_title(sh_pie, f"Tỉ lệ hoạt động 9 Shuttle chính (Tổng 100% | Lỗi: {tot_faults})")
    sh_pie.style = 10
    sh_pie.width = 14.2
    sh_pie.height = 11.0
    sh_pie_data = Reference(ws2, min_col=3, min_row=s_hdr_row, max_row=s_last_data_row)
    sh_pie_cats = Reference(ws2, min_col=1, min_row=s_hdr_row + 1, max_row=s_last_data_row)
    sh_pie.add_data(sh_pie_data, titles_from_data=True)
    sh_pie.set_categories(sh_pie_cats)
    sh_pie.dataLabels = DataLabelList()
    sh_pie.dataLabels.showPercent = True
    sh_pie.dataLabels.showVal = False
    sh_pie.dataLabels.showCatName = False
    sh_pie.legend.position = "b"
    ws2.add_chart(sh_pie, f"H{sec1_tbl_start}")

    util_bar = BarChart()
    util_bar.type = "col"
    util_bar.grouping = "stacked"
    util_bar.overlap = 100
    util_bar.style = 10
    _style_chart_title(util_bar, "Tỉ lệ hoạt động & nghỉ từng Shuttle / Thang nâng (Tối đa 100%)")
    util_bar.x_axis.title = "Thiết bị Kho AS/RS"
    util_bar.y_axis.title = "Tỉ lệ (%)"
    util_bar.y_axis.scaling.min = 0
    util_bar.y_axis.scaling.max = 100
    util_bar.width = 36.0
    util_bar.height = 14.0
    u_data = Reference(ws2, min_col=2, max_col=3, min_row=u_hdr_row, max_row=u_last_data_row)
    u_cats = Reference(ws2, min_col=1, min_row=u_hdr_row + 1, max_row=u_last_data_row)
    util_bar.add_data(u_data, titles_from_data=True)
    util_bar.set_categories(u_cats)
    util_bar.legend.position = "b"
    ws2.add_chart(util_bar, f"E{sec2_start_row}")

    row = left_end_row

    row = _style_section(ws2, row, 15, "7. NHẬT KÝ CHI TIẾT THEO QUY TRÌNH KHO AS/RS (THANG NÂNG & SHUTTLE)")
    wh_rows = []
    sum_wh_dur = sum_wh_lf_wait = sum_wh_sh_wait = sum_wh_dist = 0.0
    sum_wh_faults = 0
    for t in tracker.warehouse_tasks:
        d_s = t["duration_s"] or 0.0
        lw_s = t.get("lift_wait_s", 0.0) or 0.0
        sw_s = t.get("wait_s", 0.0) or 0.0
        sum_wh_dur += d_s
        sum_wh_lf_wait += lw_s
        sum_wh_sh_wait += sw_s
        sum_wh_dist += t["distance_m"]
        sum_wh_faults += t.get("fault_count", 0)
        wh_rows.append([
            t["task_code"], t["direction"], t["pallet_id"],
            t.get("pick_location") or "-",
            t.get("drop_location") or t["slot_id"],
            t.get("lift_code") or "-",
            t["shuttle_code"],
            _excel_days(t["start_s"]),
            _excel_days(lw_s),
            _excel_days(sw_s),
            _excel_days(t["end_s"]),
            _excel_days(t["duration_s"]),
            round(t["distance_m"], 2),
            t.get("fault_count", 0),
            t["status"],
        ])
    _write_table(
        ws2, row,
        [
            "Mã quy trình", "Loại giao dịch", "Mã Pallet",
            "Vị trí Pick hàng", "Vị trí Drop hàng",
            "Mã Thang nâng", "Mã Shuttle",
            "Thời điểm bắt đầu", "TG hàng đợi Thang nâng", "TG hàng đợi Shuttle",
            "Thời điểm kết thúc", "Tổng TG quy trình", "Quãng đường (m)", "Lỗi", "Trạng thái",
        ],
        wh_rows,
        col_formats={
            8: FMT_HMS_TENTH, 9: FMT_HMS_TENTH, 10: FMT_HMS_TENTH,
            11: FMT_HMS_TENTH, 12: FMT_HMS_TENTH, 13: "#,##0.00", 14: "#,##0",
        },
        right_cols={8, 9, 10, 11, 12, 13, 14},
        center_cols={1, 2, 6, 7, 15},
        status_col=15,
        total_row=[
            "TỔNG CỘNG", f"{len(wh_rows)} quy trình", "", "", "", "", "", "",
            _excel_days(sum_wh_lf_wait), _excel_days(sum_wh_sh_wait), "", _excel_days(sum_wh_dur),
            round(sum_wh_dist, 2), sum_wh_faults, "",
        ],
    )
    ws2.freeze_panes = "A4"
    _fit_columns(ws2, max_width=40)
    ws2_width_bounds = {
        1: (32, 36), 2: (38, 40), 3: (36, 37), 4: (35, 38),
        5: (34, 38), 6: (34, 38), 7: (36, 40),
        8: (22, 40), 9: (24, 40), 10: (22, 40),
    }
    for col_idx in range(1, 16):
        col_letter = get_column_letter(col_idx)
        cur_w = ws2.column_dimensions[col_letter].width or 11
        lo, hi = ws2_width_bounds.get(col_idx, (14, 40))
        ws2.column_dimensions[col_letter].width = max(lo, min(hi, cur_w))
    _fit_wrapped_row_heights(ws2, sec2_start_row + 2, sec2_end_row, max_col=4)
    _fit_wrapped_row_heights(ws2, sec5_start_row + 2, sec5_end_row, max_col=7)

    # ---------------------------------------------------------------- Sheet 3: Khu_Unpack_AGV
    ws3 = wb.create_sheet("Khu_Unpack_AGV")
    row = _style_banner(
        ws3, 1, 22,
        "KHU UNPACK & TOÀN ĐỘI AGV (HIỆU SUẤT, TRAFFIC, PIN, QUÃNG ĐƯỜNG, LỖI)",
        f"Thời gian quy chuẩn 1x thực tế (định dạng Giờ:Phút:Giây) | Pin = {tracker.init_battery_pct:.0f}% - ({tracker.drain_loaded_per_m}%/m tải + {tracker.drain_empty_per_m}%/m rỗng + {tracker.drain_idle_per_s}%/s chờ)",
    )
    row = _style_section(ws3, row, 22, "1. TỔNG HỢP TRAFFIC & TỈ LỆ HOẠT ĐỘNG ĐỘI AGV")
    agv_tr = tf.get("agv_traffic_stats", {}) or {}
    dual_ticks = int(tf.get("dual_agv_motion_ticks", 0) or 0)
    follow_ticks = int(tf.get("same_direction_following_ticks", 0) or 0)
    swept_chk = int(tf.get("swept_pose_checks", tf.get("route_checks", 0)) or 0)
    avg_agv_act = sum(a["active_pct"] for a in tracker.agvs.values()) / max(1, len(tracker.agvs))
    avg_agv_wait = sum(a["wait_pct"] for a in tracker.agvs.values()) / max(1, len(tracker.agvs))
    avg_agv_pure_idle = sum(a["pure_idle_pct"] for a in tracker.agvs.values()) / max(1, len(tracker.agvs))
    agv_kpi_rows = [
        [
            "Tổng quãng đường toàn đội AGV", f"{summary['agv_total_distance_m']:,.2f} m",
            "Tỉ lệ TB chạy / chờ Traffic / đứng rỗi", f"{avg_agv_act:.2f}% / {avg_agv_wait:.2f}% / {avg_agv_pure_idle:.2f}%",
            "Lượt AGV qua nút giao 4 tầng (AGV_TRAFFIC)", f"{agv_tr.get('trips', 0)} lượt | TG chờ tổng: {_fmt_vi_hms(agv_tr.get('wait_s', 0.0))}",
        ],
        [
            "Pin trung bình còn lại toàn đội", f"{summary['agv_avg_battery_pct']:.2f}%",
            "Số AGV lỗi toàn nhà máy", summary["agv_fault_count"],
            "Chờ kẹt tuyến tối đa / Kiểm tra va chạm (T1)", f"Max wait: {_fmt_vi_hms(agv_tr.get('max_wait_s', 0.0))} | Song song T1: {_pct(dual_ticks, engine_ticks):.1f}% | Swept checks: {swept_chk:,} | Bám đuôi: {follow_ticks} ticks",
        ],
    ]
    row = _write_table(
        ws3, row,
        ["Chỉ tiêu Đội AGV", "Giá trị", "Chỉ tiêu Trạng thái / Lỗi", "Giá trị", "Chỉ tiêu Traffic AGV", "Giá trị"],
        agv_kpi_rows,
        center_cols={2, 4},
        auto_filter=False,
    )

    row = _style_section(ws3, row, 22, "2. BẢNG TRẠNG THÁI CHI TIẾT TỪNG XE AGV (106 XE)")
    ordered_agvs = sorted(tracker.agvs.values(), key=lambda a: (a["group_order"], a["floor"], a["code"]))
    agv_rows = []
    sum_done = sum_run = sum_agv_faults = 0
    sum_ld = sum_em = sum_tot = sum_bs = sum_ws = sum_pid = 0.0
    for a in ordered_agvs:
        running = 1 if a["active_task"] else 0
        sum_done += a["missions_completed"]
        sum_run += running
        sum_agv_faults += a["fault_count"]
        sum_ld += a["loaded_distance_m"]
        sum_em += a["empty_distance_m"]
        sum_tot += a["total_distance_m"]
        sum_bs += a["busy_s"]
        sum_ws += a["wait_s"]
        sum_pid += a["pure_idle_s"]
        agv_rows.append([
            a["code"], a["id"], a["area"], f"Tầng {a['floor']}", a["station"],
            a["missions_completed"], running,
            round(a["loaded_distance_m"], 2), round(a["empty_distance_m"], 2), round(a["total_distance_m"], 2),
            _excel_days(a["busy_s"]), _excel_days(a["wait_s"]), _excel_days(a["pure_idle_s"]),
            a["active_pct"], a["wait_pct"], a["pure_idle_pct"],
            round(a["battery_used_pct"], 2), round(a["battery_remaining_pct"], 2),
            a["fault_count"], a["last_task"] or "-", a["status"], _fmt_codes(a["task_codes"], 10),
        ])
    _write_table(
        ws3, row,
        [
            "Mã AGV", "ID kỹ thuật", "Khu vực", "Tầng", "Trạm / Làn",
            "NV xong", "Đang chạy", "Có tải (m)", "Rỗng (m)", "Tổng quãng đường (m)",
            "TG hoạt động", "TG chờ Traffic", "TG đứng rỗi",
            "Tỉ lệ chạy (%)", "Tỉ lệ chờ Traffic (%)", "Tỉ lệ đứng rỗi (%)",
            "Pin hao (%)", "Pin còn (%)", "Số lỗi",
            "NV gần nhất", "Trạng thái", "Danh sách mã nhiệm vụ",
        ],
        agv_rows,
        col_formats={
            6: "#,##0", 7: "#,##0", 8: "#,##0.00", 9: "#,##0.00", 10: "#,##0.00",
            11: FMT_HMS_TENTH, 12: FMT_HMS_TENTH, 13: FMT_HMS_TENTH,
            14: "0.00", 15: "0.00", 16: "0.00", 17: "0.00", 18: "0.00", 19: "#,##0",
        },
        right_cols={6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19},
        center_cols={1, 4, 20, 21},
        status_col=21,
        total_row=[
            f"TỔNG / TB ({len(ordered_agvs)} AGV)", "", "", "", "",
            sum_done, sum_run, round(sum_ld, 2), round(sum_em, 2), round(sum_tot, 2),
            _excel_days(sum_bs), _excel_days(sum_ws), _excel_days(sum_pid),
            round(avg_agv_act, 2), round(avg_agv_wait, 2), round(avg_agv_pure_idle, 2),
            "", round(summary["agv_avg_battery_pct"], 2), sum_agv_faults, "", "", "",
        ],
    )
    ws3.freeze_panes = "A4"
    _fit_columns(ws3)

    # ---------------------------------------------------------------- Sheet 4: Khu_Vuc_Dock
    ws4 = wb.create_sheet("Khu_Vuc_Dock")
    dock_agv_rows = [t for t in tracker.agv_tasks if t["area"] == "Khu Dock -> Kho 01"]
    row = _style_banner(
        ws4, 1, 15,
        "BÁO CÁO KHU VỰC DOCK (AGV DOCK, PALLET CHẴN/LẺ, PICK/DROP, TRAFFIC GIAO CẮT)",
        f"Thời gian thực tế chuẩn 1x: {summary['sim_duration_hms']} | Pallet chẵn đi qua 4 Ô Pickup -> AGV Dock -> Kho 01; Pallet lẻ sang Bàn Unpack T1.",
    )
    row = _style_section(ws4, row, 15, "1. TỔNG HỢP KHU VỰC DOCK")
    tot_gate_wait = sum(d["gate_wait_s"] for d in tracker.docks.values())
    dock_sum_rows = [
        ["Số nhiệm vụ AGV khu vực Dock (chở pallet chẵn vào Kho 01)", len(dock_agv_rows), f"Hoàn thành: {sum(1 for r in dock_agv_rows if r['status']=='Hoàn thành')} | Đang chạy: {sum(1 for r in dock_agv_rows if r['status']=='Đang thực hiện')}"],
        ["Số nhiệm vụ Pallet Chẵn (Even) tại Dock", sum(d["even_ready"] for d in tracker.docks.values()), f"Batch xe tải: {sum(d['even_batches'] for d in tracker.docks.values())} | Giao ra 4 Ô Pickup: {tracker.dock_flow_stats.get('pallets_to_w1', 0)}"],
        ["Số nhiệm vụ Pallet Lẻ (Odd) tại Dock", sum(d["odd_ready"] for d in tracker.docks.values()), f"Batch xe tải: {sum(d['odd_batches'] for d in tracker.docks.values())} | Giao ra Bàn Unpack T1: {tracker.dock_flow_stats.get('pallets_to_w2', 0)}"],
        ["Tổng kiện quét barcode & Traffic chờ giao cắt AGV (FloorGate)", summary["dock_scanned_even"] + summary["dock_scanned_odd"], f"Chẵn: {summary['dock_scanned_even']} | Lẻ: {summary['dock_scanned_odd']} | Pallet rỗng hồi: {tracker.dock_flow_stats.get('empty_pallet_returns', 0)} | Chờ giao cắt làn AGV: {_fmt_vi_hms(tot_gate_wait)}"],
    ]
    row = _write_table(
        ws4, row,
        ["Chỉ tiêu khu vực Dock", "Số lượng", "Chi tiết phân bổ"],
        dock_sum_rows,
        col_formats={2: "#,##0"},
        right_cols={2},
        auto_filter=False,
    )

    row = _style_section(ws4, row, 15, "2. THỐNG KÊ CHI TIẾT TỪNG CỬA DOCK (PICK / DROP, THỜI GIAN & TRAFFIC)")
    per_dock_rows = []
    tot_d_busy = tot_d_wait = 0.0
    for did in tracker.dock_order:
        d = tracker.docks[did]
        busy_s = min(d["busy_s"], sim_dur) if sim_dur > 0 else d["busy_s"]
        tot_d_busy += busy_s
        tot_d_wait += d["gate_wait_s"]
        per_dock_rows.append([
            f"Dock {did:02d}", d["worker_code"],
            f"Xe tải Dock {did:02d}", "4 Ô Pickup (Chẵn) / Bàn Unpack T1 (Lẻ)",
            d["even_batches"], d["odd_batches"],
            d["even_cartons"], d["odd_cartons"],
            d["even_ready"], d["odd_ready"], d["empty_returns"],
            _excel_days(busy_s), _excel_days(d["gate_wait_s"]), _pct(busy_s, sim_dur),
            f"{d['agv_tasks']} ({_fmt_codes(d['agv_task_codes'], 6)})" if d["agv_tasks"] else "0",
        ])
    row = _write_table(
        ws4, row,
        [
            "Cửa Dock", "Công nhân", "Vị trí Pick", "Vị trí Drop",
            "Batch chẵn", "Batch lẻ", "Kiện chẵn", "Kiện lẻ",
            "Pallet chẵn xong", "Pallet lẻ xong", "Pallet rỗng hồi",
            "Tổng TG làm việc", "TG chờ giao cắt AGV", "Tỉ lệ hoạt động (%)", "NV AGV Dock (Số lượng & Mã)",
        ],
        per_dock_rows,
        col_formats={
            **{c: "#,##0" for c in range(5, 12)},
            12: FMT_HMS_TENTH, 13: FMT_HMS_TENTH, 14: "0.00",
        },
        right_cols=set(range(5, 15)),
        center_cols={1, 2},
        total_row=[
            "TỔNG", "", "", "",
            sum(r[4] for r in per_dock_rows), sum(r[5] for r in per_dock_rows),
            sum(r[6] for r in per_dock_rows), sum(r[7] for r in per_dock_rows),
            sum(r[8] for r in per_dock_rows), sum(r[9] for r in per_dock_rows),
            sum(r[10] for r in per_dock_rows),
            _excel_days(tot_d_busy), _excel_days(tot_d_wait),
            round(sum(r[13] for r in per_dock_rows) / max(1, len(per_dock_rows)), 2),
            f"{len(dock_agv_rows)} NV",
        ],
    )

    row = _style_section(ws4, row, 13, "3. DANH SÁCH NHIỆM VỤ AGV KHU VỰC DOCK (VỊ TRÍ PICK/DROP & THỜI GIAN)")
    sum_dk_dur = sum_dk_wait = sum_dk_dist = 0.0
    dock_agv_table = []
    for t in dock_agv_rows:
        sum_dk_dur += (t["duration_s"] or 0.0)
        sum_dk_wait += (t.get("wait_s", 0.0) or 0.0)
        sum_dk_dist += t["total_m"]
        dock_agv_table.append([
            t["task_code"], f"Dock {t['dock_id']:02d}" if t["dock_id"] is not None else "-",
            t["pallet_or_item"], t["agv_code"],
            t.get("pick_location") or "-", t.get("drop_location") or "-",
            _excel_days(t["start_s"]), _excel_days(t["end_s"]),
            _excel_days(t["duration_s"]), _excel_days(t.get("wait_s", 0.0)),
            round(t["total_m"], 2), t.get("fault_count", 0), t["status"],
        ])
    row = _write_table(
        ws4, row,
        [
            "Mã nhiệm vụ AGV", "Dock nguồn", "Mã Pallet", "Mã AGV",
            "Vị trí Pick hàng", "Vị trí Drop hàng",
            "Bắt đầu", "Kết thúc", "Tổng TG thực hiện", "TG chờ Traffic", "Quãng đường (m)", "Lỗi", "Trạng thái",
        ],
        dock_agv_table,
        col_formats={7: FMT_HMS_TENTH, 8: FMT_HMS_TENTH, 9: FMT_HMS_TENTH, 10: FMT_HMS_TENTH, 11: "#,##0.00", 12: "#,##0"},
        right_cols={7, 8, 9, 10, 11, 12},
        center_cols={1, 2, 4, 13},
        status_col=13,
        total_row=[
            "TỔNG CỘNG", f"{len(dock_agv_table)} NV", "", "", "", "", "", "",
            _excel_days(sum_dk_dur), _excel_days(sum_dk_wait), round(sum_dk_dist, 2),
            sum(r[11] for r in dock_agv_table), "",
        ],
    )

    row = _style_section(ws4, row, 6, "4. NHẬT KÝ SỰ KIỆN PALLET CHẴN / LẺ TẠI DOCK")
    dock_ev_rows = [
        [_excel_days(ev["time_s"]), f"Dock {ev['dock_id']:02d}", ev["kind"], ev["batch"], ev["state"], ev["detail"]]
        for ev in tracker.dock_events
    ]
    _write_table(
        ws4, row,
        ["Thời điểm (1x)", "Cửa Dock", "Phân loại Pallet", "Batch", "Sự kiện", "Chi tiết"],
        dock_ev_rows,
        col_formats={1: FMT_HMS_TENTH},
        right_cols={1, 4},
        center_cols={2, 3, 5},
    )
    ws4.freeze_panes = "A4"
    _fit_columns(ws4)

    # ---------------------------------------------------------------- Sheet 5: Cong_Nhan_Unpack
    ws5 = wb.create_sheet("Cong_Nhan_Unpack")
    row = _style_banner(
        ws5, 1, 13,
        "BÁO CÁO NĂNG SUẤT CÔNG NHÂN UNPACK & ĐÓNG GÓI TOÀN NHÀ MÁY",
        f"Thời gian quy chuẩn 1x ({summary['sim_duration_hms']}, định dạng Giờ:Phút:Giây) | Tỉ lệ làm việc & đứng chơi tính trên tổng thời gian vận hành.",
    )
    row = _style_section(ws5, row, 10, "1. TỔNG HỢP THEO NHÓM CÔNG NHÂN")
    grp_rows = _build_worker_groups(tracker, sim_dur)
    tot_hc = sum(r[1] for r in grp_rows)
    tot_w_busy = sum(w["busy_s"] for w in tracker.workers.values())
    tot_w_idle = sum(w["idle_s"] for w in tracker.workers.values())
    tot_cap = tot_hc * sim_dur
    row = _write_table(
        ws5, row,
        [
            "Khu vực công nhân", "Tổng nhân sự", "Có hoạt động", "Lượt pallet",
            "Thùng / Kiện xử lý", "Hộp thuốc xử lý",
            "Tổng TG làm việc", "Tổng TG đứng chơi", "Tỉ lệ làm việc (%)", "Tỉ lệ đứng chơi (%)",
        ],
        grp_rows,
        col_formats={2: "#,##0", 3: "#,##0", 4: "#,##0", 5: "#,##0", 6: "#,##0", 7: FMT_HMS, 8: FMT_HMS, 9: "0.00", 10: "0.00"},
        right_cols={2, 3, 4, 5, 6, 7, 8, 9, 10},
        total_row=[
            "TỔNG TOÀN NHÀ MÁY",
            tot_hc, sum(r[2] for r in grp_rows),
            sum(r[3] for r in grp_rows), sum(r[4] for r in grp_rows), sum(r[5] for r in grp_rows),
            _excel_days(tot_w_busy), _excel_days(tot_w_idle),
            _pct(tot_w_busy, tot_cap), round(max(0.0, 100.0 - _pct(tot_w_busy, tot_cap)), 2) if tot_cap > 0 else 0.0,
        ],
        auto_filter=False,
    )

    row = _style_section(ws5, row, 13, "2. BẢNG CHI TIẾT TỪNG CÔNG NHÂN UNPACK / XỬ LÝ HÀNG")
    ordered_workers = sorted(
        tracker.workers.values(),
        key=lambda w: (w["group_order"], 0 if w["cartons"] > 0 or w["med_boxes"] > 0 or w["busy_s"] > 0 else 1, w["floor"], w["code"]),
    )
    worker_rows = [
        [
            w["code"], w["area"], f"Tầng {w['floor']}", w["station"], w.get("route_desc") or "-",
            len(w["pallets"]), w["cartons"], w["med_boxes"], w["shared_picks"],
            _excel_days(w["busy_s"]), _excel_days(w["idle_s"]), w["active_pct"], w["idle_pct"],
        ]
        for w in ordered_workers
    ]
    _write_table(
        ws5, row,
        [
            "Mã công nhân", "Khu vực", "Tầng", "Vị trí / Trạm", "Tuyến Pick -> Drop",
            "Số pallet", "Thùng / Kiện unpack", "Hộp thuốc", "Hỗ trợ chéo",
            "TG làm việc", "TG đứng chơi", "Tỉ lệ làm (%)", "Tỉ lệ đứng (%)",
        ],
        worker_rows,
        col_formats={6: "#,##0", 7: "#,##0", 8: "#,##0", 9: "#,##0", 10: FMT_HMS_TENTH, 11: FMT_HMS_TENTH, 12: "0.00", 13: "0.00"},
        right_cols={6, 7, 8, 9, 10, 11, 12, 13},
        center_cols={1, 3},
    )
    ws5.freeze_panes = "A4"
    _fit_columns(ws5)

    # ---------------------------------------------------------------- Sheet 6: Chi_Tiet_Nhiem_Vu_AGV_Kho
    ws6 = wb.create_sheet("Chi_Tiet_Nhiem_Vu_AGV_Kho")
    row = _style_banner(
        ws6, 1, 19,
        "NHẬT KÝ CHI TIẾT TOÀN BỘ MÃ NHIỆM VỤ AGV (PICK / DROP, TRAFFIC, THỜI GIAN THỰC HIỆN)",
        f"Thời gian thực tế chuẩn 1x ({summary['sim_duration_hms']}, định dạng Giờ:Phút:Giây)",
    )
    agv_task_rows = []
    tot_at_dur = tot_at_wait = tot_at_ld = tot_at_em = tot_at_dist = 0.0
    tot_at_faults = 0
    for t in tracker.agv_tasks:
        tot_at_dur += (t["duration_s"] or 0.0)
        tot_at_wait += (t.get("wait_s", 0.0) or 0.0)
        tot_at_ld += t["loaded_m"]
        tot_at_em += t["empty_m"]
        tot_at_dist += t["total_m"]
        tot_at_faults += t.get("fault_count", 0)
        agv_task_rows.append([
            t["task_code"], t["area"], t["task_type"], t["agv_code"],
            f"Tầng {t['floor']}", t["station"],
            f"Dock {t['dock_id']:02d}" if t["dock_id"] is not None else "-",
            t["pallet_or_item"] or "-",
            t.get("pick_location") or "-", t.get("drop_location") or "-",
            _excel_days(t["start_s"]), _excel_days(t["end_s"]),
            _excel_days(t["duration_s"]), _excel_days(t.get("wait_s", 0.0)),
            round(t["loaded_m"], 2), round(t["empty_m"], 2), round(t["total_m"], 2),
            t.get("fault_count", 0), t["status"],
        ])
    _write_table(
        ws6, row,
        [
            "Mã nhiệm vụ AGV", "Khu vực", "Loại nhiệm vụ", "Mã AGV", "Tầng", "Trạm / Nhánh",
            "Dock nguồn", "Pallet / Rack / Trolley", "Vị trí Pick hàng", "Vị trí Drop hàng",
            "Bắt đầu", "Kết thúc", "Tổng TG thực hiện", "TG chờ Traffic",
            "Có tải (m)", "Rỗng (m)", "Tổng quãng đường (m)", "Lỗi", "Trạng thái",
        ],
        agv_task_rows,
        col_formats={
            11: FMT_HMS_TENTH, 12: FMT_HMS_TENTH, 13: FMT_HMS_TENTH, 14: FMT_HMS_TENTH,
            15: "#,##0.00", 16: "#,##0.00", 17: "#,##0.00", 18: "#,##0",
        },
        right_cols={11, 12, 13, 14, 15, 16, 17, 18},
        center_cols={1, 4, 5, 7, 19},
        status_col=19,
        total_row=[
            "TỔNG CỘNG", f"{len(agv_task_rows)} NV", "", "", "", "", "", "", "", "",
            "", "", _excel_days(tot_at_dur), _excel_days(tot_at_wait),
            round(tot_at_ld, 2), round(tot_at_em, 2), round(tot_at_dist, 2), tot_at_faults, "",
        ],
    )
    ws6.freeze_panes = "A4"
    _fit_columns(ws6)

    for target in (timestamped_path, latest_path):
        tmp_path = out_dir / f".{target.name}.{os.getpid()}.tmp"
        wb.save(tmp_path)
        os.replace(tmp_path, target)
    return {
        "timestamped": str(timestamped_path),
        "latest": str(latest_path),
    }
