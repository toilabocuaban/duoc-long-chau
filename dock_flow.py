"""Visible dock unloading, simulated barcode manifest, reusable pallet-jack transfer."""
import asyncio
import math
from contextlib import asynccontextmanager
import loose_goods as LG
import truck_scene as TS
from sdf_parts import box, merge_boxes

class FloorGate:
    """AGVs may run together; a manual pallet crossing excludes all moving AGVs."""
    def __init__(self):
        self.condition=asyncio.Condition();self.agvs=0;self.crossing=False;self.waiting=0
    @asynccontextmanager
    async def agv(self):
        async with self.condition:
            # One AGV owns the four-pallet pickup apron at a time. Adjacent pickup
            # poses are too close for two loaded fork sweeps to negotiate safely.
            await self.condition.wait_for(lambda:not self.crossing and not self.waiting and self.agvs==0)
            self.agvs+=1
        try:yield
        finally:
            async with self.condition:self.agvs-=1;self.condition.notify_all()
    @asynccontextmanager
    async def manual(self):
        async with self.condition:
            self.waiting+=1
            try:await self.condition.wait_for(lambda:not self.crossing and not self.agvs)
            finally:self.waiting-=1
            self.crossing=True
        try:yield
        finally:
            async with self.condition:self.crossing=False;self.condition.notify_all()

def dock_list(c):return TS.selected_docks({**c,'truck_count_visible':c.get('dock_active_count',4)})
def stage(d):return (d['x'],d['rear_y']+.62,0.)
def source_stack(d,n):
    # Cartons are physically inside the trailer, immediately behind its rear
    # opening. The worker stays north of the dock sill and reaches in by hand.
    x,y=d['x']-.60,d['rear_y']-.35
    return [(x,y,.72) for _ in range(n)]
def worker_id(i):return 10+i

def scene(c,model,pallet_deck):
    meshes={p:merge_boxes('dock_worker_'+p,[box(f'{p}{k}',*b) for k,b in enumerate(bs)]) for p,bs in LG.WORKER_PARTS.items()}
    deck=merge_boxes('dock_empty_pallet',pallet_deck())
    carton=merge_boxes('dock_carton',[box('box',0,0,0,*LG.CARTON,'.76 .57 .33 1'),box('tape',0,0,.127,.05,.3,.004,'.92 .8 .57 1')])
    jack=merge_boxes('dock_jack',[box('body',-.55,0,.12,.35,.55,.18,'.9 .55 .02 1'),box('forkl',.1,-.25,.055,1.2,.12,.08,'.9 .55 .02 1'),box('forkr',.1,.25,.055,1.2,.12,.08,'.9 .55 .02 1'),box('handle',-.78,0,.65,.06,.06,1.1,'.1 .1 .1 1'),box('grip',-.78,0,1.20,.06,.5,.06,'.1 .1 .1 1')])
    n=max(18,int(c.get('loose_cartons_per_pallet',18)))
    for i,d in enumerate(dock_list(c)):
        for name,pose in LG.operator_pose(worker_id(i),d['x']+.85,d['rear_y']+.75,math.pi).items():
            model(name,pose[:3]+(pose[5],),meshes[name.split('_',1)[1]])
        model(f'dock_deck_{i}',stage(d)[:2]+(0,0),deck)
        model(f'dock_jack_{i}',stage(d)[:2]+(0,0),jack)
        for j in range(n):model(f'dock_carton_{i}_{j}',(d['x'],d['rear_y']-1,-5),carton)
        model(f'dock_scanner_{i}',(d['x']+.75,d['rear_y']+.45,0),[box('stand',0,0,.5,.06,.06,1,'.3 .3 .3 1'),box('reader',0,0,1.02,.22,.16,.12,'.05 .12 .12 1'),box('light',0,-.085,1.03,.14,.01,.04,'.1 1 .2 1')])

class DockFlow:
    def __init__(self,c,engine,send,poses,fast,emit):
        self.c=c;self.e=engine;self.send=lambda p:send(p,transmit=not fast);self.poses=poses;self.emit=emit
        self.docks=dock_list(c);self.locks=[asyncio.Lock() for _ in self.docks];self.gate=FloorGate()
        self.stats={'scanned_even':0,'scanned_odd':0,'pallets_to_w1':0,'pallets_to_w2':0,'empty_pallet_returns':0}
        self.states={}
    def initialize(self):
        for i,d in enumerate(self.docks):
            self.states[i]=(d['x']+.85,d['rear_y']+.75,math.pi)
            self.send(LG.operator_pose(worker_id(i),*self.states[i]))
            self.send({f'dock_deck_{i}':stage(d)[:2]+(0,0),f'dock_jack_{i}':stage(d)[:2]+(0,0)})
            self.send({f'dock_carton_{i}_{j}':(d['x'],d['rear_y']-1,-5,0) for j in range(max(18,int(self.c['loose_cartons_per_pallet'])))})
    async def worker(self,i,target,carton=None):
        a=self.states[i];b=target;dyaw=math.atan2(math.sin(b[2]-a[2]),math.cos(b[2]-a[2]));dist=math.dist(a[:2],b[:2])
        def at(q):
            x=a[0]+(b[0]-a[0])*q;y=a[1]+(b[1]-a[1])*q;yaw=a[2]+dyaw*q
            result=LG.operator_pose(worker_id(i),x,y,yaw,.1 if carton else 0,1 if carton else 0,.28*math.sin(q*max(1,dist/.6)*math.pi))
            if carton:result[carton]=LG.held_carton(x,y,yaw,.1,1)
            return result
        await self.e.move(at(1),duration=max(.4,dist/self.c.get('dock_worker_speed',1.0)),transform=at,exact=True)
        self.states[i]=b
    async def idle(self,i,duration,carton=None):
        """Keep the operator visibly alive while scanning or waiting for traffic."""
        x,y,yaw=self.states[i]
        def at(q):
            sway=math.sin(q*math.pi*4)
            bend=(.10+.025*sway) if carton else .025*sway
            arm=(1.0+.035*sway) if carton else .06+.025*sway
            out=LG.operator_pose(worker_id(i),x,y,yaw,bend,arm,.035*sway)
            if carton:out[carton]=LG.held_carton(x,y,yaw,bend,arm)
            return out
        await self.e.move(at(1),duration=max(.1,duration),transform=at,exact=True)
    def transfer_waypoints(self,i,target,returning=False):
        """Remain inside: cross AGV lanes at own dock X, then use the internal corridor."""
        start=self.poses[f'dock_jack_{i}'];end=target[:2]+(0,0)
        inside_y=-5.35
        if returning:
            return [(start[0],inside_y,0,0),(self.docks[i]['x'],inside_y,0,0),end]
        return [(start[0],inside_y,0,0),(end[0],inside_y,0,0),end]
    async def transfer(self,i,names,target,returning=False,without_deck=False):
        """Pallet jack plus worker cross the AGV aisle under an exclusive interlock."""
        d=self.docks[i];deck=f'dock_deck_{i}';jack=f'dock_jack_{i}'
        traffic=self.gate.manual();permission=asyncio.create_task(traffic.__aenter__())
        while not permission.done():
            await self.idle(i,.5)
        await permission
        try:
            start=self.poses[jack];end=target[:2]+(0,0)
            offsets={n:tuple(self.poses[n][k]-start[k] for k in range(3)) for n in names}
            # All waypoints stay north of the CAD south facade (inside factory).
            waypoints=self.transfer_waypoints(i,target,returning)
            for b in waypoints:
                a=self.poses[jack];length=math.dist(a[:3],b[:3]);yaw=math.atan2(b[1]-a[1],b[0]-a[0]) if length>.01 else 0
                def at(q,a=a,b=b,yaw=yaw):
                    p=tuple(a[k]+(b[k]-a[k])*q for k in range(3))+(0,)
                    out={jack:p,**{n:tuple(p[k]+offsets[n][k] for k in range(3))+(0,) for n in names}}
                    if not without_deck:out[deck]=p
                    wx=p[0]-.95*math.cos(yaw);wy=p[1]-.95*math.sin(yaw)
                    out.update(LG.operator_pose(worker_id(i),wx,wy,yaw,.05,.65,.25*math.sin(q*math.pi*max(1,length/.6))))
                    return out
                await self.e.move(at(1),duration=max(.2,length/self.c.get('dock_pallet_speed',.7)),transform=at,exact=True)
                self.states[i]=(b[0]-.95*math.cos(yaw),b[1]-.95*math.sin(yaw),yaw)
        finally:
            await traffic.__aexit__(None,None,None)
    async def prepare(self,kind,k,target,names,consume):
        i=k%len(self.docks);d=self.docks[i]
        async with self.locks[i]:
            count=len(names);local=[f'dock_carton_{i}_{j}' for j in range(count)]
            src=source_stack(d,count);dst=list(reversed(LG.stack_positions(stage(d),count)))
            self.send({n:(d['x'],d['rear_y'],-5,0) for n in local})
            self.emit('TRUCK_BATCH_READY',dock=d['id'],classification=kind,batch=k,cartons=count)
            for j,(n,p,out) in enumerate(zip(local,src,dst)):
                self.send({n:p+(0,)})
                # Stand inside the factory at the sill; only the arm reaches to p.
                await self.worker(i,(d['x']-.15,d['rear_y']+.72,math.pi))
                self.emit('WORKER_PICK_FROM_TRUCK',dock=d['id'],carton=j,batch=k)
                held=LG.held_carton(*self.states[i],.1,1)
                await self.e.move({**LG.operator_pose(worker_id(i),*self.states[i],.1,1),n:held},duration=self.c.get('dock_pick_s',1),exact=True)
                await self.worker(i,(d['x']+.85,d['rear_y']+.65,math.pi),n)
                await self.idle(i,self.c.get('inbound_scan_s',2),n)
                self.stats['scanned_'+kind]+=1
                self.emit('BARCODE_CLASSIFIED',barcode=f'{kind.upper()}-{k:04d}-{j:03d}',classification=kind,dock=d['id'])
                await self.worker(i,(out[0]+.85,out[1],math.pi),n)
                bend,arm=LG.reach_for(out[2])
                await self.e.move({**LG.operator_pose(worker_id(i),*self.states[i],bend,arm),n:out+(0,)},duration=self.c.get('dock_place_s',1),exact=True)
            self.emit('DOCK_PALLET_READY',dock=d['id'],classification=kind,batch=k)
            await self.transfer(i,local,target)
            await consume(i,local)
            self.stats['pallets_to_w1' if kind=='even' else 'pallets_to_w2']+=1
            if kind=='even':
                self.send({f'dock_deck_{i}':target[:2]+(-5,0)})
                await self.transfer(i,[],stage(d),True,without_deck=True)
                self.send({f'dock_deck_{i}':stage(d)[:2]+(0,0)})
                self.emit('EMPTY_PALLET_FROM_POOL',dock=d['id'],batch=k)
            else:
                await self.transfer(i,[],stage(d),True)
                self.stats['empty_pallet_returns']+=1
                self.emit('EMPTY_PALLET_RETURNED',dock=d['id'],batch=k,classification=kind)
    def e_time(self):
        return self.clock()
