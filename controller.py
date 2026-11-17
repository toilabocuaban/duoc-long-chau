"""Deterministic kinematic process demonstrator. No collision/dynamics claims."""
import argparse
import asyncio
from collections import deque
import heapq
import json
import math
import signal
import time
from pathlib import Path
from gz.transport13 import Node
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.scene_pb2 import Scene
from gz.msgs10.empty_pb2 import Empty
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
U1_PER_RACK=max(1,int(C.get('u1_cartons_per_rack',1)))
TROLLEY_CAP=max(1,int(C.get('trolley_capacity',LG.TROLLEY_CAPACITY)))
U1_LOCKS={(f,b):asyncio.Lock() for f in LG.floors() for b in range(len(LG.BRANCHES))}
Z05_LOCKS={(f,z_idx):asyncio.Lock() for f in LG.floors() for z_idx in range(len(LG.Z05_WORKERS))}
Z05_WORKER_LOCKS={(f,w_idx):asyncio.Lock() for f in LG.floors() for w_idx in range(len(LG.Z05_WORKERS))}
U2_PER_RACK=max(1,int(C.get('u2_cartons_per_rack',1)))
U2_LOCKS={(f,u2_idx):asyncio.Lock() for f in LG.floors() for u2_idx in range(len(LG.U2_WORKERS))}
U2_DELIVERY_LOCKS={(f,u2_idx):asyncio.Lock() for f in LG.floors() for u2_idx in range(len(LG.U2_WORKERS))}
P1_LOCKS={(f,st_idx):asyncio.Lock() for f in LG.floors() for st_idx in range(len(LG.P1_STATIONS))}
P1_ROW_SEMAPHORES={(f,row):asyncio.Semaphore(4) for f in LG.floors() for row in (0,1)}
LOOSE_CARTON_POOL=max(180,int(C.get('loose_carton_pool',0)),int(C.get('loose_pallets',0))*int(C.get('loose_cartons_per_pallet',0)))
Z05_CARTON_POOL=max(80,int(C.get('z05_carton_pool',0)),len(LG.Z05_WORKERS)*int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))*2)
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
                'active_trolley':f'p1_trolley_{f}_{st_idx}',
                'spare_trolley':f'k06_trolley_{f}_{4+(st_idx%4)}',
                'k06_slot':4+(st_idx%4),
                'trolley_remaining':[f'p1_carton_{f}_{st_idx}_{c}' for c in range(TROLLEY_CAP-1,-1,-1)],
                'cartons_packed':0,
                'rack_swaps':0,
                'trolley_swaps':0,
            }
    return state

def _init_z05_floor_state():
    state={}
    for f in LG.floors():
        w_poses={}
        for w_idx in range(len(LG.Z05_WORKERS)):
            sx,sy,syaw,_=LG.z05_station(f,w_idx)
            w_poses[w_idx]={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
        state[f]={
            'zones':{0:None,1:None},
            'next_u2':0,
            'u2_active_task':{u2_idx:None for u2_idx in range(len(LG.U2_WORKERS))},
            'u2_reserved':{u2_idx:False for u2_idx in range(len(LG.U2_WORKERS))},
            'worker_tasks':{0:None,1:None},
            'worker_poses':w_poses,
        }
    return state

U1_STATE=_init_u1_state()
U2_STATE=_init_u2_state()
P1_STATE=_init_p1_state()
Z05_FLOOR_STATE=_init_z05_floor_state()
Z05_SPOT_BUSY={(f,z,s):False for f in LG.floors() for z in range(len(UNPACK_SPOTS)) for s in range(len(UNPACK_SPOTS[z]))}
Z05_SPOT_COND=None
Z05_NEXT_FLOOR={z:0 for z in range(len(UNPACK_SPOTS))}

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
            # Prefer a floor whose zone is currently completely idle before staging a 2nd pallet on any floor
            for pass_mode in (0,1):
                for df in range(n_f):
                    f=floors[(start_f+df)%n_f]
                    if pass_mode==0 and any(Z05_SPOT_BUSY[(f,worker,sx)] for sx in range(n_s)):
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

odd_pallet_seq=0

def inbound_pickup_position(job):
    if not INBOUND_PICKUP_SLOTS:
        return pickup_position(job)
    return INBOUND_PICKUP_SLOTS[job % len(INBOUND_PICKUP_SLOTS)]

def inbound_classification(job):
    """Return the simulated barcode result for an inbound load."""
    seq=C.get('inbound_type_sequence',['even','odd']) or ['even']
    return str(seq[job % len(seq)]).lower()
node=Node()
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
parser.add_argument('--jobs',type=int,default=None)
parser.add_argument('--duration',type=float,default=None,help='Simulated cycle duration limit in seconds (0 = finish all jobs or run continuously)')
args=parser.parse_args()
CYCLE_DURATION_S=(0.0 if (args.fast or args.jobs is not None) else float(C.get('cycle_duration_s',0))) if args.duration is None else float(args.duration)
CONTINUOUS_MODE=bool(C.get('continuous_mode',False)) and (args.jobs is None) and (not args.fast)
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
        if name.startswith(('in_lift_','out_lift_')):
            changes[name.replace('_lift_','_counterweight_')]=(xyz[0]+1.45,xyz[1]+.75,min(17.3,max(.7,17.6-xyz[2])))
    for name,xyz in list(changes.items()):
        if not (name.startswith('shuttle_') and name[8:].isdigit()): continue
        old=poses.get(name,xyz)
        dx,dy=xyz[0]-old[0],xyz[1]-old[1]
        if abs(dx)>1e-7 and abs(dy)>1e-7: raise RuntimeError('Shuttle cannot move diagonally across rail grid')
        if abs(dx)>1e-7: wheel_modes[name]='x'
        if abs(dy)>1e-7: wheel_modes[name]='y'
        riding=any(math.dist(xyz[:3],changes.get(lift,point)[:3])<1e-5 for lift,point in poses.items() if lift.startswith('in_lift_'))
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
        _EVENTS_FP=(ROOT/'events.jsonl').open('a',buffering=1)
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
        return 1.0 / 30.0, 1, 0.0
    frame = 1.0 / 20.0
    frame_sim = frame * float(C['speed_multiplier'])
    max_sub = max(0.02, float(C.get('sim_substep_s', 0.1)))
    sub_steps = max(1, math.ceil((frame_sim - 1e-9) / max_sub))
    return frame_sim / sub_steps, sub_steps, frame

class Engine:
    def __init__(self):
        self.motions={};self.running=True;self.max_active=0;self.overlap_ticks=0;self.dual_agv_ticks=0;self.multi_shuttle_ticks=0;self.following_ticks=0;self.total_ticks=0
        self._waiters=[];self._waiter_seq=0
    def wake_waiters(self):
        while self._waiters:
            _,_,w_fut=heapq.heappop(self._waiters)
            if not w_fut.done():w_fut.set_result(None)
    async def move(self,targets,speed=2,duration=None,kind=None,transform=None,shape=None,exact=False):
        targets={n:tuple(v) for n,v in targets.items()}
        if set(targets)&set(self.motions):raise RuntimeError('Equipment has two owners')
        starts={n:poses[n]+(0,)*(len(v)-len(poses[n])) for n,v in targets.items()}
        kind=kind or ('lift' if any('_lift_' in n for n in targets) else 'shuttle' if any(n.startswith('shuttle_') for n in targets) else 'conveyor')
        speed=min(speed,C['conveyor_speed']) if kind=='conveyor' else speed
        distance=max(math.dist(starts[n][:3],v[:3]) for n,v in targets.items())
        # 7-segment S-curve; a longer imposed duration stretches it in time, which only lowers v/a/jerk.
        profile=None if exact else SCurve(distance,speed,C[kind+'_acceleration'],C[kind+'_jerk'])
        duration=duration if exact else max(duration or 0,profile.duration)   # exact: scripted animation timing
        future=asyncio.get_running_loop().create_future()
        item={'targets':targets,'starts':starts,'elapsed':0.,'duration':duration,'future':future,'transform':transform,'shape':shape or (profile.shape if profile else blend)}
        for n in targets:self.motions[n]=item
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
                if all(f'agv_{i}' in changes for i in range(2)):
                    a,b=[changes[f'agv_{i}'] for i in range(2)]
                    da=[changes[f'agv_{i}'][0]-poses[f'agv_{i}'][0] for i in range(2)]
                    if abs(a[1]-b[1])<.1 and da[0]*da[1]>1e-10:self.following_ticks+=1
                prospective={**poses,**changes}
                for i in range(2):
                    if f'agv_{i}' in changes:
                        global route_checks
                        payload=agv_load[i];check_pose(i,changes[f'agv_{i}'],prospective,payload,pickups[i]);route_checks+=1
                if all(f'agv_{i}' in changes for i in range(2)):self.dual_agv_ticks+=1
                if sum(n.startswith('shuttle_') and n[8:].isdigit() for n in changes)>1:self.multi_shuttle_ticks+=1
                if changes:send(changes,transmit=not args.fast)
                self.max_active=max(self.max_active,len(set(id(v) for v in self.motions.values())))
                if inbound_active and outbound_active and changes:self.overlap_ticks+=1
                for m in finished:
                    for n in m['targets']:del self.motions[n]
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

background_tasks=[]
engine=Engine();agv_load=[None,None];pickups=[None,None]
dock_flow=DockFlow(C,engine,send,poses,args.fast,lambda state,**kw:loose_event(state,**kw))
dock_flow.clock=lambda:scheduler.time
reservations={}
# One pallet per inbound pickup bay: an arrival waits until the AGV has carried the previous load away.
pickup_bays=[asyncio.Semaphore(1) for _ in INBOUND_PICKUP_SLOTS]
in_ports=[asyncio.Lock() for _ in range(2)];out_ports=[asyncio.Lock() for _ in range(2)]
out_buffers={(w,lvl):asyncio.Lock() for w in range(2) for lvl in range(9)}
out_floor_gates={(w,fl):asyncio.Lock() for w in range(2) for fl in range(4)}
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
        peer=reservations.get(1-i,Plan(poses[f'agv_{1-i}'],poses[f'agv_{1-i}'],now,.05,agv_load[1-i] is not None))
        if compatible(plan,peer,now,C['agv_following_gap_m']):break
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
        tracker.record_shuttle_turn(s,max(0.0,scheduler.time-t0))

async def shuttle_async(s,end,z,pallet=None):
    route=LAYOUT.path(poses[s][:2],end)
    try:
        for a,b in zip(route,route[1:]):
            axis='x' if abs(b[0]-a[0])>1e-7 else 'y'
            if axis!=wheel_modes[s]:await wheel_switch(s,axis)
            targets={s:tuple(b)+(z,)}
            if pallet:targets[pallet]=tuple(b)+(z+max(0,deck_offsets[s]),0)
            start_xy=poses[s][:2];t_leg0=scheduler.time
            try:
                await engine.move(targets,C['shuttle_speed'])
            finally:
                tracker.record_shuttle_leg(s,math.dist(start_xy,poses[s][:2]),max(0.0,scheduler.time-t_leg0))
        shuttle_route_log.append({'shuttle':s,'path':route})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        tracker.record_shuttle_fault(s,str(exc))
        raise

async def deck_async(s,p,height):
    # Fine positioning over the slot/buffer, then raise or lower the deck with the pallet.
    t0=scheduler.time
    try:
        await engine.until(scheduler.time+C['shuttle_positioning_s'])
        deck_offsets[s]=height;x,y,z=poses[s][:3]
        await engine.move({s:(x,y,z),deck_name(s):(x,y,z+height),p:(x,y,z+max(0,height),0)},duration=C['shuttle_lift_s'])
    finally:
        tracker.record_shuttle_busy(s,max(0.0,scheduler.time-t0))

async def arrivals(total):
    # In continuous mode every Dock worker pre-stages 18 cartons continuously and takes any free Floor-1 slot.
    if total is None:
        cont_lock=asyncio.Lock()
        cont_job_seq=0
        cont_even_seq=0
        even_limit=INBOUND_TARGET if INBOUND_TARGET>0 else None
        even_released=0 if INBOUND_EVEN_INTERVAL_S>0 else even_limit
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
            while not STOP_REQUESTED and (even_limit is None or k<even_limit):
                await engine.until(k*INBOUND_EVEN_INTERVAL_S)
                if STOP_REQUESTED:break
                even_released=k+1
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
        for _ in range(2):await in_queue.put(None)
    elif placements:
        await asyncio.gather(*placements,return_exceptions=True)

async def agv_worker(lane):
    """AGV dispatch loop.

    Event-driven: an AGV starts towards a waiting pallet immediately. It does not wait
    for its inbound port before the pickup (the lift is lowered in parallel and is only
    awaited before the conveyor handover). On the way back it checks the queue when it
    reaches its parking column on the return lane (RETURN_Y): if a pallet is waiting it
    carries on west to the pallet spots instead of entering the parking box. Lanes
    (agv_motion): RETURN_Y westbound empty, LOAD_Y eastbound loaded, both on floor 1.
    """
    global stored_count
    job=await in_queue.get();parked=True
    while job is not None:
        t_in_q=JOB_IN_QUEUE_T.pop(job,None)
        if t_in_q is not None:
            tracker.record_stage_wait('pickup_to_agv',max(0.0,scheduler.time-t_in_q))
        t_tw0=scheduler.time
        traffic=dock_flow.gate.agv();await traffic.__aenter__()
        tracker.record_agv_leg(f'agv_{lane}',0.0,False,0.0,wait_s=max(0.0,scheduler.time-t_tw0))
        traffic_held=True
        try:
            p=JOB_PALLET_MODEL.pop(job,f'pallet_{job}');bay_idx=JOB_BAY_IDX.pop(job,(job%len(INBOUND_PICKUP_SLOTS)) if INBOUND_PICKUP_SLOTS else 0);even_seq=JOB_EVEN_SEQ.pop(job,None)
            level=(even_seq%9) if even_seq is not None else (job%9);z=C['storage_levels'][level];s=f'shuttle_{level}'
            inp=LAYOUT.data['ports']['in'][lane];ix,iy=inp['lift'];bx,by=inp['buffer'];il=f'in_lift_{lane}'
            pick_xy=INBOUND_PICKUP_SLOTS[bay_idx] if INBOUND_PICKUP_SLOTS else inbound_pickup_position(job)
            pick_loc=f"Ô Pickup {bay_idx+1} ({pick_xy[0]:.2f}, {pick_xy[1]:.2f})"
            drop_loc=f"Cửa nhập Kho 01 - Cổng {lane+1} ({ix:.2f}, {inp['drop'][1]:.2f})"
            tracker.start_agv_task(f'agv_{lane}',f'DK-AGV-{job+1:04d}','Khu Dock -> Kho 01','Chở pallet chẵn từ ô Pickup vào cửa nhập Kho 01',scheduler.time,pallet_or_item=p,dock_id=tracker.job_dock_map.get(job),pick_location=pick_loc,drop_location=drop_loc)
            route=route_to_pickup(lane,job,pick_xy)
            if not parked:route=[((None,None,0,math.pi),'TURN_TOWARDS_PICKUP')]+route[PICKUP_LEGS_FROM_LANE:]
            await follow_async(job,lane,route,pickup=p)
            await engine.move(assembly(lane,poses[f'agv_{lane}'],.2,p),.4)
            event(job,'AGV_CARRY_TO_INBOUND',agv=lane+1)
            # Reserve the inbound port (chain + lift + level buffer) only once the load is on the forks.
            t_pw0=scheduler.time
            await in_ports[lane].acquire()
            in_lift_wait=max(0.0,scheduler.time-t_pw0)
            tracker.record_agv_leg(f'agv_{lane}',0.0,True,0.0,wait_s=in_lift_wait)
            tracker.record_lift_wait(il,in_lift_wait)
            tracker.record_stage_wait('agv_to_in_lift',in_lift_wait)
            prep_wait_box=[0.0]
            async def prepare(s=s,z=z,level=level,ix=ix):
                t_sw0=scheduler.time
                async with shuttles[level]:
                    prep_wait=max(0.0,scheduler.time-t_sw0)
                    prep_wait_box[0]=prep_wait
                    tracker.record_shuttle_wait(s,prep_wait)
                    await shuttle_async(s,(ix,LAYOUT.data['road_y'][0]),z)
            ready=asyncio.create_task(prepare())
            async def lift_down(job=job,il=il,ix=ix,iy=iy):
                z_dn0=(poses.get(il) or (ix,iy,.3))[2]
                t_dn0=scheduler.time
                await engine.move({il:(ix,iy,.3)},C['lift_speed'])
                try:
                    await settled(job,il,.3)
                except Exception as exc:
                    tracker.record_lift_fault(il,str(exc))
                    raise
                tracker.record_lift_leg(il,abs(z_dn0-.3),max(0.0,scheduler.time-t_dn0))
            lift_ready=asyncio.create_task(lift_down())
            gate=route_to_gate(lane,ix)
            await follow_async(job,lane,gate[:-1],.2,p)
            if traffic_held:
                await traffic.__aexit__(None,None,None)
                traffic_held=False
            await lift_ready  # interlock: lift at floor level and settled before approaching the chain
            await follow_async(job,lane,gate[-1:],.2,p)
            if CONTINUOUS_MODE:await SLOT_COORD.release_pickup(bay_idx)
            elif pickup_bays:pickup_bays[bay_idx%len(pickup_bays)].release()
            event(job,'AGV_HANDOVER_CHAIN',agv=lane+1)
            agv_load[lane]=None;pickups[lane]=p
            await drive_async(lane,(ix,inp['drop'][1]-.5-.12-2.4,0,math.pi/2),.2,pickup=p)
            event(job,'FORKS_CLEAR')
            async def return_trip(job=job,p=p,ix=ix):
                await engine.move(assembly(lane,poses[f'agv_{lane}'],.02),.3)
                legs=route_home(lane,ix)
                await follow_async(job,lane,legs[:3],pickup=p)  # reverse, turn, run back along y=-7
                pickups[lane]=None
                await follow_async(job,lane,legs[3:])
                event(job,'AGV_RETURN_COMPLETE')
                return 'wait',True
            home=asyncio.create_task(return_trip())
            async def downstream(job=job,p=p,level=level,z=z,s=s,inp=inp,ix=ix,iy=iy,bx=bx,by=by,il=il,ready=ready,lane=lane,even_seq=even_seq):
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
                tracker.record_lift_leg(il,abs(z-z_up0),max(0.0,scheduler.time-t_lc0))
                tracker.finish_lift_task(il)
                event(job,'INPUT_BUFFER_RECEIVE')
                await ready
                t_shw=scheduler.time
                async with shuttles[level]:
                    sh_wait=max(0.0,scheduler.time-t_shw)
                    tracker.record_stage_wait('buffer_to_in_shuttle',prep_wait_box[0]+sh_wait)
                    slot_seq=even_seq if even_seq is not None else ((job//2) if CONTINUOUS_MODE else job)
                    sid=LAYOUT.job_slots[slot_seq%len(LAYOUT.job_slots)]
                    slot=LAYOUT.slot_point(sid)
                    wh_pick=f"Bàn đệm Nhập {lane+1} ({bx:.2f}, {by:.2f}, T{level+1})"
                    wh_drop=f"Ô kệ {sid} ({slot[0]:.2f}, {slot[1]:.2f}, T{level+1})"
                    tracker.start_shuttle_task(
                        s,'Nhập kho',f'WH-IN-{job+1:04d}',p,level,sid,scheduler.time,
                        job_id=job,pick_location=wh_pick,drop_location=wh_drop,wait_s=sh_wait,
                        lift_id=il,lift_wait_s=in_lift_wait,process_start_s=t_pw0,
                        process_shuttle_wait_s=prep_wait_box[0]+sh_wait,
                    )
                    await shuttle_async(s,inp['buffer'],z)
                    await deck_async(s,p,.12)
                    in_ports[lane].release()
                    await shuttle_async(s,slot,z,p)
                    await deck_async(s,p,-.06)
                    access=(slot[0],min(LAYOUT.data['road_y'],key=lambda yy:abs(yy-slot[1])))
                    await shuttle_async(s,access,z)
                    tracker.finish_shuttle_task(s,scheduler.time)
                    event(job,'PALLET_STORED',slot=sid)
                    stored_count+=1;inbound_active-=1;await stored.put((job,level,sid,lane,p))
            background_tasks.append(asyncio.create_task(downstream()))
            job,parked=await home
            tracker.finish_agv_task(f'agv_{lane}',scheduler.time)
        finally:
            if traffic_held:
                await traffic.__aexit__(None,None,None)
        if job=='wait':job=await in_queue.get()

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
    async def claim_next(self,can_even_fn,can_odd_fn=None,on_even_claim=None):
        async with self._c():
            while not STOP_REQUESTED:
                bay=self._free_bay() if can_even_fn() else None
                odd_ok=bool(LOOSE_SPOTS) and (True if can_odd_fn is None else can_odd_fn())
                spot=self._free_spot() if odd_ok else None
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

async def _run_busy_worker(wid,worker_key,coro):
    BUSY_WORKERS.add(wid)
    t0=scheduler.time
    try:return await coro
    finally:
        tracker.record_worker_activity(worker_key,duration_s=max(0.0,scheduler.time-t0))
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
    med_names=[f'u1_med_{floor}_{branch}_{med_offset+m}' for m in range(med_count)]
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
        target_on_rack=LG.u1_med_on_rack(rack_pose,m,tray_slot)
        place_yaw=math.atan2(target_on_rack[1]-sy,target_on_rack[0]-sx)
        await phase(d[0],y=pick_y,yaw=syaw,bend=pick_bend,arm=pick_arm,walk=True)
        grab=LG.held_carton(sx,pick_y,syaw,pick_bend,pick_arm,z0=z0)
        await phase(d[1],item_to=(m_name,grab))
        await phase(d[2],y=sy,bend=CARRY[0],arm=CARRY[1],carry_item=m_name,walk=True)
        await phase(d[3],yaw=place_yaw,carry_item=m_name)
        await phase(d[4],y=place_y,bend=place_bend,arm=place_arm,carry_item=m_name,walk=True)
        await phase(d[5],item_to=(m_name,target_on_rack))
        placed.append((m_name,m,tray_slot))
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

async def rack_swap(floor,agv,home,full_rack,full_contents,store_cell,next_rack,spare_cell,next_contents=None,speed=1.,on_stored=None):
    """Station AGV under full_rack at home=(x, y, agv_yaw, rack_yaw): store full_rack in store_cell (claimed
    empty first), fetch next_rack from spare_cell and bring it back to the station."""
    spots=AGV_SPOTS[floor]
    await spots.claim([cell_key(store_cell)],full_rack)
    await agv_trip(floor,agv,(store_cell['x'],store_cell['y']),None,speed,Load(full_rack,store_cell.get('yaw',0.),full_contents))
    if on_stored:on_stored()
    await agv_trip(floor,agv,(spare_cell['x'],spare_cell['y']),None,speed)
    await agv_trip(floor,agv,home[:2],home[2],speed,Load(next_rack,home[3],next_contents))
    spots.free(cell_key(spare_cell))

async def _k06_take_ready(floor,exclude):
    cond=K06_COND.setdefault(floor,asyncio.Condition())
    async with cond:
        await cond.wait_for(lambda:any(t!=exclude for _,t in K06_READY[floor]))
        k=next(i for i,(_,t) in enumerate(K06_READY[floor]) if t!=exclude)
        return K06_READY[floor].pop(k)

async def _k06_put_ready(floor,slot,trolley):
    cond=K06_COND.setdefault(floor,asyncio.Condition())
    async with cond:
        K06_READY[floor].append((slot,trolley));cond.notify_all()

async def trolley_exchange(floor,agv,home,trolley_pose,trolley,out_contents=None,on_drop=None,in_contents=None,on_pick=None,speed=1.):
    """Take the station trolley to a free Khu 06 slot, leave it there, collect a waiting trolley from another
    slot and park it at the station mark. Returns the trolley now at the station."""
    spots=AGV_SPOTS[floor];tx,ty,_,tyaw=trolley_pose
    await agv_trip(floor,agv,(tx,ty),None,speed)
    key=await spots.claim([k06_key(s) for s in range(len(LG.K06_SLOTS_XY))],trolley)
    kx,ky,_,kyaw=LG.k06_slot_pose(floor,key[1])
    await agv_trip(floor,agv,(kx,ky),None,speed,Load(trolley,kyaw,out_contents))
    if on_drop:on_drop()
    await _k06_put_ready(floor,key,trolley)
    rkey,rtrolley=await _k06_take_ready(floor,trolley)
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
    """When the U1 trolley holds 5 empty cartons, the U1 AGV takes it to Khu 06 and brings a waiting trolley back."""
    st=U1_STATE[(floor,branch)];trolley=st['active_trolley']
    def drop():
        send({c:poses[trolley][:2]+(-5,0) for c in cartons_in_trolley},transmit=not args.fast)
        for c in cartons_in_trolley:FREE_CARTONS.put_nowait(c)
        if CONTINUOUS_MODE:SLOT_COORD.wake_all()
        loose_event('U1_TROLLEY_DELIVERED_TO_K06',floor=floor,branch=branch,trolley=trolley)
    agv_id=f'u1_agv_{floor}_{branch}'
    tp=LG.u1_trolley_pose(floor,branch)
    st['active_trolley']=await _tracked_agv_mission(
        agv_id,f'UNP-U1T-T{floor}N{branch+1}','Khu Unpack U1','Đổi xe trolley rỗng tại Khu 06',trolley,
        trolley_exchange(floor,agv_id,u1_home(floor,branch),tp,trolley,
            out_contents=lambda tp:{c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(cartons_in_trolley)},on_drop=drop,
            speed=float(C.get('u1_agv_speed',.45))),
        pick_location=f"Bàn U1-T{floor}-N{branch+1} ({tp[0]:.2f}, {tp[1]:.2f})",
        drop_location=f"Khu 06 <-> Bàn U1-T{floor}-N{branch+1}")
    st['trolley_cartons']=[];st['trolley_swaps']+=1
    loose_event('U1_REPLACEMENT_TROLLEY_READY',floor=floor,branch=branch,trolley=st['active_trolley'])

async def u1_agv_swap_cycle(floor,branch,full_rack,meds_on_rack,store_cell,next_rack,spare_cell,old_spare_meds=()):
    """U1 AGV stores the full rack (+ medicine boxes) in an empty Kho 04 cell and brings the spare rack back."""
    def stored():
        send({m:poses[full_rack][:2]+(-5,0) for m,_,_ in meds_on_rack},transmit=not args.fast)
        loose_event('AGV_RACK_STORED',floor=floor,branch=branch,rack=full_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
    loose_event('AGV_REPLACEMENT_RACK_PICKUP',floor=floor,branch=branch,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    agv_id=f'u1_agv_{floor}_{branch}'
    hp=u1_home(floor,branch)
    await _tracked_agv_mission(
        agv_id,f'UNP-U1R-T{floor}N{branch+1}','Khu Unpack U1','Cất rack đầy vào Kho 04 & lấy rack rỗng',full_rack,
        rack_swap(floor,agv_id,hp,full_rack,
            lambda rp:{m:LG.u1_med_on_rack(rp,mi,ts) for m,mi,ts in meds_on_rack},store_cell,next_rack,spare_cell,
            speed=float(C.get('u1_agv_speed',.45)),on_stored=stored),
        pick_location=f"Bàn U1-T{floor}-N{branch+1} ({hp[0]:.2f}, {hp[1]:.2f})",
        drop_location=f"Kho 04 ({store_cell['x']:.2f}, {store_cell['y']:.2f}) & đổi {next_rack}")
    loose_event('AGV_REPLACEMENT_RACK_READY',floor=floor,branch=branch,rack=next_rack)

async def carton_trip(name,dest,branch):
    b=branch%len(LG.BRANCHES)
    pts=LG.route(dest,poses[name][0],b)
    await follow_path(name,pts,C['carton_conveyor_speed'])
    # Stop at the sorting-belt end, then worker opens carton and places yellow medicine boxes into the hollow blue tray on the U1 rack.
    loose_event('CARTON_WAITING_UNPACK',carton=name,floor=dest,branch=b)
    async with U1_LOCKS[(dest,b)]:
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
            await u1_agv_swap_cycle(dest,b,rack,carried_meds,store_cell,next_rack,spare_cell,old_meds)
            st['spares'].append((rack,rack_idx,store_cell,carried_meds))
            st['active_rack']=next_rack
            st['active_rack_idx']=next_rack_idx
            st['empty_cell']=spare_cell
            st['cartons']=[]
            st['swaps']+=1
        if len(st['trolley_cartons'])>=TROLLEY_CAP:
            await u1_trolley_swap_cycle(dest,b,list(st['trolley_cartons']))
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

async def _z05_operator_deliver_one(floor,worker_idx,batch,carton,stack_pos,u2_idx):
    """Zone 05 worker_idx (0 or 1) walks at constant speed z05_walk_speed to batch['spot_xy'] (whichever Zone 05
    unpack zone has goods), picks carton, walks at constant speed z05_walk_speed to U2 station u2_idx (0..5),
    and places the carton on the floor in front of the U2 worker."""
    z0=LG.LEVELS[floor-1]
    state=Z05_FLOOR_STATE[floor]['worker_poses'][worker_idx]
    px,py,pyaw=LG.z05_pallet_side(batch['spot_xy'],worker_idx)
    drop_pose=LG.u2_carton_drop_pose(floor,u2_idx)
    dx_stand,dy_stand,dyaw=drop_pose[0]+.52,drop_pose[1],math.pi
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
        send({batch['deck']:(batch['spot_xy'][0],batch['spot_xy'][1],-5,0)},transmit=not args.fast)
    head_to_u2=math.atan2(dy_stand-py,dx_stand-px)
    await _z05_move_worker(floor,worker_idx,.22,yaw=head_to_u2,carry_carton=carton)
    await _z05_move_worker(floor,worker_idx,0.,x=dx_stand,y=dy_stand,carry_carton=carton,walk=True)
    await _z05_move_worker(floor,worker_idx,.18,yaw=dyaw,carry_carton=carton)
    async with U2_DELIVERY_LOCKS[(floor,u2_idx)]:
        prev_task=Z05_FLOOR_STATE[floor]['u2_active_task'][u2_idx]
        if prev_task is not None and not prev_task.done():
            await prev_task
        await _z05_move_worker(floor,worker_idx,.40,bend=drop_bend,arm=drop_arm,carry_carton=carton)
        await _z05_move_worker(floor,worker_idx,.25,carton_to=(carton,drop_pose))
        await _z05_move_worker(floor,worker_idx,.30,bend=0.,arm=0.)
        loose_event('Z05_CARTON_DELIVERED_TO_U2',job=batch['job'],floor=floor,zone=batch['zone'],worker=worker_idx,carton=carton,u2_station=u2_idx)
        t=asyncio.create_task(u2_carton_trip(floor,u2_idx,carton,batch['job']))
        Z05_FLOOR_STATE[floor]['u2_active_task'][u2_idx]=t
        batch['u2_tasks'].append(t)
        if len(batch['u2_tasks'])>=batch['total_items']:
            batch['done_event'].set()

async def z05_operator_deliver_one(floor,worker_idx,*args,**kwargs):
    return await _run_busy_worker(LG.z05_worker_id(floor,worker_idx),f'z05_{floor}_{worker_idx}',_z05_operator_deliver_one(floor,worker_idx,*args,**kwargs))

async def z05_worker_loop(floor,worker_idx):
    """Flexible Zone 05 worker coroutine: moves to whichever Zone 05 unpack zone (0 or 1) has cartons waiting,
    and distributes them across all 6 U2 stations (0..5) at constant walking speed."""
    async with Z05_WORKER_LOCKS[(floor,worker_idx)]:
        fst=Z05_FLOOR_STATE[floor]
        n_u2=len(LG.U2_WORKERS)
        while True:
            while True:
                batch=None
                for z_cand in (worker_idx,1-worker_idx):
                    b_cand=fst['zones'].get(z_cand)
                    if b_cand is not None and b_cand['items']:
                        batch=b_cand
                        break
                if batch is None:
                    break
                carton,stack_pos=batch['items'].pop(0)
                u2_idx=None
                while u2_idx is None and not STOP_REQUESTED:
                    for k in range(n_u2):
                        cand=(fst['next_u2']+k)%n_u2
                        t_cand=fst['u2_active_task'][cand]
                        if (not fst['u2_reserved'][cand]) and (t_cand is None or t_cand.done()):
                            u2_idx=cand
                            fst['u2_reserved'][cand]=True
                            fst['next_u2']=(cand+1)%n_u2
                            break
                    if u2_idx is not None:
                        break
                    pending=[t for t in fst['u2_active_task'].values() if t is not None and not t.done()]
                    if pending:
                        await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                    elif any(fst['u2_reserved'].values()):
                        await engine.until(scheduler.time+0.2)
                    else:
                        u2_idx=fst['next_u2']%n_u2
                        fst['u2_reserved'][u2_idx]=True
                        fst['next_u2']=(u2_idx+1)%n_u2
                        break
                if u2_idx is None or STOP_REQUESTED:
                    break
                try:
                    await z05_operator_deliver_one(floor,worker_idx,batch,carton,stack_pos,u2_idx)
                finally:
                    fst['u2_reserved'][u2_idx]=False
            sx,sy,syaw,_=LG.z05_station(floor,worker_idx)
            state=fst['worker_poses'][worker_idx]
            if math.hypot(sx-state['x'],sy-state['y'])>1e-3:
                head_home=math.atan2(sy-state['y'],sx-state['x'])
                await _z05_move_worker(floor,worker_idx,.22,yaw=head_home)
                await _z05_move_worker(floor,worker_idx,0.,x=sx,y=sy,walk=True)
            await _z05_move_worker(floor,worker_idx,.18,yaw=syaw,bend=0.,arm=0.)
            if not any(b_cand is not None and b_cand['items'] for b_cand in fst['zones'].values()):
                break

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
    med_names=[f'u2_med_{floor}_{u2_idx}_{med_offset+m}' for m in range(med_count)]
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
        target_on_rack=LG.u1_med_on_rack(rack_pose,m,tray_slot)
        place_yaw=math.atan2(target_on_rack[1]-sy,target_on_rack[0]-sx)
        await phase(d[0],x=pick_x,y=sy,yaw=syaw,bend=pick_bend,arm=pick_arm,walk=True)
        grab=LG.held_carton(pick_x,sy,syaw,pick_bend,pick_arm,z0=z0)
        await phase(d[1],item_to=(m_name,grab))
        await phase(d[2],x=sx,y=sy,bend=CARRY[0],arm=CARRY[1],carry_item=m_name,walk=True)
        await phase(d[3],yaw=place_yaw,carry_item=m_name)
        await phase(d[4],x=place_x,y=target_on_rack[1],bend=place_bend,arm=place_arm,carry_item=m_name,walk=True)
        await phase(d[5],item_to=(m_name,target_on_rack))
        placed.append((m_name,m,tray_slot))
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
    """When the U2 trolley holds 5 empty cartons, the U2 AGV takes it to Khu 06 and brings a waiting trolley back."""
    st=U2_STATE[(floor,u2_idx)];trolley=st['active_trolley']
    def drop():
        send({c:poses[trolley][:2]+(-5,0) for c in cartons_in_trolley},transmit=not args.fast)
        for c in cartons_in_trolley:FREE_Z05_CARTONS[floor].put_nowait(c)
        loose_event('U2_TROLLEY_DELIVERED_TO_K06',floor=floor,u2_station=u2_idx,trolley=trolley)
    agv_id=f'u2_agv_{floor}_{u2_idx}'
    tp=LG.u2_trolley_pose(floor,u2_idx)
    st['active_trolley']=await _tracked_agv_mission(
        agv_id,f'UNP-U2T-T{floor}S{u2_idx+1}','Khu Unpack U2 (Zone 05)','Đổi xe trolley rỗng U2 tại Khu 06',trolley,
        trolley_exchange(floor,agv_id,u2_home(floor,u2_idx),tp,trolley,
            out_contents=lambda tp:{c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(cartons_in_trolley)},on_drop=drop,
            speed=float(C.get('u2_agv_speed',C.get('u1_agv_speed',.45)))),
        pick_location=f"Trạm U2-T{floor}-S{u2_idx+1} ({tp[0]:.2f}, {tp[1]:.2f})",
        drop_location=f"Khu 06 <-> Trạm U2-T{floor}-S{u2_idx+1}")
    st['trolley_cartons']=[];st['trolley_swaps']+=1
    loose_event('U2_REPLACEMENT_TROLLEY_READY',floor=floor,u2_station=u2_idx,trolley=st['active_trolley'])

async def u2_agv_swap_cycle(floor,u2_idx,full_rack,meds_on_rack,store_cell,next_rack,spare_cell,old_spare_meds=()):
    """U2 AGV stores the full rack (+ medicine boxes) in an empty Kho 04 cell and brings the spare rack back."""
    def stored():
        send({m:poses[full_rack][:2]+(-5,0) for m,_,_ in meds_on_rack},transmit=not args.fast)
        loose_event('U2_AGV_RACK_STORED',floor=floor,u2_station=u2_idx,rack=full_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
    loose_event('U2_AGV_REPLACEMENT_RACK_PICKUP',floor=floor,u2_station=u2_idx,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    agv_id=f'u2_agv_{floor}_{u2_idx}'
    hp=u2_home(floor,u2_idx)
    await _tracked_agv_mission(
        agv_id,f'UNP-U2R-T{floor}S{u2_idx+1}','Khu Unpack U2 (Zone 05)','Cất rack U2 vào Kho 04 & lấy rack rỗng',full_rack,
        rack_swap(floor,agv_id,hp,full_rack,
            lambda rp:{m:LG.u1_med_on_rack(rp,mi,ts) for m,mi,ts in meds_on_rack},store_cell,next_rack,spare_cell,
            speed=float(C.get('u2_agv_speed',C.get('u1_agv_speed',.45))),on_stored=stored),
        pick_location=f"Trạm U2-T{floor}-S{u2_idx+1} ({hp[0]:.2f}, {hp[1]:.2f})",
        drop_location=f"Kho 04 ({store_cell['x']:.2f}, {store_cell['y']:.2f}) & đổi {next_rack}")
    loose_event('U2_AGV_REPLACEMENT_RACK_READY',floor=floor,u2_station=u2_idx,rack=next_rack)

async def u2_carton_trip(floor,u2_idx,carton,job):
    """U2 station cycle once a carton has been placed on the floor in front of U2 worker u2_idx."""
    async with U2_LOCKS[(floor,u2_idx)]:
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
            await u2_agv_swap_cycle(floor,u2_idx,rack,carried_meds,store_cell,next_rack,spare_cell,old_meds)
            st['spares'].append((rack,rack_idx,store_cell,carried_meds))
            st['active_rack']=next_rack
            st['active_rack_idx']=next_rack_idx
            st['empty_cell']=spare_cell
            st['cartons']=[]
            st['swaps']+=1
        if len(st['trolley_cartons'])>=TROLLEY_CAP:
            await u2_trolley_swap_cycle(floor,u2_idx,list(st['trolley_cartons']))

# ---------------------------------------------------------------- Khu P1 (14 Picking Stations: North 0..6 & West 7..13) <-> Kho 04 & Khu 06
async def _p1_operator_cycle(floor,st_idx,carton,rack_idx):
    """P1 picking worker at (floor, st_idx):
    1. Turns to the P1 Trolley (at st['mark']), picks 1 empty carton shell, and places it onto the working edge of the desk.
    2. Picks 3 yellow medicine box clusters (m=0..2) from the loaded Rack in front of the station (st['rack']) and places them into the carton on the desk.
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
    med_names=[f'p1_med_{floor}_{st_idx}_{m}' for m in range(med_count)]
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
                if carry_item==carton:
                    for m,m_name in enumerate(med_names):
                        out[m_name]=LG.p1_med_in_carton(out[carton],m)
            elif item_to is not None:
                t_name,t_pose=item_to
                arc_z=0.06*math.sin(math.pi*q) if ('med' in t_name or 'carton' in t_name) else 0.
                cz=i0[2]+(t_pose[2]-i0[2])*q+arc_z
                out[t_name]=(i0[0]+(t_pose[0]-i0[0])*q,i0[1]+(t_pose[1]-i0[1])*q,cz,i0[3]+(t_pose[3]-i0[3])*q)
                if t_name==carton:
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
    await phase(d[1],item_to=(carton,grab_packed))
    lean_x=sx-.25 if st_idx<7 else sx
    lean_y=sy if st_idx<7 else sy+.25
    await phase(d[2],x=lean_x,y=lean_y,yaw=aside_yaw,bend=.36,arm=1.10,carry_item=carton,walk=True)
    await phase(d[3],item_to=(carton,aside_c_pose))
    loose_event('P1_PACKED_CARTON_COMPLETED',floor=floor,p1_station=st_idx,carton=carton)
    await phase(d[4],x=sx,y=sy,yaw=syaw,bend=0.,arm=0.,walk=True)
    st['aside_carton']=(carton,list(med_names))

async def p1_operator_cycle(floor,st_idx,*args,**kwargs):
    return await _run_busy_worker(LG.p1_worker_id(floor,st_idx),f'p1_{floor}_{st_idx}',_p1_operator_cycle(floor,st_idx,*args,**kwargs))

async def p1_agv_rack_swap_cycle(floor,st_idx,empty_rack,store_cell,next_rack,next_rack_idx,spare_cell):
    """After every packed carton the P1 AGV stores the now-empty rack in an empty Kho 04 cell and brings the
    loaded spare rack (3 yellow medicine boxes) to the station."""
    med_count=int(C.get('p1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    next_meds=[f'p1_med_{floor}_{st_idx}_{m}' for m in range(med_count)]
    def stored():
        loose_event('P1_AGV_EMPTY_RACK_STORED',floor=floor,p1_station=st_idx,rack=empty_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
        # The loaded spare rack in Kho 04 carries the next 3 medicine boxes.
        sp=(spare_cell['x'],spare_cell['y'],LG.LEVELS[floor-1],poses[next_rack][3] if len(poses[next_rack])>3 else 0.)
        send({m:LG.u1_med_on_rack(sp,k,0) for k,m in enumerate(next_meds)},transmit=not args.fast)
        loose_event('P1_AGV_LOADED_RACK_PICKUP',floor=floor,p1_station=st_idx,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    agv_id=f'p1_agv_{floor}_{st_idx}'
    hp=p1_home(floor,st_idx)
    await _tracked_agv_mission(
        agv_id,f'PK-P1R-T{floor}S{st_idx+1:02d}','Khu Đóng Gói P1','Cất rack rỗng vào Kho 04 & lấy rack hàng',empty_rack,
        rack_swap(floor,agv_id,hp,empty_rack,None,store_cell,next_rack,spare_cell,
            next_contents=lambda rp:{m:LG.u1_med_on_rack(rp,k,0) for k,m in enumerate(next_meds)},
            speed=float(C.get('p1_agv_speed',C.get('u1_agv_speed',.45))),on_stored=stored),
        pick_location=f"Trạm P1-T{floor}-S{st_idx+1:02d} ({hp[0]:.2f}, {hp[1]:.2f})",
        drop_location=f"Kho 04 ({store_cell['x']:.2f}, {store_cell['y']:.2f}) & lấy {next_rack}")
    loose_event('P1_AGV_LOADED_RACK_READY',floor=floor,p1_station=st_idx,rack=next_rack)

async def p1_agv_trolley_swap_cycle(floor,st_idx):
    """When all 5 empty cartons of the P1 trolley are used, the P1 AGV takes it to Khu 06 and brings back a
    waiting trolley loaded with 5 empty cartons."""
    st=P1_STATE[(floor,st_idx)];trolley=st['active_trolley']
    carton_names=[f'p1_carton_{floor}_{st_idx}_{c}' for c in range(TROLLEY_CAP)]
    def pick(t):   # restocked trolley in Khu 06: the 5 empty cartons appear in it before the AGV lifts it
        tp=poses[t];send({c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(carton_names)},transmit=not args.fast)
    def drop():
        loose_event('P1_EMPTY_TROLLEY_RETURNED_TO_K06',floor=floor,p1_station=st_idx,trolley=trolley)
    agv_id=f'p1_agv_{floor}_{st_idx}'
    tp=LG.p1_trolley_pose(floor,st_idx)
    st['active_trolley']=await _tracked_agv_mission(
        agv_id,f'PK-P1T-T{floor}S{st_idx+1:02d}','Khu Đóng Gói P1','Đổi trolley thùng rỗng P1 tại Khu 06',trolley,
        trolley_exchange(floor,agv_id,p1_home(floor,st_idx),tp,trolley,
            on_drop=drop,in_contents=lambda tp:{c:LG.trolley_carton_pose(tp,i) for i,c in enumerate(carton_names)},on_pick=pick,
            speed=float(C.get('p1_agv_speed',C.get('u1_agv_speed',.45)))),
        pick_location=f"Trạm P1-T{floor}-S{st_idx+1:02d} ({tp[0]:.2f}, {tp[1]:.2f})",
        drop_location=f"Khu 06 <-> Trạm P1-T{floor}-S{st_idx+1:02d}")
    st['trolley_remaining']=list(reversed(carton_names))
    st['trolley_swaps']+=1
    loose_event('P1_FULL_TROLLEY_READY',floor=floor,p1_station=st_idx,trolley=st['active_trolley'])

async def p1_station_loop(floor,st_idx):
    """Continuous picking loop for P1 station (floor, st_idx) across both North (0..6) and West (7..13) rows."""
    row=0 if st_idx<7 else 1
    row_sem=P1_ROW_SEMAPHORES[(floor,row)]
    await engine.until(.5+.60*(st_idx%7)+.4*(floor-1))
    while p1_running:
        async with P1_LOCKS[(floor,st_idx)]:
            if not p1_running:break
            st=P1_STATE[(floor,st_idx)]
            if not st['trolley_remaining']:
                async with row_sem:
                    await p1_agv_trolley_swap_cycle(floor,st_idx)
            carton=st['trolley_remaining'].pop(0)
            rack=st['active_rack']
            rack_idx=st['active_rack_idx']
            await p1_operator_cycle(floor,st_idx,carton,rack_idx)
            st['cartons_packed']+=1
            # Immediately after 1 packed carton (3 yellow medicine box turns), swap the empty rack for a new loaded rack from Kho 04
            store_cell=st['empty_cell']
            next_rack,next_rack_idx,spare_cell=st['spares'].pop(0)
            async with row_sem:
                await p1_agv_rack_swap_cycle(floor,st_idx,rack,store_cell,next_rack,next_rack_idx,spare_cell)
            st['spares'].append((rack,rack_idx,store_cell))
            st['active_rack']=next_rack
            st['active_rack_idx']=next_rack_idx
            st['empty_cell']=spare_cell
            st['rack_swaps']+=1
            if not st['trolley_remaining'] and p1_running:
                async with row_sem:
                    await p1_agv_trolley_swap_cycle(floor,st_idx)
        await engine.until(scheduler.time+.1)

async def z05_unpack_pallet(floor,zone,spot_xy,pallet_model,job,spot_idx=None):
    """When an outbound pallet from Kho 01 stops at its dedicated Zone 05 unpack zone (floor, zone, spot_xy):
    1. Replaces the monolithic pallet with a blue plastic pallet deck + 18 individual cartons.
    2. Both Zone 05 workers on that floor move to the active unpack zone(s) and distribute the 18 cartons across all 6 U2 stations at constant walking speed.
    3. Each U2 worker opens the carton on the ground, picks 3 yellow medicine boxes into the blue tray on its U2 rack, and U2 AGV stores the rack in Kho 04."""
    spot_released=False
    try:
        async with Z05_LOCKS[(floor,zone)]:
            z0=LG.LEVELS[floor-1]
            z05_per=int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))
            deck=f'z05_deck_{floor}_{zone}'
            cartons=[await FREE_Z05_CARTONS[floor].get() for _ in range(z05_per)]
            stack=LG.z05_stack_positions(spot_xy,floor,z05_per)
            send({pallet_model:(spot_xy[0],spot_xy[1],-5,0),deck:(spot_xy[0],spot_xy[1],z0,0.),
                  **{c:p for c,p in zip(cartons,stack)}},transmit=not args.fast)
            if CONTINUOUS_MODE:
                FREE_EVEN_PALLETS.put_nowait(pallet_model)
                await SLOT_COORD.notify()
            loose_event('Z05_PALLET_UNPACK_START',job=job,floor=floor,zone=zone,cartons=z05_per)
            fst=Z05_FLOOR_STATE[floor]
            batch={
                'job':job,
                'zone':zone,
                'spot_xy':spot_xy,
                'deck':deck,
                'items':list(zip(cartons,stack)),
                'total_items':z05_per,
                'u2_tasks':[],
                'done_event':asyncio.Event(),
            }
            fst['zones'][zone]=batch
            for w_idx in range(len(LG.Z05_WORKERS)):
                wt=fst['worker_tasks'][w_idx]
                if wt is None or wt.done():
                    fst['worker_tasks'][w_idx]=asyncio.create_task(z05_worker_loop(floor,w_idx))
            await batch['done_event'].wait()
            fst['zones'][zone]=None
            send({deck:(spot_xy[0],spot_xy[1],-5,0)},transmit=not args.fast)
            loose_event('Z05_PALLET_EMPTY',job=job,floor=floor,zone=zone)
            if batch['u2_tasks']:
                background_tasks.extend(batch['u2_tasks'])
        if spot_idx is not None:
            spot_released=True
            await _release_z05_spot(floor,zone,spot_idx)
    finally:
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

async def requests(total):
    request=0
    req_limit=total if total is not None else (OUTBOUND_TARGET if OUTBOUND_TARGET>0 else None)
    while not STOP_REQUESTED and (req_limit is None or request<req_limit):
        await engine.until(request*C['outbound_interval_s'])
        if total is None:
            while not STOP_REQUESTED and out_queue.qsize()>=8:
                await engine.until(scheduler.time+0.5)
        if STOP_REQUESTED:break
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
                claimed=await _claim_z05_spot(worker)
                if claimed is None or STOP_REQUESTED:return
                target_floor,claimed_spot_idx=claimed
            job,level,sid,lane,p=await stored.get()
            if STOP_REQUESTED:return
            if CONTINUOUS_MODE:await SLOT_COORD.notify()
            floor=(target_floor-1) if target_floor is not None else (job%4)
            s=f'shuttle_{level}';z=C['storage_levels'][level]
            out=LAYOUT.data['ports']['out'][worker];ox,oy=out['lift'];ol=f'out_lift_{worker}'
            oz=C['output_levels'][floor]+.3
            outbound_active+=1
            try:
                buf_lock=out_buffers[(worker,level)]
                buf_released=False
                gate_lock=out_floor_gates[(worker,floor)]
                gate_held=False
                await buf_lock.acquire()
                try:
                    t_req_q=REQ_QUEUE_T.pop(request,None)
                    pre_lift_wait=max(0.0,scheduler.time-t_req_q) if t_req_q is not None else 0.0
                    t_shw=scheduler.time
                    async with shuttles[level]:
                        sh_wait=max(0.0,scheduler.time-t_shw)
                        tracker.record_stage_wait('out_port_to_shuttle',sh_wait)
                        slot_pt=LAYOUT.slot_point(sid)
                        bx,by=out['buffer']
                        wh_pick=f"Ô kệ {sid} ({slot_pt[0]:.2f}, {slot_pt[1]:.2f}, T{level+1})"
                        wh_drop=f"Bàn đệm Xuất {worker+1} ({bx:.2f}, {by:.2f}, T{level+1})"
                        wh_rec=tracker.start_shuttle_task(
                            s,'Xuất kho',f'WH-OUT-{request+1:04d}',p,level,sid,scheduler.time,
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
                        port_wait=max(0.0,scheduler.time-t_port_w0)
                        total_lift_wait=pre_lift_wait+port_wait
                        tracker.record_lift_wait(ol,total_lift_wait)
                        tracker.record_stage_wait('out_req_to_lift',total_lift_wait)
                        tracker.start_lift_task(ol,'Xuất kho',f'LF-OUT-{request+1:04d}',p)
                        t_ol0=scheduler.time
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
                        await gate_lock.acquire()
                        gate_held=True
                        event(job,'OUTPUT_GATE_TRANSFER')
                        await engine.move({p:tuple(out['corner'])+(oz,0)},1)
                        tracker.record_lift_leg(ol,abs(z-z_ol0)+abs(oz-z),max(0.0,scheduler.time-t_ol0))
                        tracker.finish_lift_task(ol)
                        tracker.finish_warehouse_process(wh_rec,scheduler.time,extra_lift_wait_s=port_wait)
                    finally:
                        if port_held:
                            out_ports[worker].release()
                finally:
                    if not buf_released:
                        buf_lock.release()
                try:
                    await engine.move({p:tuple(out['gate'])+(oz,0)},1)
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
                    await engine.move({p:(spot[0],spot[1],oz,0)},1)
                    event(job,'UNPACK_SPOT',spot=spot_idx,floor=floor+1)
                    spot_handed=True
                    background_tasks.append(asyncio.create_task(z05_unpack_pallet(floor+1,worker,spot,p,job,spot_idx=claimed_spot_idx)))
                    event(job,'COMPLETE');completed+=1
                finally:
                    if gate_held:
                        gate_lock.release()
            finally:
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
    global p1_running
    for key in ['inbound_interval_s','outbound_interval_s']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    for key in ['agv_following_gap_m','speed_multiplier','agv_speed','lift_speed','shuttle_speed','conveyor_speed','agv_acceleration','lift_acceleration','shuttle_acceleration','conveyor_acceleration','agv_jerk','lift_jerk','shuttle_jerk','conveyor_jerk','agv_angular_speed','agv_angular_acceleration','agv_angular_jerk','lift_settle_s','shuttle_positioning_s','shuttle_turn_s','shuttle_lift_s','shuttle_availability','shuttle_utilization','lift_availability','lift_utilization']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    if C['agv_following_gap_m']<.25:raise ValueError('agv_following_gap_m must be at least 0.25m')
    if len(INBOUND_PICKUP_SLOTS)!=4:raise ValueError('inbound_pickup_slots must contain exactly 4 pickup positions')
    if not isinstance(CYCLE_DURATION_S,(int,float)) or isinstance(CYCLE_DURATION_S,bool) or not math.isfinite(CYCLE_DURATION_S) or CYCLE_DURATION_S<0:
        raise ValueError('cycle_duration_s must be non-negative')
    (ROOT/'events.jsonl').write_text('')
    for attempt in range(60):
        try:
            send({**assembly(0,PARKS[0],.02),**assembly(1,PARKS[1],.02)});break
        except RuntimeError:
            if attempt==59:raise
            await asyncio.sleep(1)
    for i in range(10):poses[f'shuttle_{i}']=LAYOUT.slot_point(LAYOUT.parking[i//9])+(C['storage_levels'][i%9],)
    for f,zf in enumerate(LG.LEVELS,1):
        for b in range(len(LG.BRANCHES)):
            bx,u1_y,z0,_=LG.u1_rack_pose(f,b)
            poses[f'u1_agv_{f}_{b}']=(bx,u1_y,z0,math.pi/2)
            poses[f'u1_rack_{f}_{b}_0']=(bx,u1_y,z0,0.)
            poses[f'u1_trolley_{f}_{b}']=LG.u1_trolley_pose(f,b)
            for r_idx in range(1,LG.U1_RACKS_PER_BRANCH):
                cell=LG.U1_BRANCH_CELLS[b][r_idx]
                poses[f'u1_rack_{f}_{b}_{r_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
        for u2_idx in range(len(LG.U2_WORKERS)):
            rx,ry,z0,ryaw=LG.u2_rack_pose(f,u2_idx)
            poses[f'u2_agv_{f}_{u2_idx}']=(rx,ry,z0,0.)
            poses[f'u2_rack_{f}_{u2_idx}_0']=(rx,ry,z0,ryaw)
            poses[f'u2_trolley_{f}_{u2_idx}']=LG.u2_trolley_pose(f,u2_idx)
            for r_idx in range(1,LG.U2_RACKS_PER_STATION):
                cell=LG.U2_STATION_CELLS[u2_idx][r_idx]
                poses[f'u2_rack_{f}_{u2_idx}_{r_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
        for st_idx in range(len(LG.P1_STATIONS)):
            rx,ry,z0,ryaw=LG.p1_rack_pose(f,st_idx)
            poses[f'p1_agv_{f}_{st_idx}']=(rx,ry,z0,ryaw)
            poses[f'p1_rack_{f}_{st_idx}_0']=(rx,ry,z0,ryaw)
            t_pose=LG.p1_trolley_pose(f,st_idx)
            poses[f'p1_trolley_{f}_{st_idx}']=t_pose
            for c_idx in range(TROLLEY_CAP):
                poses[f'p1_carton_{f}_{st_idx}_{c_idx}']=LG.trolley_carton_pose(t_pose,c_idx)
            for m in range(LG.U1_MED_PER_CARTON):
                poses[f'p1_med_{f}_{st_idx}_{m}']=LG.u1_med_on_rack((rx,ry,z0,ryaw),m,0)
            for r_idx in range(1,LG.P1_RACKS_PER_STATION):
                cell=LG.P1_STATION_CELLS[st_idx][r_idx]
                c_pose=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
                poses[f'p1_rack_{f}_{st_idx}_{r_idx}']=c_pose
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
    if hidden_even:send(hidden_even,transmit=False)
    if LOOSE_PALLETS:   # loose-goods pallet decks and cartons start hidden below their spot
        while not FREE_CARTONS.empty():FREE_CARTONS.get_nowait()
        for i in range(LOOSE_CARTON_POOL):FREE_CARTONS.put_nowait(f'carton_{i}')
        for f in LG.floors():
            while not FREE_Z05_CARTONS[f].empty():FREE_Z05_CARTONS[f].get_nowait()
            for c_idx in range(Z05_CARTON_POOL):FREE_Z05_CARTONS[f].put_nowait(f'z05_carton_{f}_{c_idx}')
        hidden={f'loose_pallet_{s}':(sp[0],sp[1],-5,sp[2]) for s,sp in enumerate(LOOSE_SPOTS)}
        hidden.update({f'carton_{i}':(LOOSE_SPOTS[0][0],LOOSE_SPOTS[0][1],-5,0) for i in range(LOOSE_CARTON_POOL)})
        u1_med_total=max(1,U1_PER_RACK)*int(C.get('u1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
        u2_med_total=max(1,U2_PER_RACK)*int(C.get('u2_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
        for f in LG.floors():
            for b in range(len(LG.BRANCHES)):
                bx,u1_y,_,_=LG.u1_rack_pose(f,b)
                for m in range(u1_med_total):
                    hidden[f'u1_med_{f}_{b}_{m}']=(bx,u1_y,-5,0)
            for z_idx in range(len(LG.Z05_WORKERS)):
                wx,wy,_,_=LG.z05_station(f,z_idx)
                hidden[f'z05_deck_{f}_{z_idx}']=(wx,wy,-5,0)
            w0x,w0y,_,_=LG.z05_station(f,0)
            for c_idx in range(Z05_CARTON_POOL):
                hidden[f'z05_carton_{f}_{c_idx}']=(w0x,w0y,-5,0)
            for u2_idx in range(len(LG.U2_WORKERS)):
                rx,ry,_,_=LG.u2_rack_pose(f,u2_idx)
                for m in range(u2_med_total):
                    hidden[f'u2_med_{f}_{u2_idx}_{m}']=(rx,ry,-5,0)
        send(hidden,transmit=False)
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
    workers=asyncio.gather(arrivals(total),requests(even_total),agv_worker(0),agv_worker(1),outbound_worker(0),outbound_worker(1),loose_goods())
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
        z05_wt=[t for fst in Z05_FLOOR_STATE.values() for t in fst['worker_tasks'].values() if t is not None]
        all_bg=[t for t in [duration_watch,stop_wait,checkpoint_task,*background_tasks,*p1_tasks,*z05_wt] if t is not None]
        for t in all_bg:
            if not t.done():t.cancel()
        if all_bg:await asyncio.gather(*all_bg,return_exceptions=True)
        await asyncio.gather(ticker,return_exceptions=True)
        if args.fast:send(dict(poses))
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
        print(f"Excel report saved: {excel_paths['latest']} ({excel_paths['timestamped']})",flush=True)
        print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:event(-1,'ERROR',message=str(exc));raise
