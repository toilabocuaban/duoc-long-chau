"""Rigid-body AGV motion and sampled swept-envelope checks (metres/radians).

Inbound pallets are picked from the 4 pallet spots of the full-case palletising area on floor 1 of
the 4-floor building (CAD: 4 x 0.99 x 1.21 m spots side by side, 1.2 m side north-south). The F4
enters from the south with the forks along the 1.2 m side. Lanes run inside floor 1 and continue
straight into the 4-way store (floor 1 of both buildings is open, no wall):
  RETURN_Y  -6.4  empty AGVs westbound to the spots / parking
  LOAD_Y    -8.4  loaded AGVs eastbound to the inbound chain conveyors (clear of the SE columns at y ~ -9.7)
"""
import json
import math
from pathlib import Path

OFFSET = 1.8
from cad_navigation import LAYOUT
_C = json.loads((Path(__file__).resolve().parent / 'config.json').read_text())
# Facing north (forks away from the lanes). An AGV waiting at its park column on the return lane must stay
# clear of a loaded AGV turning east under the easternmost spot (load sweep up to x ~ -27.65): forks reach px - 2.4.
PARKS = [(-24, -4.5, 0, math.pi / 2), (-19, -4.5, 0, math.pi / 2)]
LOAD_Y = -8.4
RETURN_Y = -6.4
TRANSIT_Y = LOAD_Y
PALLET_CLEARANCE = .03   # CAD spot pitch 1.045 m for 1.0 m pallets: 22 mm per side; chassis keeps 120 mm

def pickup_position(job):
    slots = _C.get('inbound_pickup_slots')
    if slots: return tuple(slots[job % len(slots)])
    return (-36 + job * 4, -17, 0, -math.pi)

def wrap(a): return math.atan2(math.sin(a), math.cos(a))

def rigid(base, forward, lateral=0, z=0):
    x,y,_,yaw=base
    return (x+forward*math.cos(yaw)-lateral*math.sin(yaw),
            y+forward*math.sin(yaw)+lateral*math.cos(yaw),z,yaw)

def assembly(i,base,fork_height,payload=None):
    result={f'agv_{i}':base, f'fork_{i}':rigid(base,.8,z=fork_height)}
    if payload is not None:
        # Forks run along the pallet's 1.2 m side (F4-1000, 1200 mm forks): pallet yaw = AGV yaw.
        result[payload]=rigid(base,OFFSET,z=fork_height+.1)
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
        # Only floor-level pallets are obstacles: skip racked (z>1) and hidden, not-yet-arrived loads (z<-1).
        if not name.startswith('pallet_') or name==payload or abs(p[2])>1.0: continue
        obstacle=rect(p,.6,.5)
        # The chassis is never permitted to enter even the intended pickup load.
        if overlaps(body,obstacle): raise RuntimeError(f'AGV {i+1} chassis intersects {name}')
        if name!=pickup:
            # Fork clearance from neighbouring goods, and payload clearance.
            for part in moving[1:]+[rect(rigid(base,1.6),.8,.38)]:
                if overlaps(part,obstacle,PALLET_CLEARANCE): raise RuntimeError(f'AGV {i+1} load/forks intersect {name}')
    # Conveyor rails begin at y=-3.5. Only forks/load may enter the transfer area.
    for port in LAYOUT.data['ports']['in']:
        x,y=port['infeed']
        if overlaps(body,(x,y,.65,port['infeed_length']/2,0),.05):
            raise RuntimeError('AGV chassis entered inbound conveyor envelope')

PICKUP_LEGS_FROM_LANE = 2   # route_to_pickup()[2:] starts on the return lane (used for redispatch)

def route_to_pickup(i,job,position=None):
    x,y,_,_=position if position is not None else pickup_position(job)
    px=PARKS[i][0]
    return [
        ((px,RETURN_Y,0,math.pi/2),'ALIGN_DEPARTURE'),        # reverse out of the parking box onto the lane
        ((px,RETURN_Y,0,math.pi),'TURN_IN_PICKUP_AISLE'),
        ((x,RETURN_Y,0,math.pi),'TRAVEL_TO_PICKUP'),
        ((x,RETURN_Y,0,math.pi/2),'ALIGN_WITH_PALLET'),
        ((x,y-OFFSET,0,math.pi/2),'INSERT_FORKS')]              # spot entered from the south

def route_to_gate(i,x):
    drop=LAYOUT.data['ports']['in'][i]['drop'][1]
    return [((None,LOAD_Y,0,math.pi/2),'REVERSE_WITH_LOAD'),
            ((None,LOAD_Y,0,0),'TURN_WITH_LOAD'),
            ((x,LOAD_Y,0,0),'TRAVEL_WITH_LOAD'),
            ((x,LOAD_Y,0,math.pi/2),'ALIGN_WITH_CONVEYOR'),
            ((x,drop-OFFSET,0,math.pi/2),'APPROACH_INBOUND')]

def route_home(i,x):
    return [((x,RETURN_Y,0,math.pi/2),'REVERSE_CLEAR_OF_CONVEYOR'),
            ((x,RETURN_Y,0,math.pi),'TURN_TO_RETURN'),
            ((PARKS[i][0],RETURN_Y,0,math.pi),'RETURN_ALONG_AISLE'),
            ((PARKS[i][0],RETURN_Y,0,math.pi/2),'ALIGN_WITH_PARKING'),
            ((PARKS[i][0],PARKS[i][1],0,math.pi/2),'PARK'),
            (PARKS[i],'PARK_ALIGNED')]
