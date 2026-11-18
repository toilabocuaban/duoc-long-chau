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


def _merge_intervals(spans: list[tuple[float, float]], clamp_end: float | None = None) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for a, b in spans:
        lo = max(0.0, float(a))
        hi = float(b) if clamp_end is None else min(float(clamp_end), float(b))
        if hi > lo + 1e-9:
            cleaned.append((lo, hi))
    if not cleaned:
        return []
    cleaned.sort()
    merged = [cleaned[0]]
    for lo, hi in cleaned[1:]:
        p_lo, p_hi = merged[-1]
        if lo <= p_hi + 1e-9:
            merged[-1] = (p_lo, max(p_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _subtract_intervals_seconds(
    wait_spans: list[tuple[float, float]],
    busy_spans: list[tuple[float, float]],
    clamp_end: float | None = None,
) -> float:
    w_merged = _merge_intervals(wait_spans, clamp_end)
    if not w_merged:
        return 0.0
    b_merged = _merge_intervals(busy_spans, clamp_end)
    total = 0.0
    b_idx = 0
    for w_lo, w_hi in w_merged:
        cur = w_lo
        while b_idx < len(b_merged) and b_merged[b_idx][1] <= cur + 1e-9:
            b_idx += 1
        j = b_idx
        while j < len(b_merged) and b_merged[j][0] < w_hi - 1e-9:
            b_lo, b_hi = b_merged[j]
            if b_lo > cur:
                total += b_lo - cur
            cur = max(cur, b_hi)
            if cur >= w_hi - 1e-9:
                break
            j += 1
        if cur < w_hi - 1e-9:
            total += w_hi - cur
    return total


def _append_span(spans: list[tuple[float, float]], t_start: float | None, t_end: float | None) -> None:
    if t_start is None or t_end is None:
        return
    lo, hi = float(t_start), float(t_end)
    if hi > lo + 1e-9:
        spans.append((max(0.0, lo), hi))


def _finalize_three_state(
    entry: dict[str, Any],
    sim_dur: float,
    raw_wait_s: float = 0.0,
) -> tuple[float, float, float, float, float]:
    """Split device/worker time into mutually exclusive (busy, cmd_wait, pure_idle) summing to sim_dur (100%)."""
    eff_busy = min(float(entry["busy_s"]), sim_dur) if sim_dur > 0 else float(entry["busy_s"])
    rem_cap = max(0.0, sim_dur - eff_busy)
    w_spans = entry.get("wait_spans") or []
    b_spans = entry.get("busy_spans") or []
    if w_spans:
        raw_cw = _subtract_intervals_seconds(w_spans, b_spans, sim_dur if sim_dur > 0 else None)
    else:
        raw_cw = max(0.0, float(raw_wait_s))
    cmd_wait_s = min(raw_cw, rem_cap) if sim_dur > 0 else raw_cw
    pure_idle_s = max(0.0, sim_dur - eff_busy - cmd_wait_s) if sim_dur > 0 else 0.0
    act_pct = _pct(eff_busy, sim_dur)
    cmd_wait_pct = round(min(max(0.0, 100.0 - act_pct), _pct(cmd_wait_s, sim_dur)), 2) if sim_dur > 0 else 0.0
    pure_idle_pct = round(max(0.0, 100.0 - act_pct - cmd_wait_pct), 2) if sim_dur > 0 else 0.0
    entry["busy_s"] = round(eff_busy, 3)
    entry["cmd_wait_s"] = round(cmd_wait_s, 3)
    entry["pure_idle_s"] = round(pure_idle_s, 3)
    entry["idle_s"] = round(max(0.0, sim_dur - eff_busy), 3)
    entry["active_pct"] = act_pct
    entry["cmd_wait_pct"] = cmd_wait_pct
    entry["pure_idle_pct"] = pure_idle_pct
    entry["idle_pct"] = round(max(0.0, 100.0 - act_pct), 2) if sim_dur > 0 else 0.0
    return eff_busy, cmd_wait_s, pure_idle_s, act_pct, cmd_wait_pct


def _agv_display_meta(agv_id: str) -> tuple[str, str, int, str, int]:
    """Return (display_code, area_label, floor, station_label, sort_group_order)."""
    if agv_id.startswith("u1_agv_"):
        _, _, fl, idx = agv_id.split("_")
        f, b = int(fl), int(idx)
        return f"U1-AGV-T{f}-N{b + 1}", "Khu Unpack U1", f, f"Đội U1-{b + 1} (T{f})", 0
    if agv_id.startswith("u2_agv_"):
        _, _, fl, idx = agv_id.split("_")
        f, u = int(fl), int(idx)
        return f"U2-AGV-T{f}-S{u + 1}", "Khu Unpack U2 (Zone 05)", f, f"Đội U2-{u + 1} (T{f})", 1
    if agv_id.startswith("agv_"):
        lane = int(agv_id.split("_")[1])
        return f"AGV-{lane + 1:02d}", "Khu Dock -> Cửa nhập Kho 01", 1, f"Làn nhập {lane + 1}", 2
    if agv_id.startswith("p1_agv_"):
        _, _, fl, idx = agv_id.split("_")
        f, s = int(fl), int(idx)
        grp = f"Đội P1-Bắc-{s + 1}" if s < 3 else (f"Đội P1-Tây-{s - 2}" if s < 6 else f"Trạm P1-{s + 1:02d}")
        return f"P1-AGV-T{f}-S{s + 1:02d}", "Khu Đóng Gói P1", f, grp, 3
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
                "return_count": 0,
                "turn_count": 0,
                "distance_m": 0.0,
                "busy_s": 0.0,
                "busy_spans": [],
                "wait_s": 0.0,
                "wait_spans": [],
                "cmd_wait_s": 0.0,
                "pure_idle_s": 0.0,
                "idle_s": 0.0,
                "active_pct": 0.0,
                "cmd_wait_pct": 0.0,
                "pure_idle_pct": 0.0,
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
                "return_count": 0,
                "distance_m": 0.0,
                "busy_s": 0.0,
                "busy_spans": [],
                "wait_s": 0.0,
                "wait_spans": [],
                "cmd_wait_s": 0.0,
                "pure_idle_s": 0.0,
                "wait_count": 0,
                "max_wait_s": 0.0,
                "idle_s": 0.0,
                "active_pct": 0.0,
                "cmd_wait_pct": 0.0,
                "pure_idle_pct": 0.0,
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
        u1_cnt = int(config.get("u1_agvs_per_floor", len(LG.U1_AGV_HOME_BRANCHES)))
        u2_cnt = int(config.get("u2_agvs_per_floor", len(LG.U2_AGV_HOME_STATIONS)))
        p1_cnt = int(config.get("p1_agvs_per_floor", len(LG.P1_AGV_HOME_STATIONS)))
        for f in LG.floors():
            for b in range(max(1, min(len(LG.BRANCHES), u1_cnt))):
                self._register_agv(f"u1_agv_{f}_{b}")
            for u in range(max(1, min(len(LG.U2_WORKERS), u2_cnt))):
                self._register_agv(f"u2_agv_{f}_{u}")
            for st in range(max(1, min(len(LG.P1_STATIONS), p1_cnt))):
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
        self._dock_batch_wait: dict[tuple[int, Any], float] = {}
        self._dock_batch_wait_spans: dict[tuple[int, Any], list[tuple[float, float]]] = {}
        self.dock_flow_stats: dict[str, int] = {}
        self.loose_stats: dict[str, Any] = {}
        self.traffic_stats: dict[str, Any] = {}
        self.z05_partial_returns: int = 0

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
            "slot_wait_s": 0.0,
            "wait_spans": [],
            "busy_spans": [],
            "busy_s": 0.0,
            "cmd_wait_s": 0.0,
            "pure_idle_s": 0.0,
            "idle_s": 0.0,
            "active_pct": 0.0,
            "cmd_wait_pct": 0.0,
            "pure_idle_pct": 0.0,
            "idle_pct": 0.0,
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
                "busy_spans": [],
                "wait_s": 0.0,
                "wait_spans": [],
                "cmd_wait_s": 0.0,
                "pure_idle_s": 0.0,
                "idle_s": 0.0,
                "active_pct": 0.0,
                "cmd_wait_pct": 0.0,
                "pure_idle_pct": 0.0,
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
        elif direction.startswith("Hoàn kho"):
            sh["return_count"] += 1
        else:
            sh["outbound_count"] += 1
        return rec

    def record_shuttle_leg(
        self, shuttle_id: str, distance_m: float, duration_s: float,
        t_start: float | None = None, t_end: float | None = None,
    ) -> None:
        sh = self.shuttles[shuttle_id]
        sh["distance_m"] += max(0.0, float(distance_m))
        sh["busy_s"] += max(0.0, float(duration_s))
        _append_span(sh["busy_spans"], t_start, t_end)
        rec = self._active_wh_tasks.get(shuttle_id)
        if rec is not None:
            rec["distance_m"] += max(0.0, float(distance_m))

    def record_shuttle_busy(
        self, shuttle_id: str, duration_s: float,
        t_start: float | None = None, t_end: float | None = None,
    ) -> None:
        sh = self.shuttles[shuttle_id]
        sh["busy_s"] += max(0.0, float(duration_s))
        _append_span(sh["busy_spans"], t_start, t_end)

    def record_shuttle_turn(
        self, shuttle_id: str, duration_s: float,
        t_start: float | None = None, t_end: float | None = None,
    ) -> None:
        sh = self.shuttles[shuttle_id]
        sh["turn_count"] += 1
        sh["busy_s"] += max(0.0, float(duration_s))
        _append_span(sh["busy_spans"], t_start, t_end)

    def record_shuttle_wait_span(self, shuttle_id: str, t_start: float, t_end: float) -> None:
        if shuttle_id in self.shuttles:
            _append_span(self.shuttles[shuttle_id]["wait_spans"], t_start, t_end)

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
        elif direction.startswith("Hoàn kho"):
            lf["return_count"] += 1
        else:
            lf["outbound_count"] += 1

    def record_lift_leg(
        self, lift_id: str, height_m: float, duration_s: float,
        t_start: float | None = None, t_end: float | None = None,
    ) -> None:
        if lift_id not in self.lifts:
            return
        lf = self.lifts[lift_id]
        lf["distance_m"] += max(0.0, float(height_m))
        lf["busy_s"] += max(0.0, float(duration_s))
        _append_span(lf["busy_spans"], t_start, t_end)

    def record_lift_wait_span(self, lift_id: str, t_start: float, t_end: float) -> None:
        if lift_id in self.lifts:
            _append_span(self.lifts[lift_id]["wait_spans"], t_start, t_end)

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
        t_start: float | None = None,
        t_end: float | None = None,
    ) -> None:
        w = self.workers.get(worker_key)
        if w is None:
            return
        w["busy_s"] += max(0.0, float(duration_s))
        _append_span(w["busy_spans"], t_start, t_end)
        w["cartons"] += int(cartons)
        w["med_boxes"] += int(med_boxes)
        if pallet_id is not None:
            w["pallets"].add(str(pallet_id))
        if shared_pick:
            w["shared_picks"] += 1

    def record_worker_wait_span(self, worker_key: str, t_start: float, t_end: float) -> None:
        w = self.workers.get(worker_key)
        if w is not None:
            _append_span(w["wait_spans"], t_start, t_end)

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

        if state in ("DOCK_GATE_WAIT", "DOCK_SLOT_WAIT") and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            ws = max(0.0, float(record.get("wait_s", 0.0) or 0.0))
            if state == "DOCK_GATE_WAIT":
                d["gate_wait_s"] += ws
            else:
                d["slot_wait_s"] += ws
            b_key = record.get("batch")
            s_t0, s_t1 = record.get("start_s"), record.get("end_s")
            if b_key is not None:
                self._dock_batch_wait[(did, b_key)] = self._dock_batch_wait.get((did, b_key), 0.0) + ws
                if s_t0 is not None and s_t1 is not None:
                    _append_span(self._dock_batch_wait_spans.setdefault((did, b_key), []), s_t0, s_t1)
            _append_span(d["wait_spans"], s_t0, s_t1)
            w = self.workers.get(f"dock_{did}")
            if w is not None:
                w["wait_s"] += ws
                _append_span(w["wait_spans"], s_t0, s_t1)
            return

        if state == "EMPTY_PALLET_FROM_POOL" and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            d["even_pool_delivered"] += 1
            b_key = record.get("batch")
            t0 = self._dock_batch_t0.pop((did, "even", b_key), None)
            if t0 is not None:
                self._record_dock_batch_work(did, b_key, t0, float(t))
            else:
                self._dock_batch_wait.pop((did, b_key), None)
                self._dock_batch_wait_spans.pop((did, b_key), None)
            return

        if state == "EMPTY_PALLET_RETURNED" and dock_id is not None:
            did = int(dock_id)
            d = self._dock_entry(did)
            d["empty_returns"] += 1
            kind = record.get("classification", "odd")
            b_key = record.get("batch")
            t0 = self._dock_batch_t0.pop((did, kind, b_key), None)
            if t0 is not None:
                self._record_dock_batch_work(did, b_key, t0, float(t))
            else:
                self._dock_batch_wait.pop((did, b_key), None)
                self._dock_batch_wait_spans.pop((did, b_key), None)
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

        if state == "Z05_PALLET_PARTIAL_RETURN":
            self.z05_partial_returns += 1
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

    def _record_dock_batch_work(self, did: int, b_key: Any, t0: float, t1: float) -> None:
        """Record net active batch work excluding any DOCK_GATE_WAIT / DOCK_SLOT_WAIT sub-intervals."""
        d = self._dock_entry(did)
        bw = self._dock_batch_wait.pop((did, b_key), 0.0)
        w_spans = _merge_intervals(self._dock_batch_wait_spans.pop((did, b_key), []), clamp_end=t1)
        dur = max(0.0, (t1 - t0) - bw)
        d["busy_s"] += dur
        cur = float(t0)
        active_spans: list[tuple[float, float]] = []
        for ws, we in w_spans:
            ws_c = max(float(t0), ws)
            we_c = min(float(t1), we)
            if ws_c > cur + 1e-9:
                active_spans.append((cur, ws_c))
            if we_c > cur:
                cur = we_c
        if float(t1) > cur + 1e-9:
            active_spans.append((cur, float(t1)))
        for a0, a1 in active_spans:
            _append_span(d["busy_spans"], a0, a1)
        self.record_worker_activity(f"dock_{did}", duration_s=dur)
        w = self.workers.get(f"dock_{did}")
        if w is not None:
            for a0, a1 in active_spans:
                _append_span(w["busy_spans"], a0, a1)

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

        for (did, _, b_key), t0 in list(self._dock_batch_t0.items()):
            self._record_dock_batch_work(did, b_key, t0, self.sim_duration_s)
        self._dock_batch_t0.clear()
        self._dock_batch_wait.clear()
        self._dock_batch_wait_spans.clear()

        for sh in self.shuttles.values():
            sh["distance_m"] = round(sh["distance_m"], 3)
            sh["wait_s"] = round(sh["wait_s"], 3)
            _finalize_three_state(sh, self.sim_duration_s, raw_wait_s=sh["wait_s"])
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
            lf["wait_s"] = round(lf["wait_s"], 3)
            lf["max_wait_s"] = round(lf["max_wait_s"], 3)
            _finalize_three_state(lf, self.sim_duration_s, raw_wait_s=lf["wait_s"])
            if lf["fault_count"] > 0:
                lf["status"] = "Lỗi"
            elif lf["active_task"] is not None:
                lf["status"] = "Đang chạy khi dừng"
            elif lf["inbound_count"] + lf["outbound_count"] > 0:
                lf["status"] = "Hoạt động bình thường"
            else:
                lf["status"] = "Chờ lệnh"

        for d in self.docks.values():
            raw_d_wait = float(d["gate_wait_s"]) + float(d.get("slot_wait_s", 0.0))
            d["gate_wait_s"] = round(float(d["gate_wait_s"]), 3)
            d["slot_wait_s"] = round(float(d.get("slot_wait_s", 0.0)), 3)
            _finalize_three_state(d, self.sim_duration_s, raw_wait_s=raw_d_wait)

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
            _finalize_three_state(w, self.sim_duration_s, raw_wait_s=float(w.get("wait_s", 0.0)))

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
        wh_ret = sum(1 for t in self.warehouse_tasks if str(t.get("direction", "")).startswith("Hoàn kho"))
        dock_agv_tasks = [t for t in self.agv_tasks if t["area"] == "Khu Dock -> Kho 01"]
        unpack_agv_tasks = [t for t in self.agv_tasks if t["area"] in ("Khu Unpack U1", "Khu Unpack U2 (Zone 05)")]
        p1_agv_tasks = [t for t in self.agv_tasks if t["area"] == "Khu Đóng Gói P1"]
        avg_bat = sum(a["battery_remaining_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        avg_agv_act = sum(a["active_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        avg_agv_wait = sum(a["wait_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        avg_agv_pure_idle = sum(a["pure_idle_pct"] for a in self.agvs.values()) / max(1, len(self.agvs))
        active_shuttles = [s for s in self.shuttles.values() if s["index"] <= 9]
        avg_sh_active = round(sum(s["active_pct"] for s in active_shuttles) / max(1, len(active_shuttles)), 2)
        avg_sh_cmd_wait = round(sum(s["cmd_wait_pct"] for s in active_shuttles) / max(1, len(active_shuttles)), 2)
        avg_sh_pure_idle = round(max(0.0, 100.0 - avg_sh_active - avg_sh_cmd_wait), 2) if self.sim_duration_s > 0 else 0.0
        avg_sh_idle = round(sum(s["idle_pct"] for s in active_shuttles) / max(1, len(active_shuttles)), 2)

        lifts_list = list(self.lifts.values())
        avg_lf_active = round(sum(lf["active_pct"] for lf in lifts_list) / max(1, len(lifts_list)), 2)
        avg_lf_cmd_wait = round(sum(lf["cmd_wait_pct"] for lf in lifts_list) / max(1, len(lifts_list)), 2)
        avg_lf_pure_idle = round(max(0.0, 100.0 - avg_lf_active - avg_lf_cmd_wait), 2) if self.sim_duration_s > 0 else 0.0
        avg_lf_idle = round(sum(lf["idle_pct"] for lf in lifts_list) / max(1, len(lifts_list)), 2)

        wrk_list = list(self.workers.values())
        wrk_tot_busy = sum(w["busy_s"] for w in wrk_list)
        wrk_tot_cw = sum(w["cmd_wait_s"] for w in wrk_list)
        wrk_tot_pid = sum(w["pure_idle_s"] for w in wrk_list)
        wrk_cap = max(1, len(wrk_list)) * self.sim_duration_s
        avg_w_active = _pct(wrk_tot_busy, wrk_cap)
        avg_w_cmd_wait = round(min(max(0.0, 100.0 - avg_w_active), _pct(wrk_tot_cw, wrk_cap)), 2) if wrk_cap > 0 else 0.0
        avg_w_pure_idle = round(max(0.0, 100.0 - avg_w_active - avg_w_cmd_wait), 2) if wrk_cap > 0 else 0.0
        return {
            "sim_duration_s": round(self.sim_duration_s, 3),
            "wall_duration_s": round(self.wall_duration_s, 3),
            "sim_duration_hms": _fmt_vi_hms(self.sim_duration_s),
            "wall_duration_hms": _fmt_vi_hms(self.wall_duration_s),
            "stop_reason": self.stop_reason,
            "warehouse_inbound_tasks": wh_in,
            "warehouse_outbound_tasks": wh_out,
            "warehouse_return_tasks": wh_ret,
            "z05_partial_returns": self.z05_partial_returns,
            "shuttle_total_distance_m": round(sum(s["distance_m"] for s in self.shuttles.values()), 3),
            "shuttle_total_busy_s": round(sum(s["busy_s"] for s in self.shuttles.values()), 3),
            "shuttle_total_wait_s": round(sum(s["wait_s"] for s in self.shuttles.values()), 3),
            "shuttle_total_cmd_wait_s": round(sum(s["cmd_wait_s"] for s in self.shuttles.values()), 3),
            "shuttle_total_pure_idle_s": round(sum(s["pure_idle_s"] for s in self.shuttles.values()), 3),
            "shuttle_total_turns": sum(s["turn_count"] for s in self.shuttles.values()),
            "shuttle_fault_count": sum(s["fault_count"] for s in self.shuttles.values()),
            "shuttle_avg_active_pct": avg_sh_active,
            "shuttle_avg_cmd_wait_pct": avg_sh_cmd_wait,
            "shuttle_avg_pure_idle_pct": avg_sh_pure_idle,
            "shuttle_avg_idle_pct": avg_sh_idle,
            "lift_inbound_tasks": sum(lf["inbound_count"] for lf in self.lifts.values()),
            "lift_outbound_tasks": sum(lf["outbound_count"] for lf in self.lifts.values()),
            "lift_return_tasks": sum(lf.get("return_count", 0) for lf in self.lifts.values()),
            "lift_total_busy_s": round(sum(lf["busy_s"] for lf in self.lifts.values()), 3),
            "lift_total_wait_s": round(sum(lf["wait_s"] for lf in self.lifts.values()), 3),
            "lift_total_cmd_wait_s": round(sum(lf["cmd_wait_s"] for lf in self.lifts.values()), 3),
            "lift_total_pure_idle_s": round(sum(lf["pure_idle_s"] for lf in self.lifts.values()), 3),
            "lift_fault_count": sum(lf["fault_count"] for lf in self.lifts.values()),
            "lift_avg_active_pct": avg_lf_active,
            "lift_avg_cmd_wait_pct": avg_lf_cmd_wait,
            "lift_avg_pure_idle_pct": avg_lf_pure_idle,
            "lift_avg_idle_pct": avg_lf_idle,
            "worker_total_busy_s": round(wrk_tot_busy, 3),
            "worker_total_cmd_wait_s": round(wrk_tot_cw, 3),
            "worker_total_pure_idle_s": round(wrk_tot_pid, 3),
            "worker_avg_active_pct": avg_w_active,
            "worker_avg_cmd_wait_pct": avg_w_cmd_wait,
            "worker_avg_pure_idle_pct": avg_w_pure_idle,
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


DASHBOARD_COL_BOUNDS: dict[int, tuple[int, int]] = {
    1: (32, 36), 2: (38, 40), 3: (36, 37), 4: (35, 38),
    5: (34, 38), 6: (34, 38), 7: (36, 40),
    8: (22, 40), 9: (24, 40), 10: (22, 40),
}


def _write_top_dashboard_headers(ws, row: int, left_title: str, right_title: str, right_cols: int = 6) -> int:
    _style_section(ws, row, 4, left_title)
    return _style_section(ws, row, right_cols, right_title, start_col=5, left_divider=True)


def _write_kpi_cards(
    ws,
    start_row: int,
    headers: list[str],
    specs: list[tuple[str, str, Any, str | None, str]],
    center_cols: set[int] | None = None,
) -> int:
    rows = [[grp, metric, val, note] for grp, metric, val, _, note in specs]
    next_row = _write_table(
        ws, start_row, headers, rows,
        center_cols={1, 3} if center_cols is None else center_cols,
        auto_filter=False,
    )
    for rel_idx, (_, _, _, fmt, _) in enumerate(specs, start=1):
        if fmt:
            ws.cell(row=start_row + rel_idx, column=3).number_format = fmt
    ws.row_dimensions[next_row - 1].height = 18
    return next_row


def _ensure_row_span_height(ws, start_row: int, end_row: int, min_height_cm: float) -> None:
    if end_row < start_row:
        return
    cur_pt = sum((ws.row_dimensions[r].height or 15.0) for r in range(start_row, end_row + 1))
    need_pt = min_height_cm * 28.3465
    if cur_pt < need_pt:
        ws.row_dimensions[end_row].height = (ws.row_dimensions[end_row].height or 15.0) + (need_pt - cur_pt)


def _finalize_dashboard_sheet(
    ws,
    max_col: int,
    width_bounds: dict[int, tuple[int, int]] | None = None,
    wrapped_ranges: list[tuple[int, int, int]] = (),
    min_span_heights: list[tuple[int, int, float]] = (),
    min_extra_w: int = 14,
) -> None:
    ws.freeze_panes = "A4"
    _fit_columns(ws, max_width=40)
    bounds = width_bounds if width_bounds is not None else DASHBOARD_COL_BOUNDS
    for col_idx in range(1, max_col + 1):
        col_letter = get_column_letter(col_idx)
        cur_w = ws.column_dimensions[col_letter].width or 11
        lo, hi = bounds.get(col_idx, (min_extra_w, 40))
        ws.column_dimensions[col_letter].width = max(lo, min(hi, cur_w))
    for s_row, e_row, m_col in wrapped_ranges:
        if e_row >= s_row:
            _fit_wrapped_row_heights(ws, s_row, e_row, max_col=m_col)
    for s_row, e_row, min_cm in min_span_heights:
        _ensure_row_span_height(ws, s_row, e_row, min_cm)


def _make_bar_chart(
    ws,
    title: str,
    x_title: str,
    y_title: str,
    width: float,
    height: float,
    min_col: int,
    max_col: int,
    min_row: int,
    max_row: int,
    cat_col: int = 1,
    stacked: bool = False,
    legend_pos: str = "b",
    y_min: float | None = None,
    y_max: float | None = None,
) -> BarChart:
    chart = BarChart()
    chart.type = "col"
    chart.grouping = "stacked" if stacked else "clustered"
    if stacked:
        chart.overlap = 100
    chart.style = 10
    _style_chart_title(chart, title)
    chart.x_axis.title = x_title
    chart.y_axis.title = y_title
    if y_min is not None:
        chart.y_axis.scaling.min = y_min
    if y_max is not None:
        chart.y_axis.scaling.max = y_max
    chart.width = width
    chart.height = height
    data_ref = Reference(ws, min_col=min_col, max_col=max_col, min_row=min_row, max_row=max_row)
    cats_ref = Reference(ws, min_col=cat_col, min_row=min_row + 1, max_row=max_row)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    chart.legend.position = legend_pos
    return chart


def _make_pie_chart(
    ws,
    title: str,
    width: float,
    height: float,
    val_col: int,
    min_row: int,
    max_row: int,
    cat_col: int = 1,
    legend_pos: str = "b",
) -> PieChart:
    chart = PieChart()
    _style_chart_title(chart, title)
    chart.style = 10
    chart.width = width
    chart.height = height
    data_ref = Reference(ws, min_col=val_col, min_row=min_row, max_row=max_row)
    cats_ref = Reference(ws, min_col=cat_col, min_row=min_row + 1, max_row=max_row)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showPercent = True
    chart.dataLabels.showVal = False
    chart.dataLabels.showCatName = False
    chart.legend.position = legend_pos
    return chart


def _build_hourly_rows(
    items: list[dict[str, Any]],
    sim_dur: float,
    bucket_keys: list[str],
    classifier,
) -> tuple[list[list[Any]], list[int], int, int, int]:
    """Aggregate items into 1x hourly buckets with share % and peak/quiet tags."""
    max_task_s = max((float(it.get("start_s") or 0.0) for it in items), default=0.0)
    hour_span_s = max(float(sim_dur or 0.0), max_task_s)
    num_hours = max(1, int(math.ceil(max(0.0, hour_span_s - 1e-3) / 3600.0))) if hour_span_s > 0 else 1
    buckets = [{k: 0 for k in bucket_keys} for _ in range(num_hours)]
    for it in items:
        h_idx = min(num_hours - 1, max(0, int(float(it.get("start_s") or 0.0) // 3600.0)))
        key = classifier(it)
        if key in buckets[h_idx]:
            buckets[h_idx][key] += 1

    col_sums = [sum(b[k] for b in buckets) for k in bucket_keys]
    grand_tot = sum(col_sums)
    hour_totals = [sum(b[k] for k in bucket_keys) for b in buckets]
    max_cnt = max(hour_totals) if hour_totals else 0
    min_cnt = min(hour_totals) if hour_totals else 0
    rows: list[list[Any]] = []
    for h_idx, b in enumerate(buckets):
        vals = [b[k] for k in bucket_keys]
        cnt = sum(vals)
        share = round((cnt * 100.0 / grand_tot), 2) if grand_tot > 0 else 0.0
        if max_cnt == 0:
            note = "Chưa có nhiệm vụ"
        elif len(buckets) > 1 and max_cnt > min_cnt:
            note = "Bận rộn nhất (Cao điểm)" if cnt == max_cnt else ("Rảnh nhất (Thấp điểm)" if cnt == min_cnt else "Bình thường")
        else:
            note = "Phân bổ đều"
        rows.append([f"Giờ {h_idx + 1} ({h_idx:02d}:00-{h_idx + 1:02d}:00)", *vals, cnt, share, note])
    return rows, col_sums, grand_tot, max_cnt, min_cnt


def _aggregate_worker_rows(
    workers: list[dict[str, Any]],
    sim_dur: float,
    key_fn,
    label_fn,
    sort_key_fn,
) -> list[list[Any]]:
    """Aggregate workers by area or floor into 3-state (busy, cmd_wait, pure_idle) summary rows."""
    grouped: dict[Any, dict[str, Any]] = {}
    for w in workers:
        gk = key_fn(w)
        g = grouped.setdefault(gk, {
            "label": label_fn(w), "sort_key": sort_key_fn(w), "headcount": 0, "active": 0,
            "pallets": 0, "cartons": 0, "med_boxes": 0,
            "busy_s": 0.0, "cmd_wait_s": 0.0, "pure_idle_s": 0.0,
        })
        g["headcount"] += 1
        if w["cartons"] > 0 or w["med_boxes"] > 0 or w["busy_s"] > 0 or w.get("cmd_wait_s", 0.0) > 0:
            g["active"] += 1
        g["pallets"] += len(w["pallets"])
        g["cartons"] += w["cartons"]
        g["med_boxes"] += w["med_boxes"]
        g["busy_s"] += w["busy_s"]
        g["cmd_wait_s"] += w.get("cmd_wait_s", 0.0)
        g["pure_idle_s"] += w.get("pure_idle_s", w["idle_s"])
    out = []
    for g in sorted(grouped.values(), key=lambda x: x["sort_key"]):
        cap = g["headcount"] * sim_dur
        act_pct = _pct(g["busy_s"], cap)
        cw_pct = round(min(max(0.0, 100.0 - act_pct), _pct(g["cmd_wait_s"], cap)), 2) if cap > 0 else 0.0
        pid_pct = round(max(0.0, 100.0 - act_pct - cw_pct), 2) if cap > 0 else 0.0
        out.append([
            g["label"], g["headcount"], g["active"], g["pallets"], g["cartons"], g["med_boxes"],
            _excel_days(g["busy_s"]), _excel_days(g["cmd_wait_s"]), _excel_days(g["pure_idle_s"]),
            act_pct, cw_pct, pid_pct,
        ])
    return out


def _build_worker_groups(tracker: SimulationTracker, sim_dur: float) -> list[list[Any]]:
    return _aggregate_worker_rows(
        list(tracker.workers.values()), sim_dur,
        key_fn=lambda w: w["area"], label_fn=lambda w: w["area"], sort_key_fn=lambda w: w["group_order"],
    )


def _build_sheet_overview(
    wb: Workbook,
    tracker: SimulationTracker,
    summary: dict[str, Any],
    extra: dict[str, Any],
    speed_x: float,
    sim_dur: float,
) -> None:
    """Build Sheet 1: Tong_Quan with separated KPI rows, system settings and top comparison charts."""
    ws = wb.active
    ws.title = "Tong_Quan"
    ws.views.sheetView[0].showGridLines = True
    subtitle = (
        f"Ngày xuất: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"Thời gian vận hành thực tế chuẩn 1x: {summary['sim_duration_hms']} | "
        f"Thời gian chạy trên đồng hồ (Wall-clock): {summary['wall_duration_hms']} | "
        f"Kết thúc: {summary['stop_reason']}"
    )
    row = _style_banner(ws, 1, 10, "BÁO CÁO TỔNG QUAN VẬN HÀNH MÔ PHỎNG KHO DƯỢC LONG CHÂU", subtitle)

    cfg = tracker.config
    loose_pallets = int(extra.get("loose_pallets_unpacked", tracker.loose_stats.get("pallets_unpacked", 0)))
    loose_cartons = int(extra.get("loose_cartons_delivered", tracker.loose_stats.get("cartons_delivered", 0)))
    tot_sys_faults = int(summary["shuttle_fault_count"] + summary["lift_fault_count"] + summary["agv_fault_count"])

    _write_top_dashboard_headers(
        ws, row,
        "1. TỔNG HỢP HOẠT ĐỘNG & CHỈ SỐ KPI TOÀN NHÀ MÁY",
        "BIỂU ĐỒ TỔNG QUAN HIỆU SUẤT & PHÂN BỔ NHIỆM VỤ TOÀN KHO",
    )
    row += 1
    sec1_tbl_start = row
    overview_kpis = [
        ("Thời gian", "Tổng thời gian vận hành kho (1x chuẩn)", summary["sim_duration_hms"], None, "1x chuẩn nhà máy"),
        ("Thời gian", "Thời gian mở mô phỏng (Wall-clock)", summary["wall_duration_hms"], None, f"Tốc độ tua {speed_x:.2f}x"),
        ("Kho AS/RS", "Tổng nhiệm vụ Shuttle - Nhập kho", int(summary["warehouse_inbound_tasks"]), "#,##0", f"Mục tiêu ca: {cfg.get('warehouse_inbound_target', '-')} nhập"),
        ("Kho AS/RS", "Tổng nhiệm vụ Shuttle - Xuất kho", int(summary["warehouse_outbound_tasks"]), "#,##0", f"Mục tiêu ca: {cfg.get('warehouse_outbound_target', '-')} xuất"),
        ("Kho AS/RS", "Tỉ lệ TB 9 Shuttle hoạt động", round(summary["shuttle_avg_active_pct"], 2), '0.00"%"', f"Quãng đường 10 xe: {summary['shuttle_total_distance_m']:,.2f} m"),
        ("Kho AS/RS", "Tỉ lệ TB 9 Shuttle có lệnh chờ hàng", round(summary["shuttle_avg_cmd_wait_pct"], 2), '0.00"%"', f"Nghỉ rảnh: {summary['shuttle_avg_pure_idle_pct']:.2f}% | Chờ pallet: {_fmt_vi_hms(summary['shuttle_total_wait_s'])}"),
        ("4 Thang nâng", "Tổng hàng qua 4 Thang nâng - Nhập kho", int(summary["lift_inbound_tasks"]), "#,##0", "Lượt qua 2 Thang Nhập"),
        ("4 Thang nâng", "Tổng hàng qua 4 Thang nâng - Xuất kho", int(summary["lift_outbound_tasks"]), "#,##0", f"Hoàn kho Z05: {summary.get('warehouse_return_tasks', 0)} lượt"),
        ("4 Thang nâng", "Tỉ lệ TB 4 Thang nâng hoạt động", round(summary["lift_avg_active_pct"], 2), '0.00"%"', "Chạy nâng-hạ cabin thực tế"),
        ("4 Thang nâng", "Tỉ lệ TB 4 Thang nâng có lệnh chờ hàng", round(summary["lift_avg_cmd_wait_pct"], 2), '0.00"%"', f"Nghỉ rảnh: {summary['lift_avg_pure_idle_pct']:.2f}% | Chờ đơn: {_fmt_vi_hms(summary['lift_total_wait_s'])}"),
        (f"Đội AGV ({len(tracker.agvs)} xe)", "Tổng nhiệm vụ toàn đội AGV", int(summary["agv_total_tasks"]), "#,##0", f"Dock: {summary['agv_dock_tasks']} | Unpack: {summary['agv_unpack_tasks']} | P1: {summary['agv_p1_tasks']}"),
        (f"Đội AGV ({len(tracker.agvs)} xe)", "Tổng quãng đường toàn đội AGV", round(summary["agv_total_distance_m"], 2), "#,##0.00", "mét di chuyển toàn nhà máy"),
        (f"Đội AGV ({len(tracker.agvs)} xe)", "Tỉ lệ TB AGV chạy có ích", round(summary["agv_avg_active_pct"], 2), '0.00"%"', f"Pin TB còn: {summary['agv_avg_battery_pct']:.2f}%"),
        (f"Đội AGV ({len(tracker.agvs)} xe)", "Tỉ lệ TB AGV có lệnh chờ Traffic", round(summary["agv_avg_wait_pct"], 2), '0.00"%"', f"Nghỉ rảnh hoàn toàn: {summary['agv_avg_pure_idle_pct']:.2f}%"),
        ("Nhân sự (125)", "Tỉ lệ TB Công nhân làm việc", round(summary["worker_avg_active_pct"], 2), '0.00"%"', f"Chờ hàng/AGV: {summary['worker_avg_cmd_wait_pct']:.2f}% | Nghỉ: {summary['worker_avg_pure_idle_pct']:.2f}%"),
        ("Khu Dock", "Pallet chẵn hoàn tất phân loại", int(summary["dock_even_pallets"]), "#,##0", f"Barcode quét: {summary['dock_scanned_even']} kiện chẵn"),
        ("Khu Dock", "Pallet lẻ hoàn tất phân loại", int(summary["dock_odd_pallets"]), "#,##0", f"Barcode quét: {summary['dock_scanned_odd']} kiện lẻ"),
        ("Unpack T1", "Pallet lẻ unpack lên băng tải", loose_pallets, "#,##0", f"Tổng thùng lên băng tải: {loose_cartons} thùng"),
        ("An toàn", "Tổng số lỗi toàn hệ thống", tot_sys_faults, "#,##0", f"Shuttle: {summary['shuttle_fault_count']} | Thang: {summary['lift_fault_count']} | AGV: {summary['agv_fault_count']}"),
    ]
    row = _write_kpi_cards(
        ws, row,
        ["Khu vực", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        overview_kpis,
    )

    sec2_start_row = row
    row = _style_section(ws, row, 4, "2. THÔNG SỐ THỜI GIAN THỰC TẾ & CẤU HÌNH HỆ THỐNG")
    mode_desc = (
        "Chạy liên tục không giới hạn tới khi đóng Gazebo"
        if tracker.continuous_mode and tracker.cycle_duration_s <= 0
        else (f"Dừng theo chu trình {tracker.cycle_duration_s:.1f}s" if tracker.cycle_duration_s > 0 else f"Chạy đủ {tracker.total_jobs} jobs")
    )
    cfg_rows = [
        ["Chế độ vận hành & Lý do dừng", mode_desc, "-", summary["stop_reason"]],
        ["Số pallet cấu hình (tắt liên tục)", tracker.total_jobs if not tracker.continuous_mode else "Không giới hạn (Liên tục)", "pallet", "Tự động tái chế pallet khi bật continuous_mode"],
        ["Giới hạn chu trình (cycle_duration_s)", _fmt_vi_hms(tracker.cycle_duration_s) if tracker.cycle_duration_s > 0 else "0 (Không giới hạn)", "giờ:phút:giây", "0 = chạy mãi; > 0 = tự dừng đúng mốc chuẩn 1x"],
        ["Nhịp nhập / xuất & Dock", f"{cfg.get('inbound_even_interval_s', cfg.get('inbound_interval_s'))}s / {cfg.get('outbound_interval_s')}s", "giây (1x)", f"Ca: {cfg.get('warehouse_inbound_target', '-')} nhập / {cfg.get('warehouse_outbound_target', '-')} xuất | Tồn đầu: {cfg.get('initial_outbound_pallets', 0)} | Dock: {cfg.get('dock_active_count')}"],
        ["Thông số Shuttle cài đặt (tốc độ / gia tốc / jerk)", f"{float(cfg.get('shuttle_speed', 1.0)):.2f} / {float(cfg.get('shuttle_acceleration', 0.3)):.2f} / {float(cfg.get('shuttle_jerk', 0.6)):.2f}", "m/s | m/s² | m/s³", f"Đổi bánh: {float(cfg.get('shuttle_turn_s', 5.0)):.1f}s | Định vị: {float(cfg.get('shuttle_positioning_s', 5.0)):.1f}s | Nâng: {float(cfg.get('shuttle_lift_s', 2.5)):.1f}s | Avail/Util: {float(cfg.get('shuttle_availability', 0.7))*100:.0f}%/{float(cfg.get('shuttle_utilization', 0.7))*100:.0f}%"],
        ["Thông số Thang nâng cài đặt (tốc độ / gia tốc / jerk)", f"{float(cfg.get('lift_speed', 0.6)):.2f} / {float(cfg.get('lift_acceleration', 0.3)):.2f} / {float(cfg.get('lift_jerk', 0.8)):.2f}", "m/s | m/s² | m/s³", f"Ổn định dừng: {float(cfg.get('lift_settle_s', 2.0)):.1f}s | Avail/Util: {float(cfg.get('lift_availability', 0.8))*100:.0f}%/{float(cfg.get('lift_utilization', 0.8))*100:.0f}% | Băng tải: {float(cfg.get('conveyor_speed', 0.6)):.2f} m/s"],
        ["Thông số AGV & Pin (Ban đầu / Có tải / Rỗng / Chờ)", f"{tracker.init_battery_pct:.0f}% / {tracker.drain_loaded_per_m}%/m / {tracker.drain_empty_per_m}%/m", "Vận tốc TB 1.8 m/s", f"Đứng chờ: {tracker.drain_idle_per_s}%/s | Khoảng cách giữ: {cfg.get('agv_following_gap_m', 1)} m"],
    ]
    row = _write_table(
        ws, row,
        ["Thông số hệ thống", "Giá trị cấu hình", "Đơn vị", "Diễn giải chi tiết"],
        cfg_rows,
        center_cols={2, 3},
        auto_filter=False,
    )
    sec2_end_row = row - 2
    ws.row_dimensions[row - 1].height = 22
    ws.row_dimensions[row].height = 22
    row += 1
    sec2_span_end = row - 1

    row = _style_section(ws, row, 10, "3. SO SÁNH SẢN LƯỢNG & HIỆU SUẤT CÁC KHU VỰC (NGUỒN 3 BIỂU ĐỒ ĐẦU TRANG)")
    task_vol_rows = [
        ["Kho AS/RS - Nhập kho", int(summary["warehouse_inbound_tasks"])],
        ["Kho AS/RS - Xuất kho", int(summary["warehouse_outbound_tasks"])],
        ["AGV Khu Dock -> Kho 01", int(summary["agv_dock_tasks"])],
        ["AGV Khu Unpack (U1 & U2)", int(summary["agv_unpack_tasks"])],
        ["AGV Khu Đóng Gói P1", int(summary["agv_p1_tasks"])],
    ]
    tot_vol = sum(r[1] for r in task_vol_rows)
    vol_src = [[lbl, cnt, round((cnt * 100.0 / tot_vol), 2) if tot_vol > 0 else 0.0] for lbl, cnt in task_vol_rows]
    v_hdr = row
    v_last = v_hdr + len(vol_src)
    row = _write_table(
        ws, row,
        ["Khu vực vận chuyển", "Số nhiệm vụ", "Tỉ trọng (%)"],
        vol_src,
        col_formats={2: "#,##0", 3: "0.00"},
        right_cols={2, 3},
        center_cols={1},
        total_row=["TỔNG CỘNG", tot_vol, 100.0 if tot_vol > 0 else 0.0],
        auto_filter=False,
    )

    equip_perf_rows = []
    for lbl, act_raw, wait_raw in (
        ("9 Shuttle chính (AS/RS)", summary["shuttle_avg_active_pct"], summary["shuttle_avg_cmd_wait_pct"]),
        ("4 Thang nâng (AS/RS)", summary["lift_avg_active_pct"], summary["lift_avg_cmd_wait_pct"]),
        (f"Đội AGV ({len(tracker.agvs)} xe)", summary["agv_avg_active_pct"], summary["agv_avg_wait_pct"]),
        ("Công nhân toàn nhà máy", summary["worker_avg_active_pct"], summary["worker_avg_cmd_wait_pct"]),
    ):
        act_v = round(min(100.0, max(0.0, float(act_raw))), 2)
        cw_v = round(min(max(0.0, 100.0 - act_v), max(0.0, float(wait_raw))), 2)
        idle_v = round(max(0.0, 100.0 - act_v - cw_v), 2)
        equip_perf_rows.append([lbl, act_v, cw_v, idle_v])
    p_hdr = row
    p_last = p_hdr + len(equip_perf_rows)
    _write_table(
        ws, row,
        ["Nhóm thiết bị / Nhân sự", "Đang hoạt động (%)", "Có lệnh - Chờ hàng / Traffic (%)", "Nghỉ rảnh (%)"],
        equip_perf_rows,
        col_formats={2: "0.00", 3: "0.00", 4: "0.00"},
        right_cols={2, 3, 4},
        center_cols={1},
        auto_filter=False,
    )

    ws.add_chart(_make_bar_chart(
        ws, "Sản lượng nhiệm vụ theo khu vực vận chuyển", "Khu vực", "Số nhiệm vụ",
        21.4, 14.0, 2, 2, v_hdr, v_last,
    ), f"E{sec1_tbl_start}")
    ws.add_chart(_make_pie_chart(
        ws, "Tỉ trọng nhiệm vụ giữa các khu vực (Tổng 100%)", 14.2, 14.0, 3, v_hdr, v_last,
    ), f"H{sec1_tbl_start}")
    ws.add_chart(_make_bar_chart(
        ws, "Hoạt động / Chờ hàng-Traffic / Nghỉ rảnh TB toàn kho (Tối đa 100%)",
        "Nhóm thiết bị / Nhân sự", "Tỉ lệ (%)", 36.0, 12.8, 2, 4, p_hdr, p_last,
        stacked=True, y_min=0, y_max=100,
    ), f"E{sec2_start_row}")

    _finalize_dashboard_sheet(
        ws, 10,
        wrapped_ranges=[(sec2_start_row + 2, sec2_end_row, 4)],
        min_span_heights=[
            (sec1_tbl_start, sec2_start_row - 1, 14.8),
            (sec2_start_row, sec2_span_end, 13.8),
        ],
    )


def _build_asrs_sheet(wb: Workbook, tracker: SimulationTracker, summary: dict[str, Any]) -> None:
    """Build Sheet 2: Kho_ASRS_Shuttle with Cách A summary, top charts, specs, waits and process log."""
    cfg = tracker.config
    ws2 = wb.create_sheet("Kho_ASRS_Shuttle")
    row = _style_banner(
        ws2, 1, 16,
        "KHU VỰC KHO AS/RS (9 TẦNG) - THÔNG SỐ CÀI ĐẶT, SHUTTLE, THANG NÂNG, HÀNG CHỜ & BIỂU ĐỒ",
        f"Mọi thời gian tính theo thời gian vận hành thực tế chuẩn 1x ({summary['sim_duration_hms']}), định dạng Giờ:Phút:Giây.",
    )
    _write_top_dashboard_headers(
        ws2, row,
        "1. TỔNG HỢP HOẠT ĐỘNG & TỈ LỆ KHO AS/RS",
        "BIỂU ĐỒ TRỰC QUAN KHO AS/RS (TẦN SUẤT THEO GIỜ & TỈ LỆ HOẠT ĐỘNG TỐI ĐA 100%)",
    )
    row += 1
    sec1_tbl_start = row
    sh_kpi_specs = [
        ("Kho AS/RS", "Tổng thời gian vận hành kho (1x chuẩn)", summary["sim_duration_hms"], None, "Thời gian mô phỏng chuẩn 1x"),
        ("Đội Shuttle (1..10)", "Tổng nhiệm vụ Shuttle - Nhập kho", int(summary["warehouse_inbound_tasks"]), "#,##0", "nhiệm vụ nhập kho"),
        ("Đội Shuttle (1..10)", "Tổng nhiệm vụ Shuttle - Xuất kho", int(summary["warehouse_outbound_tasks"]), "#,##0", f"nhiệm vụ xuất kho (Hoàn kho Z05: {summary.get('warehouse_return_tasks', 0)})"),
        ("Đội Shuttle (1..10)", "Tổng quãng đường 10 Shuttle", round(summary["shuttle_total_distance_m"], 2), "#,##0.00", "m (mét di chuyển)"),
        ("Shuttle chính (1..9)", "Tỉ lệ TB Shuttle hoạt động", round(summary["shuttle_avg_active_pct"], 2), '0.00"%"', "% chạy nhiệm vụ trên ray"),
        ("Shuttle chính (1..9)", "Tỉ lệ TB Shuttle có lệnh chờ hàng", round(summary["shuttle_avg_cmd_wait_pct"], 2), '0.00"%"', "% giữ lệnh chờ thang / bàn đệm"),
        ("Shuttle chính (1..9)", "Tỉ lệ TB Shuttle nghỉ rảnh", round(summary["shuttle_avg_pure_idle_pct"], 2), '0.00"%"', "% rảnh chưa có lệnh"),
        ("Đội Shuttle (1..10)", "Tổng thời gian hàng chờ Shuttle", _fmt_vi_hms(summary["shuttle_total_wait_s"]), None, "Cộng dồn TG xếp hàng của pallet"),
        ("Đội Shuttle (1..10)", "Tổng số lỗi Shuttle", int(summary["shuttle_fault_count"]), "#,##0", "lỗi toàn đội Shuttle"),
        ("4 Thang nâng", "Tổng hàng qua 4 Thang nâng - Nhập kho", int(summary["lift_inbound_tasks"]), "#,##0", "lượt qua 2 Thang Nhập"),
        ("4 Thang nâng", "Tổng hàng qua 4 Thang nâng - Xuất kho", int(summary["lift_outbound_tasks"]), "#,##0", f"lượt qua 2 Thang Xuất (Hoàn kho: {summary.get('lift_return_tasks', 0)})"),
        ("4 Thang nâng", "Tỉ lệ TB 4 Thang nâng hoạt động", round(summary["lift_avg_active_pct"], 2), '0.00"%"', "% cabin chạy nâng-hạ thực tế"),
        ("4 Thang nâng", "Tỉ lệ TB 4 Thang nâng có lệnh chờ hàng", round(summary["lift_avg_cmd_wait_pct"], 2), '0.00"%"', "% chờ Z05 / Shuttle / bàn giao"),
        ("4 Thang nâng", "Tỉ lệ TB 4 Thang nâng nghỉ rảnh", round(summary["lift_avg_pure_idle_pct"], 2), '0.00"%"', "% thang rảnh không có đơn"),
        ("4 Thang nâng", "Tổng thời gian hàng chờ Thang nâng", _fmt_vi_hms(summary["lift_total_wait_s"]), None, "Cộng dồn TG xếp hàng nhiều đơn"),
        ("4 Thang nâng", "Tổng số lỗi Thang nâng", int(summary["lift_fault_count"]), "#,##0", "lỗi toàn hệ Thang nâng"),
    ]
    row = _write_kpi_cards(
        ws2, row,
        ["Nhóm thiết bị", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        sh_kpi_specs,
    )

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
    sec2_span_end = row - 1

    row = _style_section(ws2, row, 16, "3. BẢNG TỔNG HỢP TRẠNG THÁI & HÀNG CHỜ 10 SHUTTLE (TỪ SHUTTLE 1 ĐẾN SHUTTLE 10)")
    shuttle_rows = []
    tot_in = tot_out = tot_faults = 0
    tot_busy = tot_cw = tot_pid = tot_wait = 0.0
    for sid in [f"shuttle_{i}" for i in range(10)]:
        sh = tracker.shuttles[sid]
        cnt = sh["inbound_count"] + sh["outbound_count"] + sh.get("return_count", 0)
        tot_in += sh["inbound_count"]
        tot_out += sh["outbound_count"]
        tot_ret_sh = sh.get("return_count", 0)
        tot_faults += sh["fault_count"]
        tot_busy += sh["busy_s"]
        tot_cw += sh["cmd_wait_s"]
        tot_pid += sh["pure_idle_s"]
        tot_wait += sh["wait_s"]
        avg_wait = round(sh["wait_s"] / cnt, 2) if cnt > 0 else 0.0
        shuttle_rows.append([
            sh["code"], sh["role"],
            sh["inbound_count"], sh["outbound_count"], cnt,
            _excel_days(sh["busy_s"]), _excel_days(sh["cmd_wait_s"]), _excel_days(sh["pure_idle_s"]),
            _excel_days(sh["wait_s"]), avg_wait,
            sh["active_pct"], sh["cmd_wait_pct"], sh["pure_idle_pct"], sh["fault_count"],
            sh["status"], _fmt_codes(sh["task_codes"], 10),
        ])
    tot_sh_cnt = sum(r[4] for r in shuttle_rows)
    row = _write_table(
        ws2, row,
        [
            "Mã Shuttle", "Vai trò vận hành",
            "Lượt nhập", "Lượt xuất", "Tổng NV",
            "TG hoạt động", "TG có lệnh chờ hàng", "TG nghỉ rảnh",
            "TG hàng chờ (cộng dồn)", "Chờ TB / NV (s)",
            "Tỉ lệ chạy (%)", "Tỉ lệ chờ hàng/lệnh (%)", "Tỉ lệ nghỉ rảnh (%)", "Số lỗi",
            "Trạng thái", "Danh sách mã nhiệm vụ",
        ],
        shuttle_rows,
        col_formats={
            3: "#,##0", 4: "#,##0", 5: "#,##0",
            6: FMT_HMS_TENTH, 7: FMT_HMS_TENTH, 8: FMT_HMS_TENTH, 9: FMT_HMS_TENTH, 10: "#,##0.00",
            11: "0.00", 12: "0.00", 13: "0.00", 14: "#,##0",
        },
        right_cols={3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14},
        center_cols={1, 15},
        status_col=15,
        total_row=[
            "TỔNG / TB 10 SHUTTLE", "",
            tot_in, tot_out, tot_sh_cnt,
            _excel_days(tot_busy), _excel_days(tot_cw), _excel_days(tot_pid), _excel_days(tot_wait),
            round(tot_wait / tot_sh_cnt, 2) if tot_sh_cnt > 0 else 0.0,
            summary["shuttle_avg_active_pct"], summary["shuttle_avg_cmd_wait_pct"], summary["shuttle_avg_pure_idle_pct"],
            tot_faults, "(*) TB hàng % tính trên 9 Shuttle hoạt động chính", "",
        ],
        auto_filter=False,
    )

    row = _style_section(ws2, row, 16, "4. BÁO CÁO CHI TIẾT HOẠT ĐỘNG & HÀNG CHỜ 4 THANG NÂNG (KHO AS/RS)")
    lift_rows = []
    lf_tot_all = lf_tot_faults = lf_tot_wait_cnt = 0
    lf_tot_dist = lf_tot_busy = lf_tot_cw = lf_tot_pid = lf_tot_wait = 0.0
    ordered_lifts = sorted(tracker.lifts.values(), key=lambda item: item["order"])
    for lf in ordered_lifts:
        cnt = lf["inbound_count"] + lf["outbound_count"] + lf.get("return_count", 0)
        lf_tot_all += cnt
        lf_tot_faults += lf["fault_count"]
        lf_tot_dist += lf["distance_m"]
        lf_tot_busy += lf["busy_s"]
        lf_tot_cw += lf["cmd_wait_s"]
        lf_tot_pid += lf["pure_idle_s"]
        lf_tot_wait += lf["wait_s"]
        denom = lf["wait_count"] if lf["wait_count"] > 0 else cnt
        lf_tot_wait_cnt += denom
        avg_lf_wait = round(lf["wait_s"] / denom, 2) if denom > 0 else 0.0
        lift_rows.append([
            lf["code"], f"{lf['direction']} ({lf['station']})",
            cnt, round(lf["distance_m"], 2),
            _excel_days(lf["busy_s"]), _excel_days(lf["cmd_wait_s"]), _excel_days(lf["pure_idle_s"]),
            _excel_days(lf["wait_s"]), avg_lf_wait,
            lf["active_pct"], lf["cmd_wait_pct"], lf["pure_idle_pct"], lf["fault_count"],
            lf["status"], _fmt_codes(lf["task_codes"], 10),
        ])
    row = _write_table(
        ws2, row,
        [
            "Mã Thang nâng", "Cổng / Chức năng",
            "Tổng hàng qua thang", "Quãng đường nâng-hạ (m)",
            "TG hoạt động", "TG có lệnh chờ hàng", "TG nghỉ rảnh",
            "TG hàng chờ (cộng dồn)", "Chờ TB / lượt (s)",
            "Tỉ lệ chạy (%)", "Tỉ lệ chờ hàng/lệnh (%)", "Tỉ lệ nghỉ rảnh (%)", "Số lỗi",
            "Trạng thái", "Danh sách mã nhiệm vụ",
        ],
        lift_rows,
        col_formats={
            3: "#,##0", 4: "#,##0.00", 5: FMT_HMS_TENTH, 6: FMT_HMS_TENTH, 7: FMT_HMS_TENTH,
            8: FMT_HMS_TENTH, 9: "#,##0.00", 10: "0.00", 11: "0.00", 12: "0.00", 13: "#,##0",
        },
        right_cols={3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13},
        center_cols={1, 14},
        status_col=14,
        total_row=[
            "TỔNG / TB 4 THANG NÂNG", "",
            lf_tot_all, round(lf_tot_dist, 2),
            _excel_days(lf_tot_busy), _excel_days(lf_tot_cw), _excel_days(lf_tot_pid),
            _excel_days(lf_tot_wait),
            round(lf_tot_wait / lf_tot_wait_cnt, 2) if lf_tot_wait_cnt > 0 else 0.0,
            summary["lift_avg_active_pct"], summary["lift_avg_cmd_wait_pct"], summary["lift_avg_pure_idle_pct"],
            lf_tot_faults, "", "",
        ],
        auto_filter=False,
    )

    sec5_start_row = row
    row = _style_section(ws2, row, 16, "5. THỜI GIAN CHỜ TỪNG CÔNG ĐOẠN (HÀNG CHỜ THANG NÂNG & HÀNG CHỜ SHUTTLE)")
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
            st["label"], evs,
            f"{wevs} ({_pct(wevs, evs):.1f}%)" if evs > 0 else "0 (0.0%)",
            _excel_days(ws_val), avg_s, round(st["max_wait_s"], 2), st["desc"],
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

    row = _style_section(ws2, row, 16, "6. SỐ LIỆU TẦN SUẤT THEO GIỜ & TỈ LỆ HOẠT ĐỘNG KHO AS/RS (NGUỒN 3 BIỂU ĐỒ ĐẦU TRANG)")
    hourly_rows, col_sums, total_hour_tasks, max_hr_cnt, min_hr_cnt = _build_hourly_rows(
        tracker.warehouse_tasks,
        tracker.sim_duration_s,
        ["in", "out"],
        lambda t: "in" if str(t.get("direction", "")) in ("Nhập kho", "Hoàn kho (Hàng dư Z05)") else "out",
    )
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
            col_sums[0], col_sums[1], total_hour_tasks,
            100.0 if total_hour_tasks > 0 else 0.0,
            f"Cao nhất: {max_hr_cnt} | Thấp nhất: {min_hr_cnt} NV/giờ",
        ],
        auto_filter=False,
    )

    active9_sh = [tracker.shuttles[f"shuttle_{i}"] for i in range(9)]
    active9_busy = sum(sh["busy_s"] for sh in active9_sh)
    active9_cw = sum(sh["cmd_wait_s"] for sh in active9_sh)
    active9_pid = sum(sh["pure_idle_s"] for sh in active9_sh)
    active9_cap = active9_busy + active9_cw + active9_pid
    active_sh_count = sum(
        1 for i in range(10)
        if tracker.shuttles[f"shuttle_{i}"]["inbound_count"] + tracker.shuttles[f"shuttle_{i}"]["outbound_count"] > 0
    )
    pct9_act = _pct(active9_busy, active9_cap)
    pct9_cw = round(min(max(0.0, 100.0 - pct9_act), _pct(active9_cw, active9_cap)), 2) if active9_cap > 0 else 0.0
    pct9_pid = round(max(0.0, 100.0 - pct9_act - pct9_cw), 2) if active9_cap > 0 else 0.0
    sh_state_rows = [
        [
            "Đang hoạt động (Chạy nhiệm vụ)",
            round(active9_busy / 60.0, 2), pct9_act,
            f"{active_sh_count}/10 Shuttle tham gia | {tot_in} nhập, {tot_out} xuất",
        ],
        [
            "Có lệnh - Chờ hàng / Bàn đệm",
            round(active9_cw / 60.0, 2), pct9_cw,
            "Shuttle giữ lệnh chờ thang nâng / bàn đệm giải tỏa",
        ],
        [
            "Nghỉ rảnh (Chưa có lệnh)",
            round(active9_pid / 60.0, 2), pct9_pid,
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
            round(active9_cap / 60.0, 2), 100.0 if active9_cap > 0 else 0.0,
            f"Hàng chờ pallet (cộng dồn): {_fmt_vi_hms(tot_wait)} | Lỗi: {tot_faults}",
        ],
        auto_filter=False,
    )

    util_rows = []
    for dev in [tracker.shuttles[f"shuttle_{i}"] for i in range(10)] + ordered_lifts:
        util_rows.append([dev["code"], dev["active_pct"], dev["cmd_wait_pct"], dev["pure_idle_pct"]])
    u_hdr_row = row
    u_last_data_row = u_hdr_row + len(util_rows)
    row = _write_table(
        ws2, row,
        ["Thiết bị Kho AS/RS", "Đang hoạt động (%)", "Có lệnh - Chờ hàng (%)", "Nghỉ rảnh (%)"],
        util_rows,
        col_formats={2: "0.00", 3: "0.00", 4: "0.00"},
        right_cols={2, 3, 4},
        center_cols={1},
        auto_filter=False,
    )

    ws2.add_chart(_make_bar_chart(
        ws2, "Tần suất Nhập / Xuất theo giờ (Kho AS/RS)", "Khung giờ (1x)", "Số nhiệm vụ",
        21.4, 12.2, 2, 4, h_hdr_row, h_last_data_row,
    ), f"E{sec1_tbl_start}")
    ws2.add_chart(_make_pie_chart(
        ws2, f"Tỉ lệ trạng thái 9 Shuttle chính (Tổng 100% | Lỗi: {tot_faults})",
        14.2, 12.2, 3, s_hdr_row, s_last_data_row,
    ), f"H{sec1_tbl_start}")
    ws2.add_chart(_make_bar_chart(
        ws2, "Hoạt động / Chờ hàng / Nghỉ rảnh từng Shuttle & Thang (Tối đa 100%)",
        "Thiết bị Kho AS/RS", "Tỉ lệ (%)", 36.0, 14.0, 2, 4, u_hdr_row, u_last_data_row,
        stacked=True, y_min=0, y_max=100,
    ), f"E{sec2_start_row}")

    row = _style_section(ws2, row, 16, "7. NHẬT KÝ CHI TIẾT THEO QUY TRÌNH KHO AS/RS (THANG NÂNG & SHUTTLE)")
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
            t.get("pick_location") or "-", t.get("drop_location") or t["slot_id"],
            t.get("lift_code") or "-", t["shuttle_code"],
            _excel_days(t["start_s"]), _excel_days(lw_s), _excel_days(sw_s),
            _excel_days(t["end_s"]), _excel_days(t["duration_s"]),
            round(t["distance_m"], 2), t.get("fault_count", 0), t["status"],
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
    _finalize_dashboard_sheet(
        ws2, 16,
        wrapped_ranges=[
            (sec2_start_row + 2, sec2_end_row, 4),
            (sec5_start_row + 2, sec5_end_row, 7),
        ],
        min_span_heights=[
            (sec1_tbl_start, sec2_start_row - 1, 13.0),
            (sec2_start_row, sec2_span_end, 14.8),
        ],
    )


def _build_agv_sheet(
    wb: Workbook,
    tracker: SimulationTracker,
    summary: dict[str, Any],
    tf: dict[str, Any],
    engine_ticks: int,
) -> None:
    """Build Sheet 3: Khu_Unpack_AGV with separated KPI cards, charts, area/floor summaries and 106-AGV log."""
    cfg = tracker.config
    ws3 = wb.create_sheet("Khu_Unpack_AGV")
    row = _style_banner(
        ws3, 1, 22,
        "KHU UNPACK & TOÀN ĐỘI AGV (HIỆU SUẤT, TRAFFIC, PIN, QUÃNG ĐƯỜNG, LỖI)",
        f"Thời gian quy chuẩn 1x thực tế (định dạng Giờ:Phút:Giây) | Pin = {tracker.init_battery_pct:.0f}% - ({tracker.drain_loaded_per_m}%/m tải + {tracker.drain_empty_per_m}%/m rỗng + {tracker.drain_idle_per_s}%/s chờ)",
    )
    agv_tr = tf.get("agv_traffic_stats", {}) or {}
    dual_ticks = int(tf.get("dual_agv_motion_ticks", 0) or 0)
    follow_ticks = int(tf.get("same_direction_following_ticks", 0) or 0)
    swept_chk = int(tf.get("swept_pose_checks", tf.get("route_checks", 0)) or 0)

    ordered_agvs = sorted(tracker.agvs.values(), key=lambda a: (a["group_order"], a["floor"], a["code"]))
    avg_agv_act = sum(a["active_pct"] for a in ordered_agvs) / max(1, len(ordered_agvs))
    avg_agv_wait = sum(a["wait_pct"] for a in ordered_agvs) / max(1, len(ordered_agvs))
    avg_agv_pure_idle = sum(a["pure_idle_pct"] for a in ordered_agvs) / max(1, len(ordered_agvs))

    agv_rows = []
    sum_done = sum_run = sum_agv_faults = 0
    sum_ld = sum_em = sum_tot = sum_bs = sum_ws = sum_pid = 0.0
    by_area: dict[str, dict[str, Any]] = {}
    by_floor: dict[int, dict[str, Any]] = {}
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

        ag = by_area.setdefault(a["area"], {
            "area": a["area"], "order": a["group_order"], "count": 0, "done": 0, "run": 0,
            "ld": 0.0, "em": 0.0, "tot": 0.0, "bs": 0.0, "ws": 0.0, "pid": 0.0,
            "act_sum": 0.0, "wait_sum": 0.0, "idle_sum": 0.0, "bat_sum": 0.0, "faults": 0,
        })
        fg = by_floor.setdefault(a["floor"], {
            "floor": a["floor"], "count": 0, "done": 0, "tot": 0.0,
            "bs": 0.0, "ws": 0.0, "pid": 0.0, "act_sum": 0.0, "wait_sum": 0.0, "idle_sum": 0.0, "bat_sum": 0.0, "faults": 0,
        })
        for grp in (ag, fg):
            grp["count"] += 1
            grp["done"] += a["missions_completed"]
            grp["tot"] += a["total_distance_m"]
            grp["bs"] += a["busy_s"]
            grp["ws"] += a["wait_s"]
            grp["pid"] += a["pure_idle_s"]
            grp["act_sum"] += a["active_pct"]
            grp["wait_sum"] += a["wait_pct"]
            grp["idle_sum"] += a["pure_idle_pct"]
            grp["bat_sum"] += a["battery_remaining_pct"]
            grp["faults"] += a["fault_count"]
        ag["run"] += running
        ag["ld"] += a["loaded_distance_m"]
        ag["em"] += a["empty_distance_m"]

        agv_rows.append([
            a["code"], a["id"], a["area"], f"Tầng {a['floor']}", a["station"],
            a["missions_completed"], running,
            round(a["loaded_distance_m"], 2), round(a["empty_distance_m"], 2), round(a["total_distance_m"], 2),
            _excel_days(a["busy_s"]), _excel_days(a["wait_s"]), _excel_days(a["pure_idle_s"]),
            a["active_pct"], a["wait_pct"], a["pure_idle_pct"],
            round(a["battery_used_pct"], 2), round(a["battery_remaining_pct"], 2),
            a["fault_count"], a["last_task"] or "-", a["status"], _fmt_codes(a["task_codes"], 10),
        ])

    _write_top_dashboard_headers(
        ws3, row,
        "1. TỔNG HỢP TRAFFIC & HIỆU SUẤT TOÀN ĐỘI AGV",
        "BIỂU ĐỒ PHÂN BỔ NHIỆM VỤ & TỈ LỆ TRAFFIC ĐỘI AGV (TỐI ĐA 100%)",
    )
    row += 1
    sec1_tbl_start = row
    n_dock_agvs = sum(1 for a in ordered_agvs if a["id"].startswith("agv_"))
    n_u1_agvs = sum(1 for a in ordered_agvs if a["id"].startswith("u1_agv_"))
    n_u2_agvs = sum(1 for a in ordered_agvs if a["id"].startswith("u2_agv_"))
    n_p1_agvs = sum(1 for a in ordered_agvs if a["id"].startswith("p1_agv_"))
    agv_kpis = [
        ("Traffic 4 tầng", "Kiểm tra va chạm quét (swept checks)", swept_chk, "#,##0", f"Nút giao: {agv_tr.get('trips', 0)} lượt | Song song T1: {_pct(dual_ticks, engine_ticks):.1f}%"),
        ("Đội AGV", "Tổng số xe AGV vận hành", len(ordered_agvs), "#,##0", f"{n_dock_agvs} Dock + {n_u1_agvs} U1 + {n_u2_agvs} U2 + {n_p1_agvs} P1"),
        ("Đội AGV", "Tổng nhiệm vụ AGV hoàn thành", sum_done, "#,##0", f"Đang chạy khi dừng: {sum_run} xe"),
        ("Theo khu vực", "Nhiệm vụ Khu Dock -> Kho 01", int(summary["agv_dock_tasks"]), "#,##0", f"{n_dock_agvs} AGV làn nhập Tầng 1"),
        ("Theo khu vực", "Nhiệm vụ Khu Unpack U1 & U2", int(summary["agv_unpack_tasks"]), "#,##0", f"{n_u1_agvs + n_u2_agvs} AGV dùng chung T1..T4 ({n_u1_agvs} U1 + {n_u2_agvs} U2)"),
        ("Theo khu vực", "Nhiệm vụ Khu Đóng Gói P1", int(summary["agv_p1_tasks"]), "#,##0", f"{n_p1_agvs} AGV dùng chung T1..T4 (6 xe/tầng)"),
        ("Quãng đường", "Quãng đường chạy có tải", round(sum_ld, 2), "#,##0.00", "m (mét có hàng)"),
        ("Quãng đường", "Quãng đường chạy rỗng", round(sum_em, 2), "#,##0.00", "m (mét chạy không tải)"),
        ("Quãng đường", "Tổng quãng đường toàn đội AGV", round(sum_tot, 2), "#,##0.00", "m (tổng quãng đường)"),
        ("Hiệu suất TB", "Tỉ lệ TB AGV chạy có ích", round(avg_agv_act, 2), '0.00"%"', "Tối đa 100% thời gian ca"),
        ("Hiệu suất TB", "Tỉ lệ TB AGV chờ Traffic", round(avg_agv_wait, 2), '0.00"%"', f"Tổng TG chờ Traffic: {_fmt_vi_hms(sum_ws)}"),
        ("Hiệu suất TB", "Tỉ lệ TB AGV đứng rỗi", round(avg_agv_pure_idle, 2), '0.00"%"', "Đứng chờ lệnh / vị trí rảnh"),
        ("Traffic 4 tầng", "Chờ kẹt tuyến tối đa (Max Wait)", _fmt_vi_hms(agv_tr.get("max_wait_s", 0.0)), None, f"Bám đuôi cùng chiều: {follow_ticks} ticks"),
        ("Pin & Lỗi", "Pin trung bình còn lại toàn đội", round(summary["agv_avg_battery_pct"], 2), '0.00"%"', f"Tổng số lỗi đội AGV: {sum_agv_faults}"),
    ]
    row = _write_kpi_cards(
        ws3, row,
        ["Nhóm chỉ tiêu", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        agv_kpis,
    )

    sec2_start_row = row
    row = _style_section(ws3, row, 4, "2. THÔNG SỐ CÀI ĐẶT VẬN TỐC, TRAFFIC & PIN AGV")
    agv_cfg_rows = [
        ["AGV Dock & chung - Vận tốc (agv_speed)", f"{float(cfg.get('agv_speed', 1.8)):.2f}", "m/s", "Vận tốc di chuyển thẳng tối đa của AGV Dock"],
        ["AGV U1 / U2 / P1 - Vận tốc từng khu", f"{float(cfg.get('u1_agv_speed', 1.5)):.2f} / {float(cfg.get('u2_agv_speed', 3.0)):.2f} / {float(cfg.get('p1_agv_speed', 1.5)):.2f}", "m/s", "Vận tốc riêng tại khu Unpack U1, U2 và Đóng gói P1"],
        ["AGV - Gia tốc / Độ giật thẳng", f"{float(cfg.get('agv_acceleration', 0.5)):.2f} / {float(cfg.get('agv_jerk', 1.0)):.2f}", "m/s² | m/s³", "Đồ thị chuyển động S-curve 7 pha êm mượt"],
        ["AGV - Vận tốc xoay & Gia tốc góc", f"{float(cfg.get('agv_angular_speed', 0.45)):.2f} / {float(cfg.get('agv_angular_acceleration', 0.35)):.2f}", "rad/s | rad/s²", f"Độ giật xoay góc: {float(cfg.get('agv_angular_jerk', 0.7)):.2f} rad/s³"],
        ["Khoảng cách giữ an toàn (agv_following_gap_m)", f"{float(cfg.get('agv_following_gap_m', 1.0)):.2f}", "m", "Khoảng cách an toàn khi bám đuôi trên tuyến hành lang"],
        ["Pin ban đầu / Hao tải / Hao rỗng", f"{tracker.init_battery_pct:.0f}% / {tracker.drain_loaded_per_m}%/m / {tracker.drain_empty_per_m}%/m", "% | %/m", f"Mức tiêu hao pin khi đứng chờ: {tracker.drain_idle_per_s}%/s"],
        ["Kiểm tra va chạm & Giao cắt 4 tầng", f"{int(agv_tr.get('trips', 0))} lượt | {swept_chk:,} checks", "4 tầng (T1..T4)", f"Tổng TG chờ AGV_TRAFFIC: {_fmt_vi_hms(agv_tr.get('wait_s', 0.0))}"],
    ]
    row = _write_table(
        ws3, row,
        ["Thông số cài đặt Đội AGV", "Giá trị cấu hình", "Đơn vị", "Diễn giải vận hành"],
        agv_cfg_rows,
        center_cols={2, 3},
        auto_filter=False,
    )
    sec2_end_row = row - 2
    ws3.row_dimensions[row - 1].height = 22
    ws3.row_dimensions[row].height = 22
    row += 1
    sec2_span_end = row - 1

    row = _style_section(ws3, row, 22, "3. BẢNG TỔNG HỢP HIỆU SUẤT THEO 4 KHU VỰC AGV (DOCK / U1 / U2 / P1)")
    area_rows = []
    for ag in sorted(by_area.values(), key=lambda x: x["order"]):
        cnt = max(1, ag["count"])
        act_p = round(ag["act_sum"] / cnt, 2)
        wait_p = round(ag["wait_sum"] / cnt, 2)
        idle_p = round(max(0.0, 100.0 - act_p - wait_p), 2)
        area_rows.append([
            ag["area"], ag["count"], ag["done"], ag["run"],
            round(ag["ld"], 2), round(ag["em"], 2), round(ag["tot"], 2),
            _excel_days(ag["bs"]), _excel_days(ag["ws"]), _excel_days(ag["pid"]),
            act_p, wait_p, idle_p, round(ag["bat_sum"] / cnt, 2), ag["faults"],
        ])
    a_hdr_row = row
    a_last_data_row = a_hdr_row + len(area_rows)
    row = _write_table(
        ws3, row,
        [
            "Khu vực AGV", "Số xe", "NV xong", "Đang chạy",
            "Có tải (m)", "Rỗng (m)", "Tổng quãng đường (m)",
            "TG hoạt động", "TG chờ Traffic", "TG đứng rỗi",
            "Tỉ lệ chạy (%)", "Tỉ lệ chờ Traffic (%)", "Tỉ lệ đứng rỗi (%)", "Pin còn TB (%)", "Số lỗi",
        ],
        area_rows,
        col_formats={
            2: "#,##0", 3: "#,##0", 4: "#,##0", 5: "#,##0.00", 6: "#,##0.00", 7: "#,##0.00",
            8: FMT_HMS_TENTH, 9: FMT_HMS_TENTH, 10: FMT_HMS_TENTH,
            11: "0.00", 12: "0.00", 13: "0.00", 14: "0.00", 15: "#,##0",
        },
        right_cols=set(range(2, 16)),
        center_cols={1},
        total_row=[
            "TỔNG / TB TOÀN ĐỘI", len(ordered_agvs), sum_done, sum_run,
            round(sum_ld, 2), round(sum_em, 2), round(sum_tot, 2),
            _excel_days(sum_bs), _excel_days(sum_ws), _excel_days(sum_pid),
            round(avg_agv_act, 2), round(avg_agv_wait, 2), round(avg_agv_pure_idle, 2),
            round(summary["agv_avg_battery_pct"], 2), sum_agv_faults,
        ],
        auto_filter=False,
    )

    row = _style_section(ws3, row, 22, "4. BẢNG TỔNG HỢP ĐỘI AGV THEO TẦNG (TẦNG 1 ĐẾN TẦNG 4)")
    floor_rows = []
    for fg in sorted(by_floor.values(), key=lambda x: x["floor"]):
        cnt = max(1, fg["count"])
        floor_rows.append([
            f"Tầng {fg['floor']}", fg["count"], fg["done"], round(fg["tot"], 2),
            _excel_days(fg["bs"]), _excel_days(fg["ws"]), _excel_days(fg["pid"]),
            round(fg["act_sum"] / cnt, 2), round(fg["wait_sum"] / cnt, 2), round(fg["idle_sum"] / cnt, 2),
            round(fg["bat_sum"] / cnt, 2), fg["faults"],
        ])
    row = _write_table(
        ws3, row,
        [
            "Tầng hoạt động", "Số xe AGV", "NV xong", "Tổng quãng đường (m)",
            "TG hoạt động", "TG chờ Traffic", "TG đứng rỗi",
            "Tỉ lệ chạy (%)", "Tỉ lệ chờ Traffic (%)", "Tỉ lệ đứng rỗi (%)", "Pin còn TB (%)", "Số lỗi",
        ],
        floor_rows,
        col_formats={
            2: "#,##0", 3: "#,##0", 4: "#,##0.00", 5: FMT_HMS_TENTH, 6: FMT_HMS_TENTH, 7: FMT_HMS_TENTH,
            8: "0.00", 9: "0.00", 10: "0.00", 11: "0.00", 12: "#,##0",
        },
        right_cols=set(range(2, 13)),
        center_cols={1},
        auto_filter=False,
    )

    row = _style_section(ws3, row, 22, "5. CƠ CẤU THỜI GIAN TRUNG BÌNH ĐỘI AGV (NGUỒN BIỂU ĐỒ TRÒN 100%)")
    agv_pct_act = round(min(100.0, max(0.0, avg_agv_act)), 2)
    agv_pct_wait = round(min(max(0.0, 100.0 - agv_pct_act), max(0.0, avg_agv_wait)), 2)
    agv_pct_idle = round(max(0.0, 100.0 - agv_pct_act - agv_pct_wait), 2)
    agv_state_rows = [
        ["Đang chạy có ích", round(sum_bs / 60.0, 2), agv_pct_act, f"{sum_done} nhiệm vụ | {sum_tot:,.2f} m"],
        ["Chờ Traffic giao cắt", round(sum_ws / 60.0, 2), agv_pct_wait, f"Nút giao 4 tầng: {int(agv_tr.get('trips', 0))} lượt"],
        ["Đứng rỗi chờ lệnh", round(sum_pid / 60.0, 2), agv_pct_idle, f"Đội {len(ordered_agvs)} xe AGV | Lỗi: {sum_agv_faults}"],
    ]
    st_hdr_row = row
    st_last_data_row = st_hdr_row + len(agv_state_rows)
    row = _write_table(
        ws3, row,
        ["Trạng thái AGV", "Tổng thời gian (phút)", "Tỉ lệ (%)", "Ghi chú chi tiết"],
        agv_state_rows,
        col_formats={2: "#,##0.00", 3: "0.00"},
        right_cols={2, 3},
        center_cols={1},
        total_row=["TỔNG (TỐI ĐA 100%)", round((sum_bs + sum_ws + sum_pid) / 60.0, 2), 100.0, f"Pin TB còn: {summary['agv_avg_battery_pct']:.2f}%"],
        auto_filter=False,
    )

    ws3.add_chart(_make_bar_chart(
        ws3, "Số nhiệm vụ hoàn thành theo 4 khu vực AGV", "Khu vực AGV", "Số nhiệm vụ",
        21.4, 11.0, 3, 3, a_hdr_row, a_last_data_row,
    ), f"E{sec1_tbl_start}")
    ws3.add_chart(_make_pie_chart(
        ws3, f"Cơ cấu thời gian TB {len(ordered_agvs)} AGV (Tổng 100%)",
        14.2, 11.0, 3, st_hdr_row, st_last_data_row,
    ), f"H{sec1_tbl_start}")
    ws3.add_chart(_make_bar_chart(
        ws3, "Phân bổ chạy / chờ Traffic / đứng rỗi theo khu vực AGV (Tối đa 100%)",
        "Khu vực AGV", "Tỉ lệ (%)", 36.0, 12.8, 11, 13, a_hdr_row, a_last_data_row,
        stacked=True, y_min=0, y_max=100,
    ), f"E{sec2_start_row}")

    row = _style_section(ws3, row, 22, f"6. BẢNG TRẠNG THÁI CHI TIẾT TỪNG XE AGV ({len(ordered_agvs)} XE)")
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
    _finalize_dashboard_sheet(
        ws3, 22,
        wrapped_ranges=[(sec2_start_row + 2, sec2_end_row, 4)],
        min_span_heights=[
            (sec1_tbl_start, sec2_start_row - 1, 11.8),
            (sec2_start_row, sec2_span_end, 13.6),
        ],
    )


def _build_dock_sheet(wb: Workbook, tracker: SimulationTracker, summary: dict[str, Any], sim_dur: float) -> None:
    """Build Sheet 4: Khu_Vuc_Dock with separated 3-state KPI rows, dock specs, 3 top charts and detailed logs."""
    cfg = tracker.config
    ws4 = wb.create_sheet("Khu_Vuc_Dock")
    dock_agv_rows = [t for t in tracker.agv_tasks if t["area"] == "Khu Dock -> Kho 01"]
    row = _style_banner(
        ws4, 1, 18,
        "BÁO CÁO KHU VỰC DOCK (AGV DOCK, PALLET CHẴN/LẺ, PICK/DROP, TRAFFIC GIAO CẮT)",
        f"Thời gian thực tế chuẩn 1x: {summary['sim_duration_hms']} | Tách riêng Hoạt động / Chờ ô & AGV / Nghỉ rảnh (Tổng 100%).",
    )
    per_dock_rows = []
    tot_d_busy = tot_d_cw = tot_d_pid = tot_d_wait = tot_d_slot_wait = 0.0
    tot_even_batches = tot_odd_batches = tot_even_cartons = tot_odd_cartons = 0
    tot_even_ready = tot_odd_ready = tot_empty_ret = 0
    for did in tracker.dock_order:
        d = tracker.docks[did]
        d_busy = float(d["busy_s"])
        d_cw = float(d.get("cmd_wait_s", 0.0))
        d_pid = float(d.get("pure_idle_s", max(0.0, sim_dur - d_busy - d_cw)))
        tot_d_busy += d_busy
        tot_d_cw += d_cw
        tot_d_pid += d_pid
        tot_d_wait += d["gate_wait_s"]
        tot_d_slot_wait += d.get("slot_wait_s", 0.0)
        tot_even_batches += d["even_batches"]
        tot_odd_batches += d["odd_batches"]
        tot_even_cartons += d["even_cartons"]
        tot_odd_cartons += d["odd_cartons"]
        tot_even_ready += d["even_ready"]
        tot_odd_ready += d["odd_ready"]
        tot_empty_ret += d["empty_returns"]
        per_dock_rows.append([
            f"Dock {did:02d}", d["worker_code"],
            f"Xe tải Dock {did:02d}", "4 Ô Pickup (Chẵn) / Bàn Unpack T1 (Lẻ)",
            d["even_batches"], d["odd_batches"], d["even_cartons"], d["odd_cartons"],
            d["even_ready"], d["odd_ready"], d["empty_returns"],
            _excel_days(d_busy), _excel_days(d_cw), _excel_days(d_pid),
            d["active_pct"], d.get("cmd_wait_pct", 0.0), d.get("pure_idle_pct", d["idle_pct"]),
            f"{d['agv_tasks']} ({_fmt_codes(d['agv_task_codes'], 6)})" if d["agv_tasks"] else "0",
        ])

    sum_dk_dur = sum((t["duration_s"] or 0.0) for t in dock_agv_rows)
    sum_dk_wait = sum((t.get("wait_s", 0.0) or 0.0) for t in dock_agv_rows)
    sum_dk_dist = sum(t["total_m"] for t in dock_agv_rows)
    dk_done = sum(1 for r in dock_agv_rows if r["status"] == "Hoàn thành")
    dk_running = sum(1 for r in dock_agv_rows if r["status"] == "Đang thực hiện")
    tot_d_cap = max(1, len(tracker.dock_order)) * sim_dur
    avg_dock_act = _pct(tot_d_busy, tot_d_cap)
    avg_dock_cw = round(min(max(0.0, 100.0 - avg_dock_act), _pct(tot_d_cw, tot_d_cap)), 2) if tot_d_cap > 0 else 0.0
    avg_dock_pid = round(max(0.0, 100.0 - avg_dock_act - avg_dock_cw), 2) if tot_d_cap > 0 else 0.0

    _write_top_dashboard_headers(
        ws4, row,
        "1. TỔNG HỢP HOẠT ĐỘNG KHU VỰC DOCK",
        "BIỂU ĐỒ SẢN LƯỢNG PALLET & PHÂN BỔ 3 TRẠNG THÁI TỪNG CỬA DOCK (TỐI ĐA 100%)",
    )
    row += 1
    sec1_tbl_start = row
    dock_kpis = [
        ("Cơ sở hạ tầng", "Số cửa Dock hoạt động", len(tracker.dock_order), "#,##0", f"Cửa Dock vật lý: {cfg.get('dock_physical_count', len(tracker.dock_order))}"),
        ("Pallet Chẵn", "Batch xe tải Pallet Chẵn", tot_even_batches, "#,##0", "batch xe tải chẵn"),
        ("Pallet Chẵn", "Tổng kiện chẵn quét Barcode", int(summary["dock_scanned_even"]), "#,##0", "kiện chẵn đã quét"),
        ("Pallet Chẵn", "Pallet chẵn hoàn tất tại Dock", tot_even_ready, "#,##0", f"Giao ra 4 Ô Pickup: {tracker.dock_flow_stats.get('pallets_to_w1', 0)}"),
        ("AGV Dock", "Tổng nhiệm vụ AGV Dock -> Kho 01", len(dock_agv_rows), "#,##0", f"Đang thực hiện: {dk_running} lượt"),
        ("AGV Dock", "Nhiệm vụ AGV Dock đã hoàn thành", dk_done, "#,##0", f"Quãng đường AGV Dock: {sum_dk_dist:,.2f} m"),
        ("Pallet Lẻ", "Batch xe tải Pallet Lẻ", tot_odd_batches, "#,##0", "batch xe tải lẻ"),
        ("Pallet Lẻ", "Tổng kiện lẻ quét Barcode", int(summary["dock_scanned_odd"]), "#,##0", "kiện lẻ đã quét"),
        ("Pallet Lẻ", "Pallet lẻ hoàn tất tại Dock", tot_odd_ready, "#,##0", f"Sang Bàn Unpack T1: {tracker.dock_flow_stats.get('pallets_to_w2', 0)}"),
        ("Pallet Rỗng", "Tổng pallet rỗng thu hồi", tot_empty_ret, "#,##0", f"Hồi pool: {tracker.dock_flow_stats.get('empty_pallet_returns', 0)}"),
        ("Nhân công Dock", "Tổng TG làm việc thực tế tại Dock", _fmt_vi_hms(tot_d_busy), None, f"Dỡ, quét & kéo hàng ({avg_dock_act:.2f}% quỹ thời gian)"),
        ("Nhân công Dock", "Tổng TG có lệnh chờ ô & làn AGV", _fmt_vi_hms(tot_d_cw), None, f"FloorGate: {_fmt_vi_hms(tot_d_wait)} | Chờ ô: {_fmt_vi_hms(tot_d_slot_wait)}"),
        ("Nhân công Dock", "Tổng TG nghỉ rảnh tại Dock", _fmt_vi_hms(tot_d_pid), None, f"TB {avg_dock_pid:.2f}% (chờ xe tải / pallet mới)"),
        ("Hiệu suất Dock", "Tỉ lệ hoạt động TB các cửa Dock", avg_dock_act, '0.00"%"', "Đang thao tác trực tiếp (Tối đa 100%)"),
        ("Hiệu suất Dock", "Tỉ lệ chờ ô / làn AGV TB", avg_dock_cw, '0.00"%"', "Có hàng nhưng đợi Ô Pickup / Bàn T1 / FloorGate"),
        ("Hiệu suất Dock", "Tỉ lệ nghỉ rảnh TB các cửa Dock", avg_dock_pid, '0.00"%"', "Trống việc (= 100% - Chạy - Chờ ô/AGV)"),
        ("Traffic AGV", "Tổng TG chờ Traffic (AGV Dock)", _fmt_vi_hms(sum_dk_wait), None, "Cộng dồn từ log AGV Dock -> Kho 01"),
    ]
    row = _write_kpi_cards(
        ws4, row,
        ["Nhóm chỉ tiêu", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        dock_kpis,
    )

    sec2_start_row = row
    row = _style_section(ws4, row, 4, "2. THÔNG SỐ CÀI ĐẶT KHU VỰC DOCK & QUÉT BARCODE")
    dock_cfg_rows = [
        ["Số cửa Dock kích hoạt", f"{cfg.get('dock_active_count', len(tracker.dock_order))} / {cfg.get('dock_physical_count', 11)}", "cửa Dock", f"Danh sách cửa: {cfg.get('dock_active_ids', tracker.dock_order)}"],
        ["Thời gian dỡ xe tải (dock_unload_s)", f"{float(cfg.get('dock_unload_s', 8.0)):.2f}", "giây/batch", "Thời gian công nhân dỡ hàng từ thùng xe tải"],
        ["Vận tốc công nhân Dock (dock_worker_speed)", f"{float(cfg.get('dock_worker_speed', 1.5)):.2f}", "m/s", "Tốc độ di chuyển xe nâng tay của công nhân Dock"],
        ["Vận tốc kéo pallet (dock_pallet_speed)", f"{float(cfg.get('dock_pallet_speed', 1.2)):.2f}", "m/s", "Tốc độ kéo pallet từ Dock ra Ô Pickup / Bàn Unpack"],
        ["Thời gian quét Barcode (inbound_scan_s)", f"{float(cfg.get('inbound_scan_s', 0.4)):.2f}", "giây/kiện", "Quét phân loại luồng hàng Chẵn / Lẻ tại cửa Dock"],
        ["Thao tác nhấc / đặt (pick / place)", f"{float(cfg.get('dock_pick_s', 0.35)):.2f} / {float(cfg.get('dock_place_s', 0.35)):.2f}", "giây", "Thời gian nâng và hạ pallet tại điểm nhận/trả"],
        ["Luồng phân loại (even / odd flow)", f"{cfg.get('even_flow', 'storage')} / {cfg.get('odd_flow', 'w2_spiral')}", "2 luồng", "Chẵn -> Kho AS/RS; Lẻ -> Khu Unpack T1 & băng tải xoắn"],
    ]
    row = _write_table(
        ws4, row,
        ["Thông số cài đặt Khu Vực Dock", "Giá trị cấu hình", "Đơn vị", "Diễn giải vận hành"],
        dock_cfg_rows,
        center_cols={2, 3},
        auto_filter=False,
    )
    sec2_end_row = row - 2
    ws4.row_dimensions[row - 1].height = 22
    ws4.row_dimensions[row].height = 22
    row += 1
    sec2_span_end = row - 1

    row = _style_section(ws4, row, 18, "3. THỐNG KÊ CHI TIẾT TỪNG CỬA DOCK (PICK / DROP, 3 TRẠNG THÁI & TRAFFIC)")
    d_hdr_row = row
    d_last_data_row = d_hdr_row + len(per_dock_rows)
    row = _write_table(
        ws4, row,
        [
            "Cửa Dock", "Công nhân", "Vị trí Pick", "Vị trí Drop",
            "Batch chẵn", "Batch lẻ", "Kiện chẵn", "Kiện lẻ",
            "Pallet chẵn xong", "Pallet lẻ xong", "Pallet rỗng hồi",
            "TG làm việc thực tế", "TG chờ ô & AGV", "TG nghỉ rảnh",
            "Tỉ lệ chạy (%)", "Tỉ lệ chờ ô & AGV (%)", "Tỉ lệ nghỉ rảnh (%)", "NV AGV Dock (Số lượng & Mã)",
        ],
        per_dock_rows,
        col_formats={
            **{c: "#,##0" for c in range(5, 12)},
            12: FMT_HMS_TENTH, 13: FMT_HMS_TENTH, 14: FMT_HMS_TENTH,
            15: "0.00", 16: "0.00", 17: "0.00",
        },
        right_cols=set(range(5, 18)),
        center_cols={1, 2},
        total_row=[
            "TỔNG / TB", "", "", "",
            tot_even_batches, tot_odd_batches, tot_even_cartons, tot_odd_cartons,
            tot_even_ready, tot_odd_ready, tot_empty_ret,
            _excel_days(tot_d_busy), _excel_days(tot_d_cw), _excel_days(tot_d_pid),
            avg_dock_act, avg_dock_cw, avg_dock_pid,
            f"{len(dock_agv_rows)} NV",
        ],
        auto_filter=False,
    )

    row = _style_section(ws4, row, 18, "4. TỈ TRỌNG PALLET CHẴN / LẺ TẠI DOCK (NGUỒN BIỂU ĐỒ TRÒN 100%)")
    tot_p_dock = tot_even_ready + tot_odd_ready
    dock_mix_rows = [
        ["Pallet Chẵn (Vào Kho AS/RS)", tot_even_ready, _pct(tot_even_ready, tot_p_dock), f"Kiện chẵn quét: {summary['dock_scanned_even']}"],
        ["Pallet Lẻ (Sang Bàn Unpack T1)", tot_odd_ready, round(max(0.0, 100.0 - _pct(tot_even_ready, tot_p_dock)), 2) if tot_p_dock > 0 else 0.0, f"Kiện lẻ quét: {summary['dock_scanned_odd']}"],
    ]
    dm_hdr = row
    dm_last = dm_hdr + len(dock_mix_rows)
    row = _write_table(
        ws4, row,
        ["Loại Pallet qua Dock", "Số pallet xong", "Tỉ trọng (%)", "Ghi chú chi tiết"],
        dock_mix_rows,
        col_formats={2: "#,##0", 3: "0.00"},
        right_cols={2, 3},
        center_cols={1},
        total_row=["TỔNG CỘNG (100%)", tot_p_dock, 100.0 if tot_p_dock > 0 else 0.0, f"Pallet rỗng thu hồi: {tot_empty_ret}"],
        auto_filter=False,
    )

    ws4.add_chart(_make_bar_chart(
        ws4, "Số lượng Pallet Chẵn & Lẻ hoàn tất theo từng cửa Dock", "Cửa Dock", "Số pallet",
        21.4, 12.2, 9, 10, d_hdr_row, d_last_data_row,
    ), f"E{sec1_tbl_start}")
    ws4.add_chart(_make_pie_chart(
        ws4, "Tỉ trọng Pallet Chẵn vs Pallet Lẻ (Tổng 100%)", 14.2, 12.2, 3, dm_hdr, dm_last,
    ), f"H{sec1_tbl_start}")
    ws4.add_chart(_make_bar_chart(
        ws4, "Hoạt động / Chờ ô & AGV / Nghỉ rảnh từng cửa Dock (Tối đa 100%)",
        "Cửa Dock", "Tỉ lệ (%)", 36.0, 12.6, 15, 17, d_hdr_row, d_last_data_row,
        stacked=True, y_min=0, y_max=100,
    ), f"E{sec2_start_row}")

    row = _style_section(ws4, row, 18, "5. DANH SÁCH NHIỆM VỤ AGV KHU VỰC DOCK (VỊ TRÍ PICK/DROP & THỜI GIAN)")
    dock_agv_table = [
        [
            t["task_code"], f"Dock {t['dock_id']:02d}" if t["dock_id"] is not None else "-",
            t["pallet_or_item"], t["agv_code"],
            t.get("pick_location") or "-", t.get("drop_location") or "-",
            _excel_days(t["start_s"]), _excel_days(t["end_s"]),
            _excel_days(t["duration_s"]), _excel_days(t.get("wait_s", 0.0)),
            round(t["total_m"], 2), t.get("fault_count", 0), t["status"],
        ]
        for t in dock_agv_rows
    ]
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
        auto_filter=False,
    )

    row = _style_section(ws4, row, 18, "6. NHẬT KÝ SỰ KIỆN PALLET CHẴN / LẺ TẠI DOCK")
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
    _finalize_dashboard_sheet(
        ws4, 18,
        wrapped_ranges=[(sec2_start_row + 2, sec2_end_row, 4)],
        min_span_heights=[
            (sec1_tbl_start, sec2_start_row - 1, 13.0),
            (sec2_start_row, sec2_span_end, 13.4),
        ],
    )


def _build_workers_sheet(wb: Workbook, tracker: SimulationTracker, summary: dict[str, Any], sim_dur: float) -> None:
    """Build Sheet 5: Cong_Nhan_Unpack with separated 3-state KPI rows, worker timings, charts and detailed roster."""
    cfg = tracker.config
    ws5 = wb.create_sheet("Cong_Nhan_Unpack")
    row = _style_banner(
        ws5, 1, 15,
        "BÁO CÁO NĂNG SUẤT CÔNG NHÂN UNPACK & ĐÓNG GÓI TOÀN NHÀ MÁY",
        f"Thời gian quy chuẩn 1x ({summary['sim_duration_hms']}, định dạng Giờ:Phút:Giây) | Tách rõ Làm việc / Có việc chờ hàng-AGV / Nghỉ rảnh (Tổng 100%).",
    )
    grp_rows = _build_worker_groups(tracker, sim_dur)
    tot_hc = sum(r[1] for r in grp_rows)
    tot_active = sum(r[2] for r in grp_rows)
    tot_pallets = sum(r[3] for r in grp_rows)
    tot_cartons = sum(r[4] for r in grp_rows)
    tot_meds = sum(r[5] for r in grp_rows)
    tot_shared = sum(w["shared_picks"] for w in tracker.workers.values())
    tot_w_busy = float(summary.get("worker_total_busy_s", sum(w["busy_s"] for w in tracker.workers.values())))
    tot_w_cw = float(summary.get("worker_total_cmd_wait_s", sum(w.get("cmd_wait_s", 0.0) for w in tracker.workers.values())))
    tot_w_pid = float(summary.get("worker_total_pure_idle_s", sum(w.get("pure_idle_s", w["idle_s"]) for w in tracker.workers.values())))
    tot_cap = tot_hc * sim_dur
    avg_w_act = float(summary.get("worker_avg_active_pct", _pct(tot_w_busy, tot_cap)))
    avg_w_cw = float(summary.get("worker_avg_cmd_wait_pct", 0.0))
    avg_w_pid = float(summary.get("worker_avg_pure_idle_pct", round(max(0.0, 100.0 - avg_w_act - avg_w_cw), 2) if tot_cap > 0 else 0.0))

    _write_top_dashboard_headers(
        ws5, row,
        "1. TỔNG HỢP NHÂN LỰC & NĂNG SUẤT CÔNG NHÂN",
        "BIỂU ĐỒ SẢN LƯỢNG & CƠ CẤU 3 TRẠNG THÁI CÔNG NHÂN (TỐI ĐA 100%)",
    )
    row += 1
    sec1_tbl_start = row
    worker_kpis = [
        ("Nhân sự", "Tổng vị trí công nhân toàn nhà máy", tot_hc, "#,##0", "6 khu vực từ Dock đến Đóng gói P1"),
        ("Nhân sự", "Số vị trí có tham gia xử lý", tot_active, "#,##0", "vị trí có phát sinh sản lượng / chờ công đoạn"),
        ("Nhân sự", "Số vị trí chưa phát sinh việc", max(0, tot_hc - tot_active), "#,##0", "vị trí dự phòng / chưa phân bổ"),
        ("Sản lượng", "Tổng lượt pallet đã xử lý", tot_pallets, "#,##0", "lượt pallet (Dock + Unpack T1)"),
        ("Sản lượng", "Tổng thùng / kiện đã xử lý", tot_cartons, "#,##0", "thùng carton"),
        ("Sản lượng", "Tổng hộp thuốc đã chia / đóng gói", tot_meds, "#,##0", "hộp thuốc (U1 + U2 + P1)"),
        ("Hỗ trợ", "Tổng lượt hỗ trợ chéo trạm khác", tot_shared, "#,##0", "lượt hỗ trợ chéo giữa công nhân"),
        ("Thời gian", "Tổng TG làm việc thực tế", _fmt_vi_hms(tot_w_busy), None, "Đang quét, nhấc, mở thùng, đặt hộp, đóng gói"),
        ("Thời gian", "Tổng TG có việc chờ hàng/AGV", _fmt_vi_hms(tot_w_cw), None, "Chờ AGV đổi Rack/Trolley, chờ U2, chờ cửa/ô"),
        ("Thời gian", "Tổng TG nghỉ rảnh (chưa có hàng)", _fmt_vi_hms(tot_w_pid), None, "Nhân sự đứng chờ chưa có hàng tới trạm"),
        ("Hiệu suất TB", "Tỉ lệ làm việc TB toàn nhân sự", avg_w_act, '0.00"%"', "Tối đa 100% thời gian ca"),
        ("Hiệu suất TB", "Tỉ lệ chờ hàng / công đoạn TB", avg_w_cw, '0.00"%"', "Có việc đang đợi bàn giao (AGV / U2 / Dock)"),
        ("Hiệu suất TB", "Tỉ lệ nghỉ rảnh TB toàn nhân sự", avg_w_pid, '0.00"%"', "Nghỉ rảnh (= 100% - Làm - Chờ hàng/AGV)"),
        ("Năng suất TB", "Số thùng TB / nhân sự hoạt động", round(tot_cartons / max(1, tot_active), 2), "#,##0.00", "thùng / vị trí có hoạt động"),
    ]
    row = _write_kpi_cards(
        ws5, row,
        ["Nhóm chỉ tiêu", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        worker_kpis,
    )

    sec2_start_row = row
    row = _style_section(ws5, row, 4, "2. THÔNG SỐ THAO TÁC CÔNG NHÂN & BĂNG TẢI CÀI ĐẶT")
    wrk_cfg_rows = [
        ["Unpack T1 - Nhấc thùng lên băng (loose_unpack_s)", f"{float(cfg.get('loose_unpack_s', 1.2)):.2f}", "giây/thùng", f"{cfg.get('loose_cartons_per_pallet', 18)} thùng/pallet lẻ"],
        ["U1 - Mở thùng & Xếp đủ Rack (cartons/rack)", f"{float(cfg.get('u1_open_carton_s', 1.2)):.2f} / {float(cfg.get('u1_pick_box_s', 1.5)):.2f}s", "giây", f"Đủ {cfg.get('u1_cartons_per_rack', 4)} khay/Rack & 5 thùng/Trolley mới gọi AGV ({cfg.get('u1_agvs_per_floor', 2)} xe/tầng)"],
        ["Zone 05 - Xe kéo tay & Hoàn kho (partial / every)", f"{float(cfg.get('z05_walk_speed', 2.8)):.2f} m/s | Mốc {cfg.get('z05_partial_return_every', 4)}", "m/s | pallet", f"Kéo pallet từ Thang Xuất về Z05; pallet thứ {cfg.get('z05_partial_return_every', 4)} unpack {cfg.get('z05_partial_unpack_cartons', 12)}/{cfg.get('z05_cartons_per_pallet', 18)} thùng rồi hoàn kho"],
        ["U2 - Mở thùng & Xếp đủ Rack (cartons/rack)", f"{float(cfg.get('u2_open_carton_s', 0.6)):.2f} / {float(cfg.get('u2_pick_box_s', 0.7)):.2f}s", "giây", f"Đủ {cfg.get('u2_cartons_per_rack', 4)} khay/Rack & 5 thùng/Trolley mới gọi AGV ({cfg.get('u2_agvs_per_floor', 2)} xe/tầng)"],
        ["Đóng gói P1 - Nhặt sạch Rack (p1_pick_box_s)", f"{float(cfg.get('p1_pick_box_s', 12.0)):.2f}", "giây/thùng", f"Nhặt hết {cfg.get('p1_cartons_per_rack', 4)} khay/Rack & 5 thùng/Trolley mới gọi AGV ({cfg.get('p1_agvs_per_floor', 6)} xe/tầng)"],
        ["Băng tải thùng Carton (carton_conveyor_speed)", f"{float(cfg.get('carton_conveyor_speed', 1.0)):.2f}", "m/s", "Vận tốc băng tải xoắn & băng nhánh lên tầng 1..4"],
    ]
    row = _write_table(
        ws5, row,
        ["Thông số thao tác Công nhân", "Giá trị cấu hình", "Đơn vị", "Diễn giải quy trình"],
        wrk_cfg_rows,
        center_cols={2, 3},
        auto_filter=False,
    )
    sec2_end_row = row - 2
    ws5.row_dimensions[row - 1].height = 22
    ws5.row_dimensions[row].height = 22
    row += 1
    sec2_span_end = row - 1

    row = _style_section(ws5, row, 15, "3. TỔNG HỢP THEO 6 NHÓM CÔNG NHÂN (LÀM VIỆC / CHỜ HÀNG-AGV / NGHỈ RẢNH)")
    g_hdr_row = row
    g_last_data_row = g_hdr_row + len(grp_rows)
    row = _write_table(
        ws5, row,
        [
            "Khu vực công nhân", "Tổng nhân sự", "Có hoạt động", "Lượt pallet",
            "Thùng / Kiện xử lý", "Hộp thuốc xử lý",
            "TG làm việc thực tế", "TG chờ hàng/AGV", "TG nghỉ rảnh",
            "Tỉ lệ làm việc (%)", "Tỉ lệ chờ hàng/AGV (%)", "Tỉ lệ nghỉ rảnh (%)",
        ],
        grp_rows,
        col_formats={
            2: "#,##0", 3: "#,##0", 4: "#,##0", 5: "#,##0", 6: "#,##0",
            7: FMT_HMS, 8: FMT_HMS, 9: FMT_HMS,
            10: "0.00", 11: "0.00", 12: "0.00",
        },
        right_cols=set(range(2, 13)),
        total_row=[
            "TỔNG TOÀN NHÀ MÁY", tot_hc, tot_active, tot_pallets, tot_cartons, tot_meds,
            _excel_days(tot_w_busy), _excel_days(tot_w_cw), _excel_days(tot_w_pid),
            avg_w_act, avg_w_cw, avg_w_pid,
        ],
        auto_filter=False,
    )

    row = _style_section(ws5, row, 15, "4. PHÂN BỔ NĂNG SUẤT CÔNG NHÂN THEO TẦNG (TẦNG 1 ĐẾN TẦNG 4)")
    wf_rows = _aggregate_worker_rows(
        list(tracker.workers.values()), sim_dur,
        key_fn=lambda w: w["floor"], label_fn=lambda w: f"Tầng {w['floor']}", sort_key_fn=lambda w: w["floor"],
    )
    row = _write_table(
        ws5, row,
        [
            "Tầng làm việc", "Tổng nhân sự", "Có hoạt động", "Lượt pallet",
            "Thùng / Kiện", "Hộp thuốc",
            "TG làm việc thực tế", "TG chờ hàng/AGV", "TG nghỉ rảnh",
            "Tỉ lệ làm (%)", "Tỉ lệ chờ hàng/AGV (%)", "Tỉ lệ nghỉ rảnh (%)",
        ],
        wf_rows,
        col_formats={
            2: "#,##0", 3: "#,##0", 4: "#,##0", 5: "#,##0", 6: "#,##0",
            7: FMT_HMS, 8: FMT_HMS, 9: FMT_HMS,
            10: "0.00", 11: "0.00", 12: "0.00",
        },
        right_cols=set(range(2, 13)),
        center_cols={1},
        auto_filter=False,
    )

    row = _style_section(ws5, row, 15, "5. CƠ CẤU THỜI GIAN TB NHÂN SỰ TOÀN NHÀ MÁY (NGUỒN BIỂU ĐỒ TRÒN 100%)")
    wrk_state_rows = [
        ["Đang thao tác xử lý hàng", round(tot_w_busy / 60.0, 2), avg_w_act, f"{tot_active}/{tot_hc} nhân sự tham gia | {tot_cartons} thùng, {tot_meds} hộp"],
        ["Có việc - Chờ hàng / Đổi Rack-Trolley", round(tot_w_cw / 60.0, 2), avg_w_cw, "Chờ AGV đổi Rack/Trolley, chờ U2, chờ FloorGate / Ô Pickup"],
        ["Nghỉ rảnh (Chưa có hàng)", round(tot_w_pid / 60.0, 2), avg_w_pid, f"Chờ hàng tới trạm / chưa phân bổ | Hỗ trợ chéo: {tot_shared} lượt"],
    ]
    ws_hdr = row
    ws_last = ws_hdr + len(wrk_state_rows)
    row = _write_table(
        ws5, row,
        ["Trạng thái nhân sự", "Tổng thời gian (phút)", "Tỉ lệ (%)", "Ghi chú chi tiết"],
        wrk_state_rows,
        col_formats={2: "#,##0.00", 3: "0.00"},
        right_cols={2, 3},
        center_cols={1},
        total_row=["TỔNG (TỐI ĐA 100%)", round((tot_w_busy + tot_w_cw + tot_w_pid) / 60.0, 2), 100.0 if tot_cap > 0 else 0.0, f"Pallet xử lý: {tot_pallets}"],
        auto_filter=False,
    )

    ws5.add_chart(_make_bar_chart(
        ws5, "Sản lượng Thùng & Hộp thuốc xử lý theo nhóm công nhân", "Nhóm công nhân", "Số lượng",
        21.4, 12.0, 5, 6, g_hdr_row, g_last_data_row,
    ), f"E{sec1_tbl_start}")
    ws5.add_chart(_make_pie_chart(
        ws5, "Cơ cấu thời gian TB nhân sự (Tổng 100%)", 14.2, 12.0, 3, ws_hdr, ws_last,
    ), f"H{sec1_tbl_start}")
    ws5.add_chart(_make_bar_chart(
        ws5, "Làm việc / Chờ hàng-AGV / Nghỉ rảnh theo nhóm công nhân (Tối đa 100%)",
        "Nhóm công nhân", "Tỉ lệ (%)", 36.0, 12.2, 10, 12, g_hdr_row, g_last_data_row,
        stacked=True, y_min=0, y_max=100,
    ), f"E{sec2_start_row}")

    row = _style_section(ws5, row, 15, "6. BẢNG CHI TIẾT TỪNG CÔNG NHÂN UNPACK / XỬ LÝ HÀNG")
    ordered_workers = sorted(
        tracker.workers.values(),
        key=lambda w: (w["group_order"], 0 if w["cartons"] > 0 or w["med_boxes"] > 0 or w["busy_s"] > 0 or w.get("cmd_wait_s", 0.0) > 0 else 1, w["floor"], w["code"]),
    )
    worker_rows = [
        [
            w["code"], w["area"], f"Tầng {w['floor']}", w["station"], w.get("route_desc") or "-",
            len(w["pallets"]), w["cartons"], w["med_boxes"], w["shared_picks"],
            _excel_days(w["busy_s"]), _excel_days(w.get("cmd_wait_s", 0.0)), _excel_days(w.get("pure_idle_s", w["idle_s"])),
            w["active_pct"], w.get("cmd_wait_pct", 0.0), w.get("pure_idle_pct", w["idle_pct"]),
        ]
        for w in ordered_workers
    ]
    _write_table(
        ws5, row,
        [
            "Mã công nhân", "Khu vực", "Tầng", "Vị trí / Trạm", "Tuyến Pick -> Drop",
            "Số pallet", "Thùng / Kiện unpack", "Hộp thuốc", "Hỗ trợ chéo",
            "TG làm việc thực tế", "TG chờ hàng/AGV", "TG nghỉ rảnh",
            "Tỉ lệ làm (%)", "Tỉ lệ chờ hàng/AGV (%)", "Tỉ lệ nghỉ rảnh (%)",
        ],
        worker_rows,
        col_formats={
            6: "#,##0", 7: "#,##0", 8: "#,##0", 9: "#,##0",
            10: FMT_HMS_TENTH, 11: FMT_HMS_TENTH, 12: FMT_HMS_TENTH,
            13: "0.00", 14: "0.00", 15: "0.00",
        },
        right_cols=set(range(6, 16)),
        center_cols={1, 3},
    )
    _finalize_dashboard_sheet(
        ws5, 15,
        wrapped_ranges=[(sec2_start_row + 2, sec2_end_row, 4)],
        min_span_heights=[
            (sec1_tbl_start, sec2_start_row - 1, 12.8),
            (sec2_start_row, sec2_span_end, 13.0),
        ],
    )


def _build_agv_log_sheet(wb: Workbook, tracker: SimulationTracker, summary: dict[str, Any]) -> None:
    """Build Sheet 6: Chi_Tiet_Nhiem_Vu_AGV_Kho with top KPI cards, hourly/area charts, source tables and 19-col log."""
    ws6 = wb.create_sheet("Chi_Tiet_Nhiem_Vu_AGV_Kho")
    row = _style_banner(
        ws6, 1, 19,
        "NHẬT KÝ CHI TIẾT TOÀN BỘ MÃ NHIỆM VỤ AGV (PICK / DROP, TRAFFIC, THỜI GIAN THỰC HIỆN)",
        f"Thời gian thực tế chuẩn 1x ({summary['sim_duration_hms']}, định dạng Giờ:Phút:Giây)",
    )
    agv_task_rows = []
    tot_at_dur = tot_at_wait = tot_at_ld = tot_at_em = tot_at_dist = 0.0
    tot_at_faults = at_done = at_running = 0
    area_order = [
        "Khu Dock -> Kho 01",
        "Khu Unpack U1",
        "Khu Unpack U2 (Zone 05)",
        "Khu Đóng Gói P1",
    ]
    area_stats: dict[str, dict[str, Any]] = {
        k: {"area": k, "cnt": 0, "dur": 0.0, "wait": 0.0, "ld": 0.0, "em": 0.0, "dist": 0.0}
        for k in area_order
    }
    for t in tracker.agv_tasks:
        d_s = t["duration_s"] or 0.0
        w_s = t.get("wait_s", 0.0) or 0.0
        tot_at_dur += d_s
        tot_at_wait += w_s
        tot_at_ld += t["loaded_m"]
        tot_at_em += t["empty_m"]
        tot_at_dist += t["total_m"]
        tot_at_faults += t.get("fault_count", 0)
        if t["status"] == "Hoàn thành":
            at_done += 1
        elif t["status"] == "Đang thực hiện":
            at_running += 1
        st = area_stats.setdefault(t["area"], {"area": t["area"], "cnt": 0, "dur": 0.0, "wait": 0.0, "ld": 0.0, "em": 0.0, "dist": 0.0})
        st["cnt"] += 1
        st["dur"] += d_s
        st["wait"] += w_s
        st["ld"] += t["loaded_m"]
        st["em"] += t["empty_m"]
        st["dist"] += t["total_m"]

        agv_task_rows.append([
            t["task_code"], t["area"], t["task_type"], t["agv_code"],
            f"Tầng {t['floor']}", t["station"],
            f"Dock {t['dock_id']:02d}" if t["dock_id"] is not None else "-",
            t["pallet_or_item"] or "-",
            t.get("pick_location") or "-", t.get("drop_location") or "-",
            _excel_days(t["start_s"]), _excel_days(t["end_s"]),
            _excel_days(t["duration_s"]), _excel_days(w_s),
            round(t["loaded_m"], 2), round(t["empty_m"], 2), round(t["total_m"], 2),
            t.get("fault_count", 0), t["status"],
        ])

    n_at = len(agv_task_rows)
    loaded_dist_pct = _pct(tot_at_ld, tot_at_dist)
    _write_top_dashboard_headers(
        ws6, row,
        "1. TỔNG HỢP NHẬT KÝ NHIỆM VỤ AGV TOÀN KHO",
        "BIỂU ĐỒ TẦN SUẤT THEO GIỜ & TỈ TRỌNG NHIỆM VỤ AGV (TỐI ĐA 100%)",
    )
    row += 1
    sec1_tbl_start = row
    log_kpis = [
        ("Nhật ký", "Tổng số nhiệm vụ AGV ghi nhận", n_at, "#,##0", "tổng số lượt trên nhật ký"),
        ("Trạng thái", "Số nhiệm vụ đã hoàn thành", at_done, "#,##0", "lượt hoàn thành"),
        ("Trạng thái", "Số nhiệm vụ đang chạy khi dừng", at_running, "#,##0", "lượt dở dang lúc dừng"),
        ("Phân loại", "Nhiệm vụ Khu Dock -> Kho 01", int(summary["agv_dock_tasks"]), "#,##0", "lượt pallet chẵn vào Kho 01"),
        ("Phân loại", "Nhiệm vụ Khu Unpack U1 & U2", int(summary["agv_unpack_tasks"]), "#,##0", "lượt cấp & đổi rack U1/U2"),
        ("Phân loại", "Nhiệm vụ Khu Đóng Gói P1", int(summary["agv_p1_tasks"]), "#,##0", "lượt cấp & đổi trạm P1"),
        ("Thời gian", "Tổng thời gian thực hiện nhiệm vụ", _fmt_vi_hms(tot_at_dur), None, "Cộng dồn từ lúc xuất phát tới khi xong"),
        ("Thời gian", "Tổng thời gian chờ Traffic", _fmt_vi_hms(tot_at_wait), None, "Cộng dồn chờ nhường tuyến"),
        ("Thời gian", "TG chờ Traffic TB / nhiệm vụ (s)", round(tot_at_wait / max(1, n_at), 2), "#,##0.00", "giây / nhiệm vụ"),
        ("Quãng đường", "Tổng quãng đường có tải (Loaded)", round(tot_at_ld, 2), "#,##0.00", "mét có tải"),
        ("Quãng đường", "Tổng quãng đường chạy rỗng (Empty)", round(tot_at_em, 2), "#,##0.00", "mét chạy rỗng"),
        ("Quãng đường", "Tổng quãng đường toàn nhật ký", round(tot_at_dist, 2), "#,##0.00", "mét tổng cộng"),
        ("Hiệu suất", "Tỉ lệ quãng đường chạy có tải", loaded_dist_pct, '0.00"%"', "% có tải trên tổng quãng đường"),
        ("An toàn", "Tổng số lỗi phát sinh trong nhật ký", tot_at_faults, "#,##0", "lỗi nhiệm vụ AGV"),
    ]
    row = _write_kpi_cards(
        ws6, row,
        ["Nhóm chỉ tiêu", "Chỉ tiêu thống kê", "Giá trị", "Đơn vị / Diễn giải"],
        log_kpis,
    )
    sec2_row = row

    row = _style_section(ws6, row, 19, "2. TẦN SUẤT NHIỆM VỤ AGV THEO GIỜ & TỈ TRỌNG KHU VỰC (NGUỒN BIỂU ĐỒ ĐẦU TRANG)")
    hourly_rows, col_sums, hr_tot, max_hr, min_hr = _build_hourly_rows(
        tracker.agv_tasks,
        tracker.sim_duration_s,
        ["dock", "unpack", "p1"],
        lambda it: "dock" if it["area"] == "Khu Dock -> Kho 01" else ("p1" if it["area"] == "Khu Đóng Gói P1" else "unpack"),
    )
    ah_hdr = row
    ah_last = ah_hdr + len(hourly_rows)
    row = _write_table(
        ws6, row,
        ["Khung giờ (1x)", "Dock -> Kho 01", "Unpack U1 & U2", "Đóng gói P1", "Tổng nhiệm vụ", "Tỉ trọng (%)", "Đánh giá cao/thấp điểm"],
        hourly_rows,
        col_formats={2: "#,##0", 3: "#,##0", 4: "#,##0", 5: "#,##0", 6: "0.00"},
        right_cols={2, 3, 4, 5, 6},
        center_cols={1, 7},
        total_row=[
            "TỔNG CỘNG THEO GIỜ", col_sums[0], col_sums[1], col_sums[2], hr_tot,
            100.0 if hr_tot > 0 else 0.0, f"Cao nhất: {max_hr} | Thấp nhất: {min_hr} NV/giờ",
        ],
        auto_filter=False,
    )

    area_mix_rows = []
    for k in area_stats:
        st = area_stats[k]
        area_mix_rows.append([
            st["area"], st["cnt"], _pct(st["cnt"], n_at),
            round(st["ld"], 2), round(st["em"], 2), round(st["dist"], 2),
            _excel_days(st["dur"]), _excel_days(st["wait"]),
        ])
    am_hdr = row
    am_last = am_hdr + len(area_mix_rows)
    row = _write_table(
        ws6, row,
        [
            "Khu vực AGV", "Số nhiệm vụ", "Tỉ trọng (%)",
            "Có tải (m)", "Rỗng (m)", "Tổng quãng đường (m)", "Tổng TG thực hiện", "Tổng TG chờ Traffic",
        ],
        area_mix_rows,
        col_formats={2: "#,##0", 3: "0.00", 4: "#,##0.00", 5: "#,##0.00", 6: "#,##0.00", 7: FMT_HMS_TENTH, 8: FMT_HMS_TENTH},
        right_cols=set(range(2, 9)),
        center_cols={1},
        total_row=[
            "TỔNG CỘNG", n_at, 100.0 if n_at > 0 else 0.0,
            round(tot_at_ld, 2), round(tot_at_em, 2), round(tot_at_dist, 2),
            _excel_days(tot_at_dur), _excel_days(tot_at_wait),
        ],
        auto_filter=False,
    )

    ws6.add_chart(_make_bar_chart(
        ws6, "Tần suất nhiệm vụ AGV theo giờ (1x)", "Khung giờ (1x)", "Số nhiệm vụ",
        21.4, 11.0, 2, 5, ah_hdr, ah_last,
    ), f"E{sec1_tbl_start}")
    ws6.add_chart(_make_pie_chart(
        ws6, "Tỉ trọng nhiệm vụ AGV theo khu vực (Tổng 100%)", 14.2, 11.0, 3, am_hdr, am_last,
    ), f"H{sec1_tbl_start}")

    row = _style_section(ws6, row, 19, "3. NHẬT KÝ CHI TIẾT TOÀN BỘ MÃ NHIỆM VỤ AGV (19 CỘT)")
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
    _finalize_dashboard_sheet(
        ws6, 19,
        min_span_heights=[(sec1_tbl_start, sec2_row - 1, 11.8)],
    )


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
    _build_sheet_overview(wb, tracker, summary, extra, speed_x, sim_dur)
    _build_asrs_sheet(wb, tracker, summary)
    _build_agv_sheet(wb, tracker, summary, tf, engine_ticks)
    _build_dock_sheet(wb, tracker, summary, sim_dur)
    _build_workers_sheet(wb, tracker, summary, sim_dur)
    _build_agv_log_sheet(wb, tracker, summary)

    for target in (timestamped_path, latest_path):
        tmp_path = out_dir / f".{target.name}.{os.getpid()}.tmp"
        wb.save(tmp_path)
        os.replace(tmp_path, target)
    return {
        "timestamped": str(timestamped_path),
        "latest": str(latest_path),
    }
