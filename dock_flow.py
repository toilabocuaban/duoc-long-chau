"""Visible dock unloading, simulated barcode manifest, reusable pallet-jack transfer."""
import asyncio
import math
import time
from contextlib import asynccontextmanager
from agv_motion import PARKS as AGV_PARKS
import loose_goods as LG
import truck_scene as TS
from sdf_parts import box, merge_boxes

class FloorGate:
    """AGVs may run together when max_agv > 1; manual pallet crossings exclude moving AGVs in overlapping corridors.
    Up to two manual dock transfers with non-overlapping corridors can cross when no conflicting AGV is waiting."""
    def __init__(self,max_manual=2,max_agv=1):
        self.condition=asyncio.Condition()
        self.agvs=0
        self.agv_waiting=0
        self.crossings=0
        self.waiting=0
        self.last_turn='agv'
        self.max_manual=max_manual
        self.max_agv=max(1,int(max_agv))
        self.active_spans=[]
        self.agv_spans=[]
        self.waiting_agv_spans=[]
        self.waiting_manual_spans=[]

    @property
    def crossing(self):
        return self.crossings > 0

    def _span_clear(self,span,spans):
        if span is None:return not spans
        for a in spans:
            if a is None or not (span[1]<a[0] or span[0]>a[1]):
                return False
        return True

    @asynccontextmanager
    async def agv(self,span=None):
        async with self.condition:
            self.agv_waiting+=1
            self.waiting_agv_spans.append(span)
            try:
                await self.condition.wait_for(
                    lambda: self.agvs<self.max_agv
                    and self._span_clear(span,self.active_spans)
                    and (self._span_clear(span,self.waiting_manual_spans) if self.waiting and self.last_turn!='manual' else True)
                )
                self.agvs+=1
                self.last_turn='agv'
                self.agv_spans.append(span)
            finally:
                self.agv_waiting-=1
                if span in self.waiting_agv_spans:
                    self.waiting_agv_spans.remove(span)
        try:yield
        finally:
            async with self.condition:
                self.agvs-=1
                if span in self.agv_spans:
                    self.agv_spans.remove(span)
                self.condition.notify_all()

    @asynccontextmanager
    async def manual(self,span=None):
        async with self.condition:
            self.waiting+=1
            self.waiting_manual_spans.append(span)
            try:
                await self.condition.wait_for(
                    lambda: self._span_clear(span,self.agv_spans)
                    and (self.crossings==0 or (self.crossings<self.max_manual and self._span_clear(span,self.waiting_agv_spans)))
                    and self._span_clear(span,self.active_spans)
                )
                self.crossings+=1
                self.last_turn='manual'
                self.active_spans.append(span)
            finally:
                self.waiting-=1
                if span in self.waiting_manual_spans:
                    self.waiting_manual_spans.remove(span)
        try:yield
        finally:
            async with self.condition:
                self.crossings-=1
                if span in self.active_spans:
                    self.active_spans.remove(span)
                self.condition.notify_all()

def dock_list(c):return TS.selected_docks({**c,'truck_count_visible':c.get('dock_active_count',4)})
def stage(d):return (d['x'],d['rear_y']+.62,0.)
def source_stack(d,n):
    # Cartons are physically inside the trailer, immediately behind its rear
    # opening. The worker stays north of the dock sill and reaches in by hand.
    x,y=d['x']-.60,d['rear_y']-.35
    return [(x,y,.72) for _ in range(n)]
def worker_id(i):return 10+i

def even_stack_positions(spot, count=18):
    """Carton centers for an even flow (standard high-bay storage pallet): 3 layers of 6 = 18 cartons.
    Returns top-layer first (unpack order), far-to-near within layers."""
    x, y, yaw = spot
    c, s = math.cos(yaw), math.sin(yaw)
    out = []
    # 3 layers (z: 0.255, 0.505, 0.755)
    # per layer: 3 along x (-0.38, 0.0, 0.38), 2 along y (-0.20, 0.20)
    for layer in range(3):
        lz = 0.255 + layer * 0.250
        for lx in (-0.38, 0.0, 0.38):
            for ly in (-0.20, 0.20):
                if len(out) < count:
                    out.append((x + lx * c - ly * s, y + lx * s + ly * c, lz))
    while len(out) < count:
        out.append((x, y, 0.255 + 2 * 0.250))
    return list(reversed(out))

def scene(c,model,pallet_deck):
    meshes={p:merge_boxes('dock_worker_'+p,[box(f'{p}{k}',*b) for k,b in enumerate(bs)]) for p,bs in LG.WORKER_PARTS.items()}
    deck=merge_boxes('dock_empty_pallet',pallet_deck())
    carton=merge_boxes('dock_carton',[box('box',0,0,0,*LG.CARTON,'.76 .57 .33 1'),box('tape',0,0,.127,.05,.3,.004,'.92 .8 .57 1')])
    jack=merge_boxes('dock_jack',[
        box('body',-.55,0,.14,.35,.55,.18,'.9 .55 .02 1'),
        box('steer_l',-.55,-.18,.05,.12,.08,.10,'.15 .15 .15 1'),
        box('steer_r',-.55,.18,.05,.12,.08,.10,'.15 .15 .15 1'),
        box('forkl',.1,-.25,.065,1.2,.12,.08,'.9 .55 .02 1'),
        box('forkr',.1,.25,.065,1.2,.12,.08,'.9 .55 .02 1'),
        box('roller_l',.62,-.25,.035,.10,.10,.07,'.15 .15 .15 1'),
        box('roller_r',.62,.25,.035,.10,.10,.07,'.15 .15 .15 1'),
        box('handle',-.78,0,.65,.06,.06,1.05,'.1 .1 .1 1'),
        box('grip',-.78,0,1.18,.06,.5,.06,'.1 .1 .1 1')
    ])
    scanner=merge_boxes('dock_scanner',[box('stand',0,0,.5,.06,.06,1,'.3 .3 .3 1'),box('reader',0,0,1.02,.22,.16,.12,'.05 .12 .12 1'),box('light',0,-.085,1.03,.14,.01,.04,'.1 1 .2 1')])
    n=max(18,int(c.get('loose_cartons_per_pallet',18)))
    for i,d in enumerate(dock_list(c)):
        for name,pose in LG.operator_pose(worker_id(i),d['x']+.85,d['rear_y']+.75,math.pi).items():
            model(name,pose[:3]+(pose[5],),meshes[name.split('_',1)[1]])
        model(f'dock_deck_{i}',stage(d)[:2]+(0,0),deck)
        model(f'dock_jack_{i}',stage(d)[:2]+(0,-math.pi/2),jack)
        init_stack=list(reversed(even_stack_positions(stage(d),n)))
        for j in range(n):
            p_init=init_stack[j] if j<len(init_stack) else (d['x'],d['rear_y']+.62,.255)
            model(f'dock_carton_{i}_{j}',p_init,carton)
        model(f'dock_scanner_{i}',(d['x']+.75,d['rear_y']+.45,0),scanner)

class DockFlow:
    def __init__(self,c,engine,send,poses,fast,emit):
        self.c=c;self.e=engine;self.send=lambda p:send(p,transmit=not fast);self.poses=poses;self.emit=emit
        self.docks=dock_list(c);self.locks=[asyncio.Lock() for _ in self.docks];self.gate=FloorGate(max_manual=2,max_agv=len(AGV_PARKS))
        self.stats={'scanned_even':0,'scanned_odd':0,'pallets_to_w1':0,'pallets_to_w2':0,'empty_pallet_returns':0}
        self.states={}
        self.preloaded_batch0={}
        self.fast=fast
        self.ambient_task=None
    def _restock_stage_visual(self,i,count=None):
        d=self.docks[i]
        cnt=count if count is not None else max(18,int(self.c.get('loose_cartons_per_pallet',18)))
        dst=list(reversed(even_stack_positions(stage(d),cnt)))
        self.send({
            f'dock_deck_{i}':stage(d)[:2]+(0,0),
            **{f'dock_carton_{i}_{j}':dst[j]+(0,) for j in range(min(cnt,len(dst)))},
        })
    def _reset_stage_deck(self,i,count=None):
        """Place an empty pallet deck on the returning pallet jack at the dock, with all cartons hidden until unloaded."""
        d=self.docks[i];st=stage(d)
        cnt=count if count is not None else max(18,int(self.c.get('loose_cartons_per_pallet',18)))
        self.poses[f'dock_jack_{i}']=st[:2]+(0.,-math.pi/2)
        self.poses[f'dock_deck_{i}']=st[:2]+(0.,0.)
        self.send({
            f'dock_jack_{i}':st[:2]+(0.,-math.pi/2),
            f'dock_deck_{i}':st[:2]+(0.,0.),
            **{f'dock_carton_{i}_{j}':(st[0],st[1],-5.,0.) for j in range(cnt)},
        })
    def initialize(self):
        cnt=max(18,int(self.c.get('loose_cartons_per_pallet',18)))
        for i,d in enumerate(self.docks):
            self.states[i]=(d['x']+.85,d['rear_y']+.75,math.pi)
            self.preloaded_batch0[i]=True
            self.send(LG.operator_pose(worker_id(i),*self.states[i]))
            self.send({f'dock_deck_{i}':stage(d)[:2]+(0,0),f'dock_jack_{i}':stage(d)[:2]+(0,-math.pi/2)})
            self._restock_stage_visual(i,cnt)
        if not self.fast and (self.ambient_task is None or self.ambient_task.done()):
            self.ambient_task=asyncio.create_task(self.ambient_loop())
    async def ambient_loop(self):
        POSTURES = [
            (0.150, 0.720, 0.000),   # Posture 0: Cầm bảng kiểm kê / tablet ở ngang ngực tại cửa dock
            (0.220, 0.840, 0.060),   # Posture 1: Nghiêng người kiểm tra tem kiện hàng / đối chiếu xe tải
            (0.120, 0.620, -0.060),  # Posture 2: Đổi trọng tâm chân, tay cầm máy quét hướng về thùng xe
        ]
        TRANS_DUR = 1.00
        CYCLE_DUR = 5.00
        LOOP_INTERVAL = 0.20
        while True:
            try:
                await asyncio.sleep(LOOP_INTERVAL)
                t = self.clock() if hasattr(self, 'clock') and callable(self.clock) else time.time()
                changes = {}
                for i, d in enumerate(self.docks):
                    if self.locks[i].locked():
                        continue
                    wx, wy, wyaw = self.states[i]
                    phi = (i * 2.3999632297) % (len(POSTURES) * CYCLE_DUR)
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
                    changes.update(LG.operator_pose(worker_id(i), wx, wy, wyaw + dyaw, bend, arm, 0.0))
                if changes:
                    self.send(changes)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
    async def worker(self,i,target,carton=None):
        a=self.states[i];b=target;dist=math.dist(a[:2],b[:2])
        dx,dy=b[0]-a[0],b[1]-a[1]
        def at(q):
            x=a[0]+dx*q;y=a[1]+dy*q
            if dist<0.30:
                dyaw=math.atan2(math.sin(b[2]-a[2]),math.cos(b[2]-a[2]))
                yaw=a[2]+dyaw*q
            else:
                travel_yaw=math.atan2(dy,dx)
                d1=math.atan2(math.sin(travel_yaw-a[2]),math.cos(travel_yaw-a[2]))
                d2=math.atan2(math.sin(b[2]-travel_yaw),math.cos(b[2]-travel_yaw))
                if q<0.25:
                    u=q/0.25;w=3*u*u-2*u*u*u;yaw=a[2]+d1*w
                elif q>0.75:
                    u=(q-0.75)/0.25;w=3*u*u-2*u*u*u;yaw=travel_yaw+d2*w
                else:
                    yaw=travel_yaw
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
            bend=(.16+.03*sway) if carton else .025*sway
            arm=(1.02+.03*sway) if carton else .06+.025*sway
            out=LG.operator_pose(worker_id(i),x,y,yaw,bend,arm,.035*sway)
            if carton:
                held=LG.held_carton(x,y,yaw,bend,arm)
                out[carton]=(held[0],held[1]-.04,held[2]+.015,held[3])
            return out
        await self.e.move(at(1),duration=max(.1,duration),transform=at,exact=True)
    def transfer_waypoints(self,i,target,returning=False):
        """Remain inside: cross AGV lanes at own dock X, then use the internal corridor.
        Detour North to y = -3.20 when crossing between West (x < -25.0) and East (x > -18.0)
        to cleanly clear the AGV parking bays (x in [-24.8, -18.2], y in [-5.45, -3.55])."""
        start=self.poses[f'dock_jack_{i}'];end=target[:2]+(0,0)
        inside_y=-5.35
        x_from=start[0]
        x_to=self.docks[i]['x'] if returning else end[0]
        pts=[(start[0],inside_y,0,0)]
        if x_from < -25.0 and x_to > -18.0:
            pts.extend([(-25.5,inside_y,0,0),(-25.5,-3.20,0,0),(-17.5,-3.20,0,0),(-17.5,inside_y,0,0)])
        elif x_from > -18.0 and x_to < -25.0:
            pts.extend([(-17.5,inside_y,0,0),(-17.5,-3.20,0,0),(-25.5,-3.20,0,0),(-25.5,inside_y,0,0)])
        if returning:
            pts.extend([(self.docks[i]['x'],inside_y,0,0),end])
        else:
            pts.extend([(end[0],inside_y,0,0),end])
        return pts
    async def pump(self,i,names,deck,d):
        """Step to the pallet jack handle and pump 2 strokes to raise the pallet by 0.05m."""
        jack=f'dock_jack_{i}'
        hx,hy=d['x'],d['rear_y']+.62+.78
        wx,wy,wyaw=hx,hy+.28,-math.pi/2
        await self.worker(i,(wx,wy,wyaw))
        carton_z={n:self.poses[n][2] for n in names}
        for target_z in (.025,.05):
            def down(q,base_z=(target_z-.025)):
                z=base_z+.025*q;bend=.12+.32*q;arm=.40+.32*q
                out=LG.operator_pose(worker_id(i),wx,wy,wyaw,bend,arm)
                out[jack]=(d['x'],d['rear_y']+.62,z,-math.pi/2)
                out[deck]=(d['x'],d['rear_y']+.62,z,0)
                for n in names:out[n]=(self.poses[n][0],self.poses[n][1],carton_z[n]+z,0)
                return out
            await self.e.move(down(1),duration=.35,transform=down,exact=True)
            def up(q):
                bend=.44-.30*q;arm=.72-.30*q
                out=LG.operator_pose(worker_id(i),wx,wy,wyaw,bend,arm)
                out[jack]=(d['x'],d['rear_y']+.62,target_z,-math.pi/2)
                out[deck]=(d['x'],d['rear_y']+.62,target_z,0)
                for n in names:out[n]=(self.poses[n][0],self.poses[n][1],carton_z[n]+target_z,0)
                return out
            await self.e.move(up(1),duration=.25,transform=up,exact=True)
        self.states[i]=(wx,wy,wyaw)
        self.poses[jack]=(d['x'],d['rear_y']+.62,.05,-math.pi/2)
        self.poses[deck]=(d['x'],d['rear_y']+.62,.05,0)
        for n in names:self.poses[n]=(self.poses[n][0],self.poses[n][1],carton_z[n]+.05,0)
    async def transfer(self,i,names,target,returning=False,without_deck=False,traffic=None):
        """Pallet jack plus worker cross the AGV aisle under an exclusive interlock."""
        d=self.docks[i];deck=f'dock_deck_{i}';jack=f'dock_jack_{i}'
        own_traffic=traffic is None
        if own_traffic:
            traffic=self.gate.manual();permission=asyncio.create_task(traffic.__aenter__())
            while not permission.done():
                await self.idle(i,.5)
            await permission
        try:
            if not returning:
                await self.pump(i,names,deck,d)
                carton_local={n:(self.poses[n][0]-d['x'],self.poses[n][1]-(d['rear_y']+.62),self.poses[n][2]-.05) for n in names}
                waypoints=self.transfer_waypoints(i,target,returning=False)
                prev_yaw_jack=self.poses[jack][3]
                corridor_pts=waypoints[:-1]
                slot_pt=waypoints[-1]
                for b in corridor_pts:
                    a=self.poses[jack];length=math.dist(a[:2],b[:2])
                    if length<.01:continue
                    yaw=math.atan2(b[1]-a[1],b[0]-a[0])
                    yaw_jack=yaw+math.pi;yaw_deck=yaw_jack+math.pi/2
                    speed=self.c.get('dock_worker_speed',1.0) if without_deck else self.c.get('dock_pallet_speed',.7)
                    dur=max(.3,length/speed)
                    turn_q=min(0.35,0.6/length) if length>0.05 else 1.0
                    dyaw_jack=math.atan2(math.sin(yaw_jack-prev_yaw_jack),math.cos(yaw_jack-prev_yaw_jack))
                    def at(q,a=a,b=b,yaw=yaw,yaw_jack=yaw_jack,prev_yj=prev_yaw_jack,dyj=dyaw_jack,turn_q=turn_q,length=length):
                        px=a[0]+(b[0]-a[0])*q;py=a[1]+(b[1]-a[1])*q
                        if q<turn_q and abs(dyj)>1e-4:
                            u=q/turn_q;w=3*u*u-2*u*u*u
                            cur_yj=prev_yj+dyj*w
                        else:
                            cur_yj=yaw_jack
                        cur_yd=cur_yj+math.pi/2
                        cos_d,sin_d=math.cos(cur_yd),math.sin(cur_yd)
                        p=(px,py,.05,cur_yj);p_deck=(px,py,.05,cur_yd)
                        out={jack:p}
                        if not without_deck:out[deck]=p_deck
                        for n in names:
                            lx,ly,lz=carton_local[n]
                            cx=px+lx*cos_d-ly*sin_d;cy=py+lx*sin_d+ly*cos_d
                            out[n]=(cx,cy,.05+lz,cur_yd)
                        cur_wyaw=cur_yj-math.pi
                        wx=px+1.06*math.cos(cur_wyaw);wy=py+1.06*math.sin(cur_wyaw)
                        swing=.25*math.sin(q*math.pi*max(1,length/.6))
                        out.update(LG.operator_pose(worker_id(i),wx,wy,cur_wyaw,.05,-.45,swing))
                        return out
                    await self.e.move(at(1),duration=dur,transform=at,exact=True)
                    prev_yaw_jack=yaw_jack
                    self.states[i]=(b[0]+1.06*math.cos(yaw),b[1]+1.06*math.sin(yaw),yaw)
                    self.poses[jack]=(b[0],b[1],.05,yaw_jack)
                    self.poses[deck]=(b[0],b[1],.05,yaw_deck)

                # Push into destination slot (thao tác công nhân đẩy xe vào slot)
                a=self.poses[jack];b=slot_pt;length=math.dist(a[:2],b[:2])
                slot_yaw=math.atan2(b[1]-a[1],b[0]-a[0]) if length>0.01 else math.pi/2
                yaw_jack=slot_yaw;yaw_deck=yaw_jack+math.pi/2
                cos_d,sin_d=math.cos(yaw_deck),math.sin(yaw_deck)
                # Quick turn to get behind handle and prepare push
                w_start=self.states[i]
                w_prep=(a[0]-1.10*math.cos(slot_yaw),a[1]-1.10*math.sin(slot_yaw),slot_yaw)
                def align_push(q):
                    u=3*q*q-2*q*q*q
                    wx=w_start[0]+(w_prep[0]-w_start[0])*u;wy=w_start[1]+(w_prep[1]-w_start[1])*u
                    wyaw=w_start[2]+math.atan2(math.sin(w_prep[2]-w_start[2]),math.cos(w_prep[2]-w_start[2]))*u
                    out={jack:(a[0],a[1],.05,yaw_jack)}
                    if not without_deck:out[deck]=(a[0],a[1],.05,yaw_deck)
                    for n in names:
                        lx,ly,lz=carton_local[n]
                        cx=a[0]+lx*cos_d-ly*sin_d;cy=a[1]+lx*sin_d+ly*cos_d
                        out[n]=(cx,cy,.05+lz,yaw_deck)
                    out.update(LG.operator_pose(worker_id(i),wx,wy,wyaw,.05,.65,0.))
                    return out
                await self.e.move(align_push(1),duration=.35,transform=align_push,exact=True)

                # Pushing into slot: pallet leads North, worker walks behind pushing handle
                push_dur=max(.4,length/self.c.get('dock_pallet_speed',.7))
                def at_push(q):
                    px=a[0]+(b[0]-a[0])*q;py=a[1]+(b[1]-a[1])*q
                    p=(px,py,.05,slot_yaw);p_deck=(px,py,.05,yaw_deck)
                    out={jack:p}
                    if not without_deck:out[deck]=p_deck
                    for n in names:
                        lx,ly,lz=carton_local[n]
                        cx=px+lx*cos_d-ly*sin_d;cy=py+lx*sin_d+ly*cos_d
                        out[n]=(cx,cy,.05+lz,yaw_deck)
                    wx=px-1.10*math.cos(slot_yaw);wy=py-1.10*math.sin(slot_yaw)
                    swing=.25*math.sin(q*math.pi*max(1,length/.6))
                    out.update(LG.operator_pose(worker_id(i),wx,wy,slot_yaw,.08,.65,swing))
                    return out
                await self.e.move(at_push(1),duration=push_dur,transform=at_push,exact=True)
                self.poses[jack]=(b[0],b[1],.05,slot_yaw)
                self.poses[deck]=(b[0],b[1],.05,yaw_deck)
                last_w=(b[0]-1.10*math.cos(slot_yaw),b[1]-1.10*math.sin(slot_yaw),slot_yaw)
                self.states[i]=last_w

                # Lowering at destination (hạ van thủy lực đặt pallet xuống sàn)
                last_jack=self.poses[jack];last_deck=self.poses[deck]
                def lower(q):
                    z=.05*(1-q)
                    out={jack:(last_jack[0],last_jack[1],z,last_jack[3])}
                    if not without_deck:out[deck]=(last_deck[0],last_deck[1],z,last_deck[3])
                    for n in names:
                        lx,ly,lz=carton_local[n]
                        cx=last_jack[0]+lx*cos_d-ly*sin_d;cy=last_jack[1]+lx*sin_d+ly*cos_d
                        out[n]=(cx,cy,z+lz,last_deck[3])
                    bend=.08+.05*q;arm=.65-.25*q
                    out.update(LG.operator_pose(worker_id(i),last_w[0],last_w[1],last_w[2],bend,arm))
                    return out
                await self.e.move(lower(1),duration=.4,transform=lower,exact=True)

                # Withdraw empty pallet jack out from under the pallet (lùi xe nâng tay ra)
                withdraw_y=-4.5
                withdraw_len=abs(last_jack[1]-withdraw_y)
                def withdraw(q):
                    cur_jy=last_jack[1]+(withdraw_y-last_jack[1])*q
                    cur_wy=cur_jy-1.10
                    out={jack:(last_jack[0],cur_jy,0.,slot_yaw)}
                    # Deck and cartons stay solidly at destination (b)
                    if not without_deck:out[deck]=(last_deck[0],last_deck[1],0.,last_deck[3])
                    for n in names:
                        lx,ly,lz=carton_local[n]
                        cx=last_jack[0]+lx*cos_d-ly*sin_d;cy=last_jack[1]+lx*sin_d+ly*cos_d
                        out[n]=(cx,cy,lz,last_deck[3])
                    swing=.20*math.sin(q*math.pi*max(1,withdraw_len/.6))
                    out.update(LG.operator_pose(worker_id(i),last_jack[0],cur_wy,slot_yaw,.05,-.40,swing))
                    return out
                await self.e.move(withdraw(1),duration=max(.4,withdraw_len/.7),transform=withdraw,exact=True)
                self.poses[jack]=(last_jack[0],withdraw_y,0.,slot_yaw)
                self.poses[deck]=(last_deck[0],last_deck[1],0.,last_deck[3])
                self.states[i]=(last_jack[0],withdraw_y-1.10,slot_yaw)
            else:
                waypoints=self.transfer_waypoints(i,target,returning=True)
                prev_yaw_jack=self.poses[jack][3]
                for b in waypoints:
                    a=self.poses[jack];length=math.dist(a[:2],b[:2])
                    if length<.01:continue
                    yaw=math.atan2(b[1]-a[1],b[0]-a[0])
                    yaw_jack=yaw;yaw_deck=yaw_jack+math.pi/2
                    speed=self.c.get('dock_worker_speed',1.0) if without_deck else self.c.get('dock_pallet_speed',.7)
                    dur=max(.3,length/speed)
                    turn_q=min(0.35,0.6/length) if length>0.05 else 1.0
                    dyaw_jack=math.atan2(math.sin(yaw_jack-prev_yaw_jack),math.cos(yaw_jack-prev_yaw_jack))
                    def at_ret(q,a=a,b=b,yaw=yaw,yaw_jack=yaw_jack,prev_yj=prev_yaw_jack,dyj=dyaw_jack,turn_q=turn_q,length=length):
                        px=a[0]+(b[0]-a[0])*q;py=a[1]+(b[1]-a[1])*q
                        if q<turn_q and abs(dyj)>1e-4:
                            u=q/turn_q;w=3*u*u-2*u*u*u
                            cur_yj=prev_yj+dyj*w
                        else:
                            cur_yj=yaw_jack
                        cur_yd=cur_yj+math.pi/2
                        p=(px,py,0.,cur_yj)
                        out={jack:p}
                        if not without_deck:out[deck]=(px,py,0.,cur_yd)
                        wx=px-1.10*math.cos(cur_yj);wy=py-1.10*math.sin(cur_yj)
                        swing=.25*math.sin(q*math.pi*max(1,length/.6))
                        out.update(LG.operator_pose(worker_id(i),wx,wy,cur_yj,.05,.65,swing))
                        return out
                    await self.e.move(at_ret(1),duration=dur,transform=at_ret,exact=True)
                    prev_yaw_jack=yaw_jack
                    self.states[i]=(b[0]-1.10*math.cos(yaw),b[1]-1.10*math.sin(yaw),yaw)
                    self.poses[jack]=(b[0],b[1],0.,yaw_jack)
                    if not without_deck:self.poses[deck]=(b[0],b[1],0.,yaw_deck)
                await self.worker(i,(d['x']+.85,d['rear_y']+.75,math.pi))
        finally:
            if own_traffic:
                await traffic.__aexit__(None,None,None)
    async def _unload_at_dock(self,i,d,kind,k,count,emit_classified=True):
        local=[f'dock_carton_{i}_{j}' for j in range(count)]
        src=source_stack(d,count);dst=list(reversed(even_stack_positions(stage(d),count))) if kind!='odd' else list(reversed(LG.stack_positions(stage(d),count)))
        self.emit('TRUCK_BATCH_READY',dock=d['id'],classification=kind,batch=k,cartons=count)
        if self.preloaded_batch0.pop(i,False):
            self.send({n:out+(0,) for n,out in zip(local,dst)})
            await self.idle(i,0.25)
            if emit_classified:
                self.stats['scanned_'+kind]+=count
                for j in range(count):
                    self.emit('BARCODE_CLASSIFIED',barcode=f'{kind.upper()}-{k:04d}-{j:03d}',classification=kind,dock=d['id'])
            return local
        for j in range(count):
            n,p,out=local[j],src[j],dst[j]
            self.send({n:p+(0,)})
            await self.worker(i,(d['x']-.45,d['rear_y']+.25,-math.pi/2))
            self.emit('WORKER_PICK_FROM_TRUCK',dock=d['id'],carton=j,batch=k)
            wx,wy,wyaw=self.states[i]
            p_carton=p+(0,)
            pick_dur=self.c.get('dock_pick_s',1.0)
            def pick_anim(q,wx=wx,wy=wy,wyaw=wyaw,p_carton=p_carton,n=n):
                if q<0.40:
                    u=q/0.40;w=3*u*u-2*u*u*u
                    bend=0.22*w;arm=0.95*w
                    c_pose=p_carton
                else:
                    u=(q-0.40)/0.60;w=3*u*u-2*u*u*u
                    bend=0.22-0.07*w;arm=0.95
                    c_tgt=LG.held_carton(wx,wy,wyaw,bend,arm)
                    c_pose=tuple(p_carton[m]+(c_tgt[m]-p_carton[m])*w for m in range(4))
                out_p=LG.operator_pose(worker_id(i),wx,wy,wyaw,bend,arm)
                out_p[n]=c_pose
                return out_p
            await self.e.move(pick_anim(1),duration=pick_dur,transform=pick_anim,exact=True)
            await self.worker(i,(d['x']+.42,d['rear_y']+.72,-math.pi/4),n)
            await self.idle(i,self.c.get('inbound_scan_s',2),n)
            if emit_classified:
                self.stats['scanned_'+kind]+=1
                self.emit('BARCODE_CLASSIFIED',barcode=f'{kind.upper()}-{k:04d}-{j:03d}',classification=kind,dock=d['id'])
            if out[0] < d['x'] - 0.15:
                place_x = out[0] - 0.46
                place_y = out[1]
                place_yaw = 0.0
            elif out[0] > d['x'] + 0.15:
                place_x = out[0]
                place_y = out[1] + 0.55
                place_yaw = -math.pi/2
            else:
                place_x = d['x'] + 0.52
                place_y = out[1]
                place_yaw = math.pi
            await self.worker(i, (place_x, place_y, place_yaw), n)
            bend, arm = LG.reach_for(out[2])
            place_dur=self.c.get('dock_place_s',1.0)
            c_start=LG.held_carton(*self.states[i],.15,.95)
            c_target=out+(0,)
            def place_anim(q,bend=bend,arm=arm,c_start=c_start,c_target=c_target,n=n):
                u=3*q*q-2*q*q*q
                b=.15+(bend-.15)*u
                a=.95+(arm-.95)*u
                cz=c_start[2]+(c_target[2]-c_start[2])*u
                cx=c_start[0]+(c_target[0]-c_start[0])*u
                cy=c_start[1]+(c_target[1]-c_start[1])*u
                out_p=LG.operator_pose(worker_id(i),*self.states[i],b,a)
                out_p[n]=(cx,cy,cz,0)
                return out_p
            await self.e.move(place_anim(1),duration=place_dur,transform=place_anim,exact=True)
            def straighten(q,bend=bend,arm=arm):
                u=3*q*q-2*q*q*q
                b=bend*(1-u);a=arm*(1-u)
                return LG.operator_pose(worker_id(i),*self.states[i],b,a)
            await self.e.move(straighten(1),duration=0.35,transform=straighten,exact=True)
        return local

    async def _deliver_staged_pallet(self,i,d,kind,k,target,local,consume,acquire_target=None,staged_cartons=0):
        extra={'cartons':staged_cartons} if staged_cartons else {}
        self.emit('DOCK_PALLET_READY',dock=d['id'],classification=kind,batch=k,**extra)
        span=(min(d['x'],target[0])-1.5,max(d['x'],target[0])+1.5)
        t_gw0=self.e_time()
        traffic=self.gate.manual(span);permission=asyncio.create_task(traffic.__aenter__())
        while not permission.done():
            await self.idle(i,.5)
        await permission
        t_gw1=self.e_time()
        gw_s=max(0.0,t_gw1-t_gw0)
        if gw_s>1e-4:
            self.emit('DOCK_GATE_WAIT',dock=d['id'],classification=kind,batch=k,wait_s=round(gw_s,4),start_s=round(t_gw0,4),end_s=round(t_gw1,4))
        try:
            if acquire_target is not None:
                t_tw0=self.e_time()
                async with acquire_target:
                    t_tw1=self.e_time()
                    if t_tw1-t_tw0>1e-4:
                        self.emit('DOCK_SLOT_WAIT',dock=d['id'],classification=kind,batch=k,wait_s=round(t_tw1-t_tw0,4),start_s=round(t_tw0,4),end_s=round(t_tw1,4))
                    await self.transfer(i,local,target,traffic=traffic)
                    await consume(i,local)
                    self.stats['pallets_to_w2']+=1
                    await self.transfer(i,[],stage(d),True,without_deck=True,traffic=traffic)
                    self._reset_stage_deck(i,len(local))
                    self.stats['empty_pallet_returns']+=1
                    self.emit('EMPTY_PALLET_RETURNED',dock=d['id'],batch=k,classification=kind)
            else:
                await self.transfer(i,local,target,traffic=traffic)
                await consume(i,local)
                self.stats['pallets_to_w1']+=1
                await self.transfer(i,[],stage(d),True,without_deck=True,traffic=traffic)
                self._reset_stage_deck(i,len(local))
                self.emit('EMPTY_PALLET_FROM_POOL',dock=d['id'],batch=k)
        finally:
            await traffic.__aexit__(None,None,None)
        await self.worker(i,(d['x']+.85,d['rear_y']+.75,math.pi))

    async def prepare(self,kind,k,target,names,consume,acquire_target=None):
        i=k%len(self.docks);d=self.docks[i]
        async with self.locks[i]:
            local=await self._unload_at_dock(i,d,kind,k,len(names),emit_classified=True)
            await self._deliver_staged_pallet(i,d,kind,k,target,local,consume,acquire_target=acquire_target)

    async def prestage_and_deliver(self,i,k,count,claim_slot_fn):
        """Worker at dock i pre-stages a full pallet immediately, then delivers to whichever slot frees first."""
        d=self.docks[i]
        async with self.locks[i]:
            local=await self._unload_at_dock(i,d,'staged',k,count,emit_classified=False)
            t_sw0=self.e_time()
            claimed=await claim_slot_fn(i,d['id'],k,count)
            t_sw1=self.e_time()
            if t_sw1-t_sw0>1e-4:
                self.emit('DOCK_SLOT_WAIT',dock=d['id'],classification='staged',batch=k,wait_s=round(t_sw1-t_sw0,4),start_s=round(t_sw0,4),end_s=round(t_sw1,4))
            if claimed is None:
                self._reset_stage_deck(i,count)
                return
            kind,target,acquire_target,consume=claimed
            self.stats['scanned_'+kind]+=count
            await self._deliver_staged_pallet(i,d,kind,k,target,local,consume,acquire_target=acquire_target,staged_cartons=count)
    def e_time(self):
        return self.clock()
