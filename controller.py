"""Deterministic kinematic process demonstrator. No collision/dynamics claims."""
import argparse
import asyncio
import json
import math
import time
from pathlib import Path
from gz.transport13 import Node
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.boolean_pb2 import Boolean
from agv_motion import PARKS, OFFSET, pickup_position, assembly, samples, check_pose, route_to_pickup, route_to_gate, route_home, PICKUP_LEGS_FROM_LANE
from rail_geometry import deck_name, wheel_name, supported
from cad_navigation import LAYOUT
from motion_scheduler import Scheduler
from motion_profile import SCurve, bounds, intersects, blend
from agv_motion import wrap
from agv_traffic import Plan,compatible
import loose_goods as LG
import truck_scene
from dock_flow import DockFlow

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
U1_LOCKS={(f,b):asyncio.Lock() for f in LG.floors() for b in range(len(LG.BRANCHES))}
Z05_LOCKS={(f,z_idx):asyncio.Lock() for f in LG.floors() for z_idx in range(len(LG.Z05_WORKERS))}
U2_PER_RACK=max(1,int(C.get('u2_cartons_per_rack',1)))
U2_LOCKS={(f,u2_idx):asyncio.Lock() for f in LG.floors() for u2_idx in range(len(LG.U2_WORKERS))}

def _init_u1_state():
    state={}
    for f in LG.floors():
        for b in range(len(LG.BRANCHES)):
            cells=[dict(c) for c in LG.U1_BRANCH_CELLS[b]]
            state[(f,b)]={
                'active_rack':f'u1_rack_{f}_{b}_0',
                'empty_cell':cells[0],
                'spares':[(f'u1_rack_{f}_{b}_{r}',cells[r],[] ) for r in range(1,LG.U1_RACKS_PER_BRANCH)],
                'cartons':[],
                'swaps':0,
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
            'worker_tasks':{0:None,1:None},
            'worker_poses':w_poses,
        }
    return state

U1_STATE=_init_u1_state()
U2_STATE=_init_u2_state()
Z05_FLOOR_STATE=_init_z05_floor_state()
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
events=[]
scheduler=Scheduler()
parser=argparse.ArgumentParser()
parser.add_argument('--fast',action='store_true',help='Accelerated integration verification')
parser.add_argument('--jobs',type=int,default=C['jobs'])
args=parser.parse_args()

def _pose_changed(old,new):
    if old is None or len(old)!=len(new):return True
    return any(abs(a-b)>1e-4 for a,b in zip(old,new))

def _flush_tx(batch):
    if not batch:return
    req=Pose_V()
    for name,xyz in batch.items():
        if not _pose_changed(last_tx_poses.get(name),xyz):continue
        p=req.pose.add(); p.name=name
        p.position.x,p.position.y,p.position.z=xyz[:3]
        if len(xyz)>=6:   # (x, y, z, roll, pitch, yaw): animated operator parts
            r,pi_,yw=xyz[3],xyz[4],xyz[5];cr,sr,cp,sp,cy_,sy_=math.cos(r/2),math.sin(r/2),math.cos(pi_/2),math.sin(pi_/2),math.cos(yw/2),math.sin(yw/2)
            p.orientation.w=cr*cp*cy_+sr*sp*sy_;p.orientation.x=sr*cp*cy_-cr*sp*sy_
            p.orientation.y=cr*sp*cy_+sr*cp*sy_;p.orientation.z=cr*cp*sy_-sr*sp*cy_
        else:
            yaw=xyz[3] if len(xyz)>3 else 0
            p.orientation.z=math.sin(yaw/2);p.orientation.w=math.cos(yaw/2)
        last_tx_poses[name]=tuple(xyz)
    if not req.pose:return
    for attempt in range(4):
        ok,reply=node.request('/world/warehouse/set_pose_vector',req,Pose_V,Boolean,5000)
        if ok and reply.data: break
        if ok: raise RuntimeError('Gazebo rejected pose update')
        time.sleep(.3)
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

def event(job,state,**extra):
    record={'job':job,'state':state,'demo_time_s':round(scheduler.time,5),**extra}
    events.append(record)
    (ROOT/'status.json').write_text(json.dumps(record,indent=2))
    with (ROOT/'events.jsonl').open('a') as f: f.write(json.dumps(record)+'\n')
    print(f'Pallet {job+1:02d}: {state}',flush=True)

route_checks=0

# Concurrent inbound/outbound process dispatcher.
import asyncio

class Engine:
    def __init__(self):
        self.motions={};self.running=True;self.max_active=0;self.overlap_ticks=0;self.dual_agv_ticks=0;self.multi_shuttle_ticks=0;self.following_ticks=0
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
        # speed_multiplier k is a global time scale: every object keeps its real parameters (speeds, dwell
        # times, arrival/request intervals) and the demo clock advances k simulated seconds per real second.
        global in_engine_loop
        in_engine_loop=True
        frame=1/20
        dt=1/30 if args.fast else frame*C['speed_multiplier']
        try:
            while self.running:
                await asyncio.sleep(0 if args.fast else frame)
                for task in background_tasks:
                    if task.done() and not task.cancelled() and task.exception() is not None:raise task.exception()
                scheduler.time+=dt
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
                if not args.fast and pending_tx:
                    tx_batch=dict(pending_tx);pending_tx.clear()
                    _flush_tx(tx_batch)
                self.max_active=max(self.max_active,len(set(id(v) for v in self.motions.values())))
                if inbound_active and outbound_active and changes:self.overlap_ticks+=1
                for m in finished:
                    for n in m['targets']:del self.motions[n]
                    m['future'].set_result(None)
                if scheduler.time>20000:raise RuntimeError('Dispatch stalled')
        finally:
            in_engine_loop=False
    async def until(self,when):
        while scheduler.time+1e-6<when:await asyncio.sleep(0 if args.fast else .005)

background_tasks=[]
engine=Engine();agv_load=[None,None];pickups=[None,None]
dock_flow=DockFlow(C,engine,send,poses,args.fast,lambda state,**kw:loose_event(state,**kw))
dock_flow.clock=lambda:scheduler.time
reservations={}
# One pallet per inbound pickup bay: an arrival waits until the AGV has carried the previous load away.
pickup_bays=[asyncio.Semaphore(1) for _ in INBOUND_PICKUP_SLOTS]
in_ports=[asyncio.Lock() for _ in range(2)];out_ports=[asyncio.Lock() for _ in range(2)]
shuttles=[asyncio.Lock() for _ in range(9)]
in_queue=asyncio.Queue();stored=asyncio.Queue();out_queue=asyncio.Queue()
inbound_active=0;outbound_active=0
completed=0;stored_count=0;max_pending=0;shuttle_route_log=[]

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
    try:
        await engine.move(end_parts,C['agv_speed'],duration=duration,kind='agv',transform=lambda q:assembly(i,base(q),height,payload),shape=shape)
    finally:reservations.pop(i,None)

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
    await engine.move(wheel_heights(s,axis),kind='shuttle',duration=C['shuttle_turn_s'])
    wheel_modes[s]=axis

async def shuttle_async(s,end,z,pallet=None):
    route=LAYOUT.path(poses[s][:2],end)
    for a,b in zip(route,route[1:]):
        axis='x' if abs(b[0]-a[0])>1e-7 else 'y'
        if axis!=wheel_modes[s]:await wheel_switch(s,axis)
        targets={s:tuple(b)+(z,)}
        if pallet:targets[pallet]=tuple(b)+(z+max(0,deck_offsets[s]),0)
        await engine.move(targets,C['shuttle_speed'])
    shuttle_route_log.append({'shuttle':s,'path':route})

async def deck_async(s,p,height):
    # Fine positioning over the slot/buffer, then raise or lower the deck with the pallet.
    await engine.until(scheduler.time+C['shuttle_positioning_s'])
    deck_offsets[s]=height;x,y,z=poses[s][:3]
    await engine.move({s:(x,y,z),deck_name(s):(x,y,z+height),p:(x,y,z+max(0,height),0)},duration=C['shuttle_lift_s'])

async def arrivals(total):
    # Arrivals keep their configured cadence; a pallet is only placed once its pickup bay is empty.
    async def place(job):
        global max_pending
        if pickup_bays:await pickup_bays[job%len(pickup_bays)].acquire()
        flow=inbound_classification(job)
        if flow=='odd' and LOOSE_SPOTS:
            global odd_pallet_seq
            odd_idx=odd_pallet_seq;odd_pallet_seq+=1
            spot=LOOSE_SPOTS[odd_idx%len(LOOSE_SPOTS)]
            spot_lock=ODD_SPOT_LOCKS[odd_idx%len(ODD_SPOT_LOCKS)]
            base_idx=(odd_idx%max(1,LOOSE_PALLETS))*LOOSE_PER
            names=[f'carton_{base_idx+j}' for j in range(LOOSE_PER)]
            stack=LG.stack_positions(spot,LOOSE_PER)
            async def receive_odd(dock_i,cartons):
                send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
                send({n:p+(spot[2],) for n,p in zip(names,stack)},transmit=not args.fast)
                for n,p in zip(names,stack):
                    op=op_of_spot=min((w for w in LG.L['workers'] if w['area']=='carton_inbound'),key=lambda w:abs(w['x']-spot[0]))
                    await operator_cycle(LG.OPERATORS.index(op),op,spot,n,p)
                    dest=LG.floors()[loose_stats['cartons_on_belt']%len(LG.floors())]
                    branch=loose_stats['cartons_on_belt']//len(LG.floors())
                    loose_stats['cartons_on_belt']+=1
                    loose_event('CARTON_ON_BELT',carton=n,pallet=job,floor=dest)
                    background_tasks.append(asyncio.create_task(carton_trip(n,dest,branch)))
                loose_stats['pallets_unpacked']+=1
                loose_event('LOOSE_PALLET_EMPTY',pallet=odd_idx,spot=odd_idx%len(LOOSE_SPOTS))
            async with spot_lock:
                try:
                    await dock_flow.prepare('odd',job,spot,names,receive_odd)
                finally:
                    if pickup_bays:pickup_bays[job%len(pickup_bays)].release()
            return
        async def receive(i,cartons):
            send({n:poses[n][:2]+(-5,0) for n in cartons},transmit=not args.fast)
            p=f'pallet_{job}';send({p:inbound_pickup_position(job)},transmit=not args.fast)
        await dock_flow.prepare('even',job,inbound_pickup_position(job),list(range(18)),receive)
        event(job,'PICKUP_BAY_LOADED');await in_queue.put(job)
        max_pending=max(max_pending,in_queue.qsize())
    placements=[]
    for job in range(total):
        await engine.until(job*C['inbound_interval_s'])
        flow='even'
        docks=truck_scene.selected_docks({**C,'truck_count_visible':C.get('dock_active_count',4)})
        event(job,'INBOUND_ARRIVAL',dock=docks[job%len(docks)]['id'] if docks else None)
        placements.append(asyncio.create_task(place(job)))
    await asyncio.gather(*placements)
    for _ in range(2):await in_queue.put(None)

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
        traffic=dock_flow.gate.agv();await traffic.__aenter__()
        p=f'pallet_{job}';level=job%9;z=C['storage_levels'][level];s=f'shuttle_{level}'
        inp=LAYOUT.data['ports']['in'][lane];ix,iy=inp['lift'];bx,by=inp['buffer'];il=f'in_lift_{lane}'
        route=route_to_pickup(lane,job,inbound_pickup_position(job))
        if not parked:route=[((None,None,0,math.pi),'TURN_TOWARDS_PICKUP')]+route[PICKUP_LEGS_FROM_LANE:]
        await follow_async(job,lane,route,pickup=p)
        await engine.move(assembly(lane,poses[f'agv_{lane}'],.2,p),.4)
        event(job,'AGV_CARRY_TO_INBOUND',agv=lane+1)
        # Reserve the inbound port (chain + lift + level buffer) only once the load is on the forks.
        await in_ports[lane].acquire()
        async def prepare(s=s,z=z,level=level,ix=ix):
            async with shuttles[level]:
                await shuttle_async(s,(ix,LAYOUT.data['road_y'][0]),z)
        ready=asyncio.create_task(prepare())
        async def lift_down(job=job,il=il,ix=ix,iy=iy):
            await engine.move({il:(ix,iy,.3)},C['lift_speed'])
            await settled(job,il,.3)
        lift_ready=asyncio.create_task(lift_down())
        gate=route_to_gate(lane,ix)
        await follow_async(job,lane,gate[:-1],.2,p)
        await lift_ready  # interlock: lift at floor level and settled before approaching the chain
        await follow_async(job,lane,gate[-1:],.2,p)
        if pickup_bays:pickup_bays[job%len(pickup_bays)].release()
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
        async def downstream(job=job,p=p,level=level,z=z,s=s,inp=inp,ix=ix,iy=iy,bx=bx,by=by,il=il,ready=ready):
            global stored_count,inbound_active
            inbound_active+=1
            # Infeed turntable: the pallet arrives with its 1.2 m side along the forks (north-south) and is
            # turned 90 degrees so it enters the store as X = 1.2 m, Y = 1.0 m (agreed design).
            event(job,'INFEED_TURNTABLE')
            await engine.move({p:poses[p][:3]+(0,)},kind='conveyor',duration=TURNTABLE_S)
            event(job,'CHAIN_TO_INPUT_LIFT')
            await engine.move({p:(ix,iy,.3,0)},1)
            event(job,'INPUT_LIFT_ASCEND')
            await engine.move({il:(ix,iy,z),p:(ix,iy,z,0)},C['lift_speed'])
            await settled(job,il,z)
            event(job,'INPUT_BUFFER_TRANSFER')
            await engine.move({p:(bx,by,z,0)},C['conveyor_speed'])
            event(job,'INPUT_BUFFER_RECEIVE')
            await ready
            async with shuttles[level]:
                await shuttle_async(s,inp['buffer'],z)
                await deck_async(s,p,.12)
                in_ports[lane].release()
                sid=LAYOUT.job_slots[job%len(LAYOUT.job_slots)];slot=LAYOUT.slot_point(sid)
                await shuttle_async(s,slot,z,p)
                await deck_async(s,p,-.06)
                access=(slot[0],min(LAYOUT.data['road_y'],key=lambda yy:abs(yy-slot[1])))
                await shuttle_async(s,access,z)
                event(job,'PALLET_STORED',slot=sid)
                stored_count+=1;inbound_active-=1;await stored.put((job,level,sid,lane,p))
        background_tasks.append(asyncio.create_task(downstream()))
        job,parked=await home
        await traffic.__aexit__(None,None,None)
        if job=='wait':job=await in_queue.get()

# ---------------------------------------------------------------- floor-1 loose goods (agreed process)
LOOSE_SPOTS=[tuple(s) for s in C.get('loose_pallet_spots',[])]
ODD_SPOT_LOCKS=[asyncio.Lock() for _ in LOOSE_SPOTS] or [asyncio.Lock()]
LOOSE_PALLETS=int(C.get('loose_pallets',0)) if LOOSE_SPOTS else 0
LOOSE_PER=int(C.get('loose_cartons_per_pallet',0))
loose_stats={'pallets_unpacked':0,'cartons_on_belt':0,'cartons_delivered':0,'per_floor':{}}

def loose_event(state,**extra):
    record={'job':None,'loose':True,'state':state,'demo_time_s':round(scheduler.time,5),**extra}
    events.append(record)
    with (ROOT/'events.jsonl').open('a') as f: f.write(json.dumps(record)+'\n')
    if 'pallet' in extra and state.startswith('LOOSE_PALLET'): print(f"Loose pallet {extra['pallet']+1:02d}: {state}",flush=True)

async def follow_path(name,pts,speed):
    """Constant-speed motion of one object along a polyline (cartons on belts / spirals)."""
    total=LG.length(pts)
    await engine.move({name:LG.along(pts,total)},speed,duration=max(.05,total/speed),kind='conveyor',
                      transform=lambda q:{name:LG.along(pts,q*total)},shape=lambda t:t)

U1_PHASES=(.15,.10,.15,.20,.12,.10,.08,.10)

async def u1_operator_cycle(floor,branch,carton,rack_pose,tray_slot=0):
    """U1 sorting worker at the end of branch (*U632):
    1. Opens carton on the belt end, revealing 3 yellow medicine boxes inside.
    2. Picks each yellow medicine box (3 turns), turns 180 deg, and places it into the hollow blue tray on the U1 rack.
    3. Clears the empty carton shell from the belt end."""
    wid=LG.u1_worker_id(floor,branch)
    sx,sy,syaw,z0=LG.u1_station(floor,branch)
    open_s=float(C.get('u1_open_carton_s',4.0))
    pick_s=float(C.get('u1_pick_box_s',C.get('u1_place_s',6.0)))
    d=[f*pick_s for f in U1_PHASES]
    pick_y=11.85;pick_bend,pick_arm=.38,.72
    place_y=12.06;place_bend,place_arm=.14,1.14
    c_idx=int(carton.split('_')[1])
    med_count=int(C.get('u1_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    med_names=[f'med_{c_idx}_{m}' for m in range(med_count)]
    c_pose=poses[carton]
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry_item=None,item_to=None,walk=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        item_name=carry_item or (item_to[0] if item_to else None)
        i0=poses[item_name] if item_name else None
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.30*math.sin(math.pi*steps*q) if walk else 0.,z0=z0)
            if carry_item:
                out[carry_item]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
            elif item_to is not None:
                t_name,t_pose=item_to
                out[t_name]=tuple(i0[j]+(t_pose[j]-i0[j])*q for j in range(4))
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
    # 3. Automatically clear the empty carton shell from the belt end
    send({carton:(c_pose[0],c_pose[1],-5,0)},transmit=not args.fast)
    loose_event('EMPTY_CARTON_CLEARED',carton=carton,floor=floor,branch=branch)
    return placed

async def u1_agv_swap_cycle(floor,branch,full_rack,meds_on_rack,store_cell,next_rack,spare_cell,old_spare_meds=()):
    """AGV slowly carries full_rack (+ yellow medicine boxes in blue tray) along agv_paths to Kho 04, then brings next_rack to U1."""
    agv=f'u1_agv_{floor}_{branch}'
    bx,u1_y,z0,_=LG.u1_rack_pose(floor,branch)
    speed=float(C.get('u1_agv_speed',0.45))
    lift_z=.03
    # 1. Carry loaded rack + its yellow medicine boxes from U1 station into Kho 04 empty cell
    pts_store=LG.u1_agv_route((bx,u1_y,z0),(store_cell['x'],store_cell['y'],z0),branch)
    len_store=LG.length(pts_store)
    def at_store(q):
        ax,ay,az,ayaw=LG.along(pts_store,q*len_store)
        r_pose=(ax,ay,z0+lift_z,0.)
        out={agv:(ax,ay,z0,ayaw),full_rack:r_pose}
        for m_name,m_idx,t_slot in meds_on_rack:
            out[m_name]=LG.u1_med_on_rack(r_pose,m_idx,t_slot)
        return out
    await engine.move(at_store(1.),speed=speed,duration=max(.1,len_store/speed),transform=at_store,shape=blend,exact=True)
    settled=(store_cell['x'],store_cell['y'],z0,0.)
    send({full_rack:settled,**{m_name:LG.u1_med_on_rack(settled,m_idx,t_slot) for m_name,m_idx,t_slot in meds_on_rack}},transmit=not args.fast)
    loose_event('AGV_RACK_STORED',floor=floor,branch=branch,rack=full_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
    # 2. Drive empty AGV to the replacement rack cell in Kho 04
    pts_spare=LG.u1_agv_route((store_cell['x'],store_cell['y'],z0),(spare_cell['x'],spare_cell['y'],z0),branch)
    len_spare=LG.length(pts_spare)
    await engine.move({agv:LG.along(pts_spare,len_spare)},speed=speed,duration=max(.1,len_spare/speed),
                      transform=lambda q:{agv:LG.along(pts_spare,q*len_spare)},shape=blend,exact=True)
    if old_spare_meds:
        send({m_name:poses[m_name][:2]+(-5,0) for m_name,_,_ in old_spare_meds},transmit=not args.fast)
    # 3. Pick up replacement empty rack in Kho 04 and bring it out to U1 station behind the worker
    loose_event('AGV_REPLACEMENT_RACK_PICKUP',floor=floor,branch=branch,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    pts_u1=LG.u1_agv_route((spare_cell['x'],spare_cell['y'],z0),(bx,u1_y,z0),branch)
    len_u1=LG.length(pts_u1)
    def at_u1(q):
        ax,ay,az,ayaw=LG.along(pts_u1,q*len_u1)
        return {agv:(ax,ay,z0,ayaw),next_rack:(ax,ay,z0+lift_z,0.)}
    await engine.move(at_u1(1.),speed=speed,duration=max(.1,len_u1/speed),transform=at_u1,shape=blend,exact=True)
    send({agv:(bx,u1_y,z0,math.pi/2),next_rack:(bx,u1_y,z0,0.)},transmit=not args.fast)
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
        tray_slot=len(st['cartons'])
        rack_pose=LG.u1_rack_pose(dest,b)
        loose_event('WORKER_OPEN_CARTON',carton=name,floor=dest,branch=b,rack=rack)
        placed_meds=await u1_operator_cycle(dest,b,name,rack_pose,tray_slot)
        st['cartons'].append((name,placed_meds))
        loose_event('CARTON_PLACED_ON_RACK',carton=name,floor=dest,branch=b,rack=rack,slot=tray_slot,med_boxes=[m[0] for m in placed_meds])
        if len(st['cartons'])>=U1_PER_RACK:
            store_cell=st['empty_cell']
            next_rack,spare_cell,old_meds=st['spares'].pop(0)
            carried_meds=[item for _,meds in st['cartons'] for item in meds]
            loose_event('AGV_RACK_PICKUP',floor=dest,branch=b,rack=rack,cartons=[c for c,_ in st['cartons']])
            await u1_agv_swap_cycle(dest,b,rack,carried_meds,store_cell,next_rack,spare_cell,old_meds)
            st['spares'].append((rack,store_cell,carried_meds))
            st['active_rack']=next_rack
            st['empty_cell']=spare_cell
            st['cartons']=[]
            st['swaps']+=1
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
    c_name=carry_carton or (carton_to[0] if carton_to else None)
    c0=poses[c_name] if c_name else None
    def at(q):
        k={key:a[key]+(b[key]-a[key])*q for key in a}
        out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.35*math.sin(math.pi*steps*q) if walk else 0.,z0=z0)
        if carry_carton:
            out[carry_carton]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
        elif carton_to is not None:
            t_name,t_pose=carton_to
            out[t_name]=tuple(c0[j]+(t_pose[j]-c0[j])*q for j in range(4))
        return out
    end=at(1.)
    await engine.move(end,duration=max(.04,dur),transform=at,shape=(lambda t:t) if walk else blend,exact=True)
    state.update(b)

async def z05_operator_deliver_one(floor,worker_idx,batch,carton,stack_pos,u2_idx):
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
        batch['done_event'].set()
    head_to_u2=math.atan2(dy_stand-py,dx_stand-px)
    await _z05_move_worker(floor,worker_idx,.22,yaw=head_to_u2,carry_carton=carton)
    await _z05_move_worker(floor,worker_idx,0.,x=dx_stand,y=dy_stand,carry_carton=carton,walk=True)
    await _z05_move_worker(floor,worker_idx,.18,yaw=dyaw,carry_carton=carton)
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

async def z05_worker_loop(floor,worker_idx):
    """Flexible Zone 05 worker coroutine: moves to whichever Zone 05 unpack zone (0 or 1) has cartons waiting,
    and distributes them across all 6 U2 stations (0..5) at constant walking speed."""
    fst=Z05_FLOOR_STATE[floor]
    n_u2=len(LG.U2_WORKERS)
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
        for k in range(n_u2):
            cand=(fst['next_u2']+k)%n_u2
            t_cand=fst['u2_active_task'][cand]
            if t_cand is None or t_cand.done():
                u2_idx=cand
                fst['next_u2']=(cand+1)%n_u2
                break
        if u2_idx is None:
            u2_idx=fst['next_u2']%n_u2
            fst['next_u2']=(u2_idx+1)%n_u2
        await z05_operator_deliver_one(floor,worker_idx,batch,carton,stack_pos,u2_idx)
    sx,sy,syaw,_=LG.z05_station(floor,worker_idx)
    state=fst['worker_poses'][worker_idx]
    if math.hypot(sx-state['x'],sy-state['y'])>1e-3:
        head_home=math.atan2(sy-state['y'],sx-state['x'])
        await _z05_move_worker(floor,worker_idx,.22,yaw=head_home)
        await _z05_move_worker(floor,worker_idx,0.,x=sx,y=sy,walk=True)
    await _z05_move_worker(floor,worker_idx,.18,yaw=syaw,bend=0.,arm=0.)

async def u2_operator_cycle(floor,u2_idx,carton,rack_pose,rack_idx,tray_slot=0):
    """U2 put-away worker at (floor, u2_idx):
    1. Bends down and opens the carton placed on the floor in front of them, revealing 3 yellow medicine boxes inside.
    2. Picks each yellow medicine box (3 turns) from the carton on the ground, turns 180 deg, and places it into the hollow blue tray on the U2 rack behind them.
    3. Clears the empty carton shell from the floor."""
    wid=LG.u2_worker_id(floor,u2_idx)
    sx,sy,syaw,z0=LG.u2_station(floor,u2_idx)
    open_s=float(C.get('u2_open_carton_s',4.0))
    pick_s=float(C.get('u2_pick_box_s',6.0))
    d=[f*pick_s for f in U1_PHASES]
    pick_x=sx+.22;pick_bend,pick_arm=.86,.52
    place_x=sx-.10;place_bend,place_arm=.14,1.14
    med_count=int(C.get('u2_med_boxes_per_carton',LG.U1_MED_PER_CARTON))
    med_names=[f'u2_med_{floor}_{u2_idx}_{rack_idx}_{m}' for m in range(med_count)]
    c_pose=poses[carton]
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry_item=None,item_to=None,walk=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        item_name=carry_item or (item_to[0] if item_to else None)
        i0=poses[item_name] if item_name else None
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(wid,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.30*math.sin(math.pi*steps*q) if walk else 0.,z0=z0)
            if carry_item:
                out[carry_item]=LG.held_carton(k['x'],k['y'],k['yaw'],k['bend'],k['arm'],z0=z0)
            elif item_to is not None:
                t_name,t_pose=item_to
                out[t_name]=tuple(i0[j]+(t_pose[j]-i0[j])*q for j in range(4))
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
    # 3. Automatically clear the empty carton shell from the floor
    send({carton:(c_pose[0],c_pose[1],-5,0)},transmit=not args.fast)
    loose_event('U2_EMPTY_CARTON_CLEARED',carton=carton,floor=floor,u2_station=u2_idx)
    return placed

async def u2_agv_swap_cycle(floor,u2_idx,full_rack,meds_on_rack,store_cell,next_rack,spare_cell,old_spare_meds=()):
    """U2 AGV carries full_rack (+ yellow medicine boxes in blue tray) along agv_paths to Kho 04, then brings next_rack to U2."""
    agv=f'u2_agv_{floor}_{u2_idx}'
    rx,ry,z0,ryaw=LG.u2_rack_pose(floor,u2_idx)
    speed=float(C.get('u2_agv_speed',C.get('u1_agv_speed',0.45)))
    lift_z=.03
    # 1. Carry loaded rack + its yellow medicine boxes from U2 station into Kho 04 empty cell
    pts_store=LG.u2_agv_route((rx,ry,z0),(store_cell['x'],store_cell['y'],z0),u2_idx)
    len_store=LG.length(pts_store)
    def at_store(q):
        ax,ay,az,ayaw=LG.along(pts_store,q*len_store)
        r_pose=(ax,ay,z0+lift_z,0.)
        out={agv:(ax,ay,z0,ayaw),full_rack:r_pose}
        for m_name,m_idx,t_slot in meds_on_rack:
            out[m_name]=LG.u1_med_on_rack(r_pose,m_idx,t_slot)
        return out
    await engine.move(at_store(1.),speed=speed,duration=max(.1,len_store/speed),transform=at_store,shape=blend,exact=True)
    settled=(store_cell['x'],store_cell['y'],z0,0.)
    send({full_rack:settled,**{m_name:LG.u1_med_on_rack(settled,m_idx,t_slot) for m_name,m_idx,t_slot in meds_on_rack}},transmit=not args.fast)
    loose_event('U2_AGV_RACK_STORED',floor=floor,u2_station=u2_idx,rack=full_rack,warehouse='04',cell=[round(store_cell['x'],3),round(store_cell['y'],3)])
    # 2. Drive empty AGV to the replacement rack cell in Kho 04
    pts_spare=LG.u2_agv_route((store_cell['x'],store_cell['y'],z0),(spare_cell['x'],spare_cell['y'],z0),u2_idx)
    len_spare=LG.length(pts_spare)
    await engine.move({agv:LG.along(pts_spare,len_spare)},speed=speed,duration=max(.1,len_spare/speed),
                      transform=lambda q:{agv:LG.along(pts_spare,q*len_spare)},shape=blend,exact=True)
    if old_spare_meds:
        send({m_name:poses[m_name][:2]+(-5,0) for m_name,_,_ in old_spare_meds},transmit=not args.fast)
    # 3. Pick up replacement empty rack in Kho 04 and bring it out to U2 station behind the U2 worker
    loose_event('U2_AGV_REPLACEMENT_RACK_PICKUP',floor=floor,u2_station=u2_idx,rack=next_rack,from_cell=[round(spare_cell['x'],3),round(spare_cell['y'],3)])
    pts_u2=LG.u2_agv_route((spare_cell['x'],spare_cell['y'],z0),(rx,ry,z0),u2_idx)
    len_u2=LG.length(pts_u2)
    def at_u2(q):
        ax,ay,az,ayaw=LG.along(pts_u2,q*len_u2)
        return {agv:(ax,ay,z0,ayaw),next_rack:(ax,ay,z0+lift_z,ryaw)}
    await engine.move(at_u2(1.),speed=speed,duration=max(.1,len_u2/speed),transform=at_u2,shape=blend,exact=True)
    send({agv:(rx,ry,z0,0.),next_rack:(rx,ry,z0,ryaw)},transmit=not args.fast)
    loose_event('U2_AGV_REPLACEMENT_RACK_READY',floor=floor,u2_station=u2_idx,rack=next_rack)

async def u2_carton_trip(floor,u2_idx,carton,job):
    """U2 station cycle once a carton has been placed on the floor in front of U2 worker u2_idx."""
    async with U2_LOCKS[(floor,u2_idx)]:
        st=U2_STATE[(floor,u2_idx)]
        rack=st['active_rack']
        rack_idx=st['active_rack_idx']
        tray_slot=len(st['cartons'])
        rack_pose=LG.u2_rack_pose(floor,u2_idx)
        loose_event('U2_WORKER_OPEN_CARTON',job=job,carton=carton,floor=floor,u2_station=u2_idx,rack=rack)
        placed_meds=await u2_operator_cycle(floor,u2_idx,carton,rack_pose,rack_idx,tray_slot)
        st['cartons'].append((carton,placed_meds))
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

async def z05_unpack_pallet(floor,zone,spot_xy,pallet_model,job):
    """When an outbound pallet from Kho 01 stops at its dedicated Zone 05 unpack zone (floor, zone, spot_xy):
    1. Replaces the monolithic pallet with a blue plastic pallet deck + 18 individual cartons.
    2. Both Zone 05 workers on that floor move to the active unpack zone(s) and distribute the 18 cartons across all 6 U2 stations at constant walking speed.
    3. Each U2 worker opens the carton on the ground, picks 3 yellow medicine boxes into the blue tray on its U2 rack, and U2 AGV stores the rack in Kho 04."""
    async with Z05_LOCKS[(floor,zone)]:
        z0=LG.LEVELS[floor-1]
        z05_per=int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))
        deck=f'z05_deck_{floor}_{zone}'
        cartons=[f'z05_carton_{floor}_{zone}_{c}' for c in range(z05_per)]
        stack=LG.z05_stack_positions(spot_xy,floor,z05_per)
        send({pallet_model:(spot_xy[0],spot_xy[1],-5,0),deck:(spot_xy[0],spot_xy[1],z0,0.),
              **{c:p for c,p in zip(cartons,stack)}},transmit=not args.fast)
        loose_event('Z05_PALLET_UNPACK_START',job=job,floor=floor,zone=zone,cartons=z05_per)
        fst=Z05_FLOOR_STATE[floor]
        batch={
            'job':job,
            'zone':zone,
            'spot_xy':spot_xy,
            'deck':deck,
            'items':list(zip(cartons,stack)),
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
            await asyncio.gather(*batch['u2_tasks'])
        active_w=[t for t in fst['worker_tasks'].values() if t is not None and not t.done()]
        if active_w and all(b is None for b in fst['zones'].values()):
            await asyncio.gather(*active_w)

# Unpack cycle phases (fraction of loose_unpack_s): turn, walk, face pallet, bend+reach, grab, rise, turn,
# walk back carrying, face belt, reach over the belt, release onto the belt, straighten.
OP_PHASES=(.05,.18,.04,.12,.05,.10,.04,.22,.04,.06,.04,.06)
CARRY=(.1,1.0);PLACE=(.3,1.0);STEP=.6   # (bend, arm raise) when carrying / placing; step length (m)

async def operator_cycle(i,op,spot,carton,stack_pos):
    """One carton: the operator walks to the pallet, bends, picks the carton, carries it back and puts it
    on the carton belt in front of the station. Timing scales to loose_unpack_s."""
    T=C['loose_unpack_s'];d=[f*T for f in OP_PHASES]
    sx,sy,syaw=LG.station(op);px,py,pyaw=LG.pallet_side(op,spot)
    head_out=math.atan2(py-sy,px-sx);head_back=math.atan2(sy-py,sx-px)
    belt=(op['x'],LG.Y758,LG.zc(1),syaw);reach=LG.reach_for(stack_pos[2])
    state={'x':sx,'y':sy,'yaw':syaw,'bend':0.,'arm':0.}
    async def phase(dur,x=None,y=None,yaw=None,bend=None,arm=None,carry=False,carton_to=None,walk=False):
        a=dict(state);b={'x':a['x'] if x is None else x,'y':a['y'] if y is None else y,
             'yaw':a['yaw'] if yaw is None else a['yaw']+math.atan2(math.sin(yaw-a['yaw']),math.cos(yaw-a['yaw'])),
             'bend':a['bend'] if bend is None else bend,'arm':a['arm'] if arm is None else arm}
        steps=max(1,round(math.hypot(b['x']-a['x'],b['y']-a['y'])/STEP)) if walk else 0
        c0=poses[carton]
        def at(q):
            k={key:a[key]+(b[key]-a[key])*q for key in a}
            out=LG.operator_pose(i,k['x'],k['y'],k['yaw'],k['bend'],k['arm'],.35*math.sin(math.pi*steps*q) if walk else 0.)
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
    await phase(d[11],bend=0.,arm=0.)

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
        stack=LG.stack_positions(spot,LOOSE_PER);names=[f'carton_{k*LOOSE_PER+j}' for j in range(LOOSE_PER)]
        async def receive(dock_i,cartons):
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
        await dock_flow.prepare('odd',k,spot,names,receive)
        free[s].release()
    jobs=[]
    for k in range(LOOSE_PALLETS):
        await engine.until(k*C['loose_pallet_interval_s'])
        loose_event('LOOSE_PALLET_ARRIVAL',pallet=k);jobs.append(asyncio.create_task(unpack(k)))
    await asyncio.gather(*jobs)

async def requests(total):
    for request in range(total):
        await engine.until(request*C['outbound_interval_s'])
        event(request,'OUTBOUND_REQUEST_QUEUED')
        await out_queue.put(request)
    for _ in range(2):await out_queue.put(None)

async def outbound_worker(worker):
    global completed,outbound_active
    while True:
        request=await out_queue.get()
        if request is None:return
        job,level,sid,lane,p=await stored.get()
        s=f'shuttle_{level}';z=C['storage_levels'][level]
        out=LAYOUT.data['ports']['out'][worker];ox,oy=out['lift'];ol=f'out_lift_{worker}'
        oz=C['output_levels'][job%4]+.3
        async with out_ports[worker]:
            outbound_active+=1
            async with shuttles[level]:
                event(job,'OUTBOUND_DISPATCH',request=request)
                await shuttle_async(s,LAYOUT.slot_point(sid),z)
                await deck_async(s,p,.12)
                await shuttle_async(s,out['buffer'],z,p)
                await deck_async(s,p,-.06)
                bx,by=out['buffer']
                await shuttle_async(s,(bx,min(LAYOUT.data['road_y'],key=lambda yy:abs(yy-by))),z)
            await engine.move({ol:(ox,oy,z)},C['lift_speed'])
            await settled(job,ol,z)
            event(job,'OUTPUT_CABIN_TRANSFER')
            await engine.move({p:(ox,oy,z,0)},1)
            event(job,'OUTPUT_LIFT_TO_FLOOR',floor=job%4+1)
            await engine.move({ol:(ox,oy,oz),p:(ox,oy,oz,0)},C['lift_speed'])
            await settled(job,ol,oz)
            event(job,'OUTPUT_GATE_TRANSFER')
            await engine.move({p:tuple(out['corner'])+(oz,0)},1)
            await engine.move({p:tuple(out['gate'])+(oz,0)},1)
            event(job,'PALLET_EXIT_GATE')
            # Placeholder until the 4-floor process is modelled: the pallet is set down on a free
            # unpack spot of that port and floor; a pallet already there counts as unpacked (removed).
            floor=job%4;spots=UNPACK_SPOTS[worker];k=unpack_count.get((worker,floor),0);unpack_count[(worker,floor)]=k+1
            spot=spots[k%len(spots)];prev=unpack_occupant.pop((worker,floor,k%len(spots)),None)
            if prev and poses.get(prev,(0,0,-5))[2]>-1:send({prev:(spot[0],spot[1],-5,0)},transmit=not args.fast)
            await engine.move({p:(spot[0],spot[1],oz,0)},1)
            unpack_occupant[(worker,floor,k%len(spots))]=p;event(job,'UNPACK_SPOT',spot=k%len(spots),floor=floor+1)
            background_tasks.append(asyncio.create_task(z05_unpack_pallet(floor+1,worker,spot,p,job)))
            event(job,'COMPLETE');completed+=1;outbound_active-=1

async def main():
    for key in ['inbound_interval_s','outbound_interval_s']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    for key in ['agv_following_gap_m','speed_multiplier','agv_speed','lift_speed','shuttle_speed','conveyor_speed','agv_acceleration','lift_acceleration','shuttle_acceleration','conveyor_acceleration','agv_jerk','lift_jerk','shuttle_jerk','conveyor_jerk','agv_angular_speed','agv_angular_acceleration','agv_angular_jerk','lift_settle_s','shuttle_positioning_s','shuttle_turn_s','shuttle_lift_s','shuttle_availability','shuttle_utilization','lift_availability','lift_utilization']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    if C['agv_following_gap_m']<.25:raise ValueError('agv_following_gap_m must be at least 0.25m')
    if len(INBOUND_PICKUP_SLOTS)!=4:raise ValueError('inbound_pickup_slots must contain exactly 4 pickup positions')
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
            for r_idx in range(1,LG.U1_RACKS_PER_BRANCH):
                cell=LG.U1_BRANCH_CELLS[b][r_idx]
                poses[f'u1_rack_{f}_{b}_{r_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
        for u2_idx in range(len(LG.U2_WORKERS)):
            rx,ry,z0,ryaw=LG.u2_rack_pose(f,u2_idx)
            poses[f'u2_agv_{f}_{u2_idx}']=(rx,ry,z0,0.)
            poses[f'u2_rack_{f}_{u2_idx}_0']=(rx,ry,z0,ryaw)
            for r_idx in range(1,LG.U2_RACKS_PER_STATION):
                cell=LG.U2_STATION_CELLS[u2_idx][r_idx]
                poses[f'u2_rack_{f}_{u2_idx}_{r_idx}']=(cell['x'],cell['y'],z0,cell.get('yaw',0.))
    for i in range(2):
        for direction in ['in','out']:poses[f'{direction}_lift_{i}']=tuple(LAYOUT.data['ports'][direction][i]['lift'])+(.3,)
    for j in range(C['jobs']):poses[f'pallet_{j}']=inbound_pickup_position(j)
    # Loads appear at configured arrival times, rather than all being present initially.
    send({f'pallet_{j}':(inbound_pickup_position(j)[0],-17,-5,0) for j in range(C['jobs'])})
    if LOOSE_PALLETS:   # loose-goods pallet decks and cartons start hidden below their spot
        hidden={f'loose_pallet_{s}':(sp[0],sp[1],-5,sp[2]) for s,sp in enumerate(LOOSE_SPOTS)}
        hidden.update({f'carton_{i}':(LOOSE_SPOTS[0][0],LOOSE_SPOTS[0][1],-5,0) for i in range(LOOSE_PALLETS*LOOSE_PER)})
        hidden.update({f'med_{i}_{m}':(LOOSE_SPOTS[0][0],LOOSE_SPOTS[0][1],-5,0) for i in range(LOOSE_PALLETS*LOOSE_PER) for m in range(LG.U1_MED_PER_CARTON)})
        z05_per=int(C.get('z05_cartons_per_pallet',LG.Z05_CARTONS_PER_PALLET))
        for f in LG.floors():
            for z_idx in range(len(LG.Z05_WORKERS)):
                wx,wy,_,_=LG.z05_station(f,z_idx)
                hidden[f'z05_deck_{f}_{z_idx}']=(wx,wy,-5,0)
                for c_idx in range(z05_per):
                    hidden[f'z05_carton_{f}_{z_idx}_{c_idx}']=(wx,wy,-5,0)
            for u2_idx in range(len(LG.U2_WORKERS)):
                rx,ry,_,_=LG.u2_rack_pose(f,u2_idx)
                for r_idx in range(LG.U2_RACKS_PER_STATION):
                    for m in range(LG.U1_MED_PER_CARTON):
                        hidden[f'u2_med_{f}_{u2_idx}_{r_idx}_{m}']=(rx,ry,-5,0)
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
        send(anim_ops,transmit=False)
    for i in range(C.get('initial_outbound_pallets',0)):
        level=(i+4)%9;sid=LAYOUT.job_slots[i%len(LAYOUT.job_slots)];p=f'pallet_out_{i}'
        poses[p]=LAYOUT.slot_point(sid)+(C['storage_levels'][level],0)
        await stored.put((C['jobs']+i,level,sid,i%2,p))
    dock_flow.initialize()
    total=min(args.jobs,C['jobs']);ticker=asyncio.create_task(engine.run())
    even_total=sum(1 for job in range(total) if inbound_classification(job)=='even')
    workers=asyncio.gather(arrivals(total),requests(even_total),agv_worker(0),agv_worker(1),outbound_worker(0),outbound_worker(1),loose_goods())
    done,_=await asyncio.wait([ticker,workers],return_when=asyncio.FIRST_COMPLETED)
    if ticker in done:await ticker
    await workers
    await asyncio.gather(*background_tasks)
    engine.running=False;await ticker
    if args.fast:send(dict(poses))
    report={'complete':completed,'inbound_stored':stored_count,'remaining_stored':stored.qsize(),'jobs_requested':total,'events':len(events),'swept_pose_checks':route_checks,'rail_contact_checks':rail_checks,
            'max_concurrent_motions':engine.max_active,'dual_agv_motion_ticks':engine.dual_agv_ticks,'same_direction_following_ticks':engine.following_ticks,'multiple_shuttle_motion_ticks':engine.multi_shuttle_ticks,'inbound_outbound_overlap_ticks':engine.overlap_ticks,
            'inbound_interval_s':C['inbound_interval_s'],'outbound_interval_s':C['outbound_interval_s'],'max_pending_inbound':max_pending,
            'loose_pallets_unpacked':loose_stats['pallets_unpacked'],'loose_cartons_delivered':loose_stats['cartons_delivered'],
            'loose_cartons_per_floor':{str(k):v for k,v in sorted(loose_stats['per_floor'].items())},
            'dock_flow':dock_flow.stats,
            'mode':'Concurrent CAD constrained kinematic','demo_duration_s':scheduler.time}
    (ROOT/'validation.json').write_text(json.dumps(report,indent=2))
    (ROOT/'cad-route-log.json').write_text(json.dumps(shuttle_route_log))
    print(json.dumps(report),flush=True)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:event(-1,'ERROR',message=str(exc));raise
