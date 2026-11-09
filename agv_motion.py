"""Rigid-body AGV motion and sampled swept-envelope checks (metres/radians)."""
import math

OFFSET = 1.8
from cad_navigation import LAYOUT
PARKS = [(-38, -5, 0, 0), (-33, -5, 0, 0)]
TRANSIT_Y = -9.0
APPROACH_Y = -12.0

def pickup_position(job):
    return (-36 + job * 4, -17, 0, -math.pi)

def wrap(a): return math.atan2(math.sin(a), math.cos(a))

def rigid(base, forward, lateral=0, z=0):
    x,y,_,yaw=base
    return (x+forward*math.cos(yaw)-lateral*math.sin(yaw),
            y+forward*math.sin(yaw)+lateral*math.cos(yaw),z,yaw)

def assembly(i,base,fork_height,payload=None):
    result={f'agv_{i}':base, f'fork_{i}':rigid(base,.8,z=fork_height)}
    if payload is not None:
        p=rigid(base,OFFSET,z=fork_height+.1)
        result[payload]=p[:3]+(p[3]-math.pi/2,)
    return result

def samples(start,end,speed=1,angular_speed=.55):
    length=math.dist(start[:3],end[:3]); angle=wrap(end[3]-start[3])
    duration=max(length/max(.1,speed),abs(angle)/angular_speed,.1)
    # Spatial/angular limits are used even during accelerated verification.
    count=max(1,math.ceil(length/.075),math.ceil(abs(angle)/.025))
    for k in range(1,count+1):
        t=k/count
        yield (tuple(start[j]+(end[j]-start[j])*t for j in range(3))+(start[3]+angle*t,),duration/count)

def rect(p,hx,hy): return (p[0],p[1],hx,hy,p[3] if len(p)>3 else 0)

def overlaps(a,b,clearance=.12):
    # Separating axis theorem for oriented rectangles, including safety clearance.
    def axes(r):
        c,s=math.cos(r[4]),math.sin(r[4]); return ((c,s),(-s,c))
    aa,bb=axes(a),axes(b)
    for ux,uy in aa+bb:
        ra=sum((a[2+j]+clearance/2)*abs(ux*v[0]+uy*v[1]) for j,v in enumerate(aa))
        rb=sum((b[2+j]+clearance/2)*abs(ux*v[0]+uy*v[1]) for j,v in enumerate(bb))
        if abs((b[0]-a[0])*ux+(b[1]-a[1])*uy)>=ra+rb: return False
    return True

def check_pose(i,base,poses,payload=None,pickup=None):
    body=rect(base,.83,.65)
    moving=[body]
    if payload: moving.append(rect(assembly(i,base,.2,payload)[payload],.6,.5))
    peer=1-i
    if f'agv_{peer}' in poses:
        other=poses[f'agv_{peer}']
        peer_parts=[rect(other,.83,.65),rect(rigid(other,1.55),.85,.4)]
        for a in moving:
            if any(overlaps(a,b,.25) for b in peer_parts):
                raise RuntimeError(f'Unsafe route: AGV {i+1} intersects parked AGV {peer+1}')
    for name,p in poses.items():
        # Pallets with z=-5 are staged off-scene until their inbound arrival.
        # Ignore only those hidden staging poses; keep floor-level and stored pallets
        # in the swept-envelope collision checks.
        if not name.startswith('pallet_') or name==payload or p[2] < -1.0 or p[2]>1.0: continue
        obstacle=rect(p,.6,.5)
        # The chassis is never permitted to enter even the intended pickup load.
        if overlaps(body,obstacle): raise RuntimeError(f'AGV {i+1} chassis intersects {name}')
        if name!=pickup:
            # Fork clearance from neighbouring goods, and payload clearance.
            for part in moving[1:]+[rect(rigid(base,1.6),.8,.38)]:
                if overlaps(part,obstacle):
                    raise RuntimeError(f'AGV {i+1} load/forks intersect {name}')
    # Conveyor rails begin at y=-3.5. Only forks/load may enter the transfer area.
    for port in LAYOUT.data['ports']['in']:
        x,y=port['infeed']
        if overlaps(body,(x,y,.65,port['infeed_length']/2,0),.05):
            raise RuntimeError('AGV chassis entered inbound conveyor envelope')

def route_to_pickup(i,job,position=None):
    x,y,_,_=position if position is not None else pickup_position(job)
    return [
        ((PARKS[i][0],PARKS[i][1],0,-math.pi/2),'ALIGN_DEPARTURE'),
        ((PARKS[i][0],-12,0,-math.pi/2),'DEPART_PARKING'),
        ((PARKS[i][0],-12,0,0),'TURN_IN_PICKUP_AISLE'),
        ((x,-12,0,0),'TRAVEL_TO_PICKUP'),
        ((x,-12,0,-math.pi/2),'ALIGN_WITH_PALLET'),
        ((x,y+OFFSET,0,-math.pi/2),'INSERT_FORKS')]

def route_to_gate(i,x):
    drop=LAYOUT.data['ports']['in'][i]['drop'][1]
    return [((None,TRANSIT_Y,0,-math.pi/2),'REVERSE_WITH_LOAD'),
            ((None,TRANSIT_Y,0,0),'TURN_WITH_LOAD'),
            ((x,TRANSIT_Y,0,0),'TRAVEL_WITH_LOAD'),
            ((x,TRANSIT_Y,0,math.pi/2),'ALIGN_WITH_CONVEYOR'),
            ((x,drop-OFFSET,0,math.pi/2),'APPROACH_INBOUND')]

def route_home(i,x):
    return [((x,-7.0,0,math.pi/2),'REVERSE_CLEAR_OF_CONVEYOR'),
            ((x,-7.0,0,math.pi),'TURN_TO_RETURN'),
            ((PARKS[i][0],-7.0,0,math.pi),'RETURN_ALONG_AISLE'),
            ((PARKS[i][0],-7.0,0,math.pi/2),'ALIGN_WITH_PARKING'),
            ((PARKS[i][0],PARKS[i][1],0,math.pi/2),'PARK'),
            (PARKS[i],'PARK_ALIGNED')]
