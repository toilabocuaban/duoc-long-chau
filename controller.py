"""Deterministic kinematic process demonstrator. No collision/dynamics claims."""
import argparse
import json
import math
import time
from pathlib import Path
from gz.transport13 import Node
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.boolean_pb2 import Boolean
from agv_motion import PARKS, OFFSET, pickup_position, assembly, samples, check_pose, route_to_pickup, route_to_gate, route_home
from rail_geometry import deck_name, wheel_name, supported
from cad_navigation import LAYOUT
from motion_scheduler import Scheduler
from motion_profile import blend, duration as profile_duration, bounds, intersects
from agv_motion import wrap
from agv_traffic import Plan,compatible

ROOT=Path(__file__).resolve().parent
C=json.loads((ROOT/'config.json').read_text())
INBOUND_PICKUP_SLOTS=[tuple(slot) for slot in C.get('inbound_pickup_slots', [])]
pickup_occupancy=[None for _ in INBOUND_PICKUP_SLOTS]

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
    async def move(self,targets,speed=2,duration=None,kind=None,transform=None):
        targets={n:tuple(v) for n,v in targets.items()}
        if set(targets)&set(self.motions):raise RuntimeError('Equipment has two owners')
        starts={n:poses[n]+(0,)*(len(v)-len(poses[n])) for n,v in targets.items()}
        kind=kind or ('lift' if any('_lift_' in n for n in targets) else 'shuttle' if any(n.startswith('shuttle_') for n in targets) else 'conveyor')
        speed=min(speed,C['conveyor_speed']) if kind=='conveyor' else speed
        distance=max(math.dist(starts[n][:3],v[:3]) for n,v in targets.items())
        duration=max(duration or 0,profile_duration(distance,speed,C[kind+'_acceleration'],C[kind+'_jerk'])/C['speed_multiplier'])
        future=asyncio.get_running_loop().create_future()
        item={'targets':targets,'starts':starts,'elapsed':0.,'duration':duration,'future':future,'transform':transform}
        for n in targets:self.motions[n]=item
        await future
    async def run(self):
        dt=1/30
        while self.running:
            await asyncio.sleep(0 if args.fast else dt)
            for task in background_tasks:
                if task.done() and not task.cancelled() and task.exception() is not None:raise task.exception()
            scheduler.time+=dt
            changes={};finished=[]
            items={id(v):v for v in self.motions.values()}.values()
            for m in items:
                m['elapsed']+=dt;t=min(1,m['elapsed']/m['duration'])
                q=blend(t)
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
    travel=profile_duration(math.dist(start[:3],target[:3]),C['agv_speed'],C['agv_acceleration'],C['agv_jerk'])
    turn=profile_duration(abs(target[3]-start[3]),C['agv_angular_speed'],C['agv_angular_acceleration'],C['agv_angular_jerk'])
    end_parts=assembly(i,target,height,payload)
    part_distance=max(math.dist(poses[n][:3],v[:3]) for n,v in end_parts.items())
    length_time=profile_duration(part_distance,C['agv_speed'],C['agv_acceleration'],C['agv_jerk'])
    duration=max(travel,turn,length_time)/C['speed_multiplier']
    while True:
        now=scheduler.time;plan=Plan(start,target,now,duration,payload is not None)
        peer=reservations.get(1-i,Plan(poses[f'agv_{1-i}'],poses[f'agv_{1-i}'],now,.05,agv_load[1-i] is not None))
        if compatible(plan,peer,now,C['agv_following_gap_m']):break
        await engine.until(scheduler.time+.1)
    reservations[i]=plan
    try:
        await engine.move(end_parts,C['agv_speed'],duration=duration,kind='agv',transform=lambda q:assembly(i,base(q),height,payload))
    finally:reservations.pop(i,None)

async def settled(job,lift,target):
    start=scheduler.time
    event(job,'LIFT_SETTLING',lift=lift,target_z=target)
    await engine.until(start+C['lift_settle_s'])
    if abs(poses[lift][2]-target)>C['lift_position_tolerance_m']:raise RuntimeError('Lift is not aligned for conveyor transfer')
    event(job,'LIFT_READY',lift=lift,target_z=target,settle_elapsed_s=round(scheduler.time-start,5),velocity_m_s=0)

async def follow_async(job,i,route,height=.02,payload=None,pickup=None):
    for target,state in route:
        event(job,state,agv=i+1)
        await drive_async(i,target,height,payload,pickup)

async def shuttle_async(s,end,z,pallet=None):
    route=LAYOUT.path(poses[s][:2],end)
    for b in route[1:]:
        targets={s:tuple(b)+(z,)}
        if pallet:targets[pallet]=tuple(b)+(z+max(0,deck_offsets[s]),0)
        await engine.move(targets,C['shuttle_speed'])
    shuttle_route_log.append({'shuttle':s,'path':route})

async def deck_async(s,p,height):
    deck_offsets[s]=height;x,y,z=poses[s][:3]
    await engine.move({s:(x,y,z),deck_name(s):(x,y,z+height),p:(x,y,z+max(0,height),0)},duration=.4/C['speed_multiplier'])

async def arrivals(total):
    global max_pending
    for job in range(total):
        await engine.until(job*C['inbound_interval_s'])
        slot=job % len(INBOUND_PICKUP_SLOTS)
        while pickup_occupancy[slot] is not None:
            await engine.until(scheduler.time + 0.5)
        p=f'pallet_{job}';pickup_occupancy[slot]=job
        send({p:inbound_pickup_position(job)},transmit=not args.fast)
        event(job,'INBOUND_ARRIVAL');await in_queue.put(job)
        max_pending=max(max_pending,in_queue.qsize())
    for _ in range(2):await in_queue.put(None)

async def agv_worker(lane):
    global stored_count
    while True:
        job=await in_queue.get()
        if job is None:return
        p=f'pallet_{job}';level=job%9;z=C['storage_levels'][level];s=f'shuttle_{level}'
        inp=LAYOUT.data['ports']['in'][lane];ix,iy=inp['lift'];bx,by=inp['buffer'];il=f'in_lift_{lane}'
        await in_ports[lane].acquire()
        async def prepare(s=s,z=z,level=level,ix=ix):
            async with shuttles[level]:
                await shuttle_async(s,(ix,LAYOUT.data['road_y'][0]),z)
        ready=asyncio.create_task(prepare())
        await engine.move({il:(ix,iy,.3)},C['lift_speed'])
        await settled(job,il,.3)
        await follow_async(job,lane,route_to_pickup(lane,job,inbound_pickup_position(job)),pickup=p)
        pickup_occupancy[job % len(INBOUND_PICKUP_SLOTS)]=None
        await engine.move(assembly(lane,poses[f'agv_{lane}'],.2,p),.4)
        event(job,'AGV_CARRY_TO_INBOUND',agv=lane+1)
        await follow_async(job,lane,route_to_gate(lane,ix),.2,p)
        event(job,'AGV_HANDOVER_CHAIN',agv=lane+1)
        agv_load[lane]=None;pickups[lane]=p
        await drive_async(lane,(ix,inp['drop'][1]-.5-.12-2.4,0,math.pi/2),.2,pickup=p)
        event(job,'FORKS_CLEAR')
        async def return_home():
            # The apron lane is reserved only while travelling; the other AGV may dock independently.
            await engine.move(assembly(lane,poses[f'agv_{lane}'],.02),.3)
            await follow_async(job,lane,route_home(lane,ix),pickup=p)
            pickups[lane]=None;event(job,'AGV_RETURN_COMPLETE')
        home=asyncio.create_task(return_home())
        async def downstream(job=job,p=p,level=level,z=z,s=s,inp=inp,ix=ix,iy=iy,bx=bx,by=by,il=il,ready=ready):
            global stored_count,inbound_active
            inbound_active+=1
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
        await home

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
            await engine.move({p:(-12-job//8*1.5,out['gate'][1]-3+job//4%2*2,oz,0)},1)
            event(job,'COMPLETE');completed+=1;outbound_active-=1

async def main():
    for key in ['inbound_interval_s','outbound_interval_s']:
        if not isinstance(C[key],(int,float)) or isinstance(C[key],bool) or not math.isfinite(C[key]) or C[key]<=0:raise ValueError(f'{key} must be positive')
    for key in ['agv_following_gap_m','speed_multiplier','agv_speed','lift_speed','shuttle_speed','conveyor_speed','agv_acceleration','lift_acceleration','shuttle_acceleration','conveyor_acceleration','agv_jerk','lift_jerk','shuttle_jerk','conveyor_jerk','agv_angular_speed','agv_angular_acceleration','agv_angular_jerk','lift_settle_s','lift_position_tolerance_m','shuttle_positioning_s','shuttle_turn_s','shuttle_lift_s','shuttle_availability','shuttle_utilization','lift_positioning_s','lift_transfer_s','lift_availability','lift_utilization']:
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
    for i in range(C.get('initial_outbound_pallets',0)):
        level=(i+4)%9;sid=LAYOUT.job_slots[i%len(LAYOUT.job_slots)];p=f'pallet_out_{i}'
        poses[p]=LAYOUT.slot_point(sid)+(C['storage_levels'][level],0)
        await stored.put((C['jobs']+i,level,sid,i%2,p))
    total=min(args.jobs,C['jobs']);ticker=asyncio.create_task(engine.run())
    workers=asyncio.gather(arrivals(total),requests(total),agv_worker(0),agv_worker(1),outbound_worker(0),outbound_worker(1))
    done,_=await asyncio.wait([ticker,workers],return_when=asyncio.FIRST_COMPLETED)
    if ticker in done:await ticker
    await workers
    await asyncio.gather(*background_tasks)
    engine.running=False;await ticker
    if args.fast:send(dict(poses))
    report={'complete':completed,'inbound_stored':stored_count,'remaining_stored':stored.qsize(),'jobs_requested':total,'events':len(events),'swept_pose_checks':route_checks,'rail_contact_checks':rail_checks,
            'max_concurrent_motions':engine.max_active,'dual_agv_motion_ticks':engine.dual_agv_ticks,'same_direction_following_ticks':engine.following_ticks,'multiple_shuttle_motion_ticks':engine.multi_shuttle_ticks,'inbound_outbound_overlap_ticks':engine.overlap_ticks,
            'inbound_interval_s':C['inbound_interval_s'],'outbound_interval_s':C['outbound_interval_s'],'max_pending_inbound':max_pending,
            'mode':'Concurrent CAD constrained kinematic','demo_duration_s':scheduler.time}
    (ROOT/'validation.json').write_text(json.dumps(report,indent=2))
    (ROOT/'cad-route-log.json').write_text(json.dumps(shuttle_route_log))
    print(json.dumps(report),flush=True)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:event(-1,'ERROR',message=str(exc));raise
