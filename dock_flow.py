"""Visible dock unloading, simulated barcode manifest, reusable 2-handlift / 2-worker pipelined transfer."""
import asyncio
import math
import random
import time
from contextlib import asynccontextmanager
from agv_motion import PARKS as AGV_PARKS
import loose_goods as LG
import truck_scene as TS
from sdf_parts import box, merge_boxes

class FloorGate:
    """AGVs may run together when max_agv > 1; manual pallet crossings exclude moving AGVs in overlapping corridors.
    Up to max_manual dock transfers with non-overlapping corridors can cross concurrently."""
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
def pallet_stack_xy(d):return (d['x']-1.52,d['rear_y']+.62)
def jack_b_home_xy(d):return (d['x']-1.52,d['rear_y']+2.05)
def source_stack(d,n):
    # Cartons inside the trailer, immediately behind its rear opening (right/left halves).
    out=[]
    for idx in range(n):
        dx=.36 if (idx%2)==0 else -.36
        out.append((d['x']+dx,d['rear_y']-.35,.72))
    return out
def worker_id(i):return 10+i
def worker_b_id(i):return 600+i

def _smooth_curve_samples(pts,radius=1.05,step=0.25):
    """Build smooth rounded-corner (quadratic Bezier fillet) polyline samples [(x, y), ...] from control points."""
    clean=[pts[0][:2]]
    for p in pts[1:]:
        if math.dist(clean[-1],p[:2])>0.02:
            clean.append(p[:2])
    if len(clean)<=2:
        return clean
    out=[clean[0]]
    for idx in range(1,len(clean)-1):
        p0,p1,p2=clean[idx-1],clean[idx],clean[idx+1]
        d1=math.dist(p0,p1);d2=math.dist(p1,p2)
        r=min(radius,0.45*d1,0.45*d2)
        if r<0.05:
            out.append(p1)
            continue
        a=(p1[0]+(p0[0]-p1[0])*(r/d1),p1[1]+(p0[1]-p1[1])*(r/d1))
        b=(p1[0]+(p2[0]-p1[0])*(r/d2),p1[1]+(p2[1]-p1[1])*(r/d2))
        n_arc=max(4,int((r*1.6)/max(0.10,step)))
        for k in range(n_arc+1):
            t=k/n_arc;u=1.0-t
            cx=u*u*a[0]+2*u*t*p1[0]+t*t*b[0]
            cy=u*u*a[1]+2*u*t*p1[1]+t*t*b[1]
            if math.dist(out[-1],(cx,cy))>0.01:
                out.append((cx,cy))
    if math.dist(out[-1],clean[-1])>0.01:
        out.append(clean[-1])
    return out

def _arc_table(samples):
    """Precompute cumulative arc-length table for smooth constant-speed evaluation."""
    cum=[0.0]
    for k in range(1,len(samples)):
        cum.append(cum[-1]+math.dist(samples[k-1],samples[k]))
    total=max(1e-6,cum[-1])
    def eval_at(q):
        s=max(0.0,min(total,q*total))
        lo,hi=0,len(cum)-1
        while lo+1<hi:
            mid=(lo+hi)//2
            if cum[mid]<=s:lo=mid
            else:hi=mid
        seg=max(1e-6,cum[hi]-cum[lo])
        t=(s-cum[lo])/seg
        x=samples[lo][0]+(samples[hi][0]-samples[lo][0])*t
        y=samples[lo][1]+(samples[hi][1]-samples[lo][1])*t
        dx=samples[hi][0]-samples[lo][0];dy=samples[hi][1]-samples[lo][1]
        yaw=math.atan2(dy,dx) if (abs(dx)+abs(dy))>1e-6 else 0.0
        return x,y,yaw
    return total,eval_at

def even_stack_positions(spot, count=30):
    """Carton centers for an even flow (standard high-bay storage pallet): layers of 6 (e.g. 5 layers = 30 cartons).
    Returns top-layer first (unpack order), far-to-near within layers."""
    x, y, yaw = spot
    c, s = math.cos(yaw), math.sin(yaw)
    out = []
    layers = max(1, math.ceil(count / 6))
    for layer in range(layers):
        lz = 0.255 + layer * 0.250
        for lx in (-0.38, 0.0, 0.38):
            for ly in (-0.20, 0.20):
                if len(out) < count:
                    out.append((x + lx * c - ly * s, y + lx * s + ly * c, lz))
    return list(reversed(out))

def _build_loaded_pallet_mesh(name, pallet_deck_fn, count, total_h, wrapped=False):
    """Single merged mesh for a full pallet carried on a handlift, freeing dock_carton_{i}_* immediately for Worker B."""
    v = list(pallet_deck_fn())
    cargo_h = max(0.50, total_h - 0.15)
    layers = max(1, math.ceil(count / 6))
    dz = cargo_h / layers
    placed = 0
    for lyr in range(layers):
        bz = 0.15 + (lyr + 0.5) * dz
        for lx in (-0.38, 0.0, 0.38):
            for ly in (-0.22, 0.22):
                if placed >= count:
                    break
                v.append(box(f'{name}_c_{placed}', lx, ly, bz, .38, .44, dz - .012, '.76 .57 .33 1'))
                v.append(box(f'{name}_t_{placed}', lx, ly, bz + dz * 0.5 - .005, .05, .43, .005, '.92 .8 .57 1'))
                placed += 1
    merged = merge_boxes(name, v)
    if wrapped:
        wf = [s.replace('</visual>', '<transparency>0.42</transparency><cast_shadows>false</cast_shadows></visual>')
              for s in merge_boxes(f'{name}_wrap', [
                  box('wf_f', 0, -.505, .15 + cargo_h * 0.5, 1.23, .018, cargo_h * 0.96, '.94 .95 .98 1'),
                  box('wf_b', 0, .505, .15 + cargo_h * 0.5, 1.23, .018, cargo_h * 0.96, '.94 .95 .98 1'),
                  box('wf_l', -.605, 0, .15 + cargo_h * 0.5, .018, 1.03, cargo_h * 0.96, '.94 .95 .98 1'),
                  box('wf_r', .605, 0, .15 + cargo_h * 0.5, .018, 1.03, cargo_h * 0.96, '.94 .95 .98 1'),
              ])]
        merged = merged + wf
    return merged

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
    n=max(1,int(c.get('loose_cartons_per_pallet',30)))
    # Carton models per dock: a truck's 200 cartons are split over 6-7 pallets, so a pallet can hold up to ~34.
    n_dock_cartons=max(n,int(c.get('dock_max_cartons_per_pallet',n)))
    even_h=float((c.get('pallet_size') or [1.2,1.0,1.4])[2])
    odd_h=float((c.get('odd_pallet_size') or [1.2,1.0,1.7])[2])
    loaded_even_mesh=_build_loaded_pallet_mesh('dock_loaded_even',pallet_deck,n,even_h,wrapped=True)
    loaded_odd_mesh=_build_loaded_pallet_mesh('dock_loaded_odd',pallet_deck,n,odd_h,wrapped=False)
    n_workers_per_dock=max(1,int(c.get('dock_workers_per_truck',2)))
    n_slots_per_dock=max(1,int(c.get('dock_pallet_slots_per_truck',6)))
    wrap_film=[v.replace('</visual>','<transparency>0.42</transparency><cast_shadows>false</cast_shadows></visual>') for v in merge_boxes('dock_wrap_film',[
        box('wf_f',0,-.505,.72,1.23,.018,1.18,'.94 .95 .98 1'),
        box('wf_b',0,.505,.72,1.23,.018,1.18,'.94 .95 .98 1'),
        box('wf_l',-.605,0,.72,.018,1.03,1.18,'.94 .95 .98 1'),
        box('wf_r',.605,0,.72,.018,1.03,1.18,'.94 .95 .98 1'),
        box('wf_t',0,0,1.32,1.21,1.01,.012,'.94 .95 .98 1'),
    ])]
    film_roll=merge_boxes('dock_film_roll',[
        box('core',0,0,0,.07,.07,.54,'.55 .38 .20 1'),
        box('film',0,0,0,.14,.14,.44,'.92 .94 .98 1'),
    ])
    staging_boxes=[]
    for i,d in enumerate(dock_list(c)):
        for name,pose in LG.operator_pose(worker_id(i),d['x']+.85,d['rear_y']+.75,math.pi).items():
            model(name,pose[:3]+(pose[5],),meshes[name.split('_',1)[1]])
        if n_workers_per_dock>=2:
            for name,pose in LG.operator_pose(worker_b_id(i),d['x']-.85,d['rear_y']+.75,0.0).items():
                model(name,pose[:3]+(pose[5],),meshes[name.split('_',1)[1]])
        psx,psy=pallet_stack_xy(d)
        staging_boxes.extend([
            box(f'spad_{i}',psx,psy,.003,1.06,1.26,.006,'.18 .46 .36 1'),
            box(f'slin_l_{i}',psx-.51,psy,.006,.04,1.22,.008,'.88 .76 .20 1'),
            box(f'slin_r_{i}',psx+.51,psy,.006,.04,1.22,.008,'.88 .76 .20 1'),
            box(f'stage_pad_{i}',d['x'],d['rear_y']+.62,.003,1.26,1.08,.006,'.18 .46 .36 1'),
        ])
        n_stack_layers=min(5,max(3,n_slots_per_dock-1))
        for lyr in range(n_stack_layers):
            zb=lyr*0.145
            staging_boxes.extend([
                box(f'wp_top_{i}_{lyr}',psx,psy,zb+.115,.98,1.18,.035,'.035 .24 .68 1'),
                box(f'wp_ft1_{i}_{lyr}',psx,psy-.46,zb+.050,.94,.16,.095,'.035 .24 .68 1'),
                box(f'wp_ft2_{i}_{lyr}',psx,psy,zb+.050,.94,.16,.095,'.035 .24 .68 1'),
                box(f'wp_ft3_{i}_{lyr}',psx,psy+.46,zb+.050,.94,.16,.095,'.035 .24 .68 1'),
            ])
        jbx,jby=jack_b_home_xy(d)
        model(f'dock_wrap_{i}',stage(d)[:2]+(-5.0,0),wrap_film)
        model(f'dock_roll_{i}',(d['x']+.75,d['rear_y']+.45,-5.0,0),film_roll)
        model(f'dock_deck_{i}',stage(d)[:2]+(-5.0,0),deck)
        model(f'dock_jack_{i}',stage(d)[:2]+(0,-math.pi/2),jack)
        model(f'dock_jack_b_{i}',(jbx,jby,0,-math.pi/2),jack)
        model(f'dock_loaded_even_{i}',stage(d)[:2]+(-5.0,0),loaded_even_mesh)
        model(f'dock_loaded_odd_{i}',stage(d)[:2]+(-5.0,0),loaded_odd_mesh)
        for j in range(n_dock_cartons):
            model(f'dock_carton_{i}_{j}',(d['x'],d['rear_y']+.62,-5.0),carton)
        model(f'dock_scanner_{i}',(d['x']+.92,d['rear_y']+.45,0),scanner)
    if staging_boxes:
        model('dock_staging_props',(0,0,0,0),merge_boxes('dock_staging_props',staging_boxes))

class DockFlow:
    def __init__(self,c,engine,send,poses,fast,emit):
        self.c=c;self.e=engine;self.send=lambda p:send(p,transmit=not fast);self.poses=poses;self.emit=emit
        self.docks=dock_list(c);self.locks=[asyncio.Lock() for _ in self.docks];self.gate=FloorGate(max_manual=8,max_agv=len(AGV_PARKS))
        self.stats={'scanned_even':0,'scanned_odd':0,'pallets_to_w1':0,'pallets_to_w2':0,'empty_pallet_returns':0}
        self.states={}
        self.states_b={}
        self.wrapped={}
        self.pipe={}
        self.bg_tasks={}
        self.has_worker_b=int(c.get('dock_workers_per_truck',2))>=2
        self.fast=fast
        self.ambient_task=None
        self._rng=random.Random(20261128)
        # Shared dock crew (truck_mode + dock_worker_pool): workers are not tied to a door. Every docked truck takes
        # one worker as its primary (worker A model of that door); a worker without a truck joins a busy door as
        # helper (worker B model, max 2 per truck = 2 handlifts per truck) to unload/stack and keep the dock
        # working while the primary pulls a finished pallet to the buffer zone. Helpers leave at a pallet boundary
        # as soon as a newly docked truck is waiting for a worker.
        self.pool_size=int(c.get('dock_worker_pool',0) or 0)
        self.pool_mode=bool(c.get('truck_mode')) and self.pool_size>0
        self.pool_free=self.pool_size
        self.primary_waiting=0
        self.pool_cond=None
        self.helper={i:False for i in range(len(self.docks))}
        self.primary_on={i:not self.pool_mode for i in range(len(self.docks))}

    def _hb(self,i):
        """True when door i currently has a second worker (fixed crew, or a pool helper)."""
        return self.helper.get(i,False) if self.pool_mode else self.has_worker_b

    def _pc(self):
        if self.pool_cond is None:self.pool_cond=asyncio.Condition()
        return self.pool_cond

    def _show_worker(self,wid,pose,visible):
        x,y,yaw=pose
        self.send(LG.operator_pose(wid,x,y,yaw,z0=0. if visible else -6.))

    async def acquire_primary(self,i):
        """A truck docked at door i waits for a free crew member to become its primary worker."""
        if not self.pool_mode:return
        async with self._pc():
            self.primary_waiting+=1
            try:
                await self._pc().wait_for(lambda:self.pool_free>0)
                self.pool_free-=1
            finally:
                self.primary_waiting-=1
        self.primary_on[i]=True
        self._show_worker(worker_id(i),self.states[i],True)
        self.emit('DOCK_PRIMARY_START',dock=self.docks[i]['id'])

    async def release_truck_crew(self,i):
        """Truck at door i finished: its helper and primary go back to the shared crew."""
        if not self.pool_mode:return
        await self._release_helper(i)
        if self.primary_on.get(i):
            self.primary_on[i]=False
            self._show_worker(worker_id(i),self.states[i],False)
            self.emit('DOCK_PRIMARY_END',dock=self.docks[i]['id'])
            async with self._pc():
                self.pool_free+=1
                self._pc().notify_all()

    async def _release_helper(self,i):
        if not (self.pool_mode and self.helper.get(i)):return
        self.helper[i]=False
        self._show_worker(worker_b_id(i),self.states_b[i],False)
        self.emit('DOCK_HELPER_LEAVE',dock=self.docks[i]['id'])
        async with self._pc():
            self.pool_free+=1
            self._pc().notify_all()

    async def rebalance_helper(self,i):
        """At each pallet start: leave if a docked truck waits for a worker, otherwise join if someone is free."""
        if not self.pool_mode or not self.primary_on.get(i):return
        if self.helper.get(i):
            if self.primary_waiting>0:await self._release_helper(i)
            return
        if self.pool_free>0 and self.primary_waiting==0:
            self.pool_free-=1
            self.helper[i]=True
            self._show_worker(worker_b_id(i),self.states_b[i],True)
            self.emit('DOCK_HELPER_JOIN',dock=self.docks[i]['id'])

    def _reset_stage_deck(self,i,count=None,keep_preload=False):
        """Reset carrier models after delivery; preserve Worker B's ongoing preload if active."""
        d=self.docks[i];st=stage(d);jbx,jby=jack_b_home_xy(d)
        cnt=count if count is not None else max(1,int(self.c.get('loose_cartons_per_pallet',30)))
        self.poses[f'dock_jack_{i}']=st[:2]+(0.,-math.pi/2)
        updates={
            f'dock_jack_{i}':st[:2]+(0.,-math.pi/2),
            f'dock_loaded_even_{i}':(st[0],st[1],-5.,0.),
            f'dock_loaded_odd_{i}':(st[0],st[1],-5.,0.),
            f'dock_roll_{i}':(st[0],st[1],-5.,0.),
        }
        if not keep_preload:
            self.wrapped[i]=False
            self.poses[f'dock_deck_{i}']=st[:2]+(-5.,0.)
            updates[f'dock_deck_{i}']=(st[0],st[1],-5.,0.)
            updates[f'dock_wrap_{i}']=(st[0],st[1],-5.,0.)
            updates[f'dock_jack_b_{i}']=(jbx,jby,0.,-math.pi/2)
            for j in range(cnt):
                updates[f'dock_carton_{i}_{j}']=(st[0],st[1],-5.,0.)
        self.send(updates)

    def initialize(self):
        cnt=max(1,int(self.c.get('loose_cartons_per_pallet',30)),int(self.c.get('dock_max_cartons_per_pallet',0)))
        for i,d in enumerate(self.docks):
            self.states[i]=(d['x']+.85,d['rear_y']+.75,math.pi)
            self.states_b[i]=(d['x']-.85,d['rear_y']+.75,0.0)
            self.wrapped[i]=False
            self.pipe[i]={'staged':False,'face_scanned':False,'next_j':0,'kind':None,'stop_b':False}
            self.send(LG.operator_pose(worker_id(i),*self.states[i]))
            if self._hb(i):
                self.send(LG.operator_pose(worker_b_id(i),*self.states_b[i]))
            self._reset_stage_deck(i,cnt,keep_preload=False)
            if self.pool_mode:   # shared crew: nobody stands at a door until a truck docks there
                self._show_worker(worker_id(i),self.states[i],False)
                self._show_worker(worker_b_id(i),self.states_b[i],False)
        if not self.fast and (self.ambient_task is None or self.ambient_task.done()):
            self.ambient_task=asyncio.create_task(self.ambient_loop())

    async def ambient_loop(self):
        POSTURES = [
            (0.150, 0.720, 0.000),
            (0.220, 0.840, 0.060),
            (0.120, 0.620, -0.060),
        ]
        CYCLE_DUR = 5.00
        LOOP_INTERVAL = 0.20
        while True:
            try:
                await asyncio.sleep(LOOP_INTERVAL)
                t = self.clock() if hasattr(self, 'clock') and callable(self.clock) else time.time()
                changes = {}
                for i, d in enumerate(self.docks):
                    bg = self.bg_tasks.get(i)
                    if self.locks[i].locked() or (bg is not None and not bg.done()) or not self.primary_on.get(i,True):
                        continue
                    for wid, st_pose, p_shift in ((worker_id(i), self.states[i], 0.0), (worker_b_id(i), self.states_b.get(i, (d['x']-.85, d['rear_y']+.75, 0.0)), 2.5)):
                        if wid == worker_b_id(i) and not self._hb(i):
                            continue
                        wx, wy, wyaw = st_pose
                        phi = (i * 2.3999632297 + p_shift) % (len(POSTURES) * CYCLE_DUR)
                        t_eff = t + phi
                        cycle_idx = int(t_eff // CYCLE_DUR)
                        tau = t_eff % CYCLE_DUR
                        k_cur = cycle_idx % len(POSTURES)
                        k_nxt = (k_cur + 1) % len(POSTURES)
                        w = 0.5 - 0.5 * math.cos(math.pi * tau / CYCLE_DUR)
                        p_from = POSTURES[k_cur]; p_to = POSTURES[k_nxt]
                        breath = 0.015 * math.sin(tau * 3.14159)
                        bend = p_from[0] + (p_to[0] - p_from[0]) * w + breath
                        arm = p_from[1] + (p_to[1] - p_from[1]) * w + breath * 1.2
                        dyaw = p_from[2] + (p_to[2] - p_from[2]) * w
                        changes.update(LG.operator_pose(wid, wx, wy, wyaw + dyaw, bend, arm, 0.0))
                if changes:
                    self.send(changes)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _walk_interp(self,wid,a,b,q,carton=None,bend_idle=0.0,arm_idle=0.0):
        dist=math.dist(a[:2],b[:2])
        dx,dy=b[0]-a[0],b[1]-a[1]
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
        bend=.12 if carton else bend_idle
        arm=1.0 if carton else arm_idle
        swing=.28*math.sin(q*max(1,dist/.6)*math.pi) if dist>0.08 else 0.0
        result=LG.operator_pose(wid,x,y,yaw,bend,arm,swing)
        if carton:result[carton]=LG.held_carton(x,y,yaw,.12,1.0)
        return result

    def _walk_speed(self):
        rng=self.c.get('worker_walk_speed_range_mps')
        if isinstance(rng,(list,tuple)) and len(rng)>=2:
            lo,hi=float(rng[0]),float(rng[1])
            if 0.05<lo<=hi:
                return self._rng.uniform(lo,hi)
        return max(0.2,float(self.c.get('dock_worker_speed',0.7)))

    def _pallet_speed(self):
        return max(0.2,float(self.c.get('dock_pallet_speed',1.0)))

    def _sample_pallet_carton_times(self,count,stage_s=20.0):
        """Sample per-carton (unload + stack) total time in dock_carton_s_range (e.g. 5-10 s/carton)
        such that filling a pallet (~30 cartons) takes 3.0 - 5.0 minutes (180 - 300 s)."""
        n=max(1,int(count))
        rng=self.c.get('dock_carton_s_range')
        if isinstance(rng,(list,tuple)) and len(rng)>=2 and 0.5<=float(rng[0])<=float(rng[1]):
            lo,hi=float(rng[0]),float(rng[1])
            if lo<=6.5 and hi>=8.5:
                pace_lo=min(hi,max(lo,(180.0-stage_s)/n+0.20))
                pace_hi=max(pace_lo,min(hi,(300.0-stage_s)/n-0.20))
                base=self._rng.uniform(pace_lo,pace_hi)
                jitter=min(0.60,(hi-lo)*0.18)
                return [min(hi,max(lo,base+self._rng.uniform(-jitter,jitter))) for _ in range(n)]
            return [self._rng.uniform(lo,hi) for _ in range(n)]
        fixed=self.step_s('dock_unload_carton_s',3.75)+self.step_s('dock_stack_carton_s',3.75)
        return [fixed]*n

    async def worker(self,i,target,carton=None):
        a=self.states[i];b=target;dist=math.dist(a[:2],b[:2])
        def at(q):return self._walk_interp(worker_id(i),a,b,q,carton=carton)
        await self.e.move(at(1),duration=max(.30,dist/self._walk_speed()),transform=at,exact=True)
        self.states[i]=b

    async def worker_b(self,i,target,carton=None,dur_override=None):
        a=self.states_b[i];b=target;dist=math.dist(a[:2],b[:2])
        walk_dur=max(.30,dist/self._walk_speed())
        dur=max(dur_override,walk_dur) if dur_override is not None else walk_dur
        def at(q):return self._walk_interp(worker_b_id(i),a,b,q,carton=carton)
        await self.e.move(at(1),duration=dur,transform=at,exact=True)
        self.states_b[i]=b

    async def both_workers(self,i,target_a,target_b,carton_a=None,carton_b=None,dur_override=None):
        if not self._hb(i):
            await self.worker(i,target_a,carton=carton_a)
            return
        aa=self.states[i];ba=target_a
        ab=self.states_b[i];bb=target_b
        dist=max(math.dist(aa[:2],ba[:2]),math.dist(ab[:2],bb[:2]))
        walk_dur=max(.30,dist/self._walk_speed())
        dur=max(dur_override,walk_dur) if dur_override is not None else walk_dur
        def at(q):
            out=self._walk_interp(worker_id(i),aa,ba,q,carton=carton_a)
            out.update(self._walk_interp(worker_b_id(i),ab,bb,q,carton=carton_b))
            return out
        await self.e.move(at(1),duration=dur,transform=at,exact=True)
        self.states[i]=ba;self.states_b[i]=bb

    async def idle(self,i,duration,carton=None,carton_b=None,solo_a=False):
        """Keep operator(s) visibly alive during brief scan passes or traffic waits."""
        x,y,yaw=self.states[i]
        xb,yb,yawb=self.states_b.get(i,(x-1.6,y,0.0))
        def at(q):
            sway=math.sin(q*math.pi*4)
            bend=(.16+.03*sway) if carton else .025*sway
            arm=(1.02+.03*sway) if carton else .06+.025*sway
            out=LG.operator_pose(worker_id(i),x,y,yaw,bend,arm,.035*sway)
            if carton:
                held=LG.held_carton(x,y,yaw,bend,arm)
                out[carton]=(held[0],held[1]-.04,held[2]+.015,held[3])
            if self._hb(i) and not solo_a:
                bb=(.16-.03*sway) if carton_b else -.025*sway
                ba=(1.02-.03*sway) if carton_b else .08-.025*sway
                out.update(LG.operator_pose(worker_b_id(i),xb,yb,yawb,bb,ba,-.035*sway))
                if carton_b:
                    held_b=LG.held_carton(xb,yb,yawb,bb,ba)
                    out[carton_b]=(held_b[0],held_b[1]-.04,held_b[2]+.015,held_b[3])
            return out
        await self.e.move(at(1),duration=max(.1,duration),transform=at,exact=True)

    async def idle_b(self,i,duration,carton=None):
        """Keep Worker B alone visibly working (Worker A may be away pulling a pallet)."""
        if not self._hb(i) or duration<=1e-6:return
        x,y,yaw=self.states_b[i]
        def at(q):
            sway=math.sin(q*math.pi*4)
            bend=(.16-.03*sway) if carton else -.025*sway
            arm=(1.02-.03*sway) if carton else .08-.025*sway
            out=LG.operator_pose(worker_b_id(i),x,y,yaw,bend,arm,-.035*sway)
            if carton:
                held=LG.held_carton(x,y,yaw,bend,arm)
                out[carton]=(held[0],held[1]-.04,held[2]+.015,held[3])
            return out
        await self.e.move(at(1),duration=duration,transform=at,exact=True)

    def step_s(self,key,default):
        """Process time of a dock step from config.json (Excel 'Zone 03 Inbound Docking')."""
        return max(0.0,float(self.c.get(key,default)))

    async def pad_to(self,i,t0,total_s,carton=None,carton_b=None,only_b=False):
        """Hold the step until total_s simulated seconds have passed since t0.
        The 3D motions are short animations; this makes each step last its real process time."""
        rem=total_s-(self.e_time()-t0)
        if rem<=1e-6:return
        if only_b:await self.idle_b(i,rem,carton=carton_b)
        else:await self.idle(i,rem,carton=carton,carton_b=carton_b)

    def transfer_waypoints(self,i,target,returning=False):
        """Remain inside: cross AGV lanes at own dock X, then use the internal corridor (-5.35 outbound, -4.15 return)."""
        start=self.poses[f'dock_jack_{i}'];end=target[:2]+(0,0)
        inside_y=-4.15 if returning else -5.35
        pts=[(start[0],inside_y,0,0)]
        if returning:
            pts.extend([(self.docks[i]['x'],inside_y,0,0),end])
        else:
            pts.extend([(end[0],inside_y,0,0),end])
        return pts

    async def pump_carrier(self,i,carrier_model,d):
        """Step to the pallet jack handle and pump 2 strokes to raise the loaded pallet by 0.05m."""
        jack=f'dock_jack_{i}'
        hx,hy=d['x'],d['rear_y']+.62+.78
        wx,wy,wyaw=hx,hy+.28,-math.pi/2
        await self.worker(i,(wx,wy,wyaw))
        for target_z in (.025,.05):
            def down(q,base_z=(target_z-.025)):
                z=base_z+.025*q;bend=.12+.32*q;arm=.40+.32*q
                out=LG.operator_pose(worker_id(i),wx,wy,wyaw,bend,arm)
                out[jack]=(d['x'],d['rear_y']+.62,z,-math.pi/2)
                out[carrier_model]=(d['x'],d['rear_y']+.62,z,0)
                return out
            await self.e.move(down(1),duration=.30,transform=down,exact=True)
            def up(q):
                bend=.44-.30*q;arm=.72-.30*q
                out=LG.operator_pose(worker_id(i),wx,wy,wyaw,bend,arm)
                out[jack]=(d['x'],d['rear_y']+.62,target_z,-math.pi/2)
                out[carrier_model]=(d['x'],d['rear_y']+.62,target_z,0)
                return out
            await self.e.move(up(1),duration=.22,transform=up,exact=True)
        self.states[i]=(wx,wy,wyaw)
        self.poses[jack]=(d['x'],d['rear_y']+.62,.05,-math.pi/2)
        self.poses[carrier_model]=(d['x'],d['rear_y']+.62,.05,0)

    async def _worker_b_solo_preload(self,i,d,next_k,next_kind,count):
        """As soon as Worker A pulls away from stage(d), Worker B brings Handlift 2 + a new empty pallet
        from the stack onto stage(d) and continuously unloads & stacks cartons until Worker A returns!"""
        pstate=self.pipe[i]
        st=stage(d);psx,psy=pallet_stack_xy(d);jbx,jby=jack_b_home_xy(d)
        deck=f'dock_deck_{i}';jack_b=f'dock_jack_b_{i}'
        odd_h=float((self.c.get('odd_pallet_size') or [1.2,1.0,1.7])[2])
        src=source_stack(d,count)
        dst=list(reversed(even_stack_positions(st,count))) if next_kind!='odd' else list(reversed(LG.stack_positions(st,count,total_height=odd_h)))
        local=[f'dock_carton_{i}_{j}' for j in range(count)]
        try:
            # Wait briefly (~1.4s) so Worker A's departing pallet clears stage(d) cleanly
            await self.e.move(LG.operator_pose(worker_b_id(i),*self.states_b[i],.08,.55),duration=1.4)
            if pstate['stop_b']:
                return
            # 1. Worker B walks to Handlift 2 & pulls it into stage(d)
            await self.worker_b(i,(jbx,jby+.65,-math.pi/2))
            def move_jack_b(q):
                u=3*q*q-2*q*q*q
                jx=jbx+(st[0]-jbx)*u;jy=jby+(st[1]-jby)*u
                wy=jy+.92
                sw=.24*math.sin(q*math.pi*4)
                out=LG.operator_pose(worker_b_id(i),jx,wy,-math.pi/2,.08,.65,sw)
                out[jack_b]=(jx,jy,0.,-math.pi/2)
                return out
            jb_dist=math.hypot(st[0]-jbx,st[1]-jby)
            await self.e.move(move_jack_b(1),duration=max(0.95,jb_dist/self._walk_speed()),transform=move_jack_b,exact=True)
            self.states_b[i]=(st[0],st[1]+.92,-math.pi/2)

            # 2. Worker B walks to north side of the vertical blue pallet stack (zero clipping!) and carries a fresh empty pallet onto stage(d)
            pick_b=(psx,psy+.82,-math.pi/2)
            t_st0=self.e_time();stage_s=self.step_s('dock_stage_pallet_s',20.0)
            await self.worker_b(i,pick_b)
            self.emit('DOCK_STAGE_EMPTY_PALLET',dock=d['id'],classification=next_kind,batch=next_k,duration_s=stage_s)
            def lift_b(q):
                u=3*q*q-2*q*q*q
                pz=0.62+0.18*u
                out=LG.operator_pose(worker_b_id(i),*pick_b,.26*(1.-.5*u),.95)
                out[deck]=(psx,psy,pz,0.0)
                return out
            await self.e.move(lift_b(1),duration=0.65,transform=lift_b,exact=True)
            drop_b=(st[0]-.80,st[1],0.0)
            def carry_b(q):
                u=3*q*q-2*q*q*q
                px=psx+(st[0]-psx)*u;py=psy+(st[1]-psy)*u
                wx=pick_b[0]+(drop_b[0]-pick_b[0])*u;wy=pick_b[1]+(drop_b[1]-pick_b[1])*u
                wyaw=-math.pi/2+(math.pi/2)*u
                sw=.22*math.sin(q*math.pi*4)
                out=LG.operator_pose(worker_b_id(i),wx,wy,wyaw,.14,.95,sw)
                out[deck]=(px,py,0.80*(1.-0.85*max(0.,(q-.65)/.35)),0.0)
                return out
            cb_dist=math.hypot(drop_b[0]-pick_b[0],drop_b[1]-pick_b[1])
            await self.e.move(carry_b(1),duration=max(1.10,cb_dist/self._walk_speed()),transform=carry_b,exact=True)
            self.send({deck:(st[0],st[1],0.,0.)})
            self.poses[deck]=st[:2]+(0.,0.)
            self.states_b[i]=drop_b
            await self.pad_to(i,t_st0,stage_s,only_b=True)
            pstate['staged']=True
            pstate['kind']=next_kind

            if next_kind=='even' and not pstate['stop_b']:
                t_sc0=self.e_time();scan_s=self.step_s('dock_pallet_scan_s',10.0)
                self.emit('DOCK_SCAN_PALLET_FACE',dock=d['id'],classification='even',batch=next_k,duration_s=scan_s)
                scan_pt=(st[0]-.25,st[1]+.80,-math.pi/2)
                await self.worker_b(i,scan_pt)
                def sf_b(q):
                    sw=math.sin(q*math.pi*4)
                    return LG.operator_pose(worker_b_id(i),scan_pt[0]+.15*sw,scan_pt[1],scan_pt[2],.28,.92)
                await self.e.move(sf_b(1),duration=0.9,transform=sf_b,exact=True)
                self.states_b[i]=scan_pt
                await self.pad_to(i,t_sc0,scan_s,only_b=True)
                pstate['face_scanned']=True

            # 3. Continuously unload cartons from trailer & stack onto the new pallet while Worker A is away!
            carton_times=self._sample_pallet_carton_times(count,stage_s=stage_s)
            pstate['carton_times']=carton_times
            while pstate['next_j']<count and not pstate['stop_b']:
                t_c0=self.e_time()
                j=pstate['next_j']
                tot_c_s=carton_times[j];unload_s=0.5*tot_c_s;stack_s=tot_c_s-unload_s
                nb,pb,outb=local[j],src[j],dst[j]
                self.send({nb:pb+(0,)})
                pick_pos=(d['x']-.36,d['rear_y']+.26,-math.pi/2)
                await self.worker_b(i,pick_pos)
                self.emit('WORKER_PICK_FROM_TRUCK',dock=d['id'],carton=j,batch=next_k)
                p_cb=pb+(0,)
                def pick_one(q,wx=pick_pos[0],wy=pick_pos[1],wyaw=pick_pos[2],p_cb=p_cb,nb=nb):
                    if q<0.40:
                        u=q/0.40;w=3*u*u-2*u*u*u
                        bend=0.22*w;arm=0.95*w;cb_p=p_cb
                    else:
                        u=(q-0.40)/0.60;w=3*u*u-2*u*u*u
                        bend=0.22-0.07*w;arm=0.95
                        tgt=LG.held_carton(wx,wy,wyaw,bend,arm)
                        cb_p=tuple(p_cb[m]+(tgt[m]-p_cb[m])*w for m in range(4))
                    out_p=LG.operator_pose(worker_b_id(i),wx,wy,wyaw,bend,arm)
                    out_p[nb]=cb_p
                    return out_p
                await self.e.move(pick_one(1),duration=max(.35,float(self.c.get('dock_pick_s',0.5))),transform=pick_one,exact=True)
                await self.pad_to(i,t_c0,unload_s,carton_b=nb,only_b=True)
                # Quick barcode pass (no frozen pause!) -> step outside pallet footprint and place immediately
                pl_b=self._place_stand_pose(d,outb,-1)
                await self.worker_b(i,pl_b,carton=nb)
                bendb,armb=LG.reach_for(outb[2])
                cs_b=LG.held_carton(*self.states_b[i],.12,1.0)
                ct_b=outb+(0,)
                def place_one(q,bendb=bendb,armb=armb,cs_b=cs_b,ct_b=ct_b,nb=nb):
                    u=3*q*q-2*q*q*q
                    bb=.12+(bendb-.12)*u;ab=1.0+(armb-1.0)*u
                    out_p=LG.operator_pose(worker_b_id(i),*self.states_b[i],bb,ab)
                    out_p[nb]=(cs_b[0]+(ct_b[0]-cs_b[0])*u,cs_b[1]+(ct_b[1]-cs_b[1])*u,cs_b[2]+(ct_b[2]-cs_b[2])*u,0)
                    return out_p
                await self.e.move(place_one(1),duration=max(.35,float(self.c.get('dock_place_s',0.5))),transform=place_one,exact=True)
                await self.pad_to(i,t_c0,unload_s+stack_s,only_b=True)
                pstate['next_j']=j+1
        except asyncio.CancelledError:
            raise

    async def transfer(self,i,carrier_model,target,returning=False,without_deck=False,traffic=None,on_departed=None):
        """Smooth Bezier-curved handlift trajectory for Worker A while Worker B works independently at the dock."""
        d=self.docks[i];jack=f'dock_jack_{i}'
        own_traffic=traffic is None
        if own_traffic:
            traffic=self.gate.manual();permission=asyncio.create_task(traffic.__aenter__())
            while not permission.done():
                await self.idle(i,.5,solo_a=True)
            await permission
        try:
            if not returning:
                await self.pump_carrier(i,carrier_model,d)
                if callable(on_departed):
                    on_departed()
                start_xy=self.poses[jack][:2]
                lane_y=-5.35+((i%3)-1)*0.24
                slot_pt=(target[0],target[1],0,0)
                approach_y=min(slot_pt[1]-1.30,lane_y+1.45)
                ctrl_pts=[start_xy,(start_xy[0],lane_y),(slot_pt[0],lane_y),(slot_pt[0],approach_y)]
                samples=_smooth_curve_samples(ctrl_pts,radius=1.15,step=0.22)
                total_len,eval_curve=_arc_table(samples)
                speed=self._walk_speed() if without_deck else self._pallet_speed()
                dur=max(.5,total_len/speed)
                def at_pull(q):
                    px,py,yaw=eval_curve(q)
                    cur_yj=yaw+math.pi;cur_yd=cur_yj+math.pi/2
                    out={jack:(px,py,.05,cur_yj)}
                    if not without_deck and carrier_model:out[carrier_model]=(px,py,.05,cur_yd)
                    wx=px+1.06*math.cos(yaw);wy=py+1.06*math.sin(yaw)
                    swing=.26*math.sin(q*math.pi*max(1,total_len/.62))
                    out.update(LG.operator_pose(worker_id(i),wx,wy,yaw,.06,-.45,swing))
                    return out
                await self.e.move(at_pull(1),duration=dur,transform=at_pull,exact=True)
                end_px,end_py,end_yaw=eval_curve(1.0)
                self.poses[jack]=(end_px,end_py,.05,end_yaw+math.pi)
                if carrier_model:self.poses[carrier_model]=(end_px,end_py,.05,end_yaw+1.5*math.pi)
                self.states[i]=(end_px+1.06*math.cos(end_yaw),end_py+1.06*math.sin(end_yaw),end_yaw)

                a=self.poses[jack];b=slot_pt;length=math.dist(a[:2],b[:2])
                slot_yaw=math.pi/2
                yaw_deck=slot_yaw+math.pi/2
                w_start=self.states[i]
                w_prep=(a[0],a[1]-1.10,slot_yaw)
                j_yaw0=self.poses[jack][3]
                dj_yaw=math.atan2(math.sin(slot_yaw-j_yaw0),math.cos(slot_yaw-j_yaw0))
                def align_push(q):
                    u=3*q*q-2*q*q*q
                    wx=a[0]+0.85*math.sin(math.pi*u);wy=w_start[1]+(w_prep[1]-w_start[1])*u
                    wyaw=math.atan2(a[1]-wy,a[0]-wx)
                    cur_jy=j_yaw0+dj_yaw*u
                    cur_dy=cur_jy+math.pi/2
                    out={jack:(a[0],a[1],.05,cur_jy)}
                    if not without_deck and carrier_model:out[carrier_model]=(a[0],a[1],.05,cur_dy)
                    out.update(LG.operator_pose(worker_id(i),wx,wy,wyaw,.05,.55,.18*math.sin(q*math.pi*2)))
                    return out
                align_dist=math.hypot(0.85,w_prep[1]-w_start[1])
                await self.e.move(align_push(1),duration=max(.55,align_dist/self._walk_speed()),transform=align_push,exact=True)

                push_dur=max(.45,length/self._pallet_speed())
                def at_push(q):
                    px=a[0]+(b[0]-a[0])*q;py=a[1]+(b[1]-a[1])*q
                    out={jack:(px,py,.05,slot_yaw)}
                    if not without_deck and carrier_model:out[carrier_model]=(px,py,.05,yaw_deck)
                    wx=px-1.10*math.cos(slot_yaw);wy=py-1.10*math.sin(slot_yaw)
                    swing=.25*math.sin(q*math.pi*max(1,length/.6))
                    out.update(LG.operator_pose(worker_id(i),wx,wy,slot_yaw,.08,.65,swing))
                    return out
                await self.e.move(at_push(1),duration=push_dur,transform=at_push,exact=True)
                self.poses[jack]=(b[0],b[1],.05,slot_yaw)
                if carrier_model:self.poses[carrier_model]=(b[0],b[1],.05,yaw_deck)
                last_w=(b[0]-1.10*math.cos(slot_yaw),b[1]-1.10*math.sin(slot_yaw),slot_yaw)
                self.states[i]=last_w

                last_jack=self.poses[jack]
                last_deck=self.poses[carrier_model] if carrier_model else (b[0],b[1],.05,yaw_deck)
                def release_valve(q):
                    z=.05*(1.-q)
                    bend=.08+.12*math.sin(q*math.pi);arm=.65+.15*math.sin(q*math.pi)
                    out={jack:(last_jack[0],last_jack[1],z,last_jack[3])}
                    if not without_deck and carrier_model:out[carrier_model]=(last_deck[0],last_deck[1],z,last_deck[3])
                    out.update(LG.operator_pose(worker_id(i),last_w[0],last_w[1],last_w[2],bend,arm))
                    return out
                await self.e.move(release_valve(1),duration=.40,transform=release_valve,exact=True)

                withdraw_y=-4.15+((i%2)-0.5)*0.26
                withdraw_len=abs(withdraw_y-last_jack[1])
                def withdraw(q):
                    cur_jy=last_jack[1]+(withdraw_y-last_jack[1])*q
                    cur_wy=cur_jy-1.10
                    out={jack:(last_jack[0],cur_jy,0.,slot_yaw)}
                    if not without_deck and carrier_model:out[carrier_model]=(last_deck[0],last_deck[1],0.,last_deck[3])
                    swing=.20*math.sin(q*math.pi*max(1,withdraw_len/.6))
                    out.update(LG.operator_pose(worker_id(i),last_jack[0],cur_wy,slot_yaw,.05,-.40,swing))
                    return out
                await self.e.move(withdraw(1),duration=max(.4,withdraw_len/self._walk_speed()),transform=withdraw,exact=True)
                self.poses[jack]=(last_jack[0],withdraw_y,0.,slot_yaw)
                self.states[i]=(last_jack[0],withdraw_y-1.10,slot_yaw)
            else:
                start_xy=self.poses[jack][:2]
                ret_y=-4.15+((i%2)-0.5)*0.26
                # Park returning jack slightly right of stage(d) while Worker B's new pallet occupies stage(d), then slide under
                end_pt=(d['x']+.85,d['rear_y']+1.45)
                ctrl_pts=[start_xy,(start_xy[0],ret_y),(end_pt[0],ret_y),end_pt]
                samples=_smooth_curve_samples(ctrl_pts,radius=1.05,step=0.22)
                total_len,eval_curve=_arc_table(samples)
                speed=self._walk_speed()
                dur=max(.5,total_len/speed)
                def at_ret(q):
                    px,py,yaw=eval_curve(q)
                    cur_yj=yaw
                    out={jack:(px,py,0.,cur_yj)}
                    wx=px-1.10*math.cos(cur_yj);wy=py-1.10*math.sin(cur_yj)
                    swing=.26*math.sin(q*math.pi*max(1,total_len/.62))
                    out.update(LG.operator_pose(worker_id(i),wx,wy,cur_yj,.05,.65,swing))
                    return out
                await self.e.move(at_ret(1),duration=dur,transform=at_ret,exact=True)
                end_px,end_py,end_yaw=eval_curve(1.0)
                self.states[i]=(end_px-1.10*math.cos(end_yaw),end_py-1.10*math.sin(end_yaw),end_yaw)
                self.poses[jack]=(end_px,end_py,0.,end_yaw)
        finally:
            if own_traffic:
                await traffic.__aexit__(None,None,None)

    async def _stage_empty_pallet(self,i,d,k,kind):
        """Visible step: take an empty pallet off the vertical stack (north approach, zero clipping) and place at stage(d)."""
        st=stage(d);psx,psy=pallet_stack_xy(d);deck=f'dock_deck_{i}'
        self.wrapped[i]=False
        t0=self.e_time();step=self.step_s('dock_stage_pallet_s',20.0)
        self.emit('DOCK_STAGE_EMPTY_PALLET',dock=d['id'],classification=kind,batch=k,duration_s=step)
        self.send({deck:(psx,psy,-5.,0.),f'dock_wrap_{i}':(st[0],st[1],-5.,0.),f'dock_roll_{i}':(st[0],st[1],-5.,0.)})
        pick_a=(psx+.32,psy+.84,-math.pi/2)
        pick_b=(psx-.32,psy+.84,-math.pi/2)
        await self.both_workers(i,pick_a,pick_b,dur_override=0.9)
        def lift_pallet(q):
            u=3*q*q-2*q*q*q
            bend=0.28*(1.0-0.5*u);arm=0.95
            pz=0.62+0.20*u
            out=LG.operator_pose(worker_id(i),*pick_a,bend,arm)
            if self._hb(i):out.update(LG.operator_pose(worker_b_id(i),*pick_b,bend,arm))
            out[deck]=(psx,psy,pz,0.0)
            return out
        await self.e.move(lift_pallet(1),duration=0.65,transform=lift_pallet,exact=True)
        drop_a=(st[0]+.80,st[1],math.pi)
        drop_b=(st[0]-.80,st[1],0.0)
        def carry_pallet(q):
            u=3*q*q-2*q*q*q
            px=psx+(st[0]-psx)*u;py=psy+(st[1]-psy)*u
            wxa=pick_a[0]+(drop_a[0]-pick_a[0])*u;wya=pick_a[1]+(drop_a[1]-pick_a[1])*u
            wxb=pick_b[0]+(drop_b[0]-pick_b[0])*u;wyb=pick_b[1]+(drop_b[1]-pick_b[1])*u
            sw=.22*math.sin(q*math.pi*4)
            out=LG.operator_pose(worker_id(i),wxa,wya,-math.pi/2*(1.-u)+math.pi*u,.14,.95,sw)
            if self._hb(i):out.update(LG.operator_pose(worker_b_id(i),wxb,wyb,-math.pi/2*(1.-u),.14,.95,-sw))
            out[deck]=(px,py,0.82,0.0)
            return out
        cp_dist=max(math.hypot(drop_a[0]-pick_a[0],drop_a[1]-pick_a[1]),math.hypot(drop_b[0]-pick_b[0],drop_b[1]-pick_b[1]))
        await self.e.move(carry_pallet(1),duration=max(1.10,cp_dist/self._walk_speed()),transform=carry_pallet,exact=True)
        self.states[i]=drop_a;self.states_b[i]=drop_b
        def lower_pallet(q):
            u=3*q*q-2*q*q*q
            pz=0.82*(1.0-u)
            bend=0.14+0.24*math.sin(q*math.pi);arm=0.95*(1.0-0.6*u)
            out=LG.operator_pose(worker_id(i),*drop_a,bend,arm)
            if self._hb(i):out.update(LG.operator_pose(worker_b_id(i),*drop_b,bend,arm))
            out[deck]=(st[0],st[1],pz,0.0)
            return out
        await self.e.move(lower_pallet(1),duration=0.55,transform=lower_pallet,exact=True)
        self.poses[deck]=st[:2]+(0.,0.)
        await self.pad_to(i,t0,step)

    async def _scan_pallet_face(self,i,d,k):
        """Visible step (even pallets): scan the empty pallet ID barcode & register manifest."""
        st=stage(d)
        t0=self.e_time();step=self.step_s('dock_pallet_scan_s',10.0)
        self.emit('DOCK_SCAN_PALLET_FACE',dock=d['id'],classification='even',batch=k,duration_s=step)
        pos_a=(st[0]+.25,st[1]+.80,-math.pi/2)
        pos_b=(d['x']+.92,d['rear_y']+.80,-math.pi/2)
        await self.both_workers(i,pos_a,pos_b,dur_override=0.6)
        def scan_face_anim(q):
            sw=math.sin(q*math.pi*4)
            out=LG.operator_pose(worker_id(i),pos_a[0]+.18*sw,pos_a[1],pos_a[2],.32+.05*sw,.95+.06*sw)
            if self._hb(i):
                out.update(LG.operator_pose(worker_b_id(i),pos_b[0],pos_b[1],pos_b[2],.14,.78+.08*math.cos(q*math.pi*6)))
            return out
        await self.e.move(scan_face_anim(1),duration=1.2,transform=scan_face_anim,exact=True)
        await self.pad_to(i,t0,step)

    async def _scan_sku_and_input(self,i,d,k):
        """Visible step (even pallets): scan SKU barcodes on stacked cartons and input quantities on terminal."""
        st=stage(d)
        t0=self.e_time();step=self.step_s('dock_sku_entry_s',20.0)
        self.emit('DOCK_SCAN_SKU_ENTRY',dock=d['id'],classification='even',batch=k,duration_s=step)
        scan_pts=[
            (st[0]-.82,st[1],0.0),
            (st[0],st[1]+.80,-math.pi/2),
            (st[0]+.82,st[1],math.pi),
        ]
        term_b=(d['x']+.92,d['rear_y']+.80,-math.pi/2)
        for pt_a in scan_pts:
            await self.both_workers(i,pt_a,term_b,dur_override=0.55)
            def sku_anim(q,pa=pt_a):
                sw=math.sin(q*math.pi*4)
                out=LG.operator_pose(worker_id(i),pa[0],pa[1],pa[2],.10+.05*sw,1.08+.12*sw)
                if self._hb(i):
                    out.update(LG.operator_pose(worker_b_id(i),term_b[0],term_b[1],term_b[2],.16,.80+.10*math.sin(q*math.pi*8)))
                return out
            await self.e.move(sku_anim(1),duration=0.75,transform=sku_anim,exact=True)
        await self.pad_to(i,t0,step)

    async def _wrap_even_pallet(self,i,d,k):
        """Visible step: Worker A carries a stretch-film roll around the full even pallet for 3 smooth laps while wrap rises."""
        st=stage(d);wrap=f'dock_wrap_{i}';roll=f'dock_roll_{i}'
        t0=self.e_time();step=self.step_s('dock_wrap_s',30.0)
        self.emit('DOCK_WRAP_START',dock=d['id'],classification='even',batch=k,duration_s=step)
        start_a=(st[0]+.94,st[1],math.pi)
        hold_b=(st[0]-.86,st[1]+.35,-0.35)
        await self.both_workers(i,start_a,hold_b,dur_override=0.6)
        orbit_s=max(4.2,step-(self.e_time()-t0))   # the wrapping laps fill the whole manual-wrap time
        laps=3
        rx,ry=0.94,0.84
        def wrap_orbit(q):
            ang=q*laps*2.0*math.pi
            wx=st[0]+rx*math.cos(ang);wy=st[1]+ry*math.sin(ang)
            inward=math.atan2(st[1]-wy,st[0]-wx)
            bend=0.22-0.14*q
            arm=0.88+0.22*q
            sw=.28*math.sin(q*laps*math.pi*8)
            out=LG.operator_pose(worker_id(i),wx,wy,inward,bend,arm,sw)
            rz=0.35+0.80*q
            rx_h=wx+0.36*math.cos(inward);ry_h=wy+0.36*math.sin(inward)
            out[roll]=(rx_h,ry_h,rz,inward)
            wz=-0.62+0.62*min(1.0,q*1.12)
            out[wrap]=(st[0],st[1],wz,0.0)
            if self._hb(i):
                bsw=0.05*math.sin(q*math.pi*10)
                out.update(LG.operator_pose(worker_b_id(i),hold_b[0],hold_b[1],hold_b[2],.12+bsw,.85-bsw,0.0))
            return out
        await self.e.move(wrap_orbit(1),duration=orbit_s,transform=wrap_orbit,exact=True)
        self.send({roll:(st[0],st[1],-5.,0.),wrap:(st[0],st[1],0.,0.)})
        self.wrapped[i]=True
        self.states[i]=(st[0]+.94,st[1],math.pi)
        self.emit('DOCK_WRAP_COMPLETE',dock=d['id'],classification='even',batch=k)

    def _place_stand_pose(self,d,out_xyz,side_sign):
        """Stand strictly outside the 1.2m x 1.0m pallet footprint (>= 0.80m from pallet center X) so legs never clip cartons."""
        if side_sign>0:
            return (d['x']+0.82,out_xyz[1],math.pi)
        return (d['x']-0.82,out_xyz[1],0.0)

    async def _unload_at_dock(self,i,d,kind,k,count,emit_classified=True):
        pstate=self.pipe.setdefault(i,{'staged':False,'face_scanned':False,'next_j':0,'kind':None,'stop_b':False})
        bg=self.bg_tasks.pop(i,None)
        if bg is not None:
            pstate['stop_b']=True
            try:
                await bg
            except BaseException:
                pass
        # Shared crew, at a pallet boundary (helper's pre-staging finished): a free worker joins this truck,
        # or the helper leaves because a newly docked truck is waiting for a worker.
        await self.rebalance_helper(i)
        local=[f'dock_carton_{i}_{j}' for j in range(count)]
        odd_h=float((self.c.get('odd_pallet_size') or [1.2,1.0,1.7])[2])
        src=source_stack(d,count)
        dst=list(reversed(even_stack_positions(stage(d),count))) if kind!='odd' else list(reversed(LG.stack_positions(stage(d),count,total_height=odd_h)))
        self.emit('TRUCK_BATCH_READY',dock=d['id'],classification=kind,batch=k,cartons=count)

        stage_s=self.step_s('dock_stage_pallet_s',20.0)
        saved_times=pstate.get('carton_times')
        start_j=0
        if pstate.get('staged'):
            # Worker B already staged the new pallet & stacked 0..next_j-1 while Worker A was away!
            start_j=int(pstate.get('next_j',0))
            # Swap Handlift 1 cleanly back under stage(d) and park Handlift 2 back at its side spot
            jbx,jby=jack_b_home_xy(d);st=stage(d)
            self.poses[f'dock_jack_{i}']=st[:2]+(0.,-math.pi/2)
            self.send({f'dock_jack_{i}':st[:2]+(0.,-math.pi/2),f'dock_jack_b_{i}':(jbx,jby,0.,-math.pi/2)})
            if kind=='even' and not pstate.get('face_scanned'):
                await self._scan_pallet_face(i,d,k)
        else:
            if k==0 and i>0:
                await self.idle(i,min(3.5,i*0.32))
            await self._stage_empty_pallet(i,d,k,kind)
            if kind=='even':
                await self._scan_pallet_face(i,d,k)

        carton_times=(saved_times if (start_j>0 and saved_times and len(saved_times)==count)
                      else self._sample_pallet_carton_times(count,stage_s=stage_s))
        pstate.update({'staged':False,'face_scanned':False,'next_j':0,'kind':None,'stop_b':False,'carton_times':None})

        # Option A relay: 1 carton per 5-10s cycle (unload from trailer + stack onto pallet) so every ~30-carton
        # pallet fills in 3.0 - 5.0 minutes. With 2 workers, Worker B unloads at the trailer rear & hands to Worker A
        # who stacks onto the pallet; with 1 worker, Worker A performs both phases sequentially.
        for j in range(start_j,count):
            t_c0=self.e_time()
            tot_c_s=carton_times[j];unload_s=0.5*tot_c_s;stack_s=tot_c_s-unload_s
            na,pa,outa=local[j],src[j],dst[j]
            self.send({na:pa+(0,)})
            pick_dur=max(.30,float(self.c.get('dock_pick_s',0.45)))
            place_dur=max(.30,float(self.c.get('dock_place_s',0.45)))
            p_ca=pa+(0,)
            has_relay=self._hb(i)
            if has_relay:
                pick_pos_b=(d['x']-.32,d['rear_y']+.26,-math.pi/2)
                handoff_a=(d['x']+.38,d['rear_y']+.44,-2.35)
                await self.both_workers(i,handoff_a,pick_pos_b)
                self.emit('WORKER_PICK_FROM_TRUCK',dock=d['id'],carton=j,batch=k)
                def pick_relay_anim(q,wxa=handoff_a[0],wya=handoff_a[1],wyawa=handoff_a[2],
                                    wxb=pick_pos_b[0],wyb=pick_pos_b[1],wyawb=pick_pos_b[2],p_ca=p_ca,na=na):
                    if q<0.45:
                        u=q/0.45;w=3*u*u-2*u*u*u
                        bb=0.24*w;ab=0.95*w;byaw=wyawb
                        ca_pose=p_ca
                    else:
                        u=(q-0.45)/0.55;w=3*u*u-2*u*u*u
                        bb=0.24-0.10*w;ab=0.95
                        byaw=wyawb+(0.95)*w
                        cb_tgt=LG.held_carton(wxb,wyb,byaw,bb,ab)
                        ca_pose=tuple(p_ca[m]+(cb_tgt[m]-p_ca[m])*w for m in range(4))
                    out_p=LG.operator_pose(worker_id(i),wxa,wya,wyawa,.08,.75+.15*q)
                    out_p.update(LG.operator_pose(worker_b_id(i),wxb,wyb,byaw,bb,ab))
                    out_p[na]=ca_pose
                    return out_p
                await self.e.move(pick_relay_anim(1),duration=pick_dur,transform=pick_relay_anim,exact=True)
                self.states_b[i]=(pick_pos_b[0],pick_pos_b[1],pick_pos_b[2]+0.95)
                await self.pad_to(i,t_c0,unload_s,carton_b=na)
            else:
                pick_pos_a=(d['x']+.32,d['rear_y']+.26,-math.pi/2)
                await self.worker(i,pick_pos_a)
                self.emit('WORKER_PICK_FROM_TRUCK',dock=d['id'],carton=j,batch=k)
                def pick_solo_anim(q,wxa=pick_pos_a[0],wya=pick_pos_a[1],wyawa=pick_pos_a[2],p_ca=p_ca,na=na):
                    if q<0.40:
                        u=q/0.40;w=3*u*u-2*u*u*u
                        bend=0.22*w;arm=0.95*w;ca_pose=p_ca
                    else:
                        u=(q-0.40)/0.60;w=3*u*u-2*u*u*u
                        bend=0.22-0.07*w;arm=0.95
                        ca_tgt=LG.held_carton(wxa,wya,wyawa,bend,arm)
                        ca_pose=tuple(p_ca[m]+(ca_tgt[m]-p_ca[m])*w for m in range(4))
                    out_p=LG.operator_pose(worker_id(i),wxa,wya,wyawa,bend,arm)
                    out_p[na]=ca_pose
                    return out_p
                await self.e.move(pick_solo_anim(1),duration=pick_dur,transform=pick_solo_anim,exact=True)
                await self.pad_to(i,t_c0,unload_s,carton=na)

            if emit_classified:
                self.stats['scanned_'+kind]+=1
                self.emit('BARCODE_CLASSIFIED',barcode=f'{kind.upper()}-{k:04d}-{j:03d}',classification=kind,dock=d['id'])

            pl_a=self._place_stand_pose(d,outa,+1)
            ready_b=(d['x']-.36,d['rear_y']+.30,-math.pi/2)
            if has_relay:
                await self.both_workers(i,pl_a,ready_b,carton_a=na)
            else:
                await self.worker(i,pl_a,carton=na)
            benda,arma=LG.reach_for(outa[2])
            cs_a=LG.held_carton(*self.states[i],.12,1.0)
            ct_a=outa+(0,)
            def place_one_anim(q,benda=benda,arma=arma,cs_a=cs_a,ct_a=ct_a,na=na,has_relay=has_relay):
                u=3*q*q-2*q*q*q
                ba=.12+(benda-.12)*u;aa=1.0+(arma-1.0)*u
                out_p=LG.operator_pose(worker_id(i),*self.states[i],ba,aa)
                out_p[na]=(cs_a[0]+(ct_a[0]-cs_a[0])*u,cs_a[1]+(ct_a[1]-cs_a[1])*u,cs_a[2]+(ct_a[2]-cs_a[2])*u,0)
                if has_relay:
                    out_p.update(LG.operator_pose(worker_b_id(i),*self.states_b[i],.06,.45))
                return out_p
            await self.e.move(place_one_anim(1),duration=place_dur,transform=place_one_anim,exact=True)
            def straighten_one(q,benda=benda,arma=arma,has_relay=has_relay):
                u=3*q*q-2*q*q*q
                out_p=LG.operator_pose(worker_id(i),*self.states[i],benda*(1-u),arma*(1-u))
                if has_relay:
                    out_p.update(LG.operator_pose(worker_b_id(i),*self.states_b[i],.04,.35))
                return out_p
            await self.e.move(straighten_one(1),duration=0.22,transform=straighten_one,exact=True)
            await self.pad_to(i,t_c0,unload_s+stack_s)
        return local

    async def _deliver_staged_pallet(self,i,d,kind,k,target,local,consume,acquire_target=None,staged_cartons=0,next_kind=None,next_count=None):
        if kind=='even':
            await self._scan_sku_and_input(i,d,k)
            await self._wrap_even_pallet(i,d,k)
        extra={'cartons':staged_cartons} if staged_cartons else {}
        self.emit('DOCK_PALLET_READY',dock=d['id'],classification=kind,batch=k,**extra)
        span=(d['x']-1.5,d['x']+1.5)
        t_gw0=self.e_time()
        traffic=self.gate.manual(span);permission=asyncio.create_task(traffic.__aenter__())
        while not permission.done():
            await self.idle(i,.5)
        await permission
        t_gw1=self.e_time()
        gw_s=max(0.0,t_gw1-t_gw0)
        if gw_s>1e-4:
            self.emit('DOCK_GATE_WAIT',dock=d['id'],classification=kind,batch=k,wait_s=round(gw_s,4),start_s=round(t_gw0,4),end_s=round(t_gw1,4))

        st=stage(d)
        carrier=f'dock_loaded_even_{i}' if kind=='even' else f'dock_loaded_odd_{i}'

        def _swap_to_carrier():
            self.poses[carrier]=(st[0],st[1],0.0,0.0)
            self.send({
                carrier:(st[0],st[1],0.0,0.0),
                f'dock_deck_{i}':(st[0],st[1],-5.0,0.0),
                f'dock_wrap_{i}':(st[0],st[1],-5.0,0.0),
                **{n:(st[0],st[1],-5.0,0.0) for n in local},
            })

        def _start_worker_b():
            if self._hb(i) and next_kind is not None:
                pstate=self.pipe.setdefault(i,{})
                pstate.update({'staged':False,'face_scanned':False,'next_j':0,'kind':next_kind,'stop_b':False})
                self.bg_tasks[i]=asyncio.create_task(self._worker_b_solo_preload(i,d,k+1,next_kind,next_count or len(local)))

        try:
            if acquire_target is not None:
                t_tw0=self.e_time()
                async with acquire_target:
                    t_tw1=self.e_time()
                    if t_tw1-t_tw0>1e-4:
                        self.emit('DOCK_SLOT_WAIT',dock=d['id'],classification=kind,batch=k,wait_s=round(t_tw1-t_tw0,4),start_s=round(t_tw0,4),end_s=round(t_tw1,4))
                    _swap_to_carrier()
                    await self.transfer(i,carrier,target,traffic=traffic,on_departed=_start_worker_b)
                    self.send({carrier:(st[0],st[1],-5.0,0.0)})
                    await consume(i,[])
                    self.stats['pallets_to_w2']+=1
                    await self.transfer(i,None,stage(d),True,without_deck=True,traffic=traffic)
                    self._reset_stage_deck(i,len(local),keep_preload=(next_kind is not None))
                    self.stats['empty_pallet_returns']+=1
                    self.emit('EMPTY_PALLET_RETURNED',dock=d['id'],batch=k,classification=kind)
            else:
                _swap_to_carrier()
                await self.transfer(i,carrier,target,traffic=traffic,on_departed=_start_worker_b)
                self.send({carrier:(st[0],st[1],-5.0,0.0)})
                await consume(i,[])
                self.stats['pallets_to_w1']+=1
                await self.transfer(i,None,stage(d),True,without_deck=True,traffic=traffic)
                self._reset_stage_deck(i,len(local),keep_preload=(next_kind is not None))
                self.emit('EMPTY_PALLET_FROM_POOL',dock=d['id'],batch=k)
        finally:
            await traffic.__aexit__(None,None,None)

    async def prepare(self,kind,k,target,names,consume,acquire_target=None):
        i=k%len(self.docks);d=self.docks[i]
        async with self.locks[i]:
            local=await self._unload_at_dock(i,d,kind,k,len(names),emit_classified=True)
            await self._deliver_staged_pallet(i,d,kind,k,target,local,consume,acquire_target=acquire_target,next_kind=None)

    async def prestage_and_deliver(self,i,k,count,claim_slot_fn,desired_kind='odd',next_desired_kind='odd',next_count=None,on_unloaded=None):
        """Pipelined 2-worker dock cycle: Worker A & B finish current pallet together, then while Worker A delivers
        to the buffer zone, Worker B immediately pulls a fresh pallet onto stage(d) and stacks the next batch!
        next_desired_kind=None: no next pallet on this truck, so Worker B does not pre-stage one.
        on_unloaded(): called once every carton of this pallet is off the truck and stacked."""
        d=self.docks[i]
        async with self.locks[i]:
            local=await self._unload_at_dock(i,d,desired_kind,k,count,emit_classified=False)
            if on_unloaded is not None:on_unloaded()
            t_sw0=self.e_time()
            claimed=await claim_slot_fn(i,d['id'],k,count)
            t_sw1=self.e_time()
            if t_sw1-t_sw0>1e-4:
                self.emit('DOCK_SLOT_WAIT',dock=d['id'],classification='staged',batch=k,wait_s=round(t_sw1-t_sw0,4),start_s=round(t_sw0,4),end_s=round(t_sw1,4))
            if claimed is None:
                self._reset_stage_deck(i,count,keep_preload=False)
                return
            kind,target,acquire_target,consume=claimed
            self.stats['scanned_'+kind]+=count
            for j in range(count):
                self.emit('BARCODE_CLASSIFIED',barcode=f'{kind.upper()}-{k:04d}-{j:03d}',classification=kind,dock=d['id'])
            await self._deliver_staged_pallet(i,d,kind,k,target,local,consume,acquire_target=acquire_target,staged_cartons=count,next_kind=next_desired_kind,next_count=next_count)

    def stop_all_bg(self):
        for pstate in self.pipe.values():
            pstate['stop_b']=True
        for t in list(self.bg_tasks.values()):
            if t is not None and not t.done():
                t.cancel()
        self.bg_tasks.clear()

    def e_time(self):
        return self.clock()
