"""Deterministic kinematic process demonstrator. No collision/dynamics claims."""
import argparse
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

def inbound_pickup_position(job):
    if not INBOUND_PICKUP_SLOTS:
        return pickup_position(job)
    return INBOUND_PICKUP_SLOTS[job % len(INBOUND_PICKUP_SLOTS)]
node=Node()
poses={}
deck_offsets={f'shuttle_{i}':-.06 for i in range(10)}
wheel_modes={f'shuttle_{i}':'y' for i in range(10)}
rail_checks=0
events=[]
scheduler=Scheduler()
parser=argparse.ArgumentParser()
parser.add_argument('--fast',action='store_true',help='Accelerated integration verification')
parser.add_argument('--jobs',type=int,default=C['jobs'])
args=parser.parse_args()

def send(changes,transmit=True):
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
    req=Pose_V()
    for name,xyz in changes.items():
        p=req.pose.add(); p.name=name
        p.position.x,p.position.y,p.position.z=xyz[:3]
        if len(xyz)>=6:   # (x, y, z, roll, pitch, yaw): animated operator parts
            r,pi_,yw=xyz[3],xyz[4],xyz[5];cr,sr,cp,sp,cy_,sy_=math.cos(r/2),math.sin(r/2),math.cos(pi_/2),math.sin(pi_/2),math.cos(yw/2),math.sin(yw/2)
            p.orientation.w=cr*cp*cy_+sr*sp*sy_;p.orientation.x=sr*cp*cy_-cr*sp*sy_
            p.orientation.y=cr*sp*cy_+sr*cp*sy_;p.orientation.z=cr*cp*sy_-sr*sp*cy_
        else:
            yaw=xyz[3] if len(xyz)>3 else 0
            p.orientation.z=math.sin(yaw/2);p.orientation.w=math.cos(yaw/2)
    if transmit:
        # Large scene startup can temporarily delay service replies. Absolute poses
        # make this bounded retry idempotent, without moving through unvalidated poses.
        for attempt in range(4):
            ok,reply=node.request('/world/warehouse/set_pose_vector',req,Pose_V,Boolean,5000)
            if ok and reply.data: break
            if ok: raise RuntimeError('Gazebo rejected pose update')
            time.sleep(.3)
        else: raise RuntimeError('Gazebo pose service unavailable after bounded retries')
    poses.update({n:tuple(v) for n,v in changes.items()})

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
        frame=1/30
        dt=frame if args.fast else frame*C['speed_multiplier']
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
            self.max_active=max(self.max_active,len(set(id(v) for v in self.motions.values())))
            if inbound_active and outbound_active and changes:self.overlap_ticks+=1
            for m in finished:
                for n in m['targets']:del self.motions[n]
                m['future'].set_result(None)
            if scheduler.time>20000:raise RuntimeError('Dispatch stalled')
    async def until(self,when):
        while scheduler.time+1e-6<when:await asyncio.sleep(0 if args.fast else .005)

background_tasks=[]
engine=Engine();agv_load=[None,None];pickups=[None,None]
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
        p=f'pallet_{job}';send({p:inbound_pickup_position(job)},transmit=not args.fast)
        event(job,'PICKUP_BAY_LOADED');await in_queue.put(job)
        max_pending=max(max_pending,in_queue.qsize())
    placements=[]
    for job in range(total):
        await engine.until(job*C['inbound_interval_s'])
        event(job,'INBOUND_ARRIVAL')
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
            if not in_queue.empty():
                nxt=in_queue.get_nowait()
                if nxt is not None:
                    event(job,'AGV_REDISPATCH',agv=lane+1,next_job=nxt)
                    return nxt,False
                await follow_async(job,lane,legs[3:])
                event(job,'AGV_RETURN_COMPLETE');return None,True
            await follow_async(job,lane,legs[3:])
            event(job,'AGV_RETURN_COMPLETE')
            return await in_queue.get(),True
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

# ---------------------------------------------------------------- floor-1 loose goods (agreed process)
LOOSE_SPOTS=[tuple(s) for s in C.get('loose_pallet_spots',[])]
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

async def carton_trip(name,dest,branch):
    pts=LG.route(dest,poses[name][0],branch)
    await follow_path(name,pts,C['carton_conveyor_speed'])
    loose_stats['cartons_delivered']+=1;loose_stats['per_floor'][dest]=loose_stats['per_floor'].get(dest,0)+1
    loose_event('CARTON_AT_SORTING_HEAD',carton=name,floor=dest,branch=branch)
    send({name:poses[name][:2]+(-5,0)},transmit=not args.fast)   # put into a tray at the sorting head

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
    if not LOOSE_PALLETS:return
    ops=sorted((w for w in LG.L['workers'] if w['area']=='carton_inbound'),key=lambda w:w['x'])
    op_of=[min(ops,key=lambda w:abs(w['x']-s[0])) for s in LOOSE_SPOTS]
    free=[asyncio.Semaphore(1) for _ in LOOSE_SPOTS];counter={'carton':0,'branch':{}}
    async def unpack(k):
        s=k%len(LOOSE_SPOTS);await free[s].acquire();spot=LOOSE_SPOTS[s];deck=f'loose_pallet_{s}'
        stack=LG.stack_positions(spot,LOOSE_PER);names=[f'carton_{k*LOOSE_PER+j}' for j in range(LOOSE_PER)]
        send({deck:spot[:2]+(0,spot[2]),**{n:p+(spot[2],) for n,p in zip(names,stack)}},transmit=not args.fast)
        loose_event('LOOSE_PALLET_ON_SPOT',pallet=k,spot=s)
        op=op_of[s]
        for n,p in zip(names,stack):
            await operator_cycle(LG.OPERATORS.index(op),op,spot,n,p)
            i=counter['carton'];counter['carton']+=1;dest=LG.floors()[i%len(LG.floors())]
            br=counter['branch'].get(dest,0);counter['branch'][dest]=br+1;loose_stats['cartons_on_belt']+=1
            loose_event('CARTON_ON_BELT',carton=n,pallet=k,floor=dest)
            background_tasks.append(asyncio.create_task(carton_trip(n,dest,br)))
        send({deck:spot[:2]+(-5,spot[2])},transmit=not args.fast);loose_stats['pallets_unpacked']+=1
        loose_event('LOOSE_PALLET_EMPTY',pallet=k,spot=s);free[s].release()
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
            if prev:send({prev:(spot[0],spot[1],-5,0)},transmit=not args.fast)
            await engine.move({p:(spot[0],spot[1],oz,0)},1)
            unpack_occupant[(worker,floor,k%len(spots))]=p;event(job,'UNPACK_SPOT',spot=k%len(spots),floor=floor+1)
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
    for i in range(2):
        for direction in ['in','out']:poses[f'{direction}_lift_{i}']=tuple(LAYOUT.data['ports'][direction][i]['lift'])+(.3,)
    for j in range(C['jobs']):poses[f'pallet_{j}']=inbound_pickup_position(j)
    # Loads appear at configured arrival times, rather than all being present initially.
    send({f'pallet_{j}':(inbound_pickup_position(j)[0],-17,-5,0) for j in range(C['jobs'])})
    if LOOSE_PALLETS:   # loose-goods pallet decks and cartons start hidden below their spot
        hidden={f'loose_pallet_{s}':(sp[0],sp[1],-5,sp[2]) for s,sp in enumerate(LOOSE_SPOTS)}
        hidden.update({f'carton_{i}':(LOOSE_SPOTS[0][0],LOOSE_SPOTS[0][1],-5,0) for i in range(LOOSE_PALLETS*LOOSE_PER)})
        send(hidden)
        for i,op in enumerate(LG.OPERATORS):send(LG.operator_pose(i,*LG.station(op)))   # operators at the belt
    for i in range(C.get('initial_outbound_pallets',0)):
        level=(i+4)%9;sid=LAYOUT.job_slots[i%len(LAYOUT.job_slots)];p=f'pallet_out_{i}'
        poses[p]=LAYOUT.slot_point(sid)+(C['storage_levels'][level],0)
        await stored.put((C['jobs']+i,level,sid,i%2,p))
    total=min(args.jobs,C['jobs']);ticker=asyncio.create_task(engine.run())
    workers=asyncio.gather(arrivals(total),requests(total),agv_worker(0),agv_worker(1),outbound_worker(0),outbound_worker(1),loose_goods())
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
            'mode':'Concurrent CAD constrained kinematic','demo_duration_s':scheduler.time}
    (ROOT/'validation.json').write_text(json.dumps(report,indent=2))
    (ROOT/'cad-route-log.json').write_text(json.dumps(shuttle_route_log))
    print(json.dumps(report),flush=True)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:event(-1,'ERROR',message=str(exc));raise
