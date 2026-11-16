"""Simulation KPI telemetry tracker and formatted Excel (.xlsx) report exporter."""
from __future__ import annotations

import copy
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
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
    ) -> None:
        sh = self.shuttles[shuttle_id]
        dock_id = self.job_dock_map.get(job_id) if job_id is not None else None
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
            "start_s": round(t_start, 3),
            "end_s": None,
            "duration_s": None,
            "wait_s": max(0.0, float(wait_s)),
            "distance_m": 0.0,
            "fault_count": 0,
            "fault_reason": "",
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
        if rec is not None:
            rec["end_s"] = round(t_end, 3)
            rec["duration_s"] = round(max(0.0, t_end - rec["start_s"]), 3)
            rec["distance_m"] = round(rec["distance_m"], 3)
            rec["status"] = "Lỗi" if rec["fault_count"] > 0 else "Hoàn thành"
        sh["active_task"] = None

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


def _style_section(ws, row: int, col_count: int, title: str) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_count)
    c = ws.cell(row=row, column=1, value=title)
    c.font = FONT_SECTION
    c.fill = FILL_SECTION
    c.alignment = ALIGN_LEFT
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
        ["Chu kỳ nhập / xuất (inbound / outbound interval)", f"{cfg.get('inbound_interval_s')}s / {cfg.get('outbound_interval_s')}s", "giây (1x)", f"Số cửa Dock mở: {cfg.get('dock_active_count')} | Phân loại: {cfg.get('inbound_classification', 'barcode')}"],
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
        ["Kho AS/RS - Hiệu suất & Thời gian 9 Shuttle chính", f"{summary['shuttle_avg_active_pct']:.2f}% chạy / {summary['shuttle_avg_idle_pct']:.2f}% đứng chơi", _fmt_vi_hms(summary['shuttle_total_busy_s']), f"Tổng TG chờ Traffic: {_fmt_vi_hms(summary['shuttle_total_wait_s'])} | Đổi bánh X/Y: {summary['shuttle_total_turns']} lượt"],
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
        ws2, 1, 18,
        "KHU VỰC KHO AS/RS (9 TẦNG) - HIỆU SUẤT, TRAFFIC, PICK/DROP & TRẠNG THÁI 10 SHUTTLE",
        f"Mọi thời gian tính theo thời gian vận hành thực tế chuẩn 1x ({summary['sim_duration_hms']}), định dạng Giờ:Phút:Giây.",
    )
    row = _style_section(ws2, row, 18, "1. TỔNG HỢP HOẠT ĐỘNG, TỈ LỆ & TRAFFIC KHO AS/RS")
    multi_sh_ticks = int(tf.get("multiple_shuttle_motion_ticks", 0) or 0)
    overlap_ticks = int(tf.get("inbound_outbound_overlap_ticks", 0) or 0)
    rail_chk = int(tf.get("rail_contact_checks", 0) or 0)
    sh_kpi_rows = [
        [
            "Tổng thời gian vận hành kho (1x chuẩn)", summary["sim_duration_hms"],
            "Tỉ lệ TB Shuttle hoạt động (Shuttle 1..9)", f"{summary['shuttle_avg_active_pct']:.2f}%",
            "Tổng thời gian chờ Traffic / Lock tầng", _fmt_vi_hms(summary["shuttle_total_wait_s"]),
        ],
        [
            "Tổng nhiệm vụ Nhập / Xuất kho", f"{summary['warehouse_inbound_tasks']} nhập / {summary['warehouse_outbound_tasks']} xuất",
            "Tỉ lệ TB Shuttle đứng chơi (Shuttle 1..9)", f"{summary['shuttle_avg_idle_pct']:.2f}%",
            "Đa Shuttle chạy song song / Nhập-Xuất chồng lấn", f"{_pct(multi_sh_ticks, engine_ticks):.2f}% ({multi_sh_ticks} ticks) / {_pct(overlap_ticks, engine_ticks):.2f}% ({overlap_ticks} ticks)",
        ],
        [
            "Tổng quãng đường 10 Shuttle", f"{summary['shuttle_total_distance_m']:,.2f} m",
            "Tổng lượt lỗi Shuttle", summary["shuttle_fault_count"],
            "Tổng chuyển hướng X/Y & Kiểm tra an toàn ray", f"{summary['shuttle_total_turns']} lần chuyển bánh | {rail_chk:,} lần kiểm tra ray",
        ],
    ]
    row = _write_table(
        ws2, row,
        ["Chỉ tiêu Kho AS/RS", "Giá trị", "Chỉ tiêu Trạng thái / Lỗi", "Giá trị", "Chỉ tiêu Traffic Kho", "Giá trị"],
        sh_kpi_rows,
        center_cols={2, 4},
        auto_filter=False,
    )

    row = _style_section(ws2, row, 18, "2. BẢNG TỔNG HỢP TRẠNG THÁI 10 SHUTTLE (TỪ SHUTTLE 1 ĐẾN SHUTTLE 10)")
    shuttle_rows = []
    tot_in = tot_out = tot_turns = tot_faults = 0
    tot_dist = tot_busy = tot_idle = tot_wait = 0.0
    for sid in [f"shuttle_{i}" for i in range(10)]:
        sh = tracker.shuttles[sid]
        cnt = sh["inbound_count"] + sh["outbound_count"]
        tot_in += sh["inbound_count"]
        tot_out += sh["outbound_count"]
        tot_turns += sh["turn_count"]
        tot_faults += sh["fault_count"]
        tot_dist += sh["distance_m"]
        tot_busy += sh["busy_s"]
        tot_idle += sh["idle_s"]
        tot_wait += sh["wait_s"]
        shuttle_rows.append([
            sh["code"], sh["id"], f"Tầng {sh['level']}", sh["role"],
            sh["inbound_count"], sh["outbound_count"], cnt, sh["turn_count"],
            round(sh["distance_m"], 2),
            _excel_days(sh["busy_s"]), _excel_days(sh["idle_s"]), _excel_days(sh["wait_s"]),
            sh["active_pct"], sh["idle_pct"], sh["fault_count"],
            sh["last_task"] or "-", sh["status"], _fmt_codes(sh["task_codes"], 10),
        ])
    row = _write_table(
        ws2, row,
        [
            "Mã Shuttle", "ID kỹ thuật", "Tầng kho", "Vai trò vận hành",
            "Lượt nhập", "Lượt xuất", "Tổng NV", "Đổi trục X/Y",
            "Quãng đường (m)", "TG hoạt động", "TG đứng chơi", "TG chờ Traffic",
            "Tỉ lệ chạy (%)", "Tỉ lệ đứng chơi (%)", "Số lỗi",
            "NV gần nhất", "Trạng thái", "Danh sách mã nhiệm vụ",
        ],
        shuttle_rows,
        col_formats={
            5: "#,##0", 6: "#,##0", 7: "#,##0", 8: "#,##0", 9: "#,##0.00",
            10: FMT_HMS_TENTH, 11: FMT_HMS_TENTH, 12: FMT_HMS_TENTH,
            13: "0.00", 14: "0.00", 15: "#,##0",
        },
        right_cols={5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
        center_cols={1, 2, 3, 16, 17},
        status_col=17,
        total_row=[
            "TỔNG / TB 10 SHUTTLE", "", "", "",
            tot_in, tot_out, tot_in + tot_out, tot_turns,
            round(tot_dist, 2),
            _excel_days(tot_busy), _excel_days(tot_idle), _excel_days(tot_wait),
            round(sum(r[12] for r in shuttle_rows[:9]) / 9.0, 2),
            round(sum(r[13] for r in shuttle_rows[:9]) / 9.0, 2),
            tot_faults, "", "(*) TB hàng % tính trên 9 Shuttle hoạt động chính", "",
        ],
        auto_filter=False,
    )

    row = _style_section(ws2, row, 16, "3. NHẬT KÝ NHIỆM VỤ NHẬP / XUẤT KHO AS/RS (VỊ TRÍ PICK, DROP & TỔNG THỜI GIAN)")
    wh_rows = []
    sum_wh_dur = sum_wh_wait = sum_wh_dist = 0.0
    sum_wh_faults = 0
    for t in tracker.warehouse_tasks:
        d_s = t["duration_s"] or 0.0
        w_s = t.get("wait_s", 0.0) or 0.0
        sum_wh_dur += d_s
        sum_wh_wait += w_s
        sum_wh_dist += t["distance_m"]
        sum_wh_faults += t.get("fault_count", 0)
        wh_rows.append([
            t["task_code"], t["direction"], t["pallet_id"],
            f"REQ-{t['request_id'] + 1:04d}" if t["request_id"] is not None else (f"JOB-{t['job_id'] + 1:04d}" if t["job_id"] is not None else "-"),
            f"Dock {t['dock_id']:02d}" if t["dock_id"] is not None else "Có sẵn đầu ca",
            f"Tầng {t['level']}",
            t.get("pick_location") or "-",
            t.get("drop_location") or t["slot_id"],
            t["shuttle_code"],
            _excel_days(t["start_s"]),
            _excel_days(t["end_s"]),
            _excel_days(t["duration_s"]),
            _excel_days(w_s),
            round(t["distance_m"], 2),
            t.get("fault_count", 0),
            t["status"],
        ])
    _write_table(
        ws2, row,
        [
            "Mã nhiệm vụ", "Loại giao dịch", "Mã Pallet", "Mã Job / Yêu cầu",
            "Dock nguồn", "Tầng kho", "Vị trí Pick hàng", "Vị trí Drop hàng",
            "Mã Shuttle", "Bắt đầu", "Kết thúc", "Tổng TG thực hiện",
            "TG chờ Traffic", "Quãng đường (m)", "Lỗi", "Trạng thái",
        ],
        wh_rows,
        col_formats={
            10: FMT_HMS_TENTH, 11: FMT_HMS_TENTH, 12: FMT_HMS_TENTH,
            13: FMT_HMS_TENTH, 14: "#,##0.00", 15: "#,##0",
        },
        right_cols={10, 11, 12, 13, 14, 15},
        center_cols={1, 2, 4, 5, 6, 9, 16},
        status_col=16,
        total_row=[
            "TỔNG CỘNG", f"{len(wh_rows)} nhiệm vụ", "", "", "", "", "", "", "", "", "",
            _excel_days(sum_wh_dur), _excel_days(sum_wh_wait), round(sum_wh_dist, 2), sum_wh_faults, "",
        ],
    )
    ws2.freeze_panes = "A4"
    _fit_columns(ws2)

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
