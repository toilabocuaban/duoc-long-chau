"""Deterministic kinematic process demonstrator. No collision/dynamics claims."""
import argparse
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import heapq
import json
import math
import signal
import time
from pathlib import Path
try:
    from gz.transport13 import Node
    from gz.msgs10.pose_v_pb2 import Pose_V
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.scene_pb2 import Scene
    from gz.msgs10.empty_pb2 import Empty
except ImportError:
    Node = Pose_V = Boolean = Scene = Empty = None
from agv_motion import PARKS, OFFSET, pickup_position, assembly, samples, check_pose, route_to_pickup, route_to_gate, route_home, PICKUP_LEGS_FROM_LANE
from rail_geometry import deck_name, wheel_name, supported
from cad_navigation import LAYOUT
from motion_scheduler import Scheduler
from motion_profile import SCurve, bounds, intersects, blend
from agv_motion import wrap
from agv_traffic import Plan,compatible
import loose_goods as LG
import truck_scene
from sdf_parts import HIDDEN_Z, HIDDEN_BELOW
import agv_network as AN
from dock_flow import DockFlow
from report_exporter import SimulationTracker, export_excel

ROOT=Path(__file__).resolve().parent
C=json.loads((ROOT/'config.json').read_text())
INBOUND_PICKUP_SLOTS=[tuple(slot) for slot in C.get('inbound_pickup_slots', [])]
TURNTABLE_S=4.0   # ASSUMPTION: 90-degree infeed turntable cycle (s); not specified in the CAD
def _unpack_spots():
    """Unpack pallet spots of the 4-floor building, grouped per 4-way output port (nearest gate)."""
    ff=json.loads((ROOT/'four_floor_layout.json').read_text(encoding='utf-8'))
    spots=[((s[0]+s[2])/2,(s[1]+s[3])/2) for s in ff['pallet_spots'] if (s[1]+s[3])/2>0]
    out=LAYOUT.data['ports']['out']
    groups=[[] for _ in out]
    for z in ff['unpack_zones']:   # each unpack zone serves the port whose gate is nearest the zone centre
        k=min(range(len(out)),key=lambda k:abs(out[k]['gate'][1]-(z[1]+z[3])/2))
        groups[k]+=[sp for sp in spots if z[0]<=sp[0]<=z[2] and z[1]<=sp[1]<=z[3]]
    if not all(groups):raise ValueError(f'Each output port needs an unpack zone with pallet spots: {groups}')
    return [sorted(g,key=lambda sp:-sp[1]) for g in groups]
UNPACK_SPOTS=_unpack_spots();unpack_count={};unpack_occupant={}
U1_PER_RACK=max(1,int(C.get('u1_cartons_per_rack',8)))
TROLLEY_CAP=max(1,int(C.get('trolley_capacity',LG.TROLLEY_CAPACITY)))
U1_LOCKS={(f,b):asyncio.Lock() for f in LG.floors() for b in range(len(LG.BRANCHES))}
U2_STAGING_CAP=max(1,int(C.get('u2_staging_capacity',3)))
Z05_SPOT_LOCKS={(f,z_idx,s_idx):asyncio.Lock() for f in LG.floors() for z_idx in range(len(UNPACK_SPOTS)) for s_idx in range(len(UNPACK_SPOTS[z_idx]))}
Z05_WORKER_LOCKS={(f,w_idx):asyncio.Lock() for f in LG.floors() for w_idx in range(len(LG.Z05_WORKERS))}
OUT_MAIN_INFLIGHT={0:0,1:0}
Z05_COMPLETE_TASKS=[]
Z05_PARTIAL_EVERY=max(0,int(C.get('z05_partial_return_every',3)))
Z05_PARTIAL_CARTONS=max(1,int(C.get('z05_partial_unpack_cartons',12)))
U2_PER_RACK=max(1,int(C.get('u2_cartons_per_rack',8)))
U2_LOCKS={(f,u2_idx):asyncio.Lock() for f in LG.floors() for u2_idx in range(len(LG.U2_WORKERS))}
U2_DELIVERY_LOCKS={(f,u2_idx):asyncio.Lock() for f in LG.floors() for u2_idx in range(len(LG.U2_WORKERS))}
P1_PER_RACK=max(1,int(C.get('p1_cartons_per_rack',8)))
P1_LOCKS={(f,st_idx):asyncio.Lock() for f in LG.floors() for st_idx in range(len(LG.P1_STATIONS))}
P1_ROW_SEMAPHORES={(f,row):asyncio.Semaphore(4) for f in LG.floors() for row in (0,1)}
LOOSE_CARTON_POOL=max(180,int(C.get('loose_carton_pool',0)),int(C.get('loose_pallets',0))*int(C.get('loose_cartons_per_pallet',0)))
Z05_CARTON_POOL=max(80,int(C.get('z05_carton_pool',0)),len(LG.Z05_WORKERS)*int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))*2)
FREE_RET_DECKS=asyncio.Queue()
FREE_CARTONS=asyncio.Queue()
FREE_Z05_CARTONS={f:asyncio.Queue() for f in LG.floors()}
p1_running=False
_EVENTS_FP=None

def _init_u1_state():
    state={}
    for f in LG.floors():
        for b in range(len(LG.BRANCHES)):
            cells=[dict(c) for c in LG.U1_BRANCH_CELLS[b]]
            state[(f,b)]={
                'active_rack':f'u1_rack_{f}_{b}_0',
                'active_rack_idx':0,
                'empty_cell':cells[0],
                'spares':[(f'u1_rack_{f}_{b}_{r}',r,cells[r],[]) for r in range(1,LG.U1_RACKS_PER_BRANCH)],
                'cartons':[],
                'swaps':0,
                'active_trolley':f'u1_trolley_{f}_{b}',
                'spare_trolley':f'k06_trolley_{f}_{b%4}',
                'k06_slot':b%4,
                'trolley_cartons':[],
                'trolley_swaps':0,
            }
    return state

def _init_u2_state():
    state={}
    for f in LG.floors():
        for u2_idx in range(len(LG.U2_WORKERS)):
            cells=[dict(c) for c in LG.U2_STATION_CELLS[u2_idx]]
            state[(f,u2_idx)]={
                'active_rack':f'u2_rack_{f}_{u2_idx}_0',
                'active_rack_idx':0,
                'empty_cell':cells[0],
                'spares':[(f'u2_rack_{f}_{u2_idx}_{r}',r,cells[r],[]) for r in range(1,LG.U2_RACKS_PER_STATION)],
                'cartons':[],
                'swaps':0,
                'active_trolley':f'u2_trolley_{f}_{u2_idx}',
                'spare_trolley':f'k06_trolley_{f}_{u2_idx%4}',
                'k06_slot':u2_idx%4,
                'trolley_cartons':[],
                'trolley_swaps':0,
            }
    return state

def _init_p1_state():
    state={}
    for f in LG.floors():
        for st_idx in range(len(LG.P1_STATIONS)):
            cells=[dict(c) for c in LG.P1_STATION_CELLS[st_idx]]
            state[(f,st_idx)]={
                'active_rack':f'p1_rack_{f}_{st_idx}_0',
                'active_rack_idx':0,
                'empty_cell':cells[0],
                'spares':[(f'p1_rack_{f}_{st_idx}_1',1,cells[1])],
                'reserve_shells':[(f'p1_rack_{f}_{st_idx}_{r}',r) for r in range(LG.P1_RACKS_PER_STATION,3)],
                'active_trolley':f'p1_trolley_{f}_{st_idx}',
                'spare_trolley':f'k06_trolley_{f}_{4+(st_idx%4)}',
                'k06_slot':4+(st_idx%4),
                'trolley_remaining':[f'p1_carton_{f}_{st_idx}_{c}' for c in range(TROLLEY_CAP-1,-1,-1)],
                'rack_remaining':list(range(P1_PER_RACK)),
                'cartons_packed':0,
                'rack_swaps':0,
                'trolley_swaps':0,
            }
    return state

K04_STOCK_POOL={f:[(f'k04_stock_{f}_{i}',dict(c)) for i,c in enumerate(LG.FF.K04_MOVABLE_STOCK_CELLS)] for f in LG.floors()}

def _init_z05_floor_state():
    state={}
    for f in LG.floors():
        w_poses={}
        for w_idx in range(len(LG.Z05_WORKERS)):
            sx,sy,syaw,_=LG.z05_station(f,w_idx)
            w_poses[w_idx]={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
        state[f]={
            'spots':{(z,s):None for z in range(len(UNPACK_SPOTS)) for s in range(len(UNPACK_SPOTS[z]))},
            'deck_in_use':{z:False for z in range(len(UNPACK_SPOTS))},
            'next_u2':0,
            'u2_queued':{u2_idx:0 for u2_idx in range(len(LG.U2_WORKERS))},
            'u2_active_task':{u2_idx:None for u2_idx in range(len(LG.U2_WORKERS))},
            'u2_reserved':{u2_idx:False for u2_idx in range(len(LG.U2_WORKERS))},
            'u2_cond':None,
            'worker_tasks':{w_idx:None for w_idx in range(len(LG.Z05_WORKERS))},
            'worker_poses':w_poses,
        }
    return state

U1_STATE=_init_u1_state()
U2_STATE=_init_u2_state()
P1_STATE=_init_p1_state()
Z05_FLOOR_STATE=_init_z05_floor_state()
Z05_SPOT_BUSY={(f,z,s):False for f in LG.floors() for z in range(len(UNPACK_SPOTS)) for s in range(len(UNPACK_SPOTS[z]))}
Z05_SPOT_COND=None
Z05_NEXT_FLOOR={z:(2*z)%max(1,len(LG.floors())) for z in range(len(UNPACK_SPOTS))}
Z05_JACK_PENDING={(f,w):0 for f in LG.floors() for w in range(len(LG.Z05_WORKERS))}
Z05_JACK_COND=None

def _z05_jack_cond():
    global Z05_JACK_COND
    if Z05_JACK_COND is None:
        Z05_JACK_COND=asyncio.Condition()
    return Z05_JACK_COND

def _wake_z05_jack():
    if Z05_JACK_COND is None:return
    async def _wakeup():
        async with Z05_JACK_COND:
            Z05_JACK_COND.notify_all()
    try:asyncio.get_running_loop().create_task(_wakeup())
    except RuntimeError:pass

def _z05_cond():
    global Z05_SPOT_COND
    if Z05_SPOT_COND is None:
        Z05_SPOT_COND=asyncio.Condition()
    return Z05_SPOT_COND

async def _claim_z05_spot(worker):
    floors=LG.floors();n_f=len(floors);n_s=len(UNPACK_SPOTS[worker])
    async with _z05_cond():
        while not STOP_REQUESTED:
            start_f=Z05_NEXT_FLOOR[worker]
            chosen=None
            # Prefer a floor whose zone is idle AND outfeed roller slots are free before falling back.
            for pass_mode in (0,1,2):
                for df in range(n_f):
                    f=floors[(start_f+df)%n_f]
                    fl0=f-1
                    gate_busy=out_corner_locks[(worker,fl0)].locked() or out_gate_locks[(worker,fl0)].locked()
                    if pass_mode==0 and (gate_busy or any(Z05_SPOT_BUSY[(f,worker,sx)] for sx in range(n_s))):
                        continue
                    if pass_mode==1 and gate_busy:
                        continue
                    for sx in range(n_s):
                        if not Z05_SPOT_BUSY[(f,worker,sx)]:
                            chosen=(f,sx);break
                    if chosen is not None:break
                if chosen is not None:break
            if chosen is not None:
                f,sx=chosen
                Z05_SPOT_BUSY[(f,worker,sx)]=True
                Z05_NEXT_FLOOR[worker]=(floors.index(f)+1)%n_f
                return f,sx
            await _z05_cond().wait()
        return None

async def _release_z05_spot(floor,zone,spot_idx):
    async with _z05_cond():
        key=(floor,zone,spot_idx)
        if key in Z05_SPOT_BUSY:Z05_SPOT_BUSY[key]=False
        _z05_cond().notify_all()

def _wake_z05_spots():
    if Z05_SPOT_COND is None:return
    async def _wakeup():
        async with Z05_SPOT_COND:
            Z05_SPOT_COND.notify_all()
    try:asyncio.get_running_loop().create_task(_wakeup())
    except RuntimeError:pass

def _u2_floor_cond(floor):
    fst=Z05_FLOOR_STATE[floor]
    if fst['u2_cond'] is None:
        fst['u2_cond']=asyncio.Condition()
    return fst['u2_cond']

def _wake_z05_u2():
    for fst in Z05_FLOOR_STATE.values():
        cond=fst.get('u2_cond')
        if cond is None:continue
        async def _wakeup(c=cond):
            async with c:
                c.notify_all()
        try:asyncio.get_running_loop().create_task(_wakeup())
        except RuntimeError:pass

SHUTTLE_CONT_S=float(C.get('shuttle_continuous_hours',7.0) or 7.0)*3600.0
SHUTTLE_CHARGE_S=float(C.get('shuttle_charge_hours',2.0) or 2.0)*3600.0
CHARGE_THRESHOLD_PCT=float(C.get('charge_threshold_pct',20.0) or 20.0)
SHUTTLE_LIFT_XY=(51.80108,44.75)
SHUTTLE_CHARGE_BAYS=((51.80108,36.75),(51.80108,28.75))
SHUTTLE_LIFT_LOCK=asyncio.Lock()
SHUTTLE_LOCKS={f'shuttle_{i}':asyncio.Lock() for i in range(10)}
_SHUTTLE_BUSY_OWNERS={}
_SHUTTLE_LEVEL_ACTIVE={lvl:f'shuttle_{lvl}' for lvl in range(9)}
_SHUTTLE_STANDBY='shuttle_9'
_SHUTTLE_LEVEL_OF={**{f'shuttle_{i}':i for i in range(9)},'shuttle_9':0}
_SHUTTLE_DUTY_START={f'shuttle_{i}':0.0 for i in range(9)}
_SHUTTLE_DUTY_START['shuttle_9']=None
_SHUTTLE_CHARGE_UNTIL={}

async def _move_shuttle_via_lift(s,from_lvl,to_lvl):
    """Drive shuttle s into the rail-locked shuttle transfer lift cabin, carry it vertically, and exit at to_lvl."""
    z_from=C['storage_levels'][from_lvl]
    z_to=C['storage_levels'][to_lvl]
    async with SHUTTLE_LIFT_LOCK:
        cur_cab_z=(poses.get('shuttle_lift_cabin') or (SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_from))[2]
        if abs(cur_cab_z-z_from)>0.05:
            await engine.move({'shuttle_lift_cabin':(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_from)},max(C['lift_speed'],1.2))
        await shuttle_async(s,SHUTTLE_LIFT_XY,z_from,speed=max(C['shuttle_speed'],2.5))
        if abs(z_to-z_from)>0.05:
            targets={
                'shuttle_lift_cabin':(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_to),
                s:(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_to),
                deck_name(s):(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_to+deck_offsets.get(s,-0.06)),
                f'shuttle_wheels_x_{s.split("_")[1]}':(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_to+(.04 if wheel_modes[s]=='y' else 0.)),
                f'shuttle_wheels_y_{s.split("_")[1]}':(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],z_to+(0. if wheel_modes[s]=='y' else .04)),
            }
            await engine.move(targets,C['lift_speed'])
        _SHUTTLE_LEVEL_OF[s]=to_lvl

async def _ensure_level_shuttle(level):
    """Return active shuttle ID for level (0..8). When battery <= 20% or continuous duty reached, send to outer
    technical-aisle charger and bring standby/charged shuttle via the rail-locked shuttle transfer lift."""
    global _SHUTTLE_STANDBY
    t_now=scheduler.time
    # Complete any finished shuttle charges
    for sid in list(_SHUTTLE_CHARGE_UNTIL.keys()):
        if t_now>=_SHUTTLE_CHARGE_UNTIL[sid]:
            _SHUTTLE_CHARGE_UNTIL.pop(sid,None)
            _SHUTTLE_DUTY_START[sid]=t_now
            if tracker is not None:
                tracker.finish_shuttle_charge(sid,t_now)
            event(-1,'SHUTTLE_CHARGING_COMPLETE',shuttle=sid)
    s_cur=_SHUTTLE_LEVEL_ACTIVE[level]
    z=C['storage_levels'][level]
    t_duty=_SHUTTLE_DUTY_START.get(s_cur)
    bat_pct=tracker.get_shuttle_battery_pct(s_cur,t_now) if tracker is not None else 100.0
    needs_chg=(bat_pct<=CHARGE_THRESHOLD_PCT) or (SHUTTLE_CONT_S>0 and t_duty is not None and (t_now-t_duty)>=SHUTTLE_CONT_S)
    if needs_chg and _SHUTTLE_STANDBY and _SHUTTLE_STANDBY not in _SHUTTLE_CHARGE_UNTIL and not SHUTTLE_LOCKS[_SHUTTLE_STANDBY].locked() and _SHUTTLE_STANDBY not in _SHUTTLE_BUSY_OWNERS:
        rep=_SHUTTLE_STANDBY
        await SHUTTLE_LOCKS[rep].acquire()
        _SHUTTLE_BUSY_OWNERS[rep]='swap'
        try:
            bay_idx=0 if level%2==0 else 1
            charge_pt=SHUTTLE_CHARGE_BAYS[bay_idx]
            await shuttle_async(s_cur,charge_pt,z)
            _SHUTTLE_CHARGE_UNTIL[s_cur]=t_now+SHUTTLE_CHARGE_S
            _SHUTTLE_DUTY_START[s_cur]=None
            if tracker is not None:
                tracker.start_shuttle_charge(s_cur,t_now,t_now+SHUTTLE_CHARGE_S)
            rep_lvl=_SHUTTLE_LEVEL_OF.get(rep,0)
            if rep_lvl!=level:
                await _move_shuttle_via_lift(rep,rep_lvl,level)
            _SHUTTLE_LEVEL_ACTIVE[level]=rep
            _SHUTTLE_DUTY_START[rep]=t_now
            _SHUTTLE_STANDBY=s_cur
            if tracker is not None and rep in tracker.shuttles:
                tracker.shuttles[rep]['status']='Hoạt động bình thường'
            event(-1,'SHUTTLE_CHARGING_SWAP',charging=s_cur,replacement=rep,level=level+1,battery_pct=bat_pct,charge_until_s=round(t_now+SHUTTLE_CHARGE_S,1))
            return rep,True
        except BaseException:
            if _SHUTTLE_BUSY_OWNERS.get(rep)=='swap':
                _SHUTTLE_BUSY_OWNERS.pop(rep,None)
            SHUTTLE_LOCKS[rep].release()
            raise
    return s_cur,False

_LEVEL_COND=None
_INBOUND_SH_WAITERS=0
_LEVEL_INBOUND_WAITERS={lvl:0 for lvl in range(9)}

def _level_cond():
    global _LEVEL_COND
    if _LEVEL_COND is None:_LEVEL_COND=asyncio.Condition()
    return _LEVEL_COND

class ShuttleLease:
    """Explicit single-owner shuttle lease safe to hand off between tasks (e.g. prepare -> downstream)."""
    def __init__(self,level,sid,held_level_lock,acq_info='',release_token=None):
        self.level=level
        self.sid=sid
        self.held_level_lock=held_level_lock
        self.acq_info=acq_info
        self._token=release_token
        self.released=False
    async def release(self,token=None):
        if self.released:
            return
        if self._token is not None and token is not self._token:
            return
        self.released=True
        cond=_level_cond()
        if self.sid and _SHUTTLE_BUSY_OWNERS.get(self.sid) is self:
            _SHUTTLE_BUSY_OWNERS.pop(self.sid,None)
            if SHUTTLE_LOCKS[self.sid].locked():
                SHUTTLE_LOCKS[self.sid].release()
            if self.held_level_lock and shuttles[self.level].locked():
                shuttles[self.level].release()
        async with cond:
            cond.notify_all()

async def acquire_shuttle_lease(level,priority='normal',release_token=None):
    """Acquire an exclusive ShuttleLease for `level` (inbound-first priority per floor) or roving standby."""
    global _INBOUND_SH_WAITERS
    cond=_level_cond()
    is_high=(priority=='high')
    acquired_sid=None
    held_level_lock=False
    async with cond:
        if is_high:
            _INBOUND_SH_WAITERS+=1
            _LEVEL_INBOUND_WAITERS[level]=_LEVEL_INBOUND_WAITERS.get(level,0)+1
        try:
            waited_for_primary=False
            while True:
                pri_sid=_SHUTTLE_LEVEL_ACTIVE[level]
                # Outbound yields whenever this floor has an inbound pallet waiting at the lift/buffer.
                if not shuttles[level].locked() and not SHUTTLE_LOCKS[pri_sid].locked() and pri_sid not in _SHUTTLE_BUSY_OWNERS and (is_high or _LEVEL_INBOUND_WAITERS.get(level,0)==0):
                    await shuttles[level].acquire()
                    await SHUTTLE_LOCKS[pri_sid].acquire()
                    held_level_lock=True
                    acquired_sid=pri_sid
                    _SHUTTLE_BUSY_OWNERS[pri_sid]=asyncio.current_task().get_name() if asyncio.current_task() else 'main'
                    break
                sb=_SHUTTLE_STANDBY
                sb_on_same_lvl=(_SHUTTLE_LEVEL_OF.get(sb,0)==level) if sb else False
                sb_ok_for_pri=is_high or (_INBOUND_SH_WAITERS==0 and in_queue.empty())
                # If standby is on another floor, give the floor's own primary shuttle 3s to finish before calling the lift.
                if sb and sb not in _SHUTTLE_CHARGE_UNTIL and not SHUTTLE_LOCKS[sb].locked() and sb not in _SHUTTLE_BUSY_OWNERS and sb_ok_for_pri:
                    sb_bat=tracker.get_shuttle_battery_pct(sb,scheduler.time) if tracker is not None else 100.0
                    if sb_bat>CHARGE_THRESHOLD_PCT and (sb_on_same_lvl or waited_for_primary):
                        await SHUTTLE_LOCKS[sb].acquire()
                        held_level_lock=False
                        acquired_sid=sb
                        _SHUTTLE_BUSY_OWNERS[sb]=asyncio.current_task().get_name() if asyncio.current_task() else 'main'
                        break
                if shuttles[level].locked() and not waited_for_primary and sb and not sb_on_same_lvl:
                    try:
                        await asyncio.wait_for(cond.wait(),timeout=3.0)
                    except asyncio.TimeoutError:
                        waited_for_primary=True
                else:
                    await cond.wait()
        finally:
            if is_high:
                _INBOUND_SH_WAITERS-=1
                _LEVEL_INBOUND_WAITERS[level]=max(0,_LEVEL_INBOUND_WAITERS.get(level,0)-1)
    acq_task_name=asyncio.current_task().get_name() if asyncio.current_task() else 'main'
    lease=ShuttleLease(level,acquired_sid,held_level_lock,acq_info=f'{scheduler.time:.2f}:{acq_task_name}',release_token=release_token)
    _SHUTTLE_BUSY_OWNERS[acquired_sid]=lease
    try:
        if not held_level_lock:
            sb_lvl=_SHUTTLE_LEVEL_OF.get(lease.sid,0)
            if sb_lvl!=level:
                await _move_shuttle_via_lift(lease.sid,sb_lvl,level)
            if tracker is not None and lease.sid in tracker.shuttles:
                tracker.shuttles[lease.sid]['status']=f'Hỗ trợ Tầng {level+1} (Active Standby)'
        else:
            s_out,swapped=await _ensure_level_shuttle(level)
            if swapped:
                if _SHUTTLE_BUSY_OWNERS.get(lease.sid) is lease:
                    _SHUTTLE_BUSY_OWNERS.pop(lease.sid,None)
                SHUTTLE_LOCKS[lease.sid].release()
                lease.sid=s_out
                _SHUTTLE_BUSY_OWNERS[s_out]=lease
        return lease
    except BaseException:
        await lease.release(release_token)
        raise

@asynccontextmanager
async def acquire_level_shuttle(level,priority='normal'):
    """Async context manager wrapper around ShuttleLease for single-task outbound/return callers."""
    tok=object()
    lease=await acquire_shuttle_lease(level,priority=priority,release_token=tok)
    try:
        yield lease.sid
    finally:
        await lease.release(tok)

AGV_CONT_H_MIN=float(C.get('agv_continuous_hours_min',6.0) or 6.0)
AGV_CONT_H_MAX=float(C.get('agv_continuous_hours_max',8.0) or 8.0)
AGV_CHARGE_H_MIN=float(C.get('agv_charge_hours_min',1.5) or 1.5)
AGV_CHARGE_H_MAX=float(C.get('agv_charge_hours_max',2.0) or 2.0)
FORKLIFT_CONT_H_MIN=float(C.get('agv_forklift_continuous_hours_min',6.0) or 6.0)
FORKLIFT_CONT_H_MAX=float(C.get('agv_forklift_continuous_hours_max',8.0) or 8.0)
FORKLIFT_CHARGE_H_MIN=float(C.get('agv_forklift_charge_hours_min',2.0) or 2.0)
FORKLIFT_CHARGE_H_MAX=float(C.get('agv_forklift_charge_hours_max',3.0) or 3.0)

_AGV_DUTY_START={}
_AGV_CHARGE_UNTIL={}
_AGV_CHARGE_HOURS={}
_ACTIVE_FORKLIFT_JOBS=set()

def _should_yield_to_peer_forklift(lane):
    """Build a natural battery stagger while queue < 2 by letting the lower-battery Forklift take solo work
    until a ~35% reserve margin exists, while immediately launching both Forklifts on queue bursts (>=2)
    or whenever the peer is charging."""
    if len(PARKS)<=1 or in_queue.qsize()>=2:
        return False
    my_id=f'agv_{lane}'
    my_bat=tracker.get_agv_battery_pct(my_id,scheduler.time) if tracker is not None else 100.0
    for k in range(len(PARKS)):
        if k==lane:
            continue
        pid=f'agv_{k}'
        if _is_agv_charging(pid):
            continue
        p_bat=tracker.get_agv_battery_pct(pid,scheduler.time) if tracker is not None else 100.0
        if p_bat<CHARGE_THRESHOLD_PCT:
            continue
        gap=my_bat-p_bat
        if (gap>0.1 and gap<35.0) or (abs(gap)<=0.1 and k<lane):
            return True
    return False

def _agv_battery_profile(agv_id):
    """Return (is_forklift, duty_s, charge_s) staggered cleanly inside configured operating & charging ranges."""
    is_forklift=agv_id.startswith('agv_')
    parts=[int(tok) for tok in agv_id.split('_') if tok.isdigit()]
    seed=sum((idx+1)*val for idx,val in enumerate(parts))
    if is_forklift:
        n_fk=max(1,len(PARKS))
        frac=(0.15+0.70*((seed%n_fk)/max(1,n_fk-1))) if n_fk>1 else 0.5
        duty_h=FORKLIFT_CONT_H_MIN+frac*max(0.0,FORKLIFT_CONT_H_MAX-FORKLIFT_CONT_H_MIN)
        charge_h=FORKLIFT_CHARGE_H_MIN+frac*max(0.0,FORKLIFT_CHARGE_H_MAX-FORKLIFT_CHARGE_H_MIN)
    else:
        frac_d=(seed%5)/4.0
        frac_c=(seed%3)/2.0
        duty_h=AGV_CONT_H_MIN+frac_d*max(0.0,AGV_CONT_H_MAX-AGV_CONT_H_MIN)
        charge_h=AGV_CHARGE_H_MIN+frac_c*max(0.0,AGV_CHARGE_H_MAX-AGV_CHARGE_H_MIN)
    return is_forklift,duty_h*3600.0,charge_h*3600.0

def _is_agv_charging(agv_id):
    until=_AGV_CHARGE_UNTIL.get(agv_id)
    return until is not None and scheduler.time<until

def _check_agv_needs_charge(agv_id,dry_run=False):
    """Trigger AGV charging cycle:
    - AGV Forklifts (agv_0..1): charge ONLY when battery drops strictly below CHARGE_THRESHOLD_PCT (<20%),
      never purely on a shift timer, and stagger mutually so at least 1 Forklift stays active unless <=5% emergency.
    - Standard AGVs (U1/U2/P1): charge when <=CHARGE_THRESHOLD_PCT or continuous duty limit (6-8h) reached."""
    if agv_id in _AGV_CHARGE_UNTIL:
        if scheduler.time+1e-6<_AGV_CHARGE_UNTIL[agv_id]:
            return _AGV_CHARGE_UNTIL[agv_id],_AGV_CHARGE_HOURS.get(agv_id,0.0)*3600.0
        if not dry_run:
            _complete_agv_charge(agv_id)
    t_start=_AGV_DUTY_START.setdefault(agv_id,0.0)
    is_forklift,duty_s,charge_s=_agv_battery_profile(agv_id)
    bat_pct=tracker.get_agv_battery_pct(agv_id,scheduler.time) if tracker is not None else 100.0
    if is_forklift and bat_pct>5.0:
        other_chg=sum(1 for k in range(len(PARKS)) if f'agv_{k}'!=agv_id and _is_agv_charging(f'agv_{k}'))
        if other_chg>=max(1,len(PARKS)-1):
            return None,0.0
    elif agv_id.startswith(('u2_agv_','u1_agv_')) and bat_pct>5.0:
        parts=agv_id.split('_')
        if len(parts)==4:
            peer_pfx=f"{parts[0]}_{parts[1]}_{parts[2]}_"
            other_chg=sum(1 for k in range(2) if f"{peer_pfx}{k}"!=agv_id and _is_agv_charging(f"{peer_pfx}{k}"))
            if other_chg>=1:
                return None,0.0
    needs=(bat_pct<CHARGE_THRESHOLD_PCT) if is_forklift else ((bat_pct<=CHARGE_THRESHOLD_PCT) or (duty_s>0 and (scheduler.time-t_start)>=duty_s))
    if needs:
        t_now=scheduler.time
        t_until=t_now+charge_s
        if dry_run:
            return t_until,charge_s
        charge_h=charge_s/3600.0
        _AGV_CHARGE_UNTIL[agv_id]=t_until
        _AGV_CHARGE_HOURS[agv_id]=charge_h
        if tracker is not None:
            tracker.start_agv_charge(agv_id,t_now,t_until,is_forklift=is_forklift)
        event(
            -1,'AGV_CHARGING_START',
            agv=agv_id,
            agv_type='forklift' if is_forklift else 'standard',
            battery_pct=round(bat_pct,2),
            duty_h=round(duty_s/3600.0,2),
            charge_h=round(charge_h,2),
            charge_until_s=round(t_until,1),
        )
        return t_until,charge_s
    return None,0.0

def _complete_agv_charge(agv_id):
    t_until=_AGV_CHARGE_UNTIL.pop(agv_id,None)
    charge_h=_AGV_CHARGE_HOURS.pop(agv_id,0.0)
    if t_until is not None:
        _AGV_DUTY_START[agv_id]=scheduler.time
        if tracker is not None:
            tracker.finish_agv_charge(agv_id,scheduler.time)
        event(-1,'AGV_CHARGING_COMPLETE',agv=agv_id,charge_h=round(charge_h,2))

odd_pallet_seq=0

def inbound_pickup_position(job):
    if not INBOUND_PICKUP_SLOTS:
        return pickup_position(job)
    return INBOUND_PICKUP_SLOTS[job % len(INBOUND_PICKUP_SLOTS)]

def inbound_classification(job):
    """Return the simulated barcode result for an inbound load."""
    seq=C.get('inbound_type_sequence',['even','odd']) or ['even']
    return str(seq[job % len(seq)]).lower()
poses={}
last_tx_poses={}
pending_tx={}
in_engine_loop=False
deck_offsets={f'shuttle_{i}':-.06 for i in range(10)}
wheel_modes={f'shuttle_{i}':'y' for i in range(10)}
rail_checks=0
events=deque(maxlen=2000)
event_count=0
scheduler=Scheduler()
parser=argparse.ArgumentParser()
parser.add_argument('--fast',action='store_true',help='Accelerated integration verification')
parser.add_argument('--no-gz',action='store_true',help='Skip Gazebo RPC transport (pure kinematics verification when used with --fast)')
parser.add_argument('--continuous',action='store_true',help='Keep continuous_mode active when running with --fast')
parser.add_argument('--step-dt',type=float,default=None,help='Override physics timestep in --fast mode (e.g. 0.1)')
parser.add_argument('--jobs',type=int,default=None)
parser.add_argument('--duration',type=float,default=None,help='Simulated cycle duration limit in seconds (0 = finish all jobs or run continuously)')
args=parser.parse_args()
node=None if (args.no_gz or Node is None) else Node()
CYCLE_DURATION_S=(0.0 if ((args.fast and not args.continuous) or args.jobs is not None) else float(C.get('cycle_duration_s',0))) if args.duration is None else float(args.duration)
CONTINUOUS_MODE=bool(C.get('continuous_mode',False)) and (args.jobs is None) and (args.continuous or not args.fast)
STOP_REQUESTED=False
WALL_START=time.monotonic()
FREE_EVEN_PALLETS=asyncio.Queue()
JOB_PALLET_MODEL={}
JOB_BAY_IDX={}
JOB_EVEN_SEQ={}
JOB_IN_QUEUE_T={}
REQ_QUEUE_T={}
MAX_STORED_INFLIGHT=int(C.get('max_stored_inflight',65))
INBOUND_TARGET=max(0,int(C.get('warehouse_inbound_target',0)))
OUTBOUND_TARGET=max(0,int(C.get('warehouse_outbound_target',0)))
INBOUND_EVEN_INTERVAL_S=max(0.0,float(C.get('inbound_even_interval_s',0.0)))
tracker=SimulationTracker(C,total_jobs=(0 if CONTINUOUS_MODE else (args.jobs if args.jobs is not None else C['jobs'])),cycle_duration_s=CYCLE_DURATION_S,continuous_mode=CONTINUOUS_MODE)

def _pose_changed(old,new):
    if old is None or len(old)!=len(new):return True
    for k in range(min(3,len(old))):
        if abs(old[k]-new[k])>1e-3:return True
    for k in range(3,len(old)):
        if abs(old[k]-new[k])>2e-3:return True
    return False

# Gazebo's set_pose_vector resolves a pose given by *name* with a scan over every model; with ~2,300 models and
# thousands of poses per second that scan saturates the server loop and the scene freezes. Poses are therefore
# addressed by entity id (looked up once from scene/info); names remain the fallback.
ENTITY_IDS={}
def _load_entity_ids():
    ok,scene=node.request('/world/warehouse/scene/info',Empty(),Empty,Scene,30000)
    if ok:ENTITY_IDS.update({m.name:m.id for m in scene.model})

def _flush_tx(batch):
    if STOP_REQUESTED or not batch:return
    if not ENTITY_IDS:_load_entity_ids()
    req=Pose_V()
    for name,xyz in batch.items():
        if not _pose_changed(last_tx_poses.get(name),xyz):continue
        p=req.pose.add();eid=ENTITY_IDS.get(name)
        if eid:p.id=eid
        else:p.name=name
        p.position.x,p.position.y,p.position.z=xyz[:3]
        if xyz[2]<HIDDEN_BELOW:p.position.z=HIDDEN_Z   # hidden pool object: park beyond the camera far clip
        if len(xyz)>=6:   # (x, y, z, roll, pitch, yaw): animated operator parts
            r,pi_,yw=xyz[3],xyz[4],xyz[5];cr,sr,cp,sp,cy_,sy_=math.cos(r/2),math.sin(r/2),math.cos(pi_/2),math.sin(pi_/2),math.cos(yw/2),math.sin(yw/2)
            p.orientation.w=cr*cp*cy_+sr*sp*sy_;p.orientation.x=sr*cp*cy_-cr*sp*sy_
            p.orientation.y=cr*sp*cy_+sr*cp*sy_;p.orientation.z=cr*cp*sy_-sr*sp*cy_
        else:
            yaw=xyz[3] if len(xyz)>3 else 0
            p.orientation.z=math.sin(yaw/2);p.orientation.w=math.cos(yaw/2)
        last_tx_poses[name]=tuple(xyz)
    if not req.pose:return
    for attempt in range(2):
        if STOP_REQUESTED:return
        ok,reply=node.request('/world/warehouse/set_pose_vector',req,Pose_V,Boolean,1200)
        if ok and reply.data: break
        if ok: raise RuntimeError('Gazebo rejected pose update')
        if STOP_REQUESTED:return
        time.sleep(.2)
    else: raise RuntimeError('Gazebo pose service unavailable after bounded retries')

def send(changes,transmit=True,immediate=False):
    global rail_checks
    changes=dict(changes)
    for name,xyz in list(changes.items()):
        if name=='shuttle_lift_cabin':
            changes['shuttle_lift_cw']=(xyz[0]+1.45,xyz[1]+.75,min(17.3,max(.7,17.6-xyz[2])))
        elif name.startswith(('in_lift_','out_lift_')):
            changes[name.replace('_lift_','_counterweight_')]=(xyz[0]+1.45,xyz[1]+.75,min(17.3,max(.7,17.6-xyz[2])))
    for name,xyz in list(changes.items()):
        if not (name.startswith('shuttle_') and name[8:].isdigit()): continue
        old=poses.get(name,xyz)
        dx,dy=xyz[0]-old[0],xyz[1]-old[1]
        if abs(dx)>1e-7 and abs(dy)>1e-7: raise RuntimeError('Shuttle cannot move diagonally across rail grid')
        if abs(dx)>1e-7: wheel_modes[name]='x'
        if abs(dy)>1e-7: wheel_modes[name]='y'
        riding=any(math.dist(xyz[:3],changes.get(lift,point)[:3])<1e-5 for lift,point in poses.items() if lift.startswith('in_lift_') or lift=='shuttle_lift_cabin')
        if not riding:
            if not LAYOUT.clear(old[:2],xyz[:2]): raise RuntimeError(f'{name} attempted to cross a CAD frame or factory column')
            if min(abs(xyz[2]-level) for level in C['storage_levels'])>1e-5 or not supported(xyz[0],xyz[1],wheel_modes[name]):
                raise RuntimeError(f'{name} left its rail datum/centreline: {xyz}')
        rail_checks+=1
        changes.setdefault(deck_name(name),tuple(xyz[:2])+(xyz[2]+deck_offsets[name],))
        for axis in ['x','y']:
            changes[wheel_name(name,axis)]=tuple(xyz[:2])+(xyz[2]+(0 if axis==wheel_modes[name] else .04),)
    poses.update({n:tuple(v) for n,v in changes.items()})
    if transmit:
        if in_engine_loop and not immediate:
            pending_tx.update({n:tuple(v) for n,v in changes.items()})
        else:
            _flush_tx(changes)
    else:
        last_tx_poses.update({n:tuple(v) for n,v in changes.items()})

def _write_event(record):
    global _EVENTS_FP
    if _EVENTS_FP is None or _EVENTS_FP.closed:
        _EVENTS_FP=(ROOT/'events.jsonl').open('a',buffering=65536)
    _EVENTS_FP.write(json.dumps(record)+'\n')

def event(job,state,**extra):
    global event_count
    record={'job':job,'state':state,'demo_time_s':round(scheduler.time,5),**extra}
    events.append(record);event_count+=1
    tracker.record_event(record)
    (ROOT/'status.json').write_text(json.dumps(record,indent=2))
    _write_event(record)
    print(f'Pallet {job+1:02d}: {state}',flush=True)

route_checks=0

# Concurrent inbound/outbound process dispatcher.
import asyncio

def _sim_step_params():
    """Return (physics_dt, sub_steps_per_frame, render_frame_s) so 40x playback keeps small physics steps."""
    if args.fast:
        dt = float(args.step_dt) if (getattr(args, 'step_dt', None) and args.step_dt > 0) else (1.0 / 30.0)
        return dt, 1, 0.0
    frame = 1.0 / 20.0
    frame_sim = frame * float(C['speed_multiplier'])
    max_sub = max(0.02, float(C.get('sim_substep_s', 0.1)))
    sub_steps = max(1, math.ceil((frame_sim - 1e-9) / max_sub))
    return frame_sim / sub_steps, sub_steps, frame

class Engine:
    def __init__(self):
        self.motions={};self.motion_owners={};self.running=True;self.max_active=0;self.overlap_ticks=0;self.dual_agv_ticks=0;self.multi_shuttle_ticks=0;self.following_ticks=0;self.total_ticks=0
        self._waiters=[];self._waiter_seq=0
    def wake_waiters(self):
        while self._waiters:
            _,_,w_fut=heapq.heappop(self._waiters)
            if not w_fut.done():w_fut.set_result(None)
    async def move(self,targets,speed=2,duration=None,kind=None,transform=None,shape=None,exact=False):
        targets={n:tuple(v) for n,v in targets.items()}
        t_cur=asyncio.current_task()
        req_tag=t_cur.get_name() if t_cur else 'no-task'
        overlap=set(targets)&set(self.motions)
        if overlap:
            owners={n:self.motion_owners.get(n) for n in sorted(overlap)}
            raise RuntimeError(f'Equipment has two owners: {sorted(overlap)} owners={owners} req={req_tag}')
        starts={n:poses[n]+(0,)*(len(v)-len(poses[n])) for n,v in targets.items()}
        kind=kind or ('lift' if any('_lift_' in n for n in targets) else 'shuttle' if any(n.startswith('shuttle_') for n in targets) else 'conveyor')
        speed=min(speed,C['conveyor_speed']) if kind=='conveyor' else speed
        distance=max(math.dist(starts[n][:3],v[:3]) for n,v in targets.items())
        # 7-segment S-curve; a longer imposed duration stretches it in time, which only lowers v/a/jerk.
        profile=None if exact else SCurve(distance,speed,C[kind+'_acceleration'],C[kind+'_jerk'])
        duration=duration if exact else max(duration or 0,profile.duration)   # exact: scripted animation timing
        future=asyncio.get_running_loop().create_future()
        item={'targets':targets,'starts':starts,'elapsed':0.,'duration':duration,'future':future,'transform':transform,'shape':shape or (profile.shape if profile else blend)}
        for n in targets:
            self.motions[n]=item
            self.motion_owners[n]=req_tag
        await future
    async def run(self):
        # speed_multiplier k is a global time scale: sub-stepping keeps physics/collision dt <= sim_substep_s
        # while flushing poses to Gazebo at 20 FPS so 40x stays accurate without leaping past collisions.
        global in_engine_loop
        in_engine_loop=True
        dt,sub_steps,frame=_sim_step_params()
        next_frame_wall=time.perf_counter()+frame
        try:
            while self.running and not STOP_REQUESTED:
                self.total_ticks+=1
                for task in background_tasks:
                    if task.done() and not task.cancelled() and task.exception() is not None:raise task.exception()
                if self.total_ticks%200==0 and background_tasks:
                    background_tasks[:]=[t for t in background_tasks if not t.done()]
                scheduler.time+=dt
                while self._waiters and self._waiters[0][0]<=scheduler.time+1e-6:
                    _,_,w_fut=heapq.heappop(self._waiters)
                    if not w_fut.done():w_fut.set_result(None)
                changes={};finished=[]
                items={id(v):v for v in self.motions.values()}.values()
                for m in items:
                    m['elapsed']+=dt;t=min(1,m['elapsed']/m['duration'])
                    q=m['shape'](t)
                    changes.update(m['transform'](q) if m['transform'] else {n:tuple(a+(b-a)*q for a,b in zip(m['starts'][n],v)) for n,v in m['targets'].items()})
                    if t>=1:finished.append(m)
                active_agvs=[i for i in range(len(PARKS)) if f'agv_{i}' in changes]
                if len(active_agvs)>=2:
                    for idx_a in range(len(active_agvs)):
                        ia=active_agvs[idx_a]
                        for idx_b in range(idx_a+1,len(active_agvs)):
                            ib=active_agvs[idx_b]
                            a,b=changes[f'agv_{ia}'],changes[f'agv_{ib}']
                            da=a[0]-poses[f'agv_{ia}'][0];db=b[0]-poses[f'agv_{ib}'][0]
                            if abs(a[1]-b[1])<.1 and da*db>1e-10:
                                self.following_ticks+=1
                                break
                    self.dual_agv_ticks+=1
                prospective={**poses,**changes}
                for i in range(len(PARKS)):
                    if f'agv_{i}' in changes:
                        global route_checks
                        payload=agv_load[i];check_pose(i,changes[f'agv_{i}'],prospective,payload,pickups[i]);route_checks+=1
                if sum(n.startswith('shuttle_') and n[8:].isdigit() for n in changes)>1:self.multi_shuttle_ticks+=1
                if changes:send(changes,transmit=not args.fast)
                self.max_active=max(self.max_active,len(set(id(v) for v in self.motions.values())))
                if inbound_active and outbound_active and changes:self.overlap_ticks+=1
                for m in finished:
                    for n in m['targets']:
                        del self.motions[n]
                        self.motion_owners.pop(n,None)
                    if not m['future'].done():m['future'].set_result(None)
                stall_limit=max(20000.0,CYCLE_DURATION_S+1000.0) if CYCLE_DURATION_S>0 else 20000.0
                if not CONTINUOUS_MODE and scheduler.time>stall_limit:raise RuntimeError('Dispatch stalled')
                if args.fast:
                    await asyncio.sleep(0)
                elif self.total_ticks%sub_steps==0:
                    if pending_tx and not STOP_REQUESTED:
                        tx_batch=dict(pending_tx);pending_tx.clear()
                        try:
                            _flush_tx(tx_batch)
                        except RuntimeError as exc:
                            if 'Gazebo pose service unavailable' in str(exc) or STOP_REQUESTED:
                                self.disconnect_reason='Cửa sổ Gazebo đã đóng (ngắt kết nối Gazebo)'
                                self.running=False
                                break
                            raise
                    now_wall=time.perf_counter()
                    sleep_dur=next_frame_wall-now_wall
                    if sleep_dur>0.001:
                        await asyncio.sleep(sleep_dur)
                        next_frame_wall+=frame
                    else:
                        next_frame_wall=max(next_frame_wall+frame,now_wall-frame)
                        await asyncio.sleep(0)
                else:
                    await asyncio.sleep(0)
        finally:
            in_engine_loop=False
            self.wake_waiters()
    async def until(self,when):
        if not self.running or STOP_REQUESTED or scheduler.time+1e-6>=when:return
        fut=asyncio.get_running_loop().create_future()
        self._waiter_seq+=1
        heapq.heappush(self._waiters,(when,self._waiter_seq,fut))
        await fut

N_FORKLIFTS=len(PARKS)
background_tasks=[]
engine=Engine();agv_load=[None]*N_FORKLIFTS;pickups=[None]*N_FORKLIFTS
dock_flow=DockFlow(C,engine,send,poses,args.fast,lambda state,**kw:loose_event(state,**kw))
dock_flow.clock=lambda:scheduler.time
reservations={}
# One pallet per inbound pickup bay: an arrival waits until the AGV has carried the previous load away.
pickup_bays=[asyncio.Semaphore(1) for _ in INBOUND_PICKUP_SLOTS]
pickup_maneuver_lock=asyncio.Lock()
in_ports=[asyncio.Lock() for _ in range(2)];out_ports=[asyncio.Lock() for _ in range(2)]
in_port_waiting=[0,0]
def _pick_inbound_port(preferred):
    p0=preferred%2;p1=1-p0
    if not in_ports[p0].locked() and in_port_waiting[p0]==0:return p0
    if not in_ports[p1].locked() and in_port_waiting[p1]==0:return p1
    if in_port_waiting[p1]<in_port_waiting[p0]:return p1
    if in_ports[p0].locked() and not in_ports[p1].locked():return p1
    return p0
out_buffers={(w,lvl):asyncio.Lock() for w in range(2) for lvl in range(9)}
# 2-slot outfeed roller buffer per floor & branch: corner (near lift) -> gate (near Z05).
out_corner_locks={(w,fl):asyncio.Lock() for w in range(2) for fl in range(4)}
out_gate_locks={(w,fl):asyncio.Lock() for w in range(2) for fl in range(4)}
out_floor_gates=out_gate_locks
shuttles=[asyncio.Lock() for _ in range(9)]
in_queue=asyncio.Queue();stored=asyncio.Queue();out_queue=asyncio.Queue()
inbound_active=0;outbound_active=0
completed=0;stored_count=0;max_pending=0;shuttle_route_log=[] if args.fast else deque(maxlen=2000)

async def drive_async(i,target,height=.02,payload=None,pickup=None):
    start=poses[f'agv_{i}'];target=tuple(start[k] if v is None else v for k,v in enumerate(target))
    target=target[:3]+(start[3]+wrap(target[3]-start[3]),)
    def base(q):return tuple(a+(b-a)*q for a,b in zip(start,target))
    agv_load[i]=payload;pickups[i]=pickup
    travel=SCurve(math.dist(start[:3],target[:3]),C['agv_speed'],C['agv_acceleration'],C['agv_jerk'])
    turn=SCurve(abs(target[3]-start[3]),C['agv_angular_speed'],C['agv_angular_acceleration'],C['agv_angular_jerk'])
    end_parts=assembly(i,target,height,payload)
    part_distance=max(math.dist(poses[n][:3],v[:3]) for n,v in end_parts.items())
    swing=SCurve(part_distance,C['agv_speed'],C['agv_acceleration'],C['agv_jerk'])
    # The slowest of travel / in-place turn / fork-and-load sweep governs the shape of the whole leg.
    governing=max([travel,turn,swing],key=lambda p:p.duration)
    duration=governing.duration;shape=governing.shape
    while True:
        now=scheduler.time;plan=Plan(start,target,now,duration,payload is not None,shape)
        compatible_all=True
        for peer_id in range(N_FORKLIFTS):
            if peer_id==i:
                continue
            peer=reservations.get(peer_id,Plan(poses[f'agv_{peer_id}'],poses[f'agv_{peer_id}'],now,.05,agv_load[peer_id] is not None))
            if not compatible(plan,peer,now,C['agv_following_gap_m']):
                compatible_all=False
                break
        if compatible_all:
            break
        await engine.until(scheduler.time+.1)
    reservations[i]=plan
    t_leg0=scheduler.time;start_xy=start[:2]
    try:
        await engine.move(end_parts,C['agv_speed'],duration=duration,kind='agv',transform=lambda q:assembly(i,base(q),height,payload),shape=shape)
    finally:
        reservations.pop(i,None)
        end_xy=poses[f'agv_{i}'][:2]
        tracker.record_agv_leg(f'agv_{i}',math.dist(start_xy,end_xy),payload is not None,max(0.0,scheduler.time-t_leg0))

async def settled(job,lift,target):
    start=scheduler.time
    event(job,'LIFT_SETTLING',lift=lift,target_z=target)
    await engine.until(start+C['lift_settle_s'])
    if abs(poses[lift][2]-target)>1e-6:raise RuntimeError('Lift is not aligned for conveyor transfer')
    event(job,'LIFT_READY',lift=lift,target_z=target,settle_elapsed_s=round(scheduler.time-start,5),velocity_m_s=0)

async def follow_async(job,i,route,height=.02,payload=None,pickup=None):
    for target,state in route:
        event(job,state,agv=i+1)
        await drive_async(i,target,height,payload,pickup)

def wheel_heights(s,axis):
    # The wheel set of the active travel axis is lowered onto its rail; the other set is raised 40 mm.
    x,y,z=poses[s][:3]
    return {wheel_name(s,w):(x,y,z+(0 if w==axis else .04)) for w in ['x','y']}

async def wheel_switch(s,axis):
    """4-way direction change at standstill: swap the lowered wheel set over shuttle_turn_s."""
    for n,v in wheel_heights(s,wheel_modes[s]).items():poses.setdefault(n,v)
    t0=scheduler.time
    try:
        await engine.move(wheel_heights(s,axis),kind='shuttle',duration=C['shuttle_turn_s'])
        wheel_modes[s]=axis
    finally:
        t1=scheduler.time
        tracker.record_shuttle_turn(s,max(0.0,t1-t0),t_start=t0,t_end=t1)

async def shuttle_async(s,end,z,pallet=None,extra_loads=None,speed=None):
    if STOP_REQUESTED:return
    route=LAYOUT.path(poses[s][:2],end)
    try:
        for a,b in zip(route,route[1:]):
            if STOP_REQUESTED:return
            axis='x' if abs(b[0]-a[0])>1e-7 else 'y'
            if axis!=wheel_modes[s]:await wheel_switch(s,axis)
            targets={s:tuple(b)+(z,)}
            if pallet:
                pz=z+max(0,deck_offsets[s])
                targets[pallet]=(b[0],b[1],pz,0)
                if extra_loads:
                    for c_name,dx,dy,dz in extra_loads:
                        targets[c_name]=(b[0]+dx,b[1]+dy,pz+dz,0)
            start_xy=poses[s][:2];t_leg0=scheduler.time
            try:
                await engine.move(targets,speed or C['shuttle_speed'])
            finally:
                t_leg1=scheduler.time
                tracker.record_shuttle_leg(s,math.dist(start_xy,poses[s][:2]),max(0.0,t_leg1-t_leg0),t_start=t_leg0,t_end=t_leg1)
        shuttle_route_log.append({'shuttle':s,'path':route})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        tracker.record_shuttle_fault(s,str(exc))
        raise

async def deck_async(s,p,height,extra_loads=None):
    # Fine positioning over the slot/buffer, then raise or lower the deck with the pallet.
    if STOP_REQUESTED:return
    t0=scheduler.time
    try:
        await engine.until(scheduler.time+C['shuttle_positioning_s'])
        deck_offsets[s]=height;x,y,z=poses[s][:3]
        pz=z+max(0,height)
        targets={s:(x,y,z),deck_name(s):(x,y,z+height),p:(x,y,pz,0)}
        if extra_loads:
            for c_name,dx,dy,dz in extra_loads:
                targets[c_name]=(x+dx,y+dy,pz+dz,0)
        await engine.move(targets,duration=C['shuttle_lift_s'])
    finally:
        t1=scheduler.time
        tracker.record_shuttle_busy(s,max(0.0,t1-t0),t_start=t0,t_end=t1)

async def arrivals(total):
    # In continuous mode every Dock worker pre-stages 18 cartons continuously and takes any free Floor-1 slot.
    if total is None:
        cont_lock=asyncio.Lock()
        cont_job_seq=0
        cont_even_seq=0
        even_limit=INBOUND_TARGET if INBOUND_TARGET>0 else None
        even_buf=max(1,len(INBOUND_PICKUP_SLOTS))
        even_released=(min(even_limit,even_buf) if even_limit is not None else even_buf) if INBOUND_EVEN_INTERVAL_S>0 else even_limit
        even_claimed=0
        def _reserve_even():
            nonlocal even_claimed
            even_claimed+=1
        def _can_even():
            if FREE_EVEN_PALLETS.empty() or stored.qsize()>=MAX_STORED_INFLIGHT:
                return False
            if even_released is not None and even_claimed>=even_released:
                return False
            if even_limit is not None and even_claimed>=even_limit:
                return False
            return True
        async def _pace_even_inbound():
            nonlocal even_released
            k=0
            while not STOP_REQUESTED and (even_limit is None or (even_buf+k)<=even_limit):
                if k>0:
                    await engine.until(k*INBOUND_EVEN_INTERVAL_S)
                if STOP_REQUESTED:break
                nxt=even_buf+k
                even_released=min(even_limit,nxt) if even_limit is not None else nxt
                k+=1
                await SLOT_COORD.notify()
        even_pacer=asyncio.create_task(_pace_even_inbound()) if INBOUND_EVEN_INTERVAL_S>0 else None
        async def dock_worker_loop(dock_i):
            nonlocal cont_job_seq,cont_even_seq
            global odd_pallet_seq,max_pending
            batch_k=0
            while not STOP_REQUESTED:
                claimed_meta={'kind':None,'job_id':None,'slot_idx':None,'p_model':None,'cartons':None,'handed':False}
                async def claim_slot_fn(di,dock_id,k,count):
                    nonlocal cont_job_seq,cont_even_seq
                    global odd_pallet_seq,max_pending
                    res=await SLOT_COORD.claim_next(
                        _can_even,
                        lambda:FREE_CARTONS.qsize()>=LOOSE_PER,
                        on_even_claim=_reserve_even,
                        dock_x=dock_flow.docks[di]['x'],
                    )
                    if res is None or STOP_REQUESTED:
                        return None
                    flow,slot_idx=res
                    async with cont_lock:
                        job_id=cont_job_seq;cont_job_seq+=1
                        if flow=='even':
                            even_idx=cont_even_seq;cont_even_seq+=1
                        else:
                            odd_idx=odd_pallet_seq;odd_pallet_seq+=1
                    event(job_id,'INBOUND_ARRIVAL',dock=dock_id)
                    if flow=='even':
                        p_model=FREE_EVEN_PALLETS.get_nowait()
                        JOB_PALLET_MODEL[job_id]=p_model
                        JOB_BAY_IDX[job_id]=slot_idx
                        JOB_EVEN_SEQ[job_id]=even_idx
                        claimed_meta.update({'kind':'even','job_id':job_id,'slot_idx':slot_idx,'p_model':p_model,'handed':False})
                        bay_pos=INBOUND_PICKUP_SLOTS[slot_idx]
                        async def receive_even(_i,cartons,job_id=job_id,p_model=p_model,bay_pos=bay_pos):
                            global max_pending
                            send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
                            send({p_model:bay_pos},transmit=not args.fast)
                            claimed_meta['handed']=True
                            event(job_id,'PICKUP_BAY_LOADED')
                            JOB_IN_QUEUE_T[job_id]=scheduler.time
                            await in_queue.put(job_id)
                            max_pending=max(max_pending,in_queue.qsize())
                        return ('even',bay_pos,None,receive_even)
                    reserved_cartons=[FREE_CARTONS.get_nowait() for _ in range(LOOSE_PER)]
                    spot=LOOSE_SPOTS[slot_idx];stack=LG.stack_positions(spot,LOOSE_PER)
                    claimed_meta.update({'kind':'odd','job_id':job_id,'slot_idx':slot_idx,'cartons':reserved_cartons,'handed':False})
                    async def receive_odd(_i,cartons,job_id=job_id,odd_idx=odd_idx,s=slot_idx,spot=spot,stack=stack,names=reserved_cartons):
                        send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
                        send({n:p+(spot[2],) for n,p in zip(names,stack)},transmit=not args.fast)
                        send({f'loose_pallet_{s}':spot[:2]+(0,0)},transmit=not args.fast)
                        claimed_meta['handed']=True
                        loose_event('LOOSE_PALLET_ON_SPOT',pallet=odd_idx,spot=s)
                        await LOOSE_POOL.add(s,odd_idx,job_id,list(zip(names,stack)))
                    return ('odd',spot,SpotHold(LOOSE_SPOT_FREE[slot_idx]),receive_odd)
                try:
                    await dock_flow.prestage_and_deliver(dock_i,batch_k,LOOSE_PER,claim_slot_fn)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    if claimed_meta['kind']=='even' and not claimed_meta['handed']:
                        JOB_PALLET_MODEL.pop(claimed_meta['job_id'],None)
                        JOB_BAY_IDX.pop(claimed_meta['job_id'],None)
                        JOB_EVEN_SEQ.pop(claimed_meta['job_id'],None)
                        if claimed_meta['p_model'] is not None:
                            FREE_EVEN_PALLETS.put_nowait(claimed_meta['p_model'])
                        await SLOT_COORD.release_pickup(claimed_meta['slot_idx'])
                    elif claimed_meta['kind']=='odd' and not claimed_meta['handed']:
                        for c_name in (claimed_meta.get('cartons') or ()):
                            FREE_CARTONS.put_nowait(c_name)
                        await SLOT_COORD.release_loose(claimed_meta['slot_idx'])
                    raise
                batch_k+=1
        n_docks=len(dock_flow.docks) if getattr(dock_flow,'docks',None) else 0
        try:
            if n_docks>0:
                await asyncio.gather(*(dock_worker_loop(i) for i in range(n_docks)))
            else:
                while not STOP_REQUESTED:await asyncio.sleep(0.1)
        finally:
            if even_pacer is not None and not even_pacer.done():
                even_pacer.cancel()
        return
    # Finite runs keep their configured cadence; a pallet is only placed once its pickup bay is empty.
    async def place(job):
        global max_pending
        flow=inbound_classification(job)
        if flow=='odd' and LOOSE_SPOTS:
            if pickup_bays:await pickup_bays[job%len(pickup_bays)].acquire()
            global odd_pallet_seq
            odd_idx=odd_pallet_seq;odd_pallet_seq+=1
            s=odd_idx%len(LOOSE_SPOTS);spot=LOOSE_SPOTS[s]
            stack=LG.stack_positions(spot,LOOSE_PER)
            async def receive_odd(dock_i,cartons):
                names=[await FREE_CARTONS.get() for _ in range(LOOSE_PER)]
                send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
                send({n:p+(spot[2],) for n,p in zip(names,stack)},transmit=not args.fast)
                send({f'loose_pallet_{s}':spot[:2]+(0,0)},transmit=not args.fast)
                loose_event('LOOSE_PALLET_ON_SPOT',pallet=odd_idx,spot=s)
                await LOOSE_POOL.add(s,odd_idx,job,list(zip(names,stack)))
            try:
                # The spot stays occupied from the pallet's arrival until its last carton is unpacked.
                await dock_flow.prepare('odd',job,spot,list(range(LOOSE_PER)),receive_odd,acquire_target=SpotHold(LOOSE_SPOT_FREE[s]))
            finally:
                if pickup_bays:pickup_bays[job%len(pickup_bays)].release()
            return
        use_pool=CONTINUOUS_MODE or job>=C['jobs']
        p_model=await FREE_EVEN_PALLETS.get() if use_pool else f'pallet_{job}'
        JOB_PALLET_MODEL[job]=p_model
        bay_acquired=False;handed=False
        async def receive(i,cartons):
            nonlocal handed
            send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
            send({p_model:inbound_pickup_position(job)},transmit=not args.fast)
            event(job,'PICKUP_BAY_LOADED');handed=True;JOB_IN_QUEUE_T[job]=scheduler.time;await in_queue.put(job)
        try:
            if pickup_bays:
                await pickup_bays[job%len(pickup_bays)].acquire()
                bay_acquired=True
            await dock_flow.prepare('even',job,inbound_pickup_position(job),list(range(18)),receive)
        except BaseException:
            if not handed and use_pool:
                JOB_PALLET_MODEL.pop(job,None)
                FREE_EVEN_PALLETS.put_nowait(p_model)
            if bay_acquired:
                pickup_bays[job%len(pickup_bays)].release()
            raise
        max_pending=max(max_pending,in_queue.qsize())
    placements=[]
    job=0
    while not STOP_REQUESTED and (total is None or job<total):
        await engine.until(job*C['inbound_interval_s'])
        if STOP_REQUESTED:break
        docks=truck_scene.selected_docks({**C,'truck_count_visible':C.get('dock_active_count',4)})
        event(job,'INBOUND_ARRIVAL',dock=docks[job%len(docks)]['id'] if docks else None)
        placements.append(asyncio.create_task(place(job)))
        if total is None and len(placements)>200:
            placements[:]=[t for t in placements if not t.done()]
        job+=1
    if total is not None:
        await asyncio.gather(*placements)
        for _ in range(N_FORKLIFTS):await in_queue.put(None)
    elif placements:
        await asyncio.gather(*placements,return_exceptions=True)

async def _run_inbound_job(lane,job,parked):
    """Run one inbound pallet cycle in its own frame so background closures never alias later jobs."""
    global stored_count
    t_in_q=JOB_IN_QUEUE_T.pop(job,None)
    if t_in_q is not None:
        tracker.record_stage_wait('pickup_to_agv',max(0.0,scheduler.time-t_in_q))
    p=JOB_PALLET_MODEL.pop(job,f'pallet_{job}');bay_idx=JOB_BAY_IDX.pop(job,(job%len(INBOUND_PICKUP_SLOTS)) if INBOUND_PICKUP_SLOTS else 0);even_seq=JOB_EVEN_SEQ.pop(job,None)
    level=(even_seq%9) if even_seq is not None else (job%9);z=C['storage_levels'][level];s=f'shuttle_{level}'
    pick_xy=INBOUND_PICKUP_SLOTS[bay_idx] if INBOUND_PICKUP_SLOTS else inbound_pickup_position(job)
    port_idx=_pick_inbound_port(even_seq if even_seq is not None else job)
    inp=LAYOUT.data['ports']['in'][port_idx];ix,iy=inp['lift'];bx,by=inp['buffer'];il=f'in_lift_{port_idx}'
    agv_span=(min(PARKS[lane][0],pick_xy[0])-1.5,max(ix,PARKS[lane][0],pick_xy[0])+1.5)
    t_tw0=scheduler.time
    await pickup_maneuver_lock.acquire()
    pm_held=True
    traffic_held=False
    downstream_started=False
    try:
        traffic=dock_flow.gate.agv(agv_span);await traffic.__aenter__()
        traffic_held=True
        fg_wait=max(0.0,scheduler.time-t_tw0)
        tracker.record_agv_leg(f'agv_{lane}',0.0,False,0.0,wait_s=fg_wait)
        pick_loc=f"Ô Pickup {bay_idx+1} ({pick_xy[0]:.2f}, {pick_xy[1]:.2f})"
        drop_loc=f"Cửa nhập Kho 01 - Cổng {port_idx+1} ({ix:.2f}, {inp['drop'][1]:.2f})"
        tracker.start_agv_task(f'agv_{lane}',f'DK-AGV-{job+1:04d}','Khu Dock -> Kho 01','Chở pallet chẵn từ ô Pickup vào cửa nhập Kho 01',scheduler.time,pallet_or_item=p,dock_id=tracker.job_dock_map.get(job),pick_location=pick_loc,drop_location=drop_loc)
        if fg_wait>0.0 and f'agv_{lane}' in tracker._active_agv_tasks:
            tracker._active_agv_tasks[f'agv_{lane}']['wait_s']=round(tracker._active_agv_tasks[f'agv_{lane}'].get('wait_s',0.0)+fg_wait,3)
        route=route_to_pickup(lane,job,pick_xy)
        if not parked:route=[((None,None,0,math.pi),'TURN_TOWARDS_PICKUP')]+route[PICKUP_LEGS_FROM_LANE:]
        await follow_async(job,lane,route,pickup=p)
        await engine.move(assembly(lane,poses[f'agv_{lane}'],.2,p),.4)
        event(job,'AGV_CARRY_TO_INBOUND',agv=lane+1)
        port_idx=_pick_inbound_port(port_idx)
        inp=LAYOUT.data['ports']['in'][port_idx];ix,iy=inp['lift'];bx,by=inp['buffer'];il=f'in_lift_{port_idx}'
        if f'agv_{lane}' in tracker._active_agv_tasks:
            tracker._active_agv_tasks[f'agv_{lane}']['drop_location']=f"Cửa nhập Kho 01 - Cổng {port_idx+1} ({ix:.2f}, {inp['drop'][1]:.2f})"
        # Signal level inbound priority early so outbound on this level yields before AGV reaches the lift.
        _LEVEL_INBOUND_WAITERS[level]=_LEVEL_INBOUND_WAITERS.get(level,0)+1
        inb_wait_reserved=True
        try:
            # Clear the narrow pickup bay maneuver (REVERSE + TURN onto LOAD_Y), then release pickup_maneuver_lock
            # so the next AGV can pick while this AGV travels eastbound along LOAD_Y.
            gate=route_to_gate(lane,ix,port_idx=port_idx)
            await follow_async(job,lane,gate[:2],.2,p)
            if pm_held:
                pickup_maneuver_lock.release()
                pm_held=False
            await follow_async(job,lane,gate[2:-1],.2,p)
            if traffic_held:
                await traffic.__aexit__(None,None,None)
                traffic_held=False
            if CONTINUOUS_MODE:await SLOT_COORD.release_pickup(bay_idx)
            elif pickup_bays:pickup_bays[bay_idx%len(pickup_bays)].release()
            # Reserve the inbound port (chain + lift + level buffer) once clear at the gate.
            t_pw0=scheduler.time
            in_port_waiting[port_idx]+=1
            try:
                await in_ports[port_idx].acquire()
            finally:
                in_port_waiting[port_idx]=max(0,in_port_waiting[port_idx]-1)
        finally:
            if pm_held:
                pickup_maneuver_lock.release()
                pm_held=False
            if inb_wait_reserved:
                _LEVEL_INBOUND_WAITERS[level]=max(0,_LEVEL_INBOUND_WAITERS.get(level,0)-1)
                inb_wait_reserved=False
        t_pw1=scheduler.time
        in_lift_wait=max(0.0,t_pw1-t_pw0)
        tracker.record_agv_leg(f'agv_{lane}',0.0,True,0.0,wait_s=in_lift_wait)
        tracker.record_lift_wait(il,in_lift_wait)
        if t_pw1>t_pw0:
            tracker.record_lift_wait_span(il,t_pw0,t_pw1)
        tracker.record_stage_wait('agv_to_in_lift',in_lift_wait)
        prep_wait_box=[0.0]
        prep_done_box=[None]
        lease_box=[None]
        lease_tok=object()
        downstream_started=False
        async def prepare(z=z,level=level,ix=ix):
            t_sw0=scheduler.time
            lease=await acquire_shuttle_lease(level,priority='high',release_token=lease_tok)
            lease_box[0]=lease
            s_cur=lease.sid
            t_sw1=scheduler.time
            prep_wait=max(0.0,t_sw1-t_sw0)
            prep_wait_box[0]=prep_wait
            tracker.record_shuttle_wait(s_cur,prep_wait)
            if t_sw1>t_sw0:
                tracker.record_shuttle_wait_span(s_cur,t_sw0,t_sw1)
            await shuttle_async(s_cur,(ix,LAYOUT.data['road_y'][0]),z)
            prep_done_box[0]=scheduler.time
        ready=asyncio.create_task(prepare())
        lift_dn_end_box=[None]
        async def lift_down(job=job,il=il,ix=ix,iy=iy):
            z_dn0=(poses.get(il) or (ix,iy,.3))[2]
            t_dn0=scheduler.time
            await engine.move({il:(ix,iy,.3)},C['lift_speed'])
            try:
                await settled(job,il,.3)
            except Exception as exc:
                tracker.record_lift_fault(il,str(exc))
                raise
            t_dn1=scheduler.time
            lift_dn_end_box[0]=t_dn1
            tracker.record_lift_leg(il,abs(z_dn0-.3),max(0.0,t_dn1-t_dn0),t_start=t_dn0,t_end=t_dn1)
        lift_ready=asyncio.create_task(lift_down())
        await lift_ready  # interlock: lift at floor level and settled before approaching the chain
        await follow_async(job,lane,gate[-1:],.2,p)
        event(job,'AGV_HANDOVER_CHAIN',agv=lane+1)
        agv_load[lane]=None;pickups[lane]=p
        await drive_async(lane,(ix,inp['drop'][1]-.5-.12-2.4,0,math.pi/2),.2,pickup=p)
        event(job,'FORKS_CLEAR')
        async def return_trip(job=job,p=p,ix=ix):
            await engine.move(assembly(lane,poses[f'agv_{lane}'],.02),.3)
            legs=route_home(lane,ix)
            await follow_async(job,lane,legs[:3],pickup=p)  # reverse, turn, run back along y=-7
            pickups[lane]=None
            # Hot-redispatch along RETURN_Y when active alone (peer charging) or on west lane so lone Forklift skips parking turns.
            peer_charging=any(_is_agv_charging(f'agv_{k}') for k in range(len(PARKS)) if k!=lane)
            bat_now=tracker.get_agv_battery_pct(f'agv_{lane}',scheduler.time) if tracker is not None else 100.0
            needs_chg_now=(_check_agv_needs_charge(f'agv_{lane}',dry_run=True)[0] is not None) or (bat_now<CHARGE_THRESHOLD_PCT+0.5)
            if (not STOP_REQUESTED) and (not needs_chg_now) and (not in_queue.empty()) and (not pickup_maneuver_lock.locked()) and (peer_charging or lane==0):
                nxt=in_queue.get_nowait()
                if nxt is not None:
                    event(job,'AGV_RETURN_COMPLETE',hot_redispatch=True)
                    return nxt,False
                await in_queue.put(None)
            await follow_async(job,lane,legs[3:])
            event(job,'AGV_RETURN_COMPLETE')
            return 'wait',True
        home=asyncio.create_task(return_trip())
        async def downstream():
            global stored_count,inbound_active
            inbound_active+=1
            # Infeed turntable: the pallet arrives with its 1.2 m side along the forks (north-south) and is
            # turned 90 degrees so it enters the store as X = 1.2 m, Y = 1.0 m (agreed design).
            event(job,'INFEED_TURNTABLE')
            start_p=poses[p]
            def rotate_turntable(q,start_p=start_p):
                u=3*q*q-2*q*q*q
                yaw=start_p[3]+(0.-start_p[3])*u
                return {p:(start_p[0],start_p[1],start_p[2],yaw)}
            await engine.move({p:start_p[:3]+(0,)},kind='conveyor',duration=TURNTABLE_S,transform=rotate_turntable,exact=True)
            tracker.start_lift_task(il,'Nhập kho',f'LF-IN-{job+1:04d}',p)
            t_lc0=scheduler.time
            if lift_dn_end_box[0] is not None and t_lc0>lift_dn_end_box[0]:
                tracker.record_lift_wait_span(il,lift_dn_end_box[0],t_lc0)
            z_up0=(poses.get(il) or (ix,iy,.3))[2]
            event(job,'CHAIN_TO_INPUT_LIFT')
            await engine.move({p:(ix,iy,.3,0)},1)
            event(job,'INPUT_LIFT_ASCEND')
            await engine.move({il:(ix,iy,z),p:(ix,iy,z,0)},C['lift_speed'])
            try:
                await settled(job,il,z)
            except Exception as exc:
                tracker.record_lift_fault(il,str(exc))
                raise
            event(job,'INPUT_BUFFER_TRANSFER')
            await engine.move({p:(bx,by,z,0)},C['conveyor_speed'])
            t_lc1=scheduler.time
            tracker.record_lift_leg(il,abs(z-z_up0),max(0.0,t_lc1-t_lc0),t_start=t_lc0,t_end=t_lc1)
            tracker.finish_lift_task(il)
            event(job,'INPUT_BUFFER_RECEIVE')
            try:
                await ready
                s=lease_box[0].sid
                t_sh1=scheduler.time
                sh_wait=0.0
                if prep_done_box[0] is not None and t_sh1>prep_done_box[0]:
                    tracker.record_shuttle_wait_span(s,prep_done_box[0],t_sh1)
                tracker.record_stage_wait('buffer_to_in_shuttle',prep_wait_box[0]+sh_wait)
                slot_seq=even_seq if even_seq is not None else ((job//2) if CONTINUOUS_MODE else job)
                sid=LAYOUT.job_slots[slot_seq%len(LAYOUT.job_slots)]
                slot=LAYOUT.slot_point(sid)
                wh_pick=f"Bàn đệm Nhập {port_idx+1} ({bx:.2f}, {by:.2f}, T{level+1})"
                wh_drop=f"Ô kệ {sid} ({slot[0]:.2f}, {slot[1]:.2f}, T{level+1})"
                tracker.start_shuttle_task(
                    s,'Nhập kho',f'WH-IN-{job+1:04d}',p,level,sid,scheduler.time,
                    job_id=job,pick_location=wh_pick,drop_location=wh_drop,wait_s=sh_wait,
                    lift_id=il,lift_wait_s=in_lift_wait,process_start_s=t_pw0,
                    process_shuttle_wait_s=prep_wait_box[0]+sh_wait,
                )
                await shuttle_async(s,inp['buffer'],z)
                await deck_async(s,p,.12)
                in_ports[port_idx].release()
                t_rel=scheduler.time
                if t_rel>t_lc1:
                    tracker.record_lift_wait_span(il,t_lc1,t_rel)
                await shuttle_async(s,slot,z,p)
                await deck_async(s,p,-.06)
                access=(slot[0],min(LAYOUT.data['road_y'],key=lambda yy:abs(yy-slot[1])))
                await shuttle_async(s,access,z)
                tracker.finish_shuttle_task(s,scheduler.time)
                event(job,'PALLET_STORED',slot=sid)
                stored_count+=1;inbound_active-=1;await stored.put((job,level,sid,port_idx,p))
            finally:
                if lease_box[0] is not None:
                    await lease_box[0].release(lease_tok)
        downstream_started=True
        background_tasks.append(asyncio.create_task(downstream()))
        next_job,parked=await home
        tracker.finish_agv_task(f'agv_{lane}',scheduler.time)
        return next_job,parked
    finally:
        if not downstream_started and 'ready' in locals():
            try:
                await ready
            except BaseException:
                pass
            if lease_box[0] is not None:
                await lease_box[0].release(lease_tok)
        if pm_held:
            pickup_maneuver_lock.release()
            pm_held=False
        if traffic_held:
            await traffic.__aexit__(None,None,None)

async def agv_worker(lane):
    """AGV dispatch loop.

    Event-driven: an AGV starts towards a waiting pallet immediately. It does not wait
    for its inbound port before the pickup (the lift is lowered in parallel and is only
    awaited before the conveyor handover). On the way back it checks the queue when it
    reaches its parking column on the return lane (RETURN_Y): if a pallet is waiting and
    its battery does not need charging, it carries on west instead of entering the parking
    box. Lanes (agv_motion): RETURN_Y westbound empty, LOAD_Y eastbound loaded, both on floor 1.
    """
    global stored_count
    while not STOP_REQUESTED and _should_yield_to_peer_forklift(lane):
        await engine.until(scheduler.time+0.25)
    if STOP_REQUESTED:return
    job=await in_queue.get();parked=True
    while job is not None:
        _ACTIVE_FORKLIFT_JOBS.add(lane)
        try:
            job,parked=await _run_inbound_job(lane,job,parked)
        finally:
            _ACTIVE_FORKLIFT_JOBS.discard(lane)
        if not STOP_REQUESTED:
            t_chg_until,_=_check_agv_needs_charge(f'agv_{lane}')
            if t_chg_until is not None:
                await engine.until(t_chg_until)
                if not STOP_REQUESTED and scheduler.time+1e-6>=t_chg_until:
                    _complete_agv_charge(f'agv_{lane}')
        if STOP_REQUESTED:break
        if job=='wait':
            while not STOP_REQUESTED and _should_yield_to_peer_forklift(lane):
                await engine.until(scheduler.time+0.25)
            if STOP_REQUESTED:break
            job=await in_queue.get()

# ---------------------------------------------------------------- floor-1 loose goods (agreed process)
LOOSE_SPOTS=[tuple(s) for s in C.get('loose_pallet_spots',[])]
ODD_SPOT_LOCKS=[asyncio.Lock() for _ in LOOSE_SPOTS] or [asyncio.Lock()]
LOOSE_PALLETS=int(C.get('loose_pallets',0)) if LOOSE_SPOTS else 0
LOOSE_PER=int(C.get('loose_cartons_per_pallet',0))
loose_stats={'pallets_unpacked':0,'cartons_on_belt':0,'cartons_delivered':0,'per_floor':{},'shared_picks':0}
LOOSE_SPOT_FREE=[asyncio.Semaphore(1) for _ in LOOSE_SPOTS]

class SlotCoordinator:
    """Dynamic floor-1 input slot allocator: pairs any free Inbound Pickup Bay or
    Floor-1 Loose Unpack Spot with the next pre-staged Dock worker without forcing fixed 50/50 waiting."""
    def __init__(self,n_bays,n_spots):
        self.n_bays=max(0,int(n_bays))
        self.n_spots=max(0,int(n_spots))
        self.pickup_busy=[False]*self.n_bays
        self.loose_busy=[False]*self.n_spots
        self.last_flow='odd'
        self._cond=None
    def _c(self):
        if self._cond is None:self._cond=asyncio.Condition()
        return self._cond
    def _free_bay(self):
        for idx,busy in enumerate(self.pickup_busy):
            if not busy:return idx
        return None
    def _free_spot(self):
        for idx,busy in enumerate(self.loose_busy):
            if not busy:return idx
        return None
    async def claim_next(self,can_even_fn,can_odd_fn=None,on_even_claim=None,dock_x=None):
        west_split=-21.0
        has_west=bool(dock_flow.docks) and any(d['x']<west_split for d in dock_flow.docks)
        has_east=bool(dock_flow.docks) and any(d['x']>=west_split for d in dock_flow.docks)
        async with self._c():
            while not STOP_REQUESTED:
                bay=self._free_bay() if can_even_fn() else None
                odd_ok=bool(LOOSE_SPOTS) and (True if can_odd_fn is None else can_odd_fn())
                spot=self._free_spot() if odd_ok else None
                if dock_x is not None and has_west and has_east:
                    # Prefer West Docks -> Pickup Bays and East Docks -> Loose Spots, falling back if preferred side is full/empty.
                    if dock_x<west_split:
                        pick='even' if bay is not None else ('odd' if spot is not None else None)
                    else:
                        pick='odd' if spot is not None else ('even' if bay is not None else None)
                    if pick is None:
                        await self._c().wait()
                        continue
                else:
                    if bay is not None and spot is not None:
                        pick='even' if self.last_flow=='odd' else 'odd'
                    elif bay is not None:
                        pick='even'
                    elif spot is not None:
                        pick='odd'
                    else:
                        await self._c().wait()
                        continue
                if pick=='even':
                    self.pickup_busy[bay]=True
                    self.last_flow='even'
                    if on_even_claim is not None:
                        on_even_claim()
                    return 'even',bay
                self.loose_busy[spot]=True
                self.last_flow='odd'
                return 'odd',spot
            return None
    async def release_pickup(self,bay_idx):
        async with self._c():
            if 0<=bay_idx<self.n_bays:self.pickup_busy[bay_idx]=False
            self._c().notify_all()
    async def release_loose(self,spot_idx):
        async with self._c():
            if 0<=spot_idx<self.n_spots:self.loose_busy[spot_idx]=False
            self._c().notify_all()
    async def notify(self):
        async with self._c():
            self._c().notify_all()
    def wake_all(self):
        if self._cond is None:return
        async def _wakeup():
            async with self._cond:
                self._cond.notify_all()
        try:asyncio.get_running_loop().create_task(_wakeup())
        except RuntimeError:pass

SLOT_COORD=SlotCoordinator(len(INBOUND_PICKUP_SLOTS),len(LOOSE_SPOTS))

class SpotHold:
    """Acquired by the pallet-jack transfer before it enters a loose spot; deliberately NOT released when the
    transfer ends: LOOSE_POOL releases it once the last carton of that pallet has been unpacked."""
    def __init__(self,sem):self.sem=sem
    async def __aenter__(self):await self.sem.acquire()
    async def __aexit__(self,*exc):return False

class LoosePool:
    """Cartons of the pallets standing on the floor-1 loose spots. Both operators take cartons from it: each
    prefers the pallet on its own spot, and helps on the other pallet when its own spot is empty."""
    def __init__(self):self.pallets={};self.cond=None
    def _c(self):
        if self.cond is None:self.cond=asyncio.Condition()
        return self.cond
    async def add(self,s,odd_idx,job,items):
        async with self._c():
            self.pallets[s]={'odd':odd_idx,'job':job,'todo':list(items),'busy':0}
            self._c().notify_all()
    async def take(self,prefer):
        async with self._c():
            await self._c().wait_for(lambda:any(p['todo'] for p in self.pallets.values()))
            s=prefer if prefer in self.pallets and self.pallets[prefer]['todo'] else next(k for k,p in self.pallets.items() if p['todo'])
            p=self.pallets[s];p['busy']+=1
            return s,p,p['todo'].pop(0)
    async def done(self,s):
        async with self._c():
            p=self.pallets[s];p['busy']-=1
            if p['todo'] or p['busy']:return
            del self.pallets[s]
        loose_stats['pallets_unpacked']+=1
        loose_event('LOOSE_PALLET_EMPTY',pallet=p['odd'],spot=s)
        send({f'loose_pallet_{s}':LOOSE_SPOTS[s][:2]+(-5,0)},transmit=not args.fast)
        LOOSE_SPOT_FREE[s].release()
        if CONTINUOUS_MODE:await SLOT_COORD.release_loose(s)
LOOSE_POOL=LoosePool()

def loose_home_spot(op):
    return min(range(len(LOOSE_SPOTS)),key=lambda s:abs(LOOSE_SPOTS[s][0]-op['x']))

async def loose_worker(k):
    """Operator k at the carton belt: unpack cartons from the loose pallets, one at a time, onto the belt."""
    op=LG.OPERATORS[k];home=loose_home_spot(op)
    while True:
        s,p,(n,pos)=await LOOSE_POOL.take(home)
        if s!=home:
            loose_stats['shared_picks']+=1
            tracker.record_worker_activity(f'op_{k}',shared_pick=True)
        # Two operators at one pallet stand side by side: the spot's own operator nearer the belt.
        await operator_cycle(k,op,LOOSE_SPOTS[s],n,pos,dy=.25 if s==home else -.25)
        dest=LG.floors()[loose_stats['cartons_on_belt']%len(LG.floors())]
        branch=loose_stats['cartons_on_belt']//len(LG.floors())
        loose_stats['cartons_on_belt']+=1
        loose_event('CARTON_ON_BELT',carton=n,pallet=p['job'],floor=dest,operator=k)
        background_tasks.append(asyncio.create_task(carton_trip(n,dest,branch)))
        await LOOSE_POOL.done(s)

def loose_event(state,**extra):
    global event_count
    record={'job':None,'loose':True,'state':state,'demo_time_s':round(scheduler.time,5),**extra}
    events.append(record);event_count+=1
    tracker.record_event(record)
    _write_event(record)
    if 'pallet' in extra and state.startswith('LOOSE_PALLET'): print(f"Loose pallet {extra['pallet']+1:02d}: {state}",flush=True)

async def _run_busy_worker(wid,worker_key,coro,wait_intervals=None):
    BUSY_WORKERS.add(wid)
    t0=scheduler.time
    try:return await coro
    finally:
        t1=scheduler.time
        if not wait_intervals:
            tracker.record_worker_activity(worker_key,duration_s=max(0.0,t1-t0),t_start=t0,t_end=t1)
        else:
            cur=t0
            for ws,we in sorted((max(t0,float(a)),min(t1,float(b))) for a,b in wait_intervals if b>a):
                if ws>cur:
                    tracker.record_worker_activity(worker_key,duration_s=ws-cur,t_start=cur,t_end=ws)
                if we>ws:
                    tracker.record_worker_wait_span(worker_key,ws,we)
                if we>cur:
                    cur=we
            if t1>cur:
                tracker.record_worker_activity(worker_key,duration_s=t1-cur,t_start=cur,t_end=t1)
        BUSY_WORKERS.discard(wid)

async def _tracked_agv_mission(agv_id,prefix,area,task_type,pallet_or_item,coro,pick_location='',drop_location=''):
    code=tracker.next_agv_code(prefix)
    tracker.start_agv_task(agv_id,code,area,task_type,scheduler.time,pallet_or_item=pallet_or_item,pick_location=pick_location,drop_location=drop_location)
    try:
        res=await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        tracker.record_agv_fault(agv_id)
        raise
    tracker.finish_agv_task(agv_id,scheduler.time)
    return res

async def follow_path(name,pts,speed):
    """Constant-speed motion of one object along a polyline (cartons on belts / spirals)."""
    total=LG.length(pts)
    await engine.move({name:LG.along(pts,total)},speed,duration=max(.05,total/speed),kind='conveyor',
                      transform=lambda q:{name:LG.along(pts,q*total)},shape=lambda t:t)

STATION_WORKERS_BASE={}
for i,op in enumerate(LG.OPERATORS):
    sx,sy,syaw=LG.station(op)
    STATION_WORKERS_BASE[i]=(sx,sy,syaw,0.0)
for f in LG.floors():
    for b in range(len(LG.BRANCHES)):
        STATION_WORKERS_BASE[LG.u1_worker_id(f,b)]=LG.u1_station(f,b)
    for z_idx in range(len(LG.Z05_WORKERS)):
        STATION_WORKERS_BASE[LG.z05_worker_id(f,z_idx)]=LG.z05_station(f,z_idx)
    for u2_idx in range(len(LG.U2_WORKERS)):
        STATION_WORKERS_BASE[LG.u2_worker_id(f,u2_idx)]=LG.u2_station(f,u2_idx)
    for st_idx in range(len(LG.P1_STATIONS)):
        STATION_WORKERS_BASE[LG.p1_worker_id(f,st_idx)]=LG.p1_station(f,st_idx)

BUSY_WORKERS=set()

async def station_workers_ambient_loop():
    if args.fast:
        return
    POSTURES = [
        (0.200, 0.760, 0.000),   # Posture 0: Đặt tay trên mặt bàn thao tác/khay hàng, cúi nhẹ kiểm tra kiện
        (0.260, 0.880, 0.080),   # Posture 1: Hơi nghiêng người, nâng tay cầm máy quét barcode / kiểm tra tem
        (0.160, 0.660, -0.080),  # Posture 2: Đổi trọng tâm chân, một tay vịn bàn, mắt quan sát màn hình HMI
    ]
    TRANS_DUR = 1.00
    CYCLE_DUR = 5.00
    LOOP_INTERVAL = 0.20
    try:
        while engine.running:
            t = scheduler.time
            changes = {}
            for wid, (wx, wy, wyaw, wz) in STATION_WORKERS_BASE.items():
                if wid in BUSY_WORKERS:
                    continue
                phi = (wid * 1.6180339887) % (len(POSTURES) * CYCLE_DUR)
                t_eff = t + phi
                cycle_idx = int(t_eff // CYCLE_DUR)
                tau = t_eff % CYCLE_DUR
                k_cur = cycle_idx % len(POSTURES)
                k_nxt = (k_cur + 1) % len(POSTURES)

                w = 0.5 - 0.5 * math.cos(math.pi * tau / CYCLE_DUR)
                p_from = POSTURES[k_cur]
                p_to = POSTURES[k_nxt]
                breath = 0.015 * math.sin(tau * 3.14159)
                bend = p_from[0] + (p_to[0] - p_from[0]) * w + breath
                arm = p_from[1] + (p_to[1] - p_from[1]) * w + breath * 1.2
                dyaw = p_from[2] + (p_to[2] - p_from[2]) * w
                changes.update(LG.operator_pose(wid, wx, wy, wyaw + dyaw, bend, arm, 0.0, z0=wz, upper_only=True))
            if changes:
                send(changes, transmit=not args.fast)
            await asyncio.sleep(0 if args.fast else LOOP_INTERVAL)
    except (asyncio.CancelledError, Exception):
        pass

async def pallet_pick_supervisor_loop():
    slots_x=[-33.485,-32.440,-31.395,-30.335]
    py=-1.26
    step=0
    wid=500
    cur_x=slots_x[0]
    try:
        while p1_running and engine.running:
            tgt_x=slots_x[step%len(slots_x)]
            step+=1
            dist=abs(tgt_x-cur_x)
            if dist>0.05:
                dur=max(0.8,dist/0.85)
                walk_yaw=0.0 if tgt_x>cur_x else math.pi
                steps=max(2,int(dist/0.35))
                def walk_at(q):
                    wx=cur_x+(tgt_x-cur_x)*q
                    sw=0.30*math.sin(math.pi*steps*q)
                    return LG.operator_pose(wid,wx,py,walk_yaw,0.04,0.0,sw)
                await engine.move(walk_at(1.0),duration=dur,transform=walk_at,exact=True)
                cur_x=tgt_x
            def turn_at(q):
                u=3*q*q-2*q*q*q
                from_yaw=0.0 if tgt_x>cur_x else math.pi
                dyaw=math.atan2(math.sin(-math.pi/2-from_yaw),math.cos(-math.pi/2-from_yaw))
                return LG.operator_pose(wid,cur_x,py,from_yaw+dyaw*u,0.04,0.0,0.0)
            await engine.move(turn_at(1.0),duration=0.35,transform=turn_at,exact=True)
            def inspect_at(q):
                sw=math.sin(q*math.pi*3)
                b=0.12+0.03*sw
                a=0.35+0.04*sw
                return LG.operator_pose(wid,cur_x,py,-math.pi/2,b,a,0.02*sw,upper_only=True)
            await engine.move(inspect_at(1.0),duration=2.2,transform=inspect_at,exact=True)
    except (asyncio.CancelledError,Exception):pass

U1_PHASES=(.15,.10,.15,.20,.12,.10,.08,.10)

async def _u1_operator_cycle(floor,branch,carton,rack_pose,rack_idx,trolley_slot,tray_slot=0):
    """U1 sorting worker at the end of branch (*U632):
    1. Opens carton on the belt end, revealing 3 yellow medicine boxes inside.
    2. Picks each yellow medicine box (3 turns), turns 180 deg, and places it into the hollow blue tray on the U1 rack.
    3. Picks up the empty carton shell and places it into the empty Trolley standing next to the worker."""
    wid=LG.u1_worker_id(floor,branch)
    sx,sy,syaw,z0=LG.u1_station(floor,branch)
    open_s=float(C.get('u1_open_carton_s',4.0))
    pick_s=float(C.get('u1_pick_box_s',C.get('u1_place_s',6.0)))
    d=[f*pick_s for f in U1_PHASES]
    pick_y=11.85;pick_bend,pick_arm=.38,.72
    place_y=12.06;place_bend,place_arm=.14,1.14
    med_count=int(C.get('u1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    med_offset=tray_slot*med_count
    med_names=[f'u1_med_{floor}_{branch}_{rack_idx}_{med_offset+m}' for m in range(med_count)]
    c_pose=poses[carton]
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry_item=None,item_to=None,walk=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        upper_only=not walk
        item_name=carry_item or (item_to[0] if item_to else None)
        i0=poses[item_name] if item_name else None
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.30*math.sin(math.pi*steps*q) if walk else 0.,z0=z0,upper_only=upper_only)
            if carry_item:
                out[carry_item]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
            elif item_to is not None:
                t_name,t_pose=item_to
                arc_z=0.06*math.sin(math.pi*q) if ('med' in t_name or 'carton' in t_name) else 0.
                cz=i0[2]+(t_pose[2]-i0[2])*q+arc_z
                out[t_name]=(i0[0]+(t_pose[0]-i0[0])*q,i0[1]+(t_pose[1]-i0[1])*q,cz,i0[3]+(t_pose[3]-i0[3])*q)
            return out
        end=at(1.)
        await engine.move(end,duration=max(.04,dur),transform=at,shape=(lambda t:t) if walk else blend,exact=True)
        state.update(b)
    # 1. Step to conveyor end and open the carton, revealing the yellow medicine boxes inside
    await phase(open_s*.45,y=pick_y,bend=pick_bend,arm=pick_arm,walk=True)
    send({med_names[m]:LG.u1_med_in_carton(c_pose,m) for m in range(med_count)},transmit=not args.fast)
    await phase(open_s*.55,y=pick_y,bend=pick_bend-.06,arm=pick_arm+.12)
    # 2. Pick each yellow medicine box one by one, turn 180 deg, and place into the hollow blue tray on the U1 rack
    placed=[]
    for m,m_name in enumerate(med_names):
        box_i=1 if med_count==1 else m
        target_on_rack=LG.u1_med_on_rack(rack_pose,box_i,tray_slot)
        place_yaw=math.atan2(target_on_rack[1]-sy,target_on_rack[0]-sx)
        await phase(d[0],y=pick_y,yaw=syaw,bend=pick_bend,arm=pick_arm,walk=True)
        grab=LG.held_carton(sx,pick_y,syaw,pick_bend,pick_arm,z0=z0)
        await phase(d[1],item_to=(m_name,grab))
        await phase(d[2],y=sy,bend=CARRY[0],arm=CARRY[1],carry_item=m_name,walk=True)
        await phase(d[3],yaw=place_yaw,carry_item=m_name)
        await phase(d[4],y=place_y,bend=place_bend,arm=place_arm,carry_item=m_name,walk=True)
        await phase(d[5],item_to=(m_name,target_on_rack))
        placed.append((m_name,box_i,tray_slot))
        loose_event('MED_BOX_PLACED_IN_TRAY',carton=carton,med_box=m_name,box_index=m,floor=floor,branch=branch)
        await phase(d[6],y=sy,bend=0.,arm=0.,walk=True)
        await phase(d[7],yaw=syaw)
    # 3. Pick up the empty carton shell and place it into the adjacent U1 Trolley
    t_pose=LG.u1_trolley_pose(floor,branch)
    target_in_trolley=LG.trolley_carton_pose(t_pose,trolley_slot)
    t_yaw=math.atan2(target_in_trolley[1]-sy,target_in_trolley[0]-sx)
    await phase(d[0],y=pick_y,yaw=syaw,bend=pick_bend,arm=pick_arm,walk=True)
    grab_c=LG.held_carton(sx,pick_y,syaw,pick_bend,pick_arm,z0=z0)
    await phase(d[1],item_to=(carton,grab_c))
    await phase(d[2],y=sy,bend=CARRY[0],arm=CARRY[1],carry_item=carton,walk=True)
    await phase(d[3],yaw=t_yaw,carry_item=carton)
    reach_bend,reach_arm=LG.reach_for(target_in_trolley[2]-z0)
    await phase(d[4],bend=reach_bend,arm=reach_arm,carry_item=carton)
    await phase(d[5],item_to=(carton,target_in_trolley))
    await phase(d[6],x=sx,y=sy,yaw=syaw,bend=0.,arm=0.)
    loose_event('EMPTY_CARTON_IN_TROLLEY',carton=carton,floor=floor,branch=branch,slot=trolley_slot)
    return placed

async def u1_operator_cycle(floor,branch,*args,**kwargs):
    return await _run_busy_worker(LG.u1_worker_id(floor,branch),f'u1_{floor}_{branch}',_u1_operator_cycle(floor,branch,*args,**kwargs))

# ---------------------------------------------------------------- 4-floor AGVs: CAD path network, traffic, drop positions
def _ff_agv_targets():
    t=[(c['x'],c['y']) for c in LG.L['rack_cells']]
    for b in range(len(LG.BRANCHES)):t+=[LG.u1_rack_pose(1,b)[:2],LG.u1_trolley_pose(1,b)[:2]]
    for u in range(len(LG.U2_WORKERS)):t+=[LG.u2_rack_pose(1,u)[:2],LG.u2_trolley_pose(1,u)[:2]]
    for s in range(len(LG.P1_STATIONS)):t+=[LG.p1_rack_pose(1,s)[:2],LG.p1_trolley_pose(1,s)[:2]]
    return t+list(LG.K06_SLOTS_XY)
AGV_NET=AN.Network(AN.segments(),_ff_agv_targets())
AGV_TRAFFIC={f:AN.Traffic(AGV_NET) for f in LG.floors()}
AGV_SPOTS={f:AN.Spots() for f in LG.floors()}
K06_READY={f:[] for f in LG.floors()}          # (slot key, trolley) waiting in Khu 06, oldest first
K06_COND={}
LOAD_LIFT=.03;LOAD_LIFT_S=.5
agv_traffic_stats={'trips':0,'wait_s':0.,'max_wait_s':0.}

def cell_key(c):return ('cell',round(c['x'],3),round(c['y'],3))
def k06_key(slot):return ('k06',slot)

class Load:
    """A rack / trolley carried on top of an AGV. yaw_to: its yaw once set down; contents(pose) -> poses of the
    items riding on it (medicine boxes, cartons)."""
    def __init__(self,name,yaw_to,contents=None):
        self.name=name;self.yaw_to=yaw_to;self.contents=contents

def _scurve_time_for_dist(prof,target_d):
    if prof.d<=1e-9 or target_d<=0.0:return 0.0
    if target_d>=prof.d:return prof.T
    lo,hi=0.0,prof.T
    for _ in range(20):
        mid=0.5*(lo+hi)
        if prof.position(mid)<target_d:lo=mid
        else:hi=mid
    return 0.5*(lo+hi)

def _agv_trip_windows(pts,keys,cum,start_yaw,end_yaw,speed,load):
    dt_tick,_,_=_sim_step_params()
    def qdur(d):return max(1,math.ceil((max(1e-6,d)-1e-9)/dt_tick))*dt_tick
    w=float(C.get('ff_agv_angular_speed',C['agv_angular_speed']))
    t_cur=qdur(LOAD_LIFT_S) if load else 0.0
    node_span={}
    def touch(nk,t0,t1):
        if nk is None:return
        if nk in node_span:
            prev=node_span[nk]
            if t0<prev[0]:prev[0]=t0
            if t1>prev[1]:prev[1]=t1
        else:
            node_span[nk]=[t0,t1]
    yaw=start_yaw;i=0
    while i<len(pts)-1:
        hd=math.atan2(pts[i+1][1]-pts[i][1],pts[i+1][0]-pts[i][0]);j=i+1
        while j+1<len(pts) and abs(math.remainder(math.atan2(pts[j+1][1]-pts[j][1],pts[j+1][0]-pts[j][0])-hd,2*math.pi))<1e-3:j+=1
        dyaw=math.remainder(hd-yaw,2*math.pi)
        if abs(dyaw)>math.pi/2+1e-6:dyaw=math.remainder(hd+math.pi-yaw,2*math.pi)
        t_turn0=t_cur
        if abs(dyaw)>1e-3:
            t_cur+=qdur(abs(dyaw)/w)
            touch(keys[i],t_turn0,t_cur)
        yaw+=dyaw
        L=cum[j]-cum[i]
        prof=SCurve(L,speed,C['agv_acceleration'],C['agv_jerk'])
        seg_dur=qdur(max(.05,prof.duration))
        for m in range(i,j+1):
            if keys[m] is not None:
                d_m=cum[m]-cum[i]
                frac=(_scurve_time_for_dist(prof,d_m)/prof.T) if prof.T>0 else (d_m/max(1e-9,L))
                t_m=t_cur+frac*seg_dur
                touch(keys[m],t_m,t_m)
        t_cur+=seg_dur;i=j
    return [(k,max(0.0,t0-1.2),t1+1.4) for k,(t0,t1) in node_span.items()]

async def agv_trip(floor,agv,end_xy,end_yaw=None,speed=1.,load=None):
    """Drive a 4-floor AGV along the CAD path network to end_xy using time-space window reservations."""
    z0=LG.LEVELS[floor-1];tr=AGV_TRAFFIC[floor]
    p=poses[agv];sx,sy=p[:2];yaw=p[3] if len(p)>3 else 0.
    pts,keys=AGV_NET.plan((sx,sy),tuple(end_xy[:2]))
    lyaw0=poses[load.name][3] if load and len(poses[load.name])>3 else 0.
    def frame(x,y,ayaw,lz,lyaw):
        out={agv:(x,y,z0,ayaw)}
        if load:
            lp=(x,y,z0+lz,lyaw);out[load.name]=lp
            if load.contents:out.update(load.contents(lp))
        return out
    cum=[0.]
    for a,b in zip(pts,pts[1:]):cum.append(cum[-1]+math.dist(a,b))
    windows=_agv_trip_windows(pts,keys,cum,yaw,end_yaw,speed,load)
    t_req=scheduler.time
    await tr.acquire_timed(agv,windows,lambda:scheduler.time,engine.until,lambda:STOP_REQUESTED)
    waited=max(0.0,scheduler.time-t_req)
    agv_traffic_stats['trips']+=1;agv_traffic_stats['wait_s']+=waited
    agv_traffic_stats['max_wait_s']=max(agv_traffic_stats['max_wait_s'],waited)
    tracker.record_agv_leg(agv,0.0,load is not None,0.0,wait_s=waited)
    w=float(C.get('ff_agv_angular_speed',C['agv_angular_speed']))
    lift=LOAD_LIFT if load else 0.
    is_loaded=load is not None
    try:
        if STOP_REQUESTED:return
        if load:
            t_lift0=scheduler.time
            try:await engine.move(frame(sx,sy,yaw,lift,lyaw0),duration=LOAD_LIFT_S,transform=lambda q:frame(sx,sy,yaw,lift*q,lyaw0),exact=True)
            finally:tracker.record_agv_leg(agv,0.0,True,max(0.0,scheduler.time-t_lift0))
        x,y=sx,sy;i=0
        while i<len(pts)-1:
            hd=math.atan2(pts[i+1][1]-pts[i][1],pts[i+1][0]-pts[i][0]);j=i+1
            while j+1<len(pts) and abs(math.remainder(math.atan2(pts[j+1][1]-pts[j][1],pts[j+1][0]-pts[j][0])-hd,2*math.pi))<1e-3:j+=1
            dyaw=math.remainder(hd-yaw,2*math.pi)
            if abs(dyaw)>math.pi/2+1e-6:dyaw=math.remainder(hd+math.pi-yaw,2*math.pi)   # latent AGVs drive both ways
            if abs(dyaw)>1e-3:
                y0=yaw;t_turn0=scheduler.time
                try:
                    await engine.move(frame(x,y,y0+dyaw,lift,lyaw0),duration=abs(dyaw)/w,
                                      transform=lambda q,y0=y0,d=dyaw,x=x,y=y:frame(x,y,y0+d*q,lift,lyaw0),exact=True)
                finally:tracker.record_agv_leg(agv,0.0,is_loaded,max(0.0,scheduler.time-t_turn0))
                yaw=y0+dyaw
            a,b=pts[i],pts[j];L=cum[j]-cum[i]
            prof=SCurve(L,speed,C['agv_acceleration'],C['agv_jerk'])
            def at(q,a=a,b=b,L=L,yaw=yaw):
                return frame(a[0]+(b[0]-a[0])*q,a[1]+(b[1]-a[1])*q,yaw,lift,lyaw0)
            seg_start=poses[agv][:2];t_seg0=scheduler.time
            try:await engine.move(at(1.),duration=max(.05,prof.duration),transform=at,shape=prof.shape,exact=True)
            finally:tracker.record_agv_leg(agv,math.dist(seg_start,poses[agv][:2]),is_loaded,max(0.0,scheduler.time-t_seg0))
            x,y=b;i=j
        tyaw=yaw if end_yaw is None else yaw+math.remainder(end_yaw-yaw,2*math.pi)
        lyaw1=lyaw0 if not load or load.yaw_to is None else lyaw0+math.remainder(load.yaw_to-lyaw0,2*math.pi)
        turn=max(abs(tyaw-yaw),abs(lyaw1-lyaw0))
        if turn>1e-3:
            t_fturn0=scheduler.time
            try:
                await engine.move(frame(x,y,tyaw,lift,lyaw1),duration=turn/w,
                                  transform=lambda q:frame(x,y,yaw+(tyaw-yaw)*q,lift,lyaw0+(lyaw1-lyaw0)*q),exact=True)
            finally:tracker.record_agv_leg(agv,0.0,is_loaded,max(0.0,scheduler.time-t_fturn0))
        if load:
            t_drop0=scheduler.time
            try:await engine.move(frame(x,y,tyaw,0.,lyaw1),duration=LOAD_LIFT_S,transform=lambda q:frame(x,y,tyaw,lift*(1-q),lyaw1),exact=True)
            finally:tracker.record_agv_leg(agv,0.0,True,max(0.0,scheduler.time-t_drop0))
    finally:
        tr.release_timed(agv)

class AgvZonePool:
    """Dynamic shared AGV dispatcher per zone/row on each floor (reduces 104 4-floor AGVs to 10/floor)."""
    def __init__(self, entries):
        self.entries=list(entries)
        self.busy={aid:False for aid,_ in self.entries}
        self._cond=None
    def _c(self):
        if self._cond is None:self._cond=asyncio.Condition()
        return self._cond
    async def acquire(self,home_xy):
        async with self._c():
            while not STOP_REQUESTED:
                free=[aid for aid,is_busy in self.busy.items() if not is_busy and not _is_agv_charging(aid)]
                if free:
                    # Prefer any free AGV already sitting right at this station rack before moving another AGV.
                    at_home=[aid for aid in free if aid in poses and math.dist(poses[aid][:2],home_xy[:2])<0.05]
                    best=at_home[0] if at_home else min(free,key=lambda aid:math.dist(poses[aid][:2],home_xy[:2]) if aid in poses else 1e9)
                    self.busy[best]=True
                    return best
                await self._c().wait()
            return None
    async def release(self,agv_id):
        if agv_id is None or self._cond is None:return
        t_chg_until=None
        if not STOP_REQUESTED:
            t_chg_until,_=_check_agv_needs_charge(agv_id)
        async with self._c():
            if agv_id in self.busy:self.busy[agv_id]=False
            self._c().notify_all()
        if t_chg_until is not None and not STOP_REQUESTED:
            async def _wait_charge(aid=agv_id,tu=t_chg_until,pool=self):
                # Move picking/unpack AGV to its floor's perimeter charging bay while charging
                parts=[int(tok) for tok in aid.split('_') if tok.isdigit()]
                fl=parts[0] if parts else 1
                bay_xy=LG.FF.AGV_CHARGING_BAYS_XY[sum(parts)%len(LG.FF.AGV_CHARGING_BAYS_XY)]
                try:
                    await agv_trip(fl,aid,bay_xy,math.pi/2,float(C.get('u1_agv_speed',.45)))
                except Exception:
                    pass
                await engine.until(tu)
                if not STOP_REQUESTED and scheduler.time+1e-6>=tu:
                    _complete_agv_charge(aid)
                    pool.wake_all()
            background_tasks.append(asyncio.create_task(_wait_charge()))
    def wake_all(self):
        if self._cond is None:return
        async def _wakeup():
            async with self._cond:
                self._cond.notify_all()
        try:asyncio.get_running_loop().create_task(_wakeup())
        except RuntimeError:pass

async def rack_swap(floor,agv,home,full_rack,full_contents,store_cell,next_rack,spare_cell,next_contents=None,speed=1.,on_stored=None,on_pickup_spare=None):
    """Shared/station AGV drives empty to home=(x, y, agv_yaw, rack_yaw) if needed, stores full_rack in
    store_cell (claimed empty first), fetches next_rack from spare_cell and brings it back to home."""
    spots=AGV_SPOTS[floor]
    if agv in poses and math.dist(poses[agv][:2],home[:2])>0.05:
        await agv_trip(floor,agv,home[:2],None,speed)
    if STOP_REQUESTED:return
    await spots.claim([cell_key(store_cell)],full_rack)
    await agv_trip(floor,agv,(store_cell['x'],store_cell['y']),None,speed,Load(full_rack,store_cell.get('yaw',0.),full_contents))
    if on_stored:on_stored()
    await agv_trip(floor,agv,(spare_cell['x'],spare_cell['y']),None,speed)
    if on_pickup_spare:on_pickup_spare()
    await agv_trip(floor,agv,home[:2],home[2],speed,Load(next_rack,home[3],next_contents))
    spots.free(cell_key(spare_cell))

K06_TROLLEY_CARTONS={f:{} for f in LG.floors()}

def _k06_release_trolley_cartons(floor,trolley):
    """Hide U1/U2 empty cartons stored on a Khu 06 trolley and return them to their pools when picked up."""
    entry=K06_TROLLEY_CARTONS[floor].pop(trolley,None)
    if not entry:return
    kind,cartons=entry
    tp=poses.get(trolley,(0.,0.,0.,0.))
    send({c:(tp[0],tp[1],-5.,0.) for c in cartons},transmit=not args.fast)
    if kind=='u1':
        for c in cartons:FREE_CARTONS.put_nowait(c)
        if CONTINUOUS_MODE:SLOT_COORD.wake_all()
    elif kind=='u2':
        for c in cartons:FREE_Z05_CARTONS[floor].put_nowait(c)

async def _k06_take_ready(floor,exclude,prefer_loaded=None):
    cond=K06_COND.setdefault(floor,asyncio.Condition())
    async with cond:
        await cond.wait_for(lambda:any(t!=exclude for _,t in K06_READY[floor]))
        loaded_map=K06_TROLLEY_CARTONS[floor]
        k=None
        if prefer_loaded is True:
            k=next((i for i,(_,t) in enumerate(K06_READY[floor]) if t!=exclude and loaded_map.get(t)),None)
        elif prefer_loaded is False:
            k=next((i for i,(_,t) in enumerate(K06_READY[floor]) if t!=exclude and not loaded_map.get(t)),None)
        if k is None:
            k=next(i for i,(_,t) in enumerate(K06_READY[floor]) if t!=exclude)
        return K06_READY[floor].pop(k)

async def _k06_put_ready(floor,slot,trolley):
    cond=K06_COND.setdefault(floor,asyncio.Condition())
    async with cond:
        K06_READY[floor].append((slot,trolley));cond.notify_all()

async def trolley_exchange(floor,agv,home,trolley_pose,trolley,out_contents=None,on_drop=None,in_contents=None,on_pick=None,speed=1.,prefer_loaded=None):
    """Take the station trolley to a free Khu 06 slot, leave it there, collect a waiting trolley from another
    slot and park it at the station mark. Returns the trolley now at the station."""
    spots=AGV_SPOTS[floor];tx,ty,_,tyaw=trolley_pose
    await agv_trip(floor,agv,(tx,ty),None,speed)
    key=await spots.claim([k06_key(s) for s in range(len(LG.K06_SLOTS_XY))],trolley)
    kx,ky,_,kyaw=LG.k06_slot_pose(floor,key[1])
    await agv_trip(floor,agv,(kx,ky),None,speed,Load(trolley,kyaw,out_contents))
    if on_drop:on_drop()
    await _k06_put_ready(floor,key,trolley)
    rkey,rtrolley=await _k06_take_ready(floor,trolley,prefer_loaded=prefer_loaded)
    rx,ry,_,_=LG.k06_slot_pose(floor,rkey[1])
    await agv_trip(floor,agv,(rx,ry),None,speed)
    if on_pick:on_pick(rtrolley)
    await agv_trip(floor,agv,(tx,ty),None,speed,Load(rtrolley,tyaw,in_contents))
    spots.free(rkey)
    await agv_trip(floor,agv,home[:2],home[2],speed)
    return rtrolley

def u1_home(floor,b):
    bx,u1_y,_,_=LG.u1_rack_pose(floor,b);return (bx,u1_y,math.pi/2,0.)
def u2_home(floor,u):
    rx,ry,_,ryaw=LG.u2_rack_pose(floor,u);return (rx,ry,0.,ryaw)
def p1_home(floor,s):
    rx,ry,_,ryaw=LG.p1_rack_pose(floor,s);return (rx,ry,-math.pi/2 if s<7 else 0.,ryaw)

U1_AGV_POOLS={f:AgvZonePool([(f'u1_agv_{f}_{k}',u1_home(f,hb)) for k,hb in enumerate(LG.U1_AGV_HOME_BRANCHES)]) for f in LG.floors()}
U2_AGV_POOLS={f:AgvZonePool([(f'u2_agv_{f}_{k}',u2_home(f,hs)) for k,hs in enumerate(LG.U2_AGV_HOME_STATIONS)]) for f in LG.floors()}
P1_AGV_POOLS={
    (f,row):AgvZonePool([
        (f'p1_agv_{f}_{k}',p1_home(f,hs))
        for k,hs in enumerate(LG.P1_AGV_HOME_STATIONS)
        if (0 if hs<7 else 1)==row
    ])
    for f in LG.floors() for row in (0,1)
}
for _f in LG.floors():
    if not P1_AGV_POOLS[(_f,0)].entries or not P1_AGV_POOLS[(_f,1)].entries:
        _all_p1=[(f'p1_agv_{_f}_{k}',p1_home(_f,hs)) for k,hs in enumerate(LG.P1_AGV_HOME_STATIONS)]
        P1_AGV_POOLS[(_f,0)]=AgvZonePool(_all_p1)
        P1_AGV_POOLS[(_f,1)]=P1_AGV_POOLS[(_f,0)]

def _init_agv_positions():
    """Initial occupancy: spare racks in their Kho 04 cells; movable trolleys waiting in Khu 06."""
    for f in LG.floors():
        sp=AGV_SPOTS[f]
        for STATE in (U1_STATE,U2_STATE,P1_STATE):
            for (fl,_),st in STATE.items():
                if fl!=f:continue
                for item in st['spares']:sp.occupy(cell_key(item[2]),item[0])
        for k,slot in enumerate(LG.K06_INITIAL_SLOTS):
            name=LG.k06_trolley_name(f,k);poses[name]=LG.k06_slot_pose(f,slot)
            sp.occupy(k06_key(slot),name);K06_READY[f].append((k06_key(slot),name))

async def u1_trolley_swap_cycle(floor,branch,cartons_in_trolley):
    """When the U1 trolley holds 5 empty cartons, a pooled U1 AGV takes it to Khu 06 and brings a waiting trolley back."""
    st=U1_STATE[(floor,branch)];trolley=st['active_trolley']
    def drop():
        # Keep the empty cartons visibly stacked on the trolley while parked in Khu 06.
        K06_TROLLEY_CARTONS[floor][trolley]=('u1',list(cartons_in_trolley))
        loose_event('U1_TROLLEY_DELIVERED_TO_K06',floor=floor,branch=branch,trolley=trolley)
    hp=u1_home(floor,branch)
    tp=LG.u1_trolley_pose(floor,branch)
    pool=U1_AGV_POOLS[floor]
    agv_id=await pool.acquire(hp[:2])
    if agv_id is None:return
    try:
        st['active_trolley']=await _tracked_agv_mission(
            agv_id,f'UNP-U1T-T{floor}N{branch+1}','Khu Unpack U1','Đổi xe trolley rỗng tại Khu 06',trolley,
            trolley_exchange(floor,agv_id,hp,tp,trolley,
                out_contents=lambda tp:{c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(cartons_in_trolley)},on_drop=drop,
                on_pick=lambda t:_k06_release_trolley_cartons(floor,t),prefer_loaded=False,
                speed=float(C.get('u1_agv_speed',.45))),
            pick_location=f"Bàn U1-T{floor}-N{branch+1} ({tp[0]:.2f}, {tp[1]:.2f})",
            drop_location=f"Khu 06 <-> Bàn U1-T{floor}-N{branch+1}")
    finally:
        await pool.release(agv_id)
    st['trolley_cartons']=[];st['trolley_swaps']+=1
    loose_event('U1_REPLACEMENT_TROLLEY_READY',floor=floor,branch=branch,trolley=st['active_trolley'])

async def u1_agv_swap_cycle(floor,branch,full_rack,meds_on_rack,store_cell,next_rack,spare_cell,old_spare_meds=()):
    """Pooled U1 AGV stores the full rack (+ medicine boxes) in an empty Kho 04 cell and brings the spare rack back."""
    def stored():
        # Keep yellow medicine boxes on full_rack in Kho 04 intact!
        loose_event('AGV_RACK_STORED',floor=floor,branch=branch,rack=full_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
    def pickup_spare():
        # Only clear old_spare_meds when AGV actually arrives at spare_cell to pick up next_rack for reuse.
        if old_spare_meds:
            send({m:(spare_cell['x'],spare_cell['y'],-5,0) for m,_,_ in old_spare_meds},transmit=not args.fast)
    loose_event('AGV_REPLACEMENT_RACK_PICKUP',floor=floor,branch=branch,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    hp=u1_home(floor,branch)
    pool=U1_AGV_POOLS[floor]
    agv_id=await pool.acquire(hp[:2])
    if agv_id is None:return
    try:
        await _tracked_agv_mission(
            agv_id,f'UNP-U1R-T{floor}N{branch+1}','Khu Unpack U1','Cất rack đầy vào Kho 04 & lấy rack rỗng',full_rack,
            rack_swap(floor,agv_id,hp,full_rack,
                lambda rp:{m:LG.u1_med_on_rack(rp,mi,ts) for m,mi,ts in meds_on_rack},store_cell,next_rack,spare_cell,
                speed=float(C.get('u1_agv_speed',.45)),on_stored=stored,on_pickup_spare=pickup_spare),
            pick_location=f"Bàn U1-T{floor}-N{branch+1} ({hp[0]:.2f}, {hp[1]:.2f})",
            drop_location=f"Kho 04 ({store_cell['x']:.2f}, {store_cell['y']:.2f}) & đổi {next_rack}")
    finally:
        await pool.release(agv_id)
    loose_event('AGV_REPLACEMENT_RACK_READY',floor=floor,branch=branch,rack=next_rack)

async def carton_trip(name,dest,branch):
    b=branch%len(LG.BRANCHES)
    pts=LG.route(dest,poses[name][0],b)
    await follow_path(name,pts,C['carton_conveyor_speed'])
    # Stop at the sorting-belt end, then worker opens carton and places yellow medicine boxes into the hollow blue tray on the U1 rack.
    loose_event('CARTON_WAITING_UNPACK',carton=name,floor=dest,branch=b)
    u1_wkey=f'u1_{dest}_{b}'
    t_arr_u1=scheduler.time
    async with U1_LOCKS[(dest,b)]:
        t_got_u1=scheduler.time
        if t_got_u1>t_arr_u1:
            tracker.record_worker_wait_span(u1_wkey,t_arr_u1,t_got_u1)
        st=U1_STATE[(dest,b)]
        rack=st['active_rack']
        rack_idx=st['active_rack_idx']
        tray_slot=len(st['cartons'])
        trolley_slot=len(st['trolley_cartons'])
        rack_pose=LG.u1_rack_pose(dest,b)
        loose_event('WORKER_OPEN_CARTON',carton=name,floor=dest,branch=b,rack=rack)
        placed_meds=await u1_operator_cycle(dest,b,name,rack_pose,rack_idx,trolley_slot,tray_slot)
        st['cartons'].append((name,placed_meds))
        st['trolley_cartons'].append(name)
        loose_event('CARTON_PLACED_ON_RACK',carton=name,floor=dest,branch=b,rack=rack,slot=tray_slot,med_boxes=[m[0] for m in placed_meds])
        if len(st['cartons'])>=U1_PER_RACK:
            store_cell=st['empty_cell']
            next_rack,next_rack_idx,spare_cell,old_meds=st['spares'].pop(0)
            carried_meds=[item for _,meds in st['cartons'] for item in meds]
            loose_event('AGV_RACK_PICKUP',floor=dest,branch=b,rack=rack,cartons=[c for c,_ in st['cartons']])
            t_swap0=scheduler.time
            await u1_agv_swap_cycle(dest,b,rack,carried_meds,store_cell,next_rack,spare_cell,old_meds)
            t_swap1=scheduler.time
            if t_swap1>t_swap0:
                tracker.record_worker_wait_span(u1_wkey,t_swap0,t_swap1)
            st['spares'].append((rack,rack_idx,store_cell,carried_meds))
            st['active_rack']=next_rack
            st['active_rack_idx']=next_rack_idx
            st['empty_cell']=spare_cell
            st['cartons']=[]
            st['swaps']+=1
        if len(st['trolley_cartons'])>=TROLLEY_CAP:
            t_swap0=scheduler.time
            await u1_trolley_swap_cycle(dest,b,list(st['trolley_cartons']))
            t_swap1=scheduler.time
            if t_swap1>t_swap0:
                tracker.record_worker_wait_span(u1_wkey,t_swap0,t_swap1)
    loose_stats['cartons_delivered']+=1
    loose_stats['per_floor'][dest]=loose_stats['per_floor'].get(dest,0)+1

# ---------------------------------------------------------------- Zone 05 (Unpack Pallet) -> U2 -> Kho 04
async def _z05_move_worker(floor,worker_idx,dur,x=None,y=None,yaw=None,bend=None,arm=None,carry_carton=None,carton_to=None,walk=False):
    wid=LG.z05_worker_id(floor,worker_idx)
    z0=LG.LEVELS[floor-1]
    state=Z05_FLOOR_STATE[floor]['worker_poses'][worker_idx]
    a=dict(state)
    b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
       'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
       'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
    dist=math.hypot(b['x']-a['x'],b['y']-a['y'])
    if walk:
        walk_speed=max(.1,float(C.get('z05_walk_speed',1.1)))
        dur=max(.04,dist/walk_speed)
    steps=max(1,round(dist/STEP)) if walk else 0
    upper_only=(not walk and abs(b['yaw']-a['yaw'])<1e-6 and dist<1e-6)
    c_name=carry_carton or (carton_to[0] if carton_to else None)
    c0=poses[c_name] if c_name else None
    def at(q):
        k={key:a[key]+(b[key]-a[key])*q for key in a}
        out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.35*math.sin(math.pi*steps*q) if walk else 0.,z0=z0,upper_only=upper_only)
        if carry_carton:
            out[carry_carton]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
        elif carton_to is not None:
            t_name,t_pose=carton_to
            out[t_name]=tuple(c0[j]+(t_pose[j]-c0[j])*q for j in range(4))
        return out
    end=at(1.)
    await engine.move(end,duration=max(.04,dur),transform=at,shape=(lambda t:t) if walk else blend,exact=True)
    state.update(b)

async def _z05_operator_deliver_one(floor,worker_idx,batch,carton,stack_pos,u2_idx,wait_intervals=None):
    """Zone 05 worker_idx (0 or 1) walks at constant speed z05_walk_speed to batch['spot_xy'] (whichever Zone 05
    unpack zone has goods), picks carton, walks at constant speed z05_walk_speed to U2 station u2_idx (0..5),
    and places the carton on the floor in front of the U2 worker."""
    z0=LG.LEVELS[floor-1]
    state=Z05_FLOOR_STATE[floor]['worker_poses'][worker_idx]
    px,py,pyaw=LG.z05_pallet_side(batch['spot_xy'],worker_idx)
    raw_drop=LG.u2_carton_drop_pose(floor,u2_idx)
    dx_stand,dy_stand,dyaw=raw_drop[0]+.52,raw_drop[1],math.pi
    reach=LG.reach_for(stack_pos[2]-z0)
    drop_bend,drop_arm=.84,.54
    if math.hypot(px-state['x'],py-state['y'])>1e-3:
        head_to_pallet=math.atan2(py-state['y'],px-state['x'])
        await _z05_move_worker(floor,worker_idx,.22,yaw=head_to_pallet)
        await _z05_move_worker(floor,worker_idx,0.,x=px,y=py,walk=True)
    await _z05_move_worker(floor,worker_idx,.18,yaw=pyaw)
    await _z05_move_worker(floor,worker_idx,.55,bend=reach[0],arm=reach[1])
    grab=LG.held_carton(px,py,pyaw,*reach,z0=z0)
    await _z05_move_worker(floor,worker_idx,.25,carton_to=(carton,grab))
    await _z05_move_worker(floor,worker_idx,.40,bend=CARRY[0],arm=CARRY[1],carry_carton=carton)
    if not batch['items']:
        if not batch.get('is_partial') and batch.get('hide_deck_on_picked',True):
            send({batch['deck']:(batch['spot_xy'][0],batch['spot_xy'][1],-5,0)},transmit=not args.fast)
        pe=batch.get('picked_event')
        if pe is not None:
            pe.set()
    head_to_u2=math.atan2(dy_stand-py,dx_stand-px)
    await _z05_move_worker(floor,worker_idx,.22,yaw=head_to_u2,carry_carton=carton)
    await _z05_move_worker(floor,worker_idx,0.,x=dx_stand,y=dy_stand,carry_carton=carton,walk=True)
    await _z05_move_worker(floor,worker_idx,.18,yaw=dyaw,carry_carton=carton)
    t_u2w0=scheduler.time
    async with U2_DELIVERY_LOCKS[(floor,u2_idx)]:
        t_u2w1=scheduler.time
        if wait_intervals is not None and t_u2w1>t_u2w0:
            wait_intervals.append((t_u2w0,t_u2w1))
        slot_off=(0.0,-0.28,0.28)[Z05_FLOOR_STATE[floor]['u2_queued'][u2_idx]%3]
        drop_pose=(raw_drop[0],raw_drop[1]+slot_off,raw_drop[2],raw_drop[3])
        await _z05_move_worker(floor,worker_idx,.40,bend=drop_bend,arm=drop_arm,carry_carton=carton)
        await _z05_move_worker(floor,worker_idx,.25,carton_to=(carton,drop_pose))
        await _z05_move_worker(floor,worker_idx,.30,bend=0.,arm=0.)
        Z05_FLOOR_STATE[floor]['u2_queued'][u2_idx]+=1
        loose_event('Z05_CARTON_DELIVERED_TO_U2',job=batch['job'],floor=floor,zone=batch['zone'],worker=worker_idx,carton=carton,u2_station=u2_idx)
        t=asyncio.create_task(u2_carton_trip(floor,u2_idx,carton,batch['job']))
        Z05_FLOOR_STATE[floor]['u2_active_task'][u2_idx]=t
        background_tasks.append(t)
        batch['u2_tasks'].append(t)
        if len(batch['u2_tasks'])>=batch['total_items']:
            batch['done_event'].set()

async def z05_operator_deliver_one(floor,worker_idx,*args,**kwargs):
    wait_intervals=[]
    return await _run_busy_worker(LG.z05_worker_id(floor,worker_idx),f'z05_{floor}_{worker_idx}',_z05_operator_deliver_one(floor,worker_idx,*args,wait_intervals=wait_intervals,**kwargs),wait_intervals=wait_intervals)

async def z05_worker_loop(floor,worker_idx):
    """Flexible Zone 05 worker coroutine: moves to whichever Zone 05 unpack zone (0 or 1) has cartons waiting,
    acquires Z05_WORKER_LOCKS per single carton (never across whole batches or U2 waits), and yields immediately
    whenever a pallet jack job needs to pull a waiting pallet from the output gate or return a partial pallet."""
    fst=Z05_FLOOR_STATE[floor]
    n_u2=len(LG.U2_WORKERS)
    while not STOP_REQUESTED:
        if Z05_JACK_PENDING[(floor,worker_idx)]>0:
            async with _z05_jack_cond():
                while Z05_JACK_PENDING[(floor,worker_idx)]>0 and not STOP_REQUESTED:
                    await _z05_jack_cond().wait()
            if STOP_REQUESTED:break
        batch=None
        n_zones=max(1,len(UNPACK_SPOTS))
        w_per_zone=max(1,len(LG.Z05_WORKERS)//n_zones)
        own_zone=min(n_zones-1,worker_idx//w_per_zone)
        zone_order=[own_zone]+[z for z in range(n_zones) if z!=own_zone]
        local_slot=worker_idx%w_per_zone
        for z_cand in zone_order:
            n_sp=len(UNPACK_SPOTS[z_cand])
            spot_order=sorted(range(n_sp),key=lambda s:(s-local_slot)%max(1,n_sp))
            for s_cand in spot_order:
                b_cand=fst['spots'].get((z_cand,s_cand))
                if b_cand is not None and b_cand['items']:
                    batch=b_cand
                    break
            if batch is not None:
                break
        if batch is None:
            if Z05_JACK_PENDING[(floor,worker_idx)]==0:
                async with Z05_WORKER_LOCKS[(floor,worker_idx)]:
                    if Z05_JACK_PENDING[(floor,worker_idx)]==0 and not any(
                        b_c is not None and b_c['items'] for b_c in fst['spots'].values()
                    ):
                        sx,sy,syaw,_=LG.z05_station(floor,worker_idx)
                        state=fst['worker_poses'][worker_idx]
                        if math.hypot(sx-state['x'],sy-state['y'])>1e-3:
                            head_home=math.atan2(sy-state['y'],sx-state['x'])
                            await _z05_move_worker(floor,worker_idx,.22,yaw=head_home)
                            await _z05_move_worker(floor,worker_idx,0.,x=sx,y=sy,walk=True)
                        await _z05_move_worker(floor,worker_idx,.18,yaw=syaw,bend=0.,arm=0.)
            if not any(b_c is not None and b_c['items'] for b_c in fst['spots'].values()):
                break
            continue
        u2_idx=None
        t_fw0=scheduler.time
        cond=_u2_floor_cond(floor)
        while u2_idx is None and not STOP_REQUESTED:
            if Z05_JACK_PENDING[(floor,worker_idx)]>0:
                break
            best_cand=None;best_load=U2_STAGING_CAP
            for k in range(n_u2):
                cand=(fst['next_u2']+k)%n_u2
                load=fst['u2_queued'][cand]+(1 if fst['u2_reserved'][cand] else 0)
                if load<best_load:
                    best_load=load;best_cand=cand
                    if load==0:
                        break
            if best_cand is not None:
                u2_idx=best_cand
                fst['u2_reserved'][u2_idx]=True
                fst['next_u2']=(u2_idx+1)%n_u2
                break
            async with cond:
                if STOP_REQUESTED or Z05_JACK_PENDING[(floor,worker_idx)]>0:
                    break
                if any((fst['u2_queued'][c]+(1 if fst['u2_reserved'][c] else 0))<U2_STAGING_CAP for c in range(n_u2)):
                    continue
                await cond.wait()
        t_fw1=scheduler.time
        if t_fw1>t_fw0:
            tracker.record_worker_wait_span(f'z05_{floor}_{worker_idx}',t_fw0,t_fw1)
        if STOP_REQUESTED:
            if u2_idx is not None:
                fst['u2_reserved'][u2_idx]=False
            break
        if u2_idx is None:
            continue
        try:
            if Z05_JACK_PENDING[(floor,worker_idx)]>0 or not batch['items']:
                continue
            async with Z05_WORKER_LOCKS[(floor,worker_idx)]:
                if Z05_JACK_PENDING[(floor,worker_idx)]>0 or not batch['items']:
                    continue
                carton,stack_pos=batch['items'].pop(0)
                await z05_operator_deliver_one(floor,worker_idx,batch,carton,stack_pos,u2_idx)
        finally:
            fst['u2_reserved'][u2_idx]=False

async def _u2_operator_cycle(floor,u2_idx,carton,rack_pose,rack_idx,trolley_slot,tray_slot=0):
    """U2 put-away worker at (floor, u2_idx):
    1. Bends down and opens the carton placed on the floor in front of them, revealing 3 yellow medicine boxes inside.
    2. Picks each yellow medicine box (3 turns) from the carton on the ground, turns 180 deg, and places it into the hollow blue tray on the U2 rack behind them.
    3. Picks up the empty carton shell from the floor and places it into the adjacent U2 Trolley."""
    wid=LG.u2_worker_id(floor,u2_idx)
    sx,sy,syaw,z0=LG.u2_station(floor,u2_idx)
    open_s=float(C.get('u2_open_carton_s',4.0))
    pick_s=float(C.get('u2_pick_box_s',6.0))
    d=[f*pick_s for f in U1_PHASES]
    pick_x=sx+.22;pick_bend,pick_arm=.86,.52
    place_x=sx-.10;place_bend,place_arm=.14,1.14
    med_count=int(C.get('u2_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    med_offset=tray_slot*med_count
    med_names=[f'u2_med_{floor}_{u2_idx}_{rack_idx}_{med_offset+m}' for m in range(med_count)]
    c_pose=poses[carton]
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry_item=None,item_to=None,walk=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        upper_only=not walk
        item_name=carry_item or (item_to[0] if item_to else None)
        i0=poses[item_name] if item_name else None
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.30*math.sin(math.pi*steps*q) if walk else 0.,z0=z0,upper_only=upper_only)
            if carry_item:
                out[carry_item]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
            elif item_to is not None:
                t_name,t_pose=item_to
                arc_z=0.06*math.sin(math.pi*q) if ('med' in t_name or 'carton' in t_name) else 0.
                cz=i0[2]+(t_pose[2]-i0[2])*q+arc_z
                out[t_name]=(i0[0]+(t_pose[0]-i0[0])*q,i0[1]+(t_pose[1]-i0[1])*q,cz,i0[3]+(t_pose[3]-i0[3])*q)
            return out
        end=at(1.)
        await engine.move(end,duration=max(.04,dur),transform=at,shape=(lambda t:t) if walk else blend,exact=True)
        state.update(b)
    # 1. Step to carton on the ground and bend down to open it, revealing 3 yellow medicine boxes inside
    await phase(open_s*.45,x=pick_x,bend=pick_bend,arm=pick_arm,walk=True)
    send({med_names[m]:LG.u2_med_in_carton(c_pose,m) for m in range(med_count)},transmit=not args.fast)
    await phase(open_s*.55,x=pick_x,bend=pick_bend-.06,arm=pick_arm+.12)
    # 2. Pick each yellow medicine box (3 turns), turn 180 deg, and place into the hollow blue tray on the U2 rack
    placed=[]
    for m,m_name in enumerate(med_names):
        box_i=1 if med_count==1 else m
        target_on_rack=LG.u1_med_on_rack(rack_pose,box_i,tray_slot)
        place_yaw=math.atan2(target_on_rack[1]-sy,target_on_rack[0]-sx)
        await phase(d[0],x=pick_x,y=sy,yaw=syaw,bend=pick_bend,arm=pick_arm,walk=True)
        grab=LG.held_carton(pick_x,sy,syaw,pick_bend,pick_arm,z0=z0)
        await phase(d[1],item_to=(m_name,grab))
        await phase(d[2],x=sx,y=sy,bend=CARRY[0],arm=CARRY[1],carry_item=m_name,walk=True)
        await phase(d[3],yaw=place_yaw,carry_item=m_name)
        await phase(d[4],x=place_x,y=target_on_rack[1],bend=place_bend,arm=place_arm,carry_item=m_name,walk=True)
        await phase(d[5],item_to=(m_name,target_on_rack))
        placed.append((m_name,box_i,tray_slot))
        loose_event('U2_MED_BOX_PLACED_IN_TRAY',carton=carton,med_box=m_name,box_index=m,floor=floor,u2_station=u2_idx)
        await phase(d[6],x=sx,y=sy,bend=0.,arm=0.,walk=True)
        await phase(d[7],yaw=syaw)
    # 3. Pick up the empty carton shell from the floor and place it into the adjacent U2 Trolley
    t_pose=LG.u2_trolley_pose(floor,u2_idx)
    target_in_trolley=LG.trolley_carton_pose(t_pose,trolley_slot)
    t_yaw=math.atan2(target_in_trolley[1]-sy,target_in_trolley[0]-sx)
    await phase(d[0],x=pick_x,y=sy,yaw=syaw,bend=pick_bend,arm=pick_arm,walk=True)
    grab_c=LG.held_carton(pick_x,sy,syaw,pick_bend,pick_arm,z0=z0)
    await phase(d[1],item_to=(carton,grab_c))
    await phase(d[2],x=sx,y=sy,bend=CARRY[0],arm=CARRY[1],carry_item=carton,walk=True)
    await phase(d[3],yaw=t_yaw,carry_item=carton)
    reach_bend,reach_arm=LG.reach_for(target_in_trolley[2]-z0)
    await phase(d[4],bend=reach_bend,arm=reach_arm,carry_item=carton)
    await phase(d[5],item_to=(carton,target_in_trolley))
    await phase(d[6],x=sx,y=sy,yaw=syaw,bend=0.,arm=0.)
    loose_event('U2_EMPTY_CARTON_IN_TROLLEY',carton=carton,floor=floor,u2_station=u2_idx,slot=trolley_slot)
    return placed

async def u2_operator_cycle(floor,u2_idx,*args,**kwargs):
    return await _run_busy_worker(LG.u2_worker_id(floor,u2_idx),f'u2_{floor}_{u2_idx}',_u2_operator_cycle(floor,u2_idx,*args,**kwargs))

async def u2_trolley_swap_cycle(floor,u2_idx,cartons_in_trolley):
    """When the U2 trolley holds 5 empty cartons, a pooled U2 AGV takes it to Khu 06 and brings a waiting trolley back."""
    st=U2_STATE[(floor,u2_idx)];trolley=st['active_trolley']
    def drop():
        # Keep the empty cartons visibly stacked on the trolley while parked in Khu 06.
        K06_TROLLEY_CARTONS[floor][trolley]=('u2',list(cartons_in_trolley))
        loose_event('U2_TROLLEY_DELIVERED_TO_K06',floor=floor,u2_station=u2_idx,trolley=trolley)
    hp=u2_home(floor,u2_idx)
    tp=LG.u2_trolley_pose(floor,u2_idx)
    pool=U2_AGV_POOLS[floor]
    agv_id=await pool.acquire(hp[:2])
    if agv_id is None:return
    try:
        st['active_trolley']=await _tracked_agv_mission(
            agv_id,f'UNP-U2T-T{floor}S{u2_idx+1}','Khu Unpack U2 (Zone 05)','Đổi xe trolley rỗng U2 tại Khu 06',trolley,
            trolley_exchange(floor,agv_id,hp,tp,trolley,
                out_contents=lambda tp:{c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(cartons_in_trolley)},on_drop=drop,
                on_pick=lambda t:_k06_release_trolley_cartons(floor,t),prefer_loaded=False,
                speed=float(C.get('u2_agv_speed',C.get('u1_agv_speed',.45)))),
            pick_location=f"Trạm U2-T{floor}-S{u2_idx+1} ({tp[0]:.2f}, {tp[1]:.2f})",
            drop_location=f"Khu 06 <-> Trạm U2-T{floor}-S{u2_idx+1}")
    finally:
        await pool.release(agv_id)
    st['trolley_cartons']=[];st['trolley_swaps']+=1
    loose_event('U2_REPLACEMENT_TROLLEY_READY',floor=floor,u2_station=u2_idx,trolley=st['active_trolley'])

async def u2_agv_swap_cycle(floor,u2_idx,full_rack,meds_on_rack,store_cell,next_rack,spare_cell,old_spare_meds=()):
    """Pooled U2 AGV stores the full rack (+ medicine boxes) in an empty Kho 04 cell and brings the spare rack back."""
    def stored():
        # Keep yellow medicine boxes on full_rack in Kho 04 intact!
        loose_event('U2_AGV_RACK_STORED',floor=floor,u2_station=u2_idx,rack=full_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
    def pickup_spare():
        # Only clear old_spare_meds when AGV actually arrives at spare_cell to pick up next_rack for reuse.
        if old_spare_meds:
            send({m:(spare_cell['x'],spare_cell['y'],-5,0) for m,_,_ in old_spare_meds},transmit=not args.fast)
    loose_event('U2_AGV_REPLACEMENT_RACK_PICKUP',floor=floor,u2_station=u2_idx,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    hp=u2_home(floor,u2_idx)
    pool=U2_AGV_POOLS[floor]
    agv_id=await pool.acquire(hp[:2])
    if agv_id is None:return
    try:
        await _tracked_agv_mission(
            agv_id,f'UNP-U2R-T{floor}S{u2_idx+1}','Khu Unpack U2 (Zone 05)','Cất rack U2 vào Kho 04 & lấy rack rỗng',full_rack,
            rack_swap(floor,agv_id,hp,full_rack,
                lambda rp:{m:LG.u1_med_on_rack(rp,mi,ts) for m,mi,ts in meds_on_rack},store_cell,next_rack,spare_cell,
                speed=float(C.get('u2_agv_speed',C.get('u1_agv_speed',.45))),on_stored=stored,on_pickup_spare=pickup_spare),
            pick_location=f"Trạm U2-T{floor}-S{u2_idx+1} ({hp[0]:.2f}, {hp[1]:.2f})",
            drop_location=f"Kho 04 ({store_cell['x']:.2f}, {store_cell['y']:.2f}) & đổi {next_rack}")
    finally:
        await pool.release(agv_id)
    loose_event('U2_AGV_REPLACEMENT_RACK_READY',floor=floor,u2_station=u2_idx,rack=next_rack)

async def u2_carton_trip(floor,u2_idx,carton,job):
    """U2 station cycle once a carton has been placed on the floor in front of U2 worker u2_idx."""
    u2_wkey=f'u2_{floor}_{u2_idx}'
    fst=Z05_FLOOR_STATE[floor]
    try:
        t_arr_u2=scheduler.time
        async with U2_LOCKS[(floor,u2_idx)]:
            t_got_u2=scheduler.time
            if t_got_u2>t_arr_u2:
                tracker.record_worker_wait_span(u2_wkey,t_arr_u2,t_got_u2)
            st=U2_STATE[(floor,u2_idx)]
            rack=st['active_rack']
            rack_idx=st['active_rack_idx']
            tray_slot=len(st['cartons'])
            trolley_slot=len(st['trolley_cartons'])
            rack_pose=LG.u2_rack_pose(floor,u2_idx)
            loose_event('U2_WORKER_OPEN_CARTON',job=job,carton=carton,floor=floor,u2_station=u2_idx,rack=rack)
            placed_meds=await u2_operator_cycle(floor,u2_idx,carton,rack_pose,rack_idx,trolley_slot,tray_slot)
            st['cartons'].append((carton,placed_meds))
            st['trolley_cartons'].append(carton)
            loose_event('U2_CARTON_PLACED_ON_RACK',job=job,carton=carton,floor=floor,u2_station=u2_idx,rack=rack,slot=tray_slot,med_boxes=[m[0] for m in placed_meds])
            if len(st['cartons'])>=U2_PER_RACK:
                store_cell=st['empty_cell']
                next_rack,next_rack_idx,spare_cell,old_meds=st['spares'].pop(0)
                carried_meds=[item for _,meds in st['cartons'] for item in meds]
                loose_event('U2_AGV_RACK_PICKUP',job=job,floor=floor,u2_station=u2_idx,rack=rack,cartons=[c for c,_ in st['cartons']])
                t_swap0=scheduler.time
                await u2_agv_swap_cycle(floor,u2_idx,rack,carried_meds,store_cell,next_rack,spare_cell,old_meds)
                t_swap1=scheduler.time
                if t_swap1>t_swap0:
                    tracker.record_worker_wait_span(u2_wkey,t_swap0,t_swap1)
                st['spares'].append((rack,rack_idx,store_cell,carried_meds))
                st['active_rack']=next_rack
                st['active_rack_idx']=next_rack_idx
                st['empty_cell']=spare_cell
                st['cartons']=[]
                st['swaps']+=1
            if len(st['trolley_cartons'])>=TROLLEY_CAP:
                t_swap0=scheduler.time
                await u2_trolley_swap_cycle(floor,u2_idx,list(st['trolley_cartons']))
                t_swap1=scheduler.time
                if t_swap1>t_swap0:
                    tracker.record_worker_wait_span(u2_wkey,t_swap0,t_swap1)
    finally:
        fst['u2_queued'][u2_idx]=max(0,fst['u2_queued'][u2_idx]-1)
        cond=fst.get('u2_cond')
        if cond is not None:
            async with cond:
                cond.notify_all()

# ---------------------------------------------------------------- Khu P1 (14 Picking Stations: North 0..6 & West 7..13) <-> Kho 04 & Khu 06
async def _p1_operator_cycle(floor,st_idx,carton,rack_idx,tray_slot=0):
    """P1 picking worker at (floor, st_idx):
    1. Turns to the P1 Trolley (at st['mark']), picks 1 empty carton shell, and places it onto the working edge of the desk.
    2. Picks 3 yellow medicine box clusters (m=0..2) from tray_slot on the loaded Rack in front of the station and places them into the carton on the desk.
    3. Picks up the packed carton, moves it aside onto the far end of the desk, and the packed carton disappears."""
    wid=LG.p1_worker_id(floor,st_idx)
    st=P1_STATE[(floor,st_idx)]
    sx,sy,syaw,z0=LG.p1_station(floor,st_idx)
    rx,ry,_,_=LG.p1_rack_pose(floor,st_idx)
    tx,ty,_,_=LG.p1_trolley_pose(floor,st_idx)
    active_c_pose=LG.p1_active_carton_pose(floor,st_idx)
    aside_c_pose=LG.p1_aside_carton_pose(floor,st_idx)
    if st.get('aside_carton'):
        prev_c,prev_meds=st.pop('aside_carton')
        send({prev_c:(aside_c_pose[0],aside_c_pose[1],-5,0),**{m:(aside_c_pose[0],aside_c_pose[1],-5,0) for m in prev_meds}},transmit=not args.fast)
    pick_s=float(C.get('p1_pick_box_s',4.5))
    d=[f*pick_s for f in U1_PHASES]
    med_count=int(C.get('p1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    med_offset=tray_slot*med_count
    med_names=[f'p1_med_{floor}_{st_idx}_{med_offset+m}' for m in range(med_count)]
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry_item=None,item_to=None,walk=False,pack_meds=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        upper_only=not walk
        item_name=carry_item or (item_to[0] if item_to else None)
        i0=poses[item_name] if item_name else None
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.30*math.sin(math.pi*steps*q) if walk else 0.,z0=z0,upper_only=upper_only)
            if carry_item:
                out[carry_item]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
                if pack_meds and carry_item==carton:
                    for m,m_name in enumerate(med_names):
                        out[m_name]=LG.p1_med_in_carton(out[carton],m)
            elif item_to is not None:
                t_name,t_pose=item_to
                arc_z=0.06*math.sin(math.pi*q) if ('med' in t_name or 'carton' in t_name) else 0.
                cz=i0[2]+(t_pose[2]-i0[2])*q+arc_z
                out[t_name]=(i0[0]+(t_pose[0]-i0[0])*q,i0[1]+(t_pose[1]-i0[1])*q,cz,i0[3]+(t_pose[3]-i0[3])*q)
                if pack_meds and t_name==carton:
                    for m,m_name in enumerate(med_names):
                        out[m_name]=LG.p1_med_in_carton(out[carton],m)
            return out
        end=at(1.)
        await engine.move(end,duration=max(.04,dur),transform=at,shape=(lambda t:t) if walk else blend,exact=True)
        state.update(b)
    # 1. Pick 1 empty carton from the P1 Trolley and place it on the working edge of the desk
    trolley_yaw=math.atan2(ty-sy,tx-sx)
    desk_yaw=math.atan2(active_c_pose[1]-sy,active_c_pose[0]-sx)
    await phase(d[0],yaw=trolley_yaw,bend=.22,arm=.88)
    grab_c=LG.held_carton(sx,sy,trolley_yaw,.22,.88,z0=z0)
    await phase(d[1],item_to=(carton,grab_c))
    await phase(d[2],yaw=desk_yaw,bend=CARRY[0],arm=CARRY[1],carry_item=carton)
    await phase(d[3],bend=.25,arm=.85,carry_item=carton)
    await phase(d[4],item_to=(carton,active_c_pose))
    loose_event('P1_EMPTY_CARTON_PLACED_ON_DESK',floor=floor,p1_station=st_idx,carton=carton)
    # 2. Pick 3 yellow medicine box clusters from the P1 Rack and place them into the carton on the desk
    rack_yaw=math.atan2(ry-sy,rx-sx)
    for m,m_name in enumerate(med_names):
        target_in_c=LG.p1_med_in_carton(active_c_pose,m)
        await phase(d[0],yaw=rack_yaw,bend=.14,arm=1.10)
        grab_m=LG.held_carton(sx,sy,rack_yaw,.14,1.10,z0=z0)
        await phase(d[1],item_to=(m_name,grab_m))
        await phase(d[2],yaw=desk_yaw,bend=CARRY[0],arm=CARRY[1],carry_item=m_name)
        await phase(d[3],bend=.26,arm=.82,carry_item=m_name)
        await phase(d[4],item_to=(m_name,target_in_c))
        loose_event('P1_MED_BOX_PACKED',floor=floor,p1_station=st_idx,carton=carton,med_box=m_name,box_index=m)
    # 3. Move packed carton aside onto the desk, keeping medicine boxes inside until destination
    aside_yaw=math.atan2(aside_c_pose[1]-sy,aside_c_pose[0]-sx)
    await phase(d[0],yaw=desk_yaw,bend=.25,arm=.85)
    grab_packed=LG.held_carton(sx,sy,desk_yaw,.25,.85,z0=z0)
    await phase(d[1],item_to=(carton,grab_packed),pack_meds=True)
    lean_x=sx-.25 if st_idx<7 else sx
    lean_y=sy if st_idx<7 else sy+.25
    await phase(d[2],x=lean_x,y=lean_y,yaw=aside_yaw,bend=.36,arm=1.10,carry_item=carton,walk=True,pack_meds=True)
    await phase(d[3],item_to=(carton,aside_c_pose),pack_meds=True)
    loose_event('P1_PACKED_CARTON_COMPLETED',floor=floor,p1_station=st_idx,carton=carton)
    await phase(d[4],x=sx,y=sy,yaw=syaw,bend=0.,arm=0.,walk=True)
    st['aside_carton']=(carton,list(med_names))

async def p1_operator_cycle(floor,st_idx,*args,**kwargs):
    return await _run_busy_worker(LG.p1_worker_id(floor,st_idx),f'p1_{floor}_{st_idx}',_p1_operator_cycle(floor,st_idx,*args,**kwargs))

def _take_k04_stock(floor,st_idx):
    """Pop an available Kho 04 loaded stock rack (preferring the station's own corridor row) for P1 outbound picking."""
    pool=K04_STOCK_POOL.get(floor,[])
    if not pool:return None
    _,_,corr_y=LG.P1_CELL_MAP[st_idx]
    rx,ry,_,_=LG.p1_rack_pose(floor,st_idx)
    best_i=min(range(len(pool)),key=lambda i:(0 if abs(LG._cell_corridor_y(pool[i][1]['y'],corr_y)-corr_y)<0.1 else 1,math.hypot(pool[i][1]['x']-rx,pool[i][1]['y']-ry)))
    return pool.pop(best_i)

async def p1_agv_rack_swap_cycle(floor,st_idx,empty_rack,store_cell,next_rack,next_rack_idx,spare_cell,from_k04_stock=False,shell_rack=None):
    """After ALL trays on the P1 rack are picked empty, a pooled P1 AGV stores the empty rack in Kho 04 and brings
    the loaded stock rack (yellow medicine boxes) from Kho 04 to the station."""
    med_count=int(C.get('p1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    total_meds=P1_PER_RACK*med_count
    next_meds=[f'p1_med_{floor}_{st_idx}_{k}' for k in range(total_meds)]
    def stored():
        loose_event('P1_AGV_EMPTY_RACK_STORED',floor=floor,p1_station=st_idx,rack=empty_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
        if not from_k04_stock:
            sp=(spare_cell['x'],spare_cell['y'],LG.LEVELS[floor-1],poses[next_rack][3] if len(poses[next_rack])>3 else 0.)
            send({m:LG.u1_med_on_rack(sp,1 if med_count==1 else (k%med_count),k//med_count) for k,m in enumerate(next_meds)},transmit=not args.fast)
        loose_event('P1_AGV_LOADED_RACK_PICKUP',floor=floor,p1_station=st_idx,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    hp=p1_home(floor,st_idx)
    row=0 if st_idx<7 else 1
    pool=P1_AGV_POOLS[(floor,row)]
    agv_id=await pool.acquire(hp[:2])
    if agv_id is None:return
    try:
        next_contents=None if from_k04_stock else (lambda rp:{m:LG.u1_med_on_rack(rp,1 if med_count==1 else (k%med_count),k//med_count) for k,m in enumerate(next_meds)})
        await _tracked_agv_mission(
            agv_id,f'PK-P1R-T{floor}S{st_idx+1:02d}','Khu Đóng Gói P1','Cất rack rỗng vào Kho 04 & lấy rack hàng',empty_rack,
            rack_swap(floor,agv_id,hp,empty_rack,None,store_cell,next_rack,spare_cell,
                next_contents=next_contents,
                speed=float(C.get('p1_agv_speed',C.get('u1_agv_speed',.45))),on_stored=stored),
            pick_location=f"Trạm P1-T{floor}-S{st_idx+1:02d} ({hp[0]:.2f}, {hp[1]:.2f})",
            drop_location=f"Kho 04 ({store_cell['x']:.2f}, {store_cell['y']:.2f}) & lấy {next_rack}")
        if from_k04_stock and shell_rack is not None:
            rx,ry,z0,ryaw=LG.p1_rack_pose(floor,st_idx)
            rp=(rx,ry,z0,ryaw)
            swap_poses={next_rack:(spare_cell['x'],spare_cell['y'],-5.,0.),shell_rack:rp}
            for k,m in enumerate(next_meds):
                swap_poses[m]=LG.u1_med_on_rack(rp,1 if med_count==1 else (k%med_count),k//med_count)
            send(swap_poses,transmit=not args.fast)
    finally:
        await pool.release(agv_id)
    loose_event('P1_AGV_LOADED_RACK_READY',floor=floor,p1_station=st_idx,rack=shell_rack or next_rack)

async def p1_agv_trolley_swap_cycle(floor,st_idx):
    """When all 5 empty cartons of the P1 trolley are used, a pooled P1 AGV takes it to Khu 06 and brings back a
    waiting trolley loaded with 5 empty cartons."""
    st=P1_STATE[(floor,st_idx)];trolley=st['active_trolley']
    carton_names=[f'p1_carton_{floor}_{st_idx}_{c}' for c in range(TROLLEY_CAP)]
    def pick(t):   # restocked trolley in Khu 06: hand off U1/U2 cartons to P1 station carton models seamlessly
        _k06_release_trolley_cartons(floor,t)
        tp=poses[t];send({c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(carton_names)},transmit=not args.fast)
    def drop():
        loose_event('P1_EMPTY_TROLLEY_RETURNED_TO_K06',floor=floor,p1_station=st_idx,trolley=trolley)
    hp=p1_home(floor,st_idx)
    tp=LG.p1_trolley_pose(floor,st_idx)
    row=0 if st_idx<7 else 1
    pool=P1_AGV_POOLS[(floor,row)]
    agv_id=await pool.acquire(hp[:2])
    if agv_id is None:return
    try:
        st['active_trolley']=await _tracked_agv_mission(
            agv_id,f'PK-P1T-T{floor}S{st_idx+1:02d}','Khu Đóng Gói P1','Đổi trolley thùng rỗng P1 tại Khu 06',trolley,
            trolley_exchange(floor,agv_id,hp,tp,trolley,
                on_drop=drop,in_contents=lambda tp:{c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(carton_names)},on_pick=pick,
                prefer_loaded=True,
                speed=float(C.get('p1_agv_speed',C.get('u1_agv_speed',.45)))),
            pick_location=f"Trạm P1-T{floor}-S{st_idx+1:02d} ({tp[0]:.2f}, {tp[1]:.2f})",
            drop_location=f"Khu 06 <-> Trạm P1-T{floor}-S{st_idx+1:02d}")
    finally:
        await pool.release(agv_id)
    st['trolley_remaining']=list(reversed(carton_names))
    st['trolley_swaps']+=1
    loose_event('P1_FULL_TROLLEY_READY',floor=floor,p1_station=st_idx,trolley=st['active_trolley'])

async def _p1_do_rack_swap(floor,st_idx,st,row_sem,p1_wkey):
    store_cell=st['empty_cell']
    stock_pick=_take_k04_stock(floor,st_idx) if st.get('reserve_shells') else None
    if stock_pick is not None:
        next_rack,spare_cell=stock_pick
        shell_rack,next_rack_idx=st['reserve_shells'].pop(0)
        from_k04=True
    else:
        next_rack,next_rack_idx,spare_cell=st['spares'].pop(0)
        shell_rack=None
        from_k04=False
    t_swap0=scheduler.time
    async with row_sem:
        await p1_agv_rack_swap_cycle(floor,st_idx,st['active_rack'],store_cell,next_rack,next_rack_idx,spare_cell,from_k04_stock=from_k04,shell_rack=shell_rack)
    t_swap1=scheduler.time
    if t_swap1>t_swap0:
        tracker.record_worker_wait_span(p1_wkey,t_swap0,t_swap1)
    st['spares'].append((st['active_rack'],st['active_rack_idx'],store_cell))
    st['active_rack']=shell_rack if from_k04 else next_rack
    st['active_rack_idx']=next_rack_idx
    st['empty_cell']=spare_cell
    st['rack_remaining']=list(range(P1_PER_RACK))
    st['rack_swaps']+=1

async def p1_station_loop(floor,st_idx):
    """Continuous picking loop for P1 station (floor, st_idx) across both North (0..6) and West (7..13) rows."""
    row=0 if st_idx<7 else 1
    row_sem=P1_ROW_SEMAPHORES[(floor,row)]
    p1_wkey=f'p1_{floor}_{st_idx}'
    await engine.until(.5+.60*(st_idx%7)+.4*(floor-1))
    while p1_running:
        async with P1_LOCKS[(floor,st_idx)]:
            if not p1_running:break
            st=P1_STATE[(floor,st_idx)]
            if not st['trolley_remaining']:
                t_swap0=scheduler.time
                async with row_sem:
                    await p1_agv_trolley_swap_cycle(floor,st_idx)
                t_swap1=scheduler.time
                if t_swap1>t_swap0:
                    tracker.record_worker_wait_span(p1_wkey,t_swap0,t_swap1)
            if not st['rack_remaining'] and p1_running:
                await _p1_do_rack_swap(floor,st_idx,st,row_sem,p1_wkey)
            if not p1_running or not st['trolley_remaining'] or not st['rack_remaining']:
                break
            carton=st['trolley_remaining'].pop(0)
            tray_slot=st['rack_remaining'].pop(0)
            await p1_operator_cycle(floor,st_idx,carton,st['active_rack_idx'],tray_slot=tray_slot)
            st['cartons_packed']+=1
            # Call P1 AGV ONLY when the entire rack (all 4 trays / 12 boxes) or full 5-carton trolley is empty.
            if not st['rack_remaining'] and p1_running:
                await _p1_do_rack_swap(floor,st_idx,st,row_sem,p1_wkey)
            if not st['trolley_remaining'] and p1_running:
                t_swap0=scheduler.time
                async with row_sem:
                    await p1_agv_trolley_swap_cycle(floor,st_idx)
                t_swap1=scheduler.time
                if t_swap1>t_swap0:
                    tracker.record_worker_wait_span(p1_wkey,t_swap0,t_swap1)
        await engine.until(scheduler.time+.1)

_z05_pallet_seq=0

async def _pick_z05_worker(floor,preferred=0):
    """Pick the least-busy Zone 05 worker index on floor, preferring workers stationed in `preferred` zone."""
    n_w=len(LG.Z05_WORKERS)
    n_zones=max(1,len(UNPACK_SPOTS))
    w_per_zone=max(1,n_w//n_zones)
    base=((preferred%n_zones)*w_per_zone)%n_w
    order=[((base+i)%n_w) for i in range(n_w)]
    for w_i in order:
        if not Z05_WORKER_LOCKS[(floor,w_i)].locked() and Z05_JACK_PENDING[(floor,w_i)]==0:
            return w_i
    return min(order,key=lambda w_i:(Z05_JACK_PENDING[(floor,w_i)],1 if Z05_WORKER_LOCKS[(floor,w_i)].locked() else 0))

async def _z05_jack_transfer(floor,w_idx,from_xy,to_xy,load_poses_fn=None,z_load_start=None,z_load_end=None,return_home=True,on_cleared_from=None):
    """Move manual pallet jack z05_jack_{floor}_{w_idx} with Zone 05 worker between from_xy and to_xy along x=-2.95."""
    z0=LG.LEVELS[floor-1]
    jack=f'z05_jack_{floor}_{w_idx}'
    wid=LG.z05_worker_id(floor,w_idx)
    state=Z05_FLOOR_STATE[floor]['worker_poses'][w_idx]
    jx,jy,_,jyaw=poses.get(jack,LG.z05_jack_home(floor,w_idx))
    x_corr=-2.95
    pallet_speed=max(.3,float(C.get('dock_pallet_speed',1.2)))
    walk_speed=max(.3,float(C.get('dock_worker_speed',1.5)))
    z_a=z0 if z_load_start is None else z_load_start
    z_b=z0 if z_load_end is None else z_load_end

    async def _move_jack_chain(pts,carry_load):
        total_len=0.0
        leg_list=[]
        px0,py0=poses.get(jack,(jx,jy,z0,jyaw))[:2]
        for bx,by in pts:
            d=math.hypot(bx-px0,by-py0)
            if d>=1e-3:
                leg_list.append((px0,py0,bx,by,d))
                total_len+=d
                px0,py0=bx,by
        walked=0.0
        for ax,ay,bx,by,dist in leg_list:
            yaw=math.atan2(by-ay,bx-ax)
            dur=max(.15,dist/(pallet_speed if carry_load else walk_speed))
            steps=max(1,round(dist/STEP))
            def frame(q,ax=ax,ay=ay,bx=bx,by=by,dist=dist,walked=walked,yaw=yaw,steps=steps,carry_load=carry_load):
                px=ax+(bx-ax)*q;py=ay+(by-ay)*q
                pz=z0+(.05 if carry_load else 0.)
                wx=px-1.05*math.cos(yaw);wy=py-1.05*math.sin(yaw)
                swing=.25*math.sin(q*math.pi*steps)
                out={jack:(px,py,pz,yaw)}
                out.update(LG.operator_pose(wid,wx,wy,yaw,.06,-.42 if carry_load else .55,swing,z0=z0))
                if carry_load and load_poses_fn is not None:
                    frac=min(1.,max(0.,(walked+dist*q)/max(1e-6,total_len)))
                    lz=z_a+(z_b-z_a)*frac+.05
                    out.update(load_poses_fn(px,py,lz))
                return out
            await engine.move(frame(1.),duration=dur,transform=frame,exact=True)
            walked+=dist
            state.update({'x':bx-1.05*math.cos(yaw),'y':by-1.05*math.sin(yaw),'yaw':yaw,'bend':0.,'arm':0.})

    head_to_jack=math.atan2(jy-state['y'],(jx-1.05*math.cos(jyaw))-state['x'])
    await _z05_move_worker(floor,w_idx,.18,yaw=head_to_jack)
    await _z05_move_worker(floor,w_idx,0.,x=jx-1.05*math.cos(jyaw),y=jy-1.05*math.sin(jyaw),walk=True)
    await _move_jack_chain([(x_corr,jy),(x_corr,from_xy[1]),(from_xy[0],from_xy[1])],False)
    if load_poses_fn is not None:
        await engine.move({jack:(from_xy[0],from_xy[1],z0+.05,poses[jack][3]),**load_poses_fn(from_xy[0],from_xy[1],z_a+.05)},duration=.25,exact=True)
    await _move_jack_chain([(x_corr,from_xy[1])],True)
    if on_cleared_from is not None:
        on_cleared_from()
    await _move_jack_chain([(x_corr,to_xy[1]),(to_xy[0],to_xy[1])],True)
    if load_poses_fn is not None:
        await engine.move({jack:(to_xy[0],to_xy[1],z0,poses[jack][3]),**load_poses_fn(to_xy[0],to_xy[1],z_b)},duration=.25,exact=True)
    if return_home:
        await _z05_return_jack_home(floor,w_idx,to_xy[1])

async def _z05_return_jack_home(floor,w_idx,cur_y=None):
    """Walk empty Zone 05 pallet jack back along x=-2.95 to its parked bay."""
    z0=LG.LEVELS[floor-1]
    jack=f'z05_jack_{floor}_{w_idx}'
    wid=LG.z05_worker_id(floor,w_idx)
    state=Z05_FLOOR_STATE[floor]['worker_poses'][w_idx]
    hjx,hjy,_,hjyaw=LG.z05_jack_home(floor,w_idx)
    x_corr=-2.95
    walk_speed=max(.3,float(C.get('dock_worker_speed',1.5)))
    px0,py0,_,pyaw0=poses.get(jack,(hjx,hjy,z0,hjyaw))
    y_start=py0 if cur_y is None else cur_y
    pts=[(x_corr,y_start),(x_corr,hjy),(hjx,hjy)]
    for bx,by in pts:
        d=math.hypot(bx-px0,by-py0)
        if d<1e-3:continue
        yaw=math.atan2(by-py0,bx-px0)
        dur=max(.15,d/walk_speed)
        steps=max(1,round(d/STEP))
        def frame(q,ax=px0,ay=py0,bx=bx,by=by,yaw=yaw,steps=steps):
            px=ax+(bx-ax)*q;py=ay+(by-ay)*q
            wx=px-1.05*math.cos(yaw);wy=py-1.05*math.sin(yaw)
            swing=.25*math.sin(q*math.pi*steps)
            out={jack:(px,py,z0,yaw)}
            out.update(LG.operator_pose(wid,wx,wy,yaw,.06,.55,swing,z0=z0))
            return out
        await engine.move(frame(1.),duration=dur,transform=frame,exact=True)
        state.update({'x':bx-1.05*math.cos(yaw),'y':by-1.05*math.sin(yaw),'yaw':yaw,'bend':0.,'arm':0.})
        px0,py0=bx,by
    poses[jack]=(hjx,hjy,z0,hjyaw)
    send({jack:poses[jack]},transmit=not args.fast)

async def _z05_pull_from_gate(floor,preferred_worker,gate_xy,spot_xy,pallet_model,job,on_gate_cleared=None):
    """Zone 05 worker uses manual pallet jack to pull outbound pallet from output gate to Zone 05 unpack spot."""
    z0=LG.LEVELS[floor-1];oz=z0+.3
    w_idx=await _pick_z05_worker(floor,preferred_worker)
    Z05_JACK_PENDING[(floor,w_idx)]+=1
    try:
        async with Z05_WORKER_LOCKS[(floor,w_idx)]:
            loose_event('Z05_JACK_PULL_FROM_GATE',job=job,floor=floor,worker=w_idx)
            async def _run():
                await _z05_jack_transfer(
                    floor,w_idx,gate_xy,spot_xy,
                    load_poses_fn=lambda px,py,lz:{pallet_model:(px,py,lz,0.)},
                    z_load_start=oz,z_load_end=oz,on_cleared_from=on_gate_cleared,
                )
            await _run_busy_worker(LG.z05_worker_id(floor,w_idx),f'z05_{floor}_{w_idx}',_run())
    finally:
        if on_gate_cleared is not None:
            on_gate_cleared()
        Z05_JACK_PENDING[(floor,w_idx)]=max(0,Z05_JACK_PENDING[(floor,w_idx)]-1)
        async with _z05_jack_cond():
            _z05_jack_cond().notify_all()

async def _z05_return_partial_pallet(floor,zone,spot_xy,spot_idx,pallet_model,deck,leftover_items,job,level,sid,lane):
    """Stage leftover pallet beside the output gate first (no lift lock held), then acquire strict
    lock order (buf -> port -> corner -> gate -> worker), push onto the 2-slot outfeed roller, release
    roller & lift locks as soon as each slot clears, and return empty jack home in the background."""
    fl0=floor-1;z0=LG.LEVELS[fl0];oz=z0+.3
    out=LAYOUT.data['ports']['out'][zone];ox,oy=out['lift'];bx,by=out['buffer'];ol=f'out_lift_{zone}'
    z=C['storage_levels'][level]
    buf_lock=out_buffers[(zone,level)]
    corner_lock=out_corner_locks[(zone,fl0)]
    gate_lock=out_gate_locks[(zone,fl0)]
    gx,gy=out['gate']
    stage_xy=(-2.10,gy+(-1.35 if zone==0 else 1.35))
    carton_offsets=[(c,p[0]-spot_xy[0],p[1]-spot_xy[1],p[2]-z0) for c,p in leftover_items]
    def load_fn(px,py,lz):
        d={deck:(px,py,lz,0.)}
        for c,dx,dy,dz in carton_offsets:
            d[c]=(px+dx,py+dy,lz+dz,0.)
        return d

    spot_released=False
    buf_held=False;port_held=False;corner_held=False;gate_held=False
    ret_deck=None
    ret_handed_bg=False
    leftover_returned=False
    t_ret0=scheduler.time
    # Phase 1: move leftover pallet from Zone 05 spot -> adjacent floor staging WITHOUT holding any lift/conveyor lock.
    w_stage=await _pick_z05_worker(floor,zone)
    Z05_JACK_PENDING[(floor,w_stage)]+=1
    try:
        async with Z05_WORKER_LOCKS[(floor,w_stage)]:
            async def _stage_partial():
                await _z05_jack_transfer(
                    floor,w_stage,spot_xy,stage_xy,
                    load_poses_fn=load_fn,z_load_start=z0,z_load_end=z0,return_home=True,
                )
            await _run_busy_worker(LG.z05_worker_id(floor,w_stage),f'z05_{floor}_{w_stage}',_stage_partial())
    finally:
        Z05_JACK_PENDING[(floor,w_stage)]=max(0,Z05_JACK_PENDING[(floor,w_stage)]-1)
        async with _z05_jack_cond():
            _z05_jack_cond().notify_all()
    if spot_idx is not None:
        spot_released=True
        await _release_z05_spot(floor,zone,spot_idx)

    # Yield at staging beside the gate (holding zero locks) while full outbound deliveries need this port.
    t_yield_limit=scheduler.time+120.0
    while OUT_MAIN_INFLIGHT.get(zone,0)>0 and not STOP_REQUESTED and scheduler.time<t_yield_limit:
        await engine.until(scheduler.time+0.5)

    # Phase 2: strict lock rank order: buf_lock (1) -> out_ports (2) -> out_corner_locks (3) -> out_gate_locks (4) -> Z05_WORKER_LOCKS (5).
    await buf_lock.acquire();buf_held=True
    try:
        t_pw0=scheduler.time
        await out_ports[zone].acquire();port_held=True
        port_wait=max(0.,scheduler.time-t_pw0)
        try:
            await corner_lock.acquire();corner_held=True
            try:
                await gate_lock.acquire();gate_held=True
                try:
                    w_idx=await _pick_z05_worker(floor,zone)
                    Z05_JACK_PENDING[(floor,w_idx)]+=1
                    jack_ret_handed=False
                    try:
                        async with Z05_WORKER_LOCKS[(floor,w_idx)]:
                            async def _push_staged_to_gate():
                                await _z05_jack_transfer(
                                    floor,w_idx,stage_xy,(gx,gy),
                                    load_poses_fn=load_fn,z_load_start=z0,z_load_end=oz,return_home=False,
                                )
                            await _run_busy_worker(LG.z05_worker_id(floor,w_idx),f'z05_{floor}_{w_idx}',_push_staged_to_gate())
                            # Swap to a dedicated return pallet deck while keeping the actual leftover cartons visibly on top!
                            ret_deck=await FREE_RET_DECKS.get()
                            def partial_targets(px,py,pz,r_deck=ret_deck):
                                d={r_deck:(px,py,pz,0.)}
                                for c,dx,dy,dz in carton_offsets:
                                    d[c]=(px+dx,py+dy,pz+dz,0.)
                                return d
                            send({deck:(spot_xy[0],spot_xy[1],-5,0),**partial_targets(gx,gy,oz)},transmit=not args.fast)
                        # Spawn jack return home in background holding Z05_WORKER_LOCKS so out_gate/lift NEVER waits on human walking!
                        async def _ret_jack_bg(floor=floor,w_idx=w_idx,gy=gy):
                            try:
                                async with Z05_WORKER_LOCKS[(floor,w_idx)]:
                                    await _run_busy_worker(LG.z05_worker_id(floor,w_idx),f'z05_{floor}_{w_idx}',_z05_return_jack_home(floor,w_idx,gy))
                            finally:
                                Z05_JACK_PENDING[(floor,w_idx)]=max(0,Z05_JACK_PENDING[(floor,w_idx)]-1)
                                async with _z05_jack_cond():
                                    _z05_jack_cond().notify_all()
                        background_tasks.append(asyncio.create_task(_ret_jack_bg()))
                        jack_ret_handed=True
                    finally:
                        if not jack_ret_handed:
                            Z05_JACK_PENDING[(floor,w_idx)]=max(0,Z05_JACK_PENDING[(floor,w_idx)]-1)
                            async with _z05_jack_cond():
                                _z05_jack_cond().notify_all()
                    await engine.move(partial_targets(out['corner'][0],out['corner'][1],oz),1)
                finally:
                    if gate_held:
                        gate_lock.release();gate_held=False
                tracker.start_lift_task(ol,'Hoàn kho (Hàng dư Z05)',f'LF-RET-{job+1:04d}',pallet_model)
                t_ol0=scheduler.time
                z_ol0=(poses.get(ol) or (ox,oy,oz))[2]
                await engine.move({ol:(ox,oy,oz)},C['lift_speed'])
                await settled(job,ol,oz)
                await engine.move(partial_targets(ox,oy,oz),1)
            finally:
                if corner_held:
                    corner_lock.release();corner_held=False
            async def _finish_lift_and_shuttle(r_deck=ret_deck,p_targets=partial_targets):
                nonlocal leftover_returned
                b_held=True;p_held=True
                try:
                    try:
                        await engine.move({ol:(ox,oy,z),**p_targets(ox,oy,z)},C['lift_speed'])
                        await settled(job,ol,z)
                        await engine.move(p_targets(bx,by,z),C['conveyor_speed'])
                        t_ol1=scheduler.time
                        tracker.record_lift_leg(ol,abs(oz-z_ol0)+abs(z-oz),max(0.,t_ol1-t_ol0),t_start=t_ol0,t_end=t_ol1)
                        tracker.finish_lift_task(ol)
                    finally:
                        if p_held:
                            out_ports[zone].release();p_held=False
                    t_shw=scheduler.time
                    async with acquire_level_shuttle(level) as s:
                        sh_wait=max(0.,scheduler.time-t_shw)
                        slot_pt=LAYOUT.slot_point(sid)
                        wh_pick=f"Bàn đệm Xuất {zone+1} ({bx:.2f}, {by:.2f}, T{level+1})"
                        wh_drop=f"Ô kệ {sid} ({slot_pt[0]:.2f}, {slot_pt[1]:.2f}, T{level+1})"
                        tracker.start_shuttle_task(
                            s,'Hoàn kho (Hàng dư Z05)',f'WH-RET-{job+1:04d}',pallet_model,level,sid,scheduler.time,
                            job_id=job,pick_location=wh_pick,drop_location=wh_drop,wait_s=sh_wait,
                            lift_id=ol,lift_wait_s=port_wait,process_start_s=t_ret0,
                        )
                        await shuttle_async(s,out['buffer'],z)
                        await deck_async(s,r_deck,.12,extra_loads=carton_offsets)
                        if b_held:
                            buf_lock.release();b_held=False
                        await shuttle_async(s,slot_pt,z,r_deck,extra_loads=carton_offsets)
                        await deck_async(s,r_deck,-.06,extra_loads=carton_offsets)
                        access=(slot_pt[0],min(LAYOUT.data['road_y'],key=lambda yy:abs(yy-slot_pt[1])))
                        await shuttle_async(s,access,z)
                        tracker.finish_shuttle_task(s,scheduler.time)
                        # Swap partial return deck into stored pallet_model once tucked back inside Kho 01 rack slot
                        send({
                            r_deck:(spot_xy[0],spot_xy[1],-5,0),
                            **{c:(spot_xy[0],spot_xy[1],-5,0) for c,_ in leftover_items},
                            pallet_model:(slot_pt[0],slot_pt[1],z,0.),
                        },transmit=not args.fast)
                        event(job,'PALLET_RETURNED_TO_STORAGE',slot=sid,remaining_cartons=len(leftover_items))
                        await stored.put((job,level,sid,lane,pallet_model))
                finally:
                    if p_held:out_ports[zone].release()
                    if b_held:buf_lock.release()
                    if r_deck is not None:
                        FREE_RET_DECKS.put_nowait(r_deck)
                    if not leftover_returned:
                        leftover_returned=True
                        for c,_ in leftover_items:
                            FREE_Z05_CARTONS[floor].put_nowait(c)
            ret_task=asyncio.create_task(_finish_lift_and_shuttle())
            ret_handed_bg=True
            port_held=False;buf_held=False
            background_tasks.append(ret_task)
        finally:
            if port_held:
                out_ports[zone].release();port_held=False
    finally:
        if buf_held:buf_lock.release()
        if not ret_handed_bg:
            if ret_deck is not None:
                FREE_RET_DECKS.put_nowait(ret_deck)
            if not leftover_returned:
                leftover_returned=True
                for c,_ in leftover_items:
                    FREE_Z05_CARTONS[floor].put_nowait(c)
        if spot_idx is not None and not spot_released:
            await _release_z05_spot(floor,zone,spot_idx)

async def z05_unpack_pallet(floor,zone,spot_xy,pallet_model,job,spot_idx=None,level=0,sid='',lane=0,is_partial_override=None):
    """Zone 05 unpack at (floor, zone, spot_xy): unpacks each Z05 spot independently in parallel,
    releases empty spots as soon as cartons leave the pallet, and supports partial return to Kho 01."""
    global _z05_pallet_seq,completed
    spot_i=0 if spot_idx is None else spot_idx
    spot_released=False
    is_partial=False
    owns_zone_deck=False
    fst=Z05_FLOOR_STATE[floor]
    try:
        async with Z05_SPOT_LOCKS[(floor,zone,spot_i)]:
            _z05_pallet_seq+=1
            seq=_z05_pallet_seq
            z0=LG.LEVELS[floor-1]
            z05_per=int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))
            if is_partial_override is not None:
                is_partial_flag=bool(is_partial_override) and 0<Z05_PARTIAL_CARTONS<z05_per and bool(sid)
            else:
                is_partial_flag=Z05_PARTIAL_EVERY>0 and (seq%Z05_PARTIAL_EVERY==(2%Z05_PARTIAL_EVERY)) and 0<Z05_PARTIAL_CARTONS<z05_per and bool(sid)
            if is_partial_flag:
                take_n=Z05_PARTIAL_CARTONS
                is_partial=True
            else:
                take_n=z05_per
            if not fst['deck_in_use'][zone]:
                fst['deck_in_use'][zone]=True
                owns_zone_deck=True
                deck=f'z05_deck_{floor}_{zone}'
            else:
                deck=pallet_model
            cartons=[await FREE_Z05_CARTONS[floor].get() for _ in range(z05_per)]
            stack=LG.z05_stack_positions(spot_xy,floor,z05_per)
            if deck!=pallet_model:
                send({pallet_model:(spot_xy[0],spot_xy[1],-5,0),deck:(spot_xy[0],spot_xy[1],z0,0.),
                      **{c:p for c,p in zip(cartons,stack)}},transmit=not args.fast)
                if CONTINUOUS_MODE and not is_partial:
                    FREE_EVEN_PALLETS.put_nowait(pallet_model)
                    await SLOT_COORD.notify()
            else:
                send({deck:(spot_xy[0],spot_xy[1],z0,0.),
                      **{c:p for c,p in zip(cartons,stack)}},transmit=not args.fast)
            loose_event('Z05_PALLET_UNPACK_START',job=job,floor=floor,zone=zone,spot=spot_i,cartons=take_n,partial=is_partial)
            all_items=list(zip(cartons,stack))
            unpack_items=all_items[:take_n]
            leftover_items=all_items[take_n:]
            batch={
                'job':job,
                'zone':zone,
                'spot_idx':spot_i,
                'spot_xy':spot_xy,
                'deck':deck,
                'hide_deck_on_picked':True,
                'is_partial':is_partial,
                'items':list(unpack_items),
                'total_items':take_n,
                'u2_tasks':[],
                'picked_event':asyncio.Event(),
                'done_event':asyncio.Event(),
            }
            fst['spots'][(zone,spot_i)]=batch
            for w_idx in range(len(LG.Z05_WORKERS)):
                wt=fst['worker_tasks'][w_idx]
                if wt is None or wt.done():
                    fst['worker_tasks'][w_idx]=asyncio.create_task(z05_worker_loop(floor,w_idx))
            await batch['picked_event'].wait()
            fst['spots'][(zone,spot_i)]=None
            if owns_zone_deck and not is_partial:
                fst['deck_in_use'][zone]=False
                owns_zone_deck=False
            if is_partial and leftover_items and not STOP_REQUESTED:
                loose_event('Z05_PALLET_PARTIAL_RETURN',job=job,floor=floor,zone=zone,unpacked=take_n,remaining=len(leftover_items))
                if owns_zone_deck:
                    fst['deck_in_use'][zone]=False
                    owns_zone_deck=False
                spot_released=True
                await _z05_return_partial_pallet(floor,zone,spot_xy,spot_idx,pallet_model,deck,leftover_items,job,level,sid,lane)
                return
            send({deck:(spot_xy[0],spot_xy[1],-5,0),**{c:(spot_xy[0],spot_xy[1],-5,0) for c,_ in leftover_items}},transmit=not args.fast)
            for c,_ in leftover_items:
                FREE_Z05_CARTONS[floor].put_nowait(c)
            if (is_partial or deck==pallet_model) and CONTINUOUS_MODE:
                FREE_EVEN_PALLETS.put_nowait(pallet_model)
                await SLOT_COORD.notify()
            loose_event('Z05_PALLET_EMPTY',job=job,floor=floor,zone=zone,spot=spot_i)
            if spot_idx is not None and not spot_released:
                spot_released=True
                await _release_z05_spot(floor,zone,spot_idx)
            async def _wait_complete(done_ev=batch['done_event'],j_id=job,is_p=is_partial):
                global completed
                await done_ev.wait()
                if not is_p:
                    event(j_id,'COMPLETE');completed+=1
            ct=asyncio.create_task(_wait_complete())
            Z05_COMPLETE_TASKS.append(ct)
            background_tasks.append(ct)
    finally:
        if owns_zone_deck:
            fst['deck_in_use'][zone]=False
        if spot_idx is not None and not spot_released:
            await _release_z05_spot(floor,zone,spot_idx)

# Unpack cycle phases (fraction of loose_unpack_s): turn, walk, face pallet, bend+reach, grab, rise, turn,
# walk back carrying, face belt, reach over the belt, release onto the belt, straighten.
OP_PHASES=(.05,.18,.04,.12,.05,.10,.04,.22,.04,.06,.04,.06)
CARRY=(.1,1.0);PLACE=(.3,1.0);STEP=.6   # (bend, arm raise) when carrying / placing; step length (m)

async def _operator_cycle(i,op,spot,carton,stack_pos,dy=0.):
    """One carton: the operator walks to the pallet, bends, picks the carton, carries it back and puts it
    on the carton belt in front of the station. Timing scales to loose_unpack_s."""
    T=C['loose_unpack_s'];d=[f*T for f in OP_PHASES]
    sx,sy,syaw=LG.station(op);px,py,pyaw=LG.pallet_side(op,spot);py+=dy
    head_out=math.atan2(py-sy,px-sx);head_back=math.atan2(sy-py,sx-px)
    belt=(op['x'],LG.Y758,LG.zc(1),syaw);reach=LG.reach_for(stack_pos[2])
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry=False,carton_to=None,walk=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        upper_only=not walk
        c0=poses[carton]
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(i,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.35*math.sin(math.pi*steps*q) if walk else 0.,upper_only=upper_only)
            if carry: out[carton]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'])
            elif carton_to is not None: out[carton]=tuple(c0[j]+(carton_to[j]-c0[j])*q for j in range(4))
            return out
        end=at(1.)
        await engine.move(end,duration=max(.05,dur),transform=at,shape=(lambda t:t) if walk else blend,exact=True)
        state.update(b)
    await phase(d[0],yaw=head_out)
    await phase(d[1],x=px,y=py,walk=True)
    await phase(d[2],yaw=pyaw)
    await phase(d[3],bend=reach[0],arm=reach[1])
    grab=LG.held_carton(px,py,pyaw,*reach)
    await phase(d[4],carton_to=grab)
    await phase(d[5],bend=CARRY[0],arm=CARRY[1],carry=True)
    await phase(d[6],yaw=head_back,carry=True)
    await phase(d[7],x=sx,y=sy,walk=True,carry=True)
    await phase(d[8],yaw=syaw,carry=True)
    await phase(d[9],bend=PLACE[0],arm=PLACE[1],carry=True)
    await phase(d[10],carton_to=belt)
    await phase(d[11],bend=0.15,arm=0.70)

async def operator_cycle(i,*args,**kwargs):
    return await _run_busy_worker(i,f'op_{i}',_operator_cycle(i,*args,**kwargs))

async def loose_goods():
    """Loose pallets arrive at the floor-1 spots; the operator next to each spot unpacks it carton by
    carton onto the carton belt; cartons go up to floors 1-4 in turn and to the sorting branches in turn."""
    # When barcode classification is integrated into arrivals(), the legacy
    # independent loose-pallet generator must stay off to avoid duplicate odd loads.
    if not LOOSE_PALLETS or C.get('inbound_classification')=='barcode':return
    ops=sorted((w for w in LG.L['workers'] if w['area']=='carton_inbound'),key=lambda w:w['x'])
    op_of=[min(ops,key=lambda w:abs(w['x']-s[0])) for s in LOOSE_SPOTS]
    free=[asyncio.Semaphore(1) for _ in LOOSE_SPOTS];counter={'carton':0,'branch':{}}
    async def unpack(k):
        s=k%len(LOOSE_SPOTS);await free[s].acquire();spot=LOOSE_SPOTS[s];deck=f'loose_pallet_{s}'
        stack=LG.stack_positions(spot,LOOSE_PER)
        async def receive(dock_i,cartons):
            names=[await FREE_CARTONS.get() for _ in range(LOOSE_PER)]
            send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
            send({n:p+(spot[2],) for n,p in zip(names,stack)},transmit=not args.fast)
            loose_event('LOOSE_PALLET_ON_SPOT',pallet=k,spot=s)
            op=op_of[s]
            for n,p in zip(names,stack):
                await operator_cycle(LG.OPERATORS.index(op),op,spot,n,p)
                i=counter['carton'];counter['carton']+=1;dest=LG.floors()[i%len(LG.floors())]
                br=counter['branch'].get(dest,0);counter['branch'][dest]=br+1;loose_stats['cartons_on_belt']+=1
                loose_event('CARTON_ON_BELT',carton=n,pallet=k,floor=dest)
                background_tasks.append(asyncio.create_task(carton_trip(n,dest,br)))
            loose_stats['pallets_unpacked']+=1
            loose_event('LOOSE_PALLET_EMPTY',pallet=k,spot=s)
        await dock_flow.prepare('odd',k,spot,list(range(LOOSE_PER)),receive)
        free[s].release()
    jobs=[]
    for k in range(LOOSE_PALLETS):
        await engine.until(k*C['loose_pallet_interval_s'])
        loose_event('LOOSE_PALLET_ARRIVAL',pallet=k);jobs.append(asyncio.create_task(unpack(k)))
    await asyncio.gather(*jobs)

REQ_IS_PARTIAL={}

async def requests(total):
    request=0
    full_dispatched=0
    req_limit=total if total is not None else (OUTBOUND_TARGET if OUTBOUND_TARGET>0 else None)
    z05_per=int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))
    partial_enabled=(Z05_PARTIAL_EVERY>0 and 0<Z05_PARTIAL_CARTONS<z05_per)
    # Keep outbound pipeline fed up to outbound_prefill_limit without dumping 16 requests at t=0
    total_z05_spots=len(LG.floors())*sum(len(sp) for sp in UNPACK_SPOTS)
    prefill_lim=max(4,int(C.get('outbound_prefill_limit',6)))
    out_buf=min(total_z05_spots,prefill_lim) if (CONTINUOUS_MODE and total is None) else 0
    if req_limit is not None:
        out_buf=min(out_buf,req_limit)
    while not STOP_REQUESTED and (req_limit is None or (request<req_limit if total is not None else full_dispatched<req_limit)):
        if request<out_buf:
            await engine.until(request*1.0)
        elif CONTINUOUS_MODE and total is None:
            while not STOP_REQUESTED:
                has_free_spot=any(not busy for busy in Z05_SPOT_BUSY.values())
                if has_free_spot and out_queue.qsize()<2 and outbound_active<8 and not stored.empty():
                    break
                await engine.until(scheduler.time+1.0)
            if not STOP_REQUESTED:
                await engine.until(scheduler.time+1.0)
        else:
            await engine.until((request-out_buf+1)*C['outbound_interval_s'])
        if STOP_REQUESTED:break
        is_partial_req=partial_enabled and (request%Z05_PARTIAL_EVERY==(2%Z05_PARTIAL_EVERY))
        REQ_IS_PARTIAL[request]=is_partial_req
        if not is_partial_req:
            full_dispatched+=1
        event(request,'OUTBOUND_REQUEST_QUEUED')
        REQ_QUEUE_T[request]=scheduler.time
        await out_queue.put(request)
        request+=1
    if req_limit is not None:
        for _ in range(2):await out_queue.put(None)

async def outbound_worker(worker):
    inflight_sem=asyncio.Semaphore(8)
    active_tasks=set()
    async def _run_outbound_request(request):
        global completed,outbound_active
        target_floor=None;claimed_spot_idx=None;spot_handed=False
        wh_rec=None
        try:
            if CONTINUOUS_MODE:
                t_claim0=scheduler.time
                claimed=await _claim_z05_spot(worker)
                t_claim1=scheduler.time
                if t_claim1>t_claim0:
                    tracker.record_lift_wait_span(f'out_lift_{worker}',t_claim0,t_claim1)
                if claimed is None or STOP_REQUESTED:return
                target_floor,claimed_spot_idx=claimed
            job,level,sid,lane,p=await stored.get()
            if STOP_REQUESTED:return
            if CONTINUOUS_MODE:await SLOT_COORD.notify()
            floor=(target_floor-1) if target_floor is not None else (job%4)
            is_partial_req=REQ_IS_PARTIAL.pop(request,False)
            out_dir='Xuất tạm hoàn Z05' if is_partial_req else 'Xuất kho'
            s=f'shuttle_{level}';z=C['storage_levels'][level]
            out=LAYOUT.data['ports']['out'][worker];ox,oy=out['lift'];ol=f'out_lift_{worker}'
            oz=C['output_levels'][floor]+.3
            outbound_active+=1
            main_priority_added=False
            try:
                if not is_partial_req:
                    OUT_MAIN_INFLIGHT[worker]+=1
                    main_priority_added=True
                buf_lock=out_buffers[(worker,level)]
                buf_released=False
                corner_lock=out_corner_locks[(worker,floor)]
                gate_lock=out_gate_locks[(worker,floor)]
                corner_held=False
                gate_held=False
                t_bw0=scheduler.time
                await buf_lock.acquire()
                t_bw1=scheduler.time
                if t_bw1>t_bw0:
                    tracker.record_shuttle_wait_span(s,t_bw0,t_bw1)
                try:
                    t_req_q=REQ_QUEUE_T.pop(request,None)
                    pre_lift_wait=max(0.0,t_bw1-t_req_q) if t_req_q is not None else 0.0
                    t_ol_cmd0=min(t_req_q,t_bw0) if t_req_q is not None else t_bw0
                    t_shw=scheduler.time
                    async with acquire_level_shuttle(level) as s:
                        t_sh1=scheduler.time
                        sh_wait=max(0.0,t_sh1-t_shw)
                        if t_sh1>t_shw:
                            tracker.record_shuttle_wait_span(s,t_shw,t_sh1)
                        tracker.record_stage_wait('out_port_to_shuttle',sh_wait)
                        slot_pt=LAYOUT.slot_point(sid)
                        bx,by=out['buffer']
                        wh_pick=f"Ô kệ {sid} ({slot_pt[0]:.2f}, {slot_pt[1]:.2f}, T{level+1})"
                        wh_drop=f"Bàn đệm Xuất {worker+1} ({bx:.2f}, {by:.2f}, T{level+1})"
                        wh_rec=tracker.start_shuttle_task(
                            s,out_dir,f'WH-OUT-{request+1:04d}',p,level,sid,scheduler.time,
                            job_id=job,request_id=request,pick_location=wh_pick,drop_location=wh_drop,
                            wait_s=sh_wait,lift_id=ol,lift_wait_s=pre_lift_wait,
                            process_start_s=(t_req_q if t_req_q is not None else scheduler.time),
                            keep_process_open=True,
                        )
                        event(job,'OUTBOUND_DISPATCH',request=request)
                        await shuttle_async(s,slot_pt,z)
                        await deck_async(s,p,.12)
                        await shuttle_async(s,out['buffer'],z,p)
                        await deck_async(s,p,-.06)
                        await shuttle_async(s,(bx,min(LAYOUT.data['road_y'],key=lambda yy:abs(yy-by))),z)
                        tracker.finish_shuttle_task(s,scheduler.time)
                    t_port_w0=scheduler.time
                    await out_ports[worker].acquire()
                    port_held=True
                    try:
                        t_ol0=scheduler.time
                        port_wait=max(0.0,t_ol0-t_port_w0)
                        if t_ol0>t_ol_cmd0:
                            tracker.record_lift_wait_span(ol,t_ol_cmd0,t_ol0)
                        total_lift_wait=pre_lift_wait+port_wait
                        tracker.record_lift_wait(ol,total_lift_wait)
                        tracker.record_stage_wait('out_req_to_lift',total_lift_wait)
                        tracker.start_lift_task(ol,out_dir,f'LF-OUT-{request+1:04d}',p)
                        z_ol0=(poses.get(ol) or (ox,oy,oz))[2]
                        await engine.move({ol:(ox,oy,z)},C['lift_speed'])
                        try:
                            await settled(job,ol,z)
                        except Exception as exc:
                            tracker.record_lift_fault(ol,str(exc))
                            tracker.finish_warehouse_process(wh_rec,scheduler.time,extra_lift_wait_s=port_wait,fault_count_add=1)
                            raise
                        event(job,'OUTPUT_CABIN_TRANSFER')
                        await engine.move({p:(ox,oy,z,0)},1)
                        # Pallet has cleared the tier buffer conveyor into the lift cabin.
                        buf_lock.release()
                        buf_released=True
                        event(job,'OUTPUT_LIFT_TO_FLOOR',floor=floor+1)
                        await engine.move({ol:(ox,oy,oz),p:(ox,oy,oz,0)},C['lift_speed'])
                        try:
                            await settled(job,ol,oz)
                        except Exception as exc:
                            tracker.record_lift_fault(ol,str(exc))
                            tracker.finish_warehouse_process(wh_rec,scheduler.time,extra_lift_wait_s=port_wait,fault_count_add=1)
                            raise
                        t_gw0=scheduler.time
                        await corner_lock.acquire()
                        t_gw1=scheduler.time
                        corner_held=True
                        if t_gw1>t_gw0:
                            tracker.record_lift_wait_span(ol,t_gw0,t_gw1)
                        event(job,'OUTPUT_GATE_TRANSFER')
                        await engine.move({p:tuple(out['corner'])+(oz,0)},1)
                        t_ol1=scheduler.time
                        tracker.record_lift_leg(ol,abs(z-z_ol0)+abs(oz-z),max(0.0,(t_gw0-t_ol0)+(t_ol1-t_gw1)),t_start=t_ol0,t_end=t_gw0)
                        if t_ol1>t_gw1:
                            tracker.record_lift_leg(ol,0.0,0.0,t_start=t_gw1,t_end=t_ol1)
                        tracker.finish_lift_task(ol)
                        tracker.finish_warehouse_process(wh_rec,scheduler.time,extra_lift_wait_s=port_wait)
                    finally:
                        if main_priority_added:
                            OUT_MAIN_INFLIGHT[worker]=max(0,OUT_MAIN_INFLIGHT[worker]-1)
                            main_priority_added=False
                        if port_held:
                            out_ports[worker].release()
                finally:
                    if not buf_released:
                        buf_lock.release()
                try:
                    await gate_lock.acquire()
                    gate_held=True
                    await engine.move({p:tuple(out['gate'])+(oz,0)},1)
                    if corner_held:
                        corner_lock.release();corner_held=False
                    event(job,'PALLET_EXIT_GATE')
                    spots=UNPACK_SPOTS[worker]
                    if claimed_spot_idx is not None:
                        spot_idx=claimed_spot_idx
                        spot=spots[spot_idx]
                        unpack_occupant[(worker,floor,spot_idx)]=p
                    else:
                        k=unpack_count.get((worker,floor),0);unpack_count[(worker,floor)]=k+1
                        spot_idx=k%len(spots);spot=spots[spot_idx];prev=unpack_occupant.pop((worker,floor,spot_idx),None)
                        if prev and math.dist(poses.get(prev,(0,0,-5))[:3],(spot[0],spot[1],oz))<0.6:send({prev:(spot[0],spot[1],-5,0)},transmit=not args.fast)
                        unpack_occupant[(worker,floor,spot_idx)]=p
                    def _release_gate_once():
                        nonlocal gate_held
                        if gate_held:
                            gate_lock.release();gate_held=False
                    await _z05_pull_from_gate(floor+1,worker,tuple(out['gate']),spot,p,job,on_gate_cleared=_release_gate_once)
                    event(job,'UNPACK_SPOT',spot=spot_idx,floor=floor+1)
                    spot_handed=True
                    background_tasks.append(asyncio.create_task(z05_unpack_pallet(floor+1,worker,spot,p,job,spot_idx=claimed_spot_idx,level=level,sid=sid,lane=lane,is_partial_override=is_partial_req)))
                finally:
                    if corner_held:
                        corner_lock.release()
                    if gate_held:
                        gate_lock.release()
            finally:
                if main_priority_added:
                    OUT_MAIN_INFLIGHT[worker]=max(0,OUT_MAIN_INFLIGHT[worker]-1)
                outbound_active-=1
        finally:
            if claimed_spot_idx is not None and target_floor is not None and not spot_handed:
                await _release_z05_spot(target_floor,worker,claimed_spot_idx)
            inflight_sem.release()
    try:
        while not STOP_REQUESTED:
            await inflight_sem.acquire()
            if STOP_REQUESTED:
                inflight_sem.release()
                break
            request=await out_queue.get()
            if request is None:
                inflight_sem.release()
                break
            t=asyncio.create_task(_run_outbound_request(request))
            active_tasks.add(t)
            t.add_done_callback(active_tasks.discard)
        if active_tasks:
            await asyncio.gather(*active_tasks,return_exceptions=True)
    except asyncio.CancelledError:
        for t in list(active_tasks):
            if not t.done():t.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks,return_exceptions=True)
        raise

async def main():
    global p1_running,STOP_REQUESTED,_SHUTTLE_STANDBY,_z05_pallet_seq
    STOP_REQUESTED=False
    _z05_pallet_seq=0
    _SHUTTLE_LEVEL_ACTIVE.update({lvl:f'shuttle_{lvl}' for lvl in range(9)})
    _SHUTTLE_STANDBY='shuttle_9'
    _SHUTTLE_LEVEL_OF.update({**{f'shuttle_{i}':i for i in range(9)},'shuttle_9':0})
    _SHUTTLE_DUTY_START.update({f'shuttle_{i}':0.0 for i in range(9)})
    _SHUTTLE_DUTY_START['shuttle_9']=None
    _SHUTTLE_CHARGE_UNTIL.clear()
    poses['shuttle_lift_cabin']=(SHUTTLE_LIFT_XY[0],SHUTTLE_LIFT_XY[1],C['storage_levels'][0])
    _AGV_DUTY_START.clear()
    _AGV_CHARGE_UNTIL.clear()
    _AGV_CHARGE_HOURS.clear()
    _ACTIVE_FORKLIFT_JOBS.clear()
    OUT_MAIN_INFLIGHT[0]=0
    OUT_MAIN_INFLIGHT[1]=0
    Z05_COMPLETE_TASKS.clear()
    for _k in Z05_JACK_PENDING:
        Z05_JACK_PENDING[_k]=0
    for key in ['inbound_interval_s','outbound_interval_s']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    for key in ['agv_following_gap_m','speed_multiplier','agv_speed','lift_speed','shuttle_speed','conveyor_speed','agv_acceleration','lift_acceleration','shuttle_acceleration','conveyor_acceleration','agv_jerk','lift_jerk','shuttle_jerk','conveyor_jerk','agv_angular_speed','agv_angular_acceleration','agv_angular_jerk','lift_settle_s','shuttle_positioning_s','shuttle_turn_s','shuttle_lift_s','shuttle_availability','shuttle_utilization','lift_availability','lift_utilization']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    if C['agv_following_gap_m']<.25:raise ValueError('agv_following_gap_m must be at least 0.25m')
    if len(INBOUND_PICKUP_SLOTS)<4:raise ValueError('inbound_pickup_slots must contain at least 4 pickup positions')
    if not isinstance(CYCLE_DURATION_S,(int,float)) or isinstance(CYCLE_DURATION_S,bool) or not math.isfinite(CYCLE_DURATION_S) or CYCLE_DURATION_S<0:
        raise ValueError('cycle_duration_s must be non-negative')
    (ROOT/'events.jsonl').write_text('')
    for attempt in range(60):
        try:
            send({k:v for i in range(N_FORKLIFTS) for k,v in assembly(i,PARKS[i],.02).items()},transmit=not args.no_gz);break
        except RuntimeError:
            if attempt==59:raise
            await asyncio.sleep(1)
    for i in range(10):poses[f'shuttle_{i}']=LAYOUT.slot_point(LAYOUT.parking[i//9])+(C['storage_levels'][i%9],)
    p1_med_count=int(C.get('p1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    p1_med_total=max(1,P1_PER_RACK)*p1_med_count
    for f,zf in enumerate(LG.LEVELS,1):
        for agv_k,home_br in enumerate(LG.U1_AGV_HOME_BRANCHES):
            bx,u1_y,z0,_=LG.u1_rack_pose(f,home_br)
            poses[f'u1_agv_{f}_{agv_k}']=(bx,u1_y,z0,math.pi/2)
        for b in range(len(LG.BRANCHES)):
            bx,u1_y,z0,_=LG.u1_rack_pose(f,b)
            poses[f'u1_rack_{f}_{b}_0']=(bx,u1_y,z0,0.)
            poses[f'u1_trolley_{f}_{b}']=LG.u1_trolley_pose(f,b)
            for r_idx in range(1,LG.U1_RACKS_PER_BRANCH):
                cell=LG.U1_BRANCH_CELLS[b][r_idx]
                poses[f'u1_rack_{f}_{b}_{r_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
        for z_idx in range(len(LG.Z05_WORKERS)):
            poses[f'z05_jack_{f}_{z_idx}']=LG.z05_jack_home(f,z_idx)
        for agv_k,home_u2 in enumerate(LG.U2_AGV_HOME_STATIONS):
            rx,ry,z0,_=LG.u2_rack_pose(f,home_u2)
            poses[f'u2_agv_{f}_{agv_k}']=(rx,ry,z0,0.)
        for u2_idx in range(len(LG.U2_WORKERS)):
            rx,ry,z0,ryaw=LG.u2_rack_pose(f,u2_idx)
            poses[f'u2_rack_{f}_{u2_idx}_0']=(rx,ry,z0,ryaw)
            poses[f'u2_trolley_{f}_{u2_idx}']=LG.u2_trolley_pose(f,u2_idx)
            for r_idx in range(1,LG.U2_RACKS_PER_STATION):
                cell=LG.U2_STATION_CELLS[u2_idx][r_idx]
                poses[f'u2_rack_{f}_{u2_idx}_{r_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
        for agv_k,home_st in enumerate(LG.P1_AGV_HOME_STATIONS):
            rx,ry,z0,_=LG.p1_rack_pose(f,home_st)
            agv_yaw=-math.pi/2 if home_st<7 else 0.
            poses[f'p1_agv_{f}_{agv_k}']=(rx,ry,z0,agv_yaw)
        for st_idx in range(len(LG.P1_STATIONS)):
            rx,ry,z0,ryaw=LG.p1_rack_pose(f,st_idx)
            poses[f'p1_rack_{f}_{st_idx}_0']=(rx,ry,z0,ryaw)
            t_pose=LG.p1_trolley_pose(f,st_idx)
            poses[f'p1_trolley_{f}_{st_idx}']=t_pose
            for c_idx in range(TROLLEY_CAP):
                poses[f'p1_carton_{f}_{st_idx}_{c_idx}']=LG.trolley_carton_pose(t_pose,c_idx)
            for m in range(p1_med_total):
                poses[f'p1_med_{f}_{st_idx}_{m}']=LG.u1_med_on_rack((rx,ry,z0,ryaw),1 if p1_med_count==1 else (m%p1_med_count),m//p1_med_count)
            for r_idx in range(1,LG.P1_RACKS_PER_STATION):
                cell=LG.P1_STATION_CELLS[st_idx][r_idx]
                c_pose=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
                poses[f'p1_rack_{f}_{st_idx}_{r_idx}']=c_pose
            for r_idx in range(LG.P1_RACKS_PER_STATION,3):
                poses[f'p1_rack_{f}_{st_idx}_{r_idx}']=(rx,ry,-5.,ryaw)
        for k_idx,cell in enumerate(LG.FF.K04_MOVABLE_STOCK_CELLS):
            poses[f'k04_stock_{f}_{k_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
    _init_agv_positions()
    for i in range(2):
        for direction in ['in','out']:poses[f'{direction}_lift_{i}']=tuple(LAYOUT.data['ports'][direction][i]['lift'])+(.3,)
    use_barcode=C.get('inbound_classification')=='barcode' and bool(LOOSE_SPOTS)
    even_jobs=[j for j in range(C['jobs']) if not use_barcode or inbound_classification(j)=='even']
    for j in even_jobs:poses[f'pallet_{j}']=inbound_pickup_position(j)
    while not FREE_EVEN_PALLETS.empty():FREE_EVEN_PALLETS.get_nowait()
    for j in even_jobs:FREE_EVEN_PALLETS.put_nowait(f'pallet_{j}')
    extra_pallets=[f'pallet_pool_{i}' for i in range(max(0,int(C.get('continuous_extra_pallets',0))))]
    for i,p in enumerate(extra_pallets):
        poses[p]=inbound_pickup_position(i)
        if CONTINUOUS_MODE:FREE_EVEN_PALLETS.put_nowait(p)
    # Loads appear at configured arrival times, rather than all being present initially.
    hidden_even={f'pallet_{j}':(inbound_pickup_position(j)[0],-17,-5,0) for j in even_jobs}
    hidden_even.update({p:(inbound_pickup_position(i)[0],-17,-5,0) for i,p in enumerate(extra_pallets)})
    if hidden_even:
        poses.update(hidden_even)
        last_tx_poses.update(hidden_even)
    if LOOSE_PALLETS:   # loose-goods pallet decks and cartons start hidden below their spot
        while not FREE_CARTONS.empty():FREE_CARTONS.get_nowait()
        for i in range(LOOSE_CARTON_POOL):FREE_CARTONS.put_nowait(f'carton_{i}')
        while not FREE_RET_DECKS.empty():FREE_RET_DECKS.get_nowait()
        for r_idx in range(8):FREE_RET_DECKS.put_nowait(f'z05_ret_deck_{r_idx}')
        for f in LG.floors():
            while not FREE_Z05_CARTONS[f].empty():FREE_Z05_CARTONS[f].get_nowait()
            for c_idx in range(Z05_CARTON_POOL):FREE_Z05_CARTONS[f].put_nowait(f'z05_carton_{f}_{c_idx}')
        hidden={f'loose_pallet_{s}':(sp[0],sp[1],-5,sp[2]) for s,sp in enumerate(LOOSE_SPOTS)}
        hidden.update({f'z05_ret_deck_{r_idx}':(LOOSE_SPOTS[0][0],LOOSE_SPOTS[0][1],-5,0) for r_idx in range(8)})
        hidden.update({f'carton_{i}':(LOOSE_SPOTS[0][0],LOOSE_SPOTS[0][1],-5,0) for i in range(LOOSE_CARTON_POOL)})
        u1_med_total=max(1,U1_PER_RACK)*int(C.get('u1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
        u2_med_total=max(1,U2_PER_RACK)*int(C.get('u2_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
        for f in LG.floors():
            for b in range(len(LG.BRANCHES)):
                bx,u1_y,_,_=LG.u1_rack_pose(f,b)
                for r_idx in range(LG.U1_RACKS_PER_BRANCH):
                    for m in range(u1_med_total):
                        hidden[f'u1_med_{f}_{b}_{r_idx}_{m}']=(bx,u1_y,-5,0)
            for z_idx in range(len(LG.Z05_WORKERS)):
                wx,wy,_,_=LG.z05_station(f,z_idx)
                hidden[f'z05_deck_{f}_{z_idx}']=(wx,wy,-5,0)
            w0x,w0y,_,_=LG.z05_station(f,0)
            for c_idx in range(Z05_CARTON_POOL):
                hidden[f'z05_carton_{f}_{c_idx}']=(w0x,w0y,-5,0)
            for u2_idx in range(len(LG.U2_WORKERS)):
                rx,ry,_,_=LG.u2_rack_pose(f,u2_idx)
                for r_idx in range(LG.U2_RACKS_PER_STATION):
                    for m in range(u2_med_total):
                        hidden[f'u2_med_{f}_{u2_idx}_{r_idx}_{m}']=(rx,ry,-5,0)
        poses.update(hidden)
        last_tx_poses.update(hidden)
        for i,op in enumerate(LG.OPERATORS):send(LG.operator_pose(i,*LG.station(op)),transmit=False)   # operators at the belt
        anim_ops={}
        for f in LG.floors():
            for b in range(len(LG.BRANCHES)):
                wx,wy,wyaw,wz=LG.u1_station(f,b)
                anim_ops.update(LG.operator_pose(LG.u1_worker_id(f,b),wx,wy,wyaw,z0=wz))
            for z_idx in range(len(LG.Z05_WORKERS)):
                wx,wy,wyaw,wz=LG.z05_station(f,z_idx)
                anim_ops.update(LG.operator_pose(LG.z05_worker_id(f,z_idx),wx,wy,wyaw,z0=wz))
            for u2_idx in range(len(LG.U2_WORKERS)):
                wx,wy,wyaw,wz=LG.u2_station(f,u2_idx)
                anim_ops.update(LG.operator_pose(LG.u2_worker_id(f,u2_idx),wx,wy,wyaw,z0=wz))
            for st_idx in range(len(LG.P1_STATIONS)):
                wx,wy,wyaw,wz=LG.p1_station(f,st_idx)
                anim_ops.update(LG.operator_pose(LG.p1_worker_id(f,st_idx),wx,wy,wyaw,z0=wz))
        anim_ops.update(LG.operator_pose(500,-31.91375,-1.26,-math.pi/2,z0=0.))
        send(anim_ops,transmit=False)
    _n_js=max(1,len(LAYOUT.job_slots))
    for i in range(C.get('initial_outbound_pallets',0)):
        level=(i+4)%9;sid=LAYOUT.job_slots[(i//9)%_n_js];p=f'pallet_out_{i}'
        poses[p]=LAYOUT.slot_point(sid)+(C['storage_levels'][level],0)
        await stored.put((C['jobs']+i,level,sid,i%2,p))
    dock_flow.initialize()
    p1_running=True
    p1_tasks=[asyncio.create_task(p1_station_loop(f,st_idx)) for f in LG.floors() for st_idx in range(len(LG.P1_STATIONS))]
    if LOOSE_SPOTS:background_tasks.extend(asyncio.create_task(loose_worker(k)) for k in range(len(LG.OPERATORS)))
    if not args.fast:
        background_tasks.append(asyncio.create_task(station_workers_ambient_loop()))
        background_tasks.append(asyncio.create_task(pallet_pick_supervisor_loop()))
    total=None if CONTINUOUS_MODE else (args.jobs if args.jobs is not None else C['jobs'])
    ticker=asyncio.create_task(engine.run())
    even_total=None if total is None else sum(1 for job in range(total) if inbound_classification(job)=='even')
    stop_event=asyncio.Event()
    stop_reason={'text':'Dừng mô phỏng liên tục' if total is None else f'Hoàn thành đủ {total} pallet cấu hình'}
    def _traffic_snapshot():
        return {
            'engine_ticks':engine.total_ticks,
            'dual_agv_motion_ticks':engine.dual_agv_ticks,
            'same_direction_following_ticks':engine.following_ticks,
            'multiple_shuttle_motion_ticks':engine.multi_shuttle_ticks,
            'inbound_outbound_overlap_ticks':engine.overlap_ticks,
            'route_checks':route_checks,
            'rail_contact_checks':rail_checks,
            'agv_traffic_stats':dict(agv_traffic_stats),
        }
    def request_stop(reason_text):
        global STOP_REQUESTED
        STOP_REQUESTED=True
        SLOT_COORD.wake_all()
        _wake_z05_spots()
        _wake_z05_jack()
        _wake_z05_u2()
        for pool in (*U1_AGV_POOLS.values(),*U2_AGV_POOLS.values(),*P1_AGV_POOLS.values()):
            pool.wake_all()
        engine.wake_waiters()
        if not stop_event.is_set():
            stop_reason['text']=reason_text
            stop_event.set()
    loop=asyncio.get_running_loop()
    sigs=[s for s in (signal.SIGINT,signal.SIGTERM,getattr(signal,'SIGHUP',None)) if s is not None]
    def _sig_handler(signum,_frame):
        global STOP_REQUESTED
        STOP_REQUESTED=True
        sname=signal.Signals(signum).name
        try:loop.call_soon_threadsafe(request_stop,f'Dừng mô phỏng theo tín hiệu {sname}')
        except RuntimeError:pass
    for sig in sigs:
        try:signal.signal(sig,_sig_handler)
        except (ValueError,OSError,RuntimeError):pass
    async def _watch_duration():
        await engine.until(CYCLE_DURATION_S)
        if engine.running:request_stop(f'Kết thúc đúng thời lượng chu trình ({CYCLE_DURATION_S:.1f}s)')
    async def _auto_save_reports():
        try:
            delay=2.0
            while engine.running and not stop_event.is_set():
                await asyncio.sleep(delay)
                delay=10.0
                if not engine.running or stop_event.is_set():break
                snap=tracker.snapshot(
                    scheduler.time,'Đang chạy mô phỏng (bản lưu tự động)',dock_flow.stats,loose_stats,
                    wall_duration_s=max(0.0,time.monotonic()-WALL_START),traffic_stats=_traffic_snapshot(),
                )
                await asyncio.to_thread(export_excel,snap,ROOT/'reports',{**loose_stats,'complete':completed,'inbound_stored':stored_count})
        except asyncio.CancelledError:
            pass
    workers=asyncio.gather(arrivals(total),requests(even_total),*(agv_worker(i) for i in range(N_FORKLIFTS)),outbound_worker(0),outbound_worker(1),loose_goods())
    stop_wait=asyncio.create_task(stop_event.wait())
    duration_watch=asyncio.create_task(_watch_duration()) if CYCLE_DURATION_S>0 else None
    checkpoint_task=asyncio.create_task(_auto_save_reports()) if not args.fast else None
    try:
        if CYCLE_DURATION_S>0:
            def _on_workers_done(t):
                if not t.cancelled():
                    exc=t.exception()
                    if exc is not None and not isinstance(exc,asyncio.CancelledError):
                        request_stop(f'Lỗi tiến trình: {exc}')
            workers.add_done_callback(_on_workers_done)
            done,_=await asyncio.wait([ticker,stop_wait],return_when=asyncio.FIRST_COMPLETED)
        else:
            done,_=await asyncio.wait([ticker,workers,stop_wait],return_when=asyncio.FIRST_COMPLETED)
        if ticker in done:
            await ticker
            if getattr(engine,'disconnect_reason',None) and not stop_event.is_set():
                request_stop(engine.disconnect_reason)
        if not stop_event.is_set() and workers in done and total is not None:
            await workers
            while not stop_event.is_set() and (inbound_active>0 or loose_stats['pallets_unpacked']<sum(1 for j in range(total) if inbound_classification(j)=='odd')):await asyncio.sleep(0 if args.fast else .01)
            while not stop_event.is_set() and loose_stats['cartons_delivered']<loose_stats['cartons_on_belt']:await asyncio.sleep(0 if args.fast else .01)
            if Z05_COMPLETE_TASKS and not stop_event.is_set():
                await asyncio.gather(*Z05_COMPLETE_TASKS,return_exceptions=True)
        else:
            was_cancelled_by_stop=stop_event.is_set()
            if not workers.done():workers.cancel()
            await asyncio.gather(workers,return_exceptions=True)
            if not workers.cancelled() and workers.done():
                exc=workers.exception()
                if exc is not None and not (was_cancelled_by_stop and isinstance(exc,asyncio.CancelledError)):
                    raise exc
    finally:
        STOP_REQUESTED=True
        p1_running=False
        engine.running=False
        engine.wake_waiters()
        for m_item in list({id(v):v for v in engine.motions.values()}.values()):
            fut=m_item.get('future')
            if fut is not None and not fut.done():fut.cancel()
        _wake_z05_spots()
        _wake_z05_jack()
        _wake_z05_u2()
        z05_wt=[t for fst in Z05_FLOOR_STATE.values() for t in fst['worker_tasks'].values() if t is not None]
        all_bg=[t for t in [duration_watch,stop_wait,checkpoint_task,*background_tasks,*p1_tasks,*z05_wt] if t is not None]
        pending=[t for t in all_bg if not t.done()]
        for _ in range(4):
            if not pending:break
            for t in pending:
                if not t.done():t.cancel()
            _,still=await asyncio.wait(pending,timeout=0.05)
            pending=list(still)
        if all_bg:await asyncio.gather(*all_bg,return_exceptions=True)
        await asyncio.gather(ticker,return_exceptions=True)
        if args.fast and not args.no_gz:send(dict(poses))
        wall_elapsed=max(0.0,time.monotonic()-WALL_START)
        report_summary=tracker.finalize(scheduler.time,stop_reason['text'],dock_flow.stats,loose_stats,wall_duration_s=wall_elapsed,traffic_stats=_traffic_snapshot())
        excel_paths=export_excel(tracker,ROOT/'reports',report_summary={**loose_stats,'complete':completed,'inbound_stored':stored_count})
        report={'complete':completed,'inbound_stored':stored_count,'remaining_stored':stored.qsize(),'jobs_requested':('continuous' if total is None else total),'events':event_count,'swept_pose_checks':route_checks,'rail_contact_checks':rail_checks,
                'max_concurrent_motions':engine.max_active,'dual_agv_motion_ticks':engine.dual_agv_ticks,'same_direction_following_ticks':engine.following_ticks,'multiple_shuttle_motion_ticks':engine.multi_shuttle_ticks,'inbound_outbound_overlap_ticks':engine.overlap_ticks,
                'inbound_interval_s':C['inbound_interval_s'],'outbound_interval_s':C['outbound_interval_s'],'max_pending_inbound':max_pending,
                'loose_pallets_unpacked':loose_stats['pallets_unpacked'],'loose_cartons_delivered':loose_stats['cartons_delivered'],
                'loose_cartons_per_floor':{str(k):v for k,v in sorted(loose_stats['per_floor'].items())},
                'dock_flow':dock_flow.stats,
                'cycle_duration_limit_s':CYCLE_DURATION_S,'continuous_mode':CONTINUOUS_MODE,'stop_reason':stop_reason['text'],
                'excel_report':excel_paths,'report_summary':report_summary,
                'mode':'Concurrent CAD constrained kinematic','demo_duration_s':scheduler.time,'wall_duration_s':wall_elapsed}
        (ROOT/'validation.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
        (ROOT/'cad-route-log.json').write_text(json.dumps(list(shuttle_route_log)))
        if _EVENTS_FP is not None and not _EVENTS_FP.closed:
            _EVENTS_FP.flush()
            _EVENTS_FP.close()
        print(f"Excel report saved: {excel_paths['latest']} ({excel_paths['timestamped']})",flush=True)
        print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:event(-1,'ERROR',message=str(exc));raise
