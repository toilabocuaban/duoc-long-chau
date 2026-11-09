"""CAD-derived constrained lane graph; no free-grid cross-rack shortcuts."""
import json,math,heapq
from pathlib import Path

def hit(a,b):return a[0]<b[2] and a[2]>b[0] and a[1]<b[3] and a[3]>b[1]
def key(p):return (round(p[0],5),round(p[1],5))

class DesignLayout:
    HX=.66
    HY=.56
    MARGIN=.01
    def __init__(self):
        self.data=json.loads((Path(__file__).parent/'design_layout.json').read_text())
        self.slots={s['id']:s for s in self.data['slots']}
        self.barriers=self.data['rack_barriers']
        self.columns=[c['rect'] for c in self.data['columns']]
        self.graph={};self.edges=[]
        xs=sorted(set(self.data['lane_x']+self.data['trunk_x']+[p['buffer'][0] for p in self.data['ports']['out']]))
        ys=self.data['road_y']
        for y in ys:
            for x in xs:self.graph.setdefault(key((x,y)),{})
            for a,b in zip(xs,xs[1:]):self.add((a,y),(b,y),'shared_grating')
        for x in self.data['trunk_x']:
            for a,b in zip(ys,ys[1:]):self.add((x,a),(x,b),'main_lane')
        self.front_slots=[]
        for s in self.slots.values():
            road=min(ys,key=lambda y:abs(y-s['y']))
            if abs(road-s['y'])<=1.65 and self.add((s['x'],road),(s['x'],s['y']),'slot_entry'):
                self.front_slots.append(s['id'])
        for kind in ['in','out']:
            for p in self.data['ports'][kind]:
                x,y=p['buffer'];road=min(ys,key=lambda yy:abs(yy-y))
                if not self.add((x,road),(x,y),'port_buffer'):
                    raise ValueError(f'CAD buffer is not reachable: {kind} {p}')
                if kind=='in' and not self.add(p['buffer'],p['lift'],'lift_entry'):
                    raise ValueError('Input lift entry crosses a CAD barrier')
        preferred=sorted(self.front_slots,key=lambda sid:abs(self.slots[sid]['y']-ys[0])+abs(self.slots[sid]['x']-5))
        self.parking=preferred[:2]
        available=[sid for sid in sorted(self.front_slots,key=lambda sid:(self.slots[sid]['y'],self.slots[sid]['x'])) if sid not in self.parking]
        self.job_slots=[available[(j*37)%len(available)] for j in range(18)]
        self.reserved=set(self.job_slots+self.parking)

    def clear(self,a,b):
        if abs(a[0]-b[0])>1e-4 and abs(a[1]-b[1])>1e-4:return False
        r=[min(a[0],b[0])-self.HX-self.MARGIN,min(a[1],b[1])-self.HY-self.MARGIN,
           max(a[0],b[0])+self.HX+self.MARGIN,max(a[1],b[1])+self.HY+self.MARGIN]
        return not any(hit(r,ob) for ob in self.barriers+self.columns)

    def add(self,a,b,kind):
        a,b=key(a),key(b)
        if a==b:return True
        if not self.clear(a,b):return False
        length=math.dist(a,b)
        self.graph.setdefault(a,{})[b]=length;self.graph.setdefault(b,{})[a]=length
        self.edges.append((a,b,kind))
        return True

    def slot_point(self,sid):
        s=self.slots[sid];return key((s['x'],s['y']))

    def path(self,start,end):
        start,end=key(start),key(end)
        if start not in self.graph or end not in self.graph:raise ValueError(f'Point outside CAD graph: {start} -> {end}')
        queue=[(0,start)];cost={start:0};previous={}
        while queue:
            d,u=heapq.heappop(queue)
            if u==end:break
            if d!=cost[u]:continue
            for v,w in self.graph[u].items():
                nd=d+w
                if nd<cost.get(v,math.inf):cost[v]=nd;previous[v]=u;heapq.heappush(queue,(nd,v))
        if end not in cost:raise ValueError(f'No CAD-permitted route: {start} -> {end}')
        out=[end]
        while out[-1]!=start:out.append(previous[out[-1]])
        out.reverse()
        # Keep turns, combine collinear edges only after graph validation.
        simple=[]
        for p in out:
            if len(simple)>=2 and ((abs(simple[-2][0]-simple[-1][0])<1e-5 and abs(p[0]-simple[-1][0])<1e-5) or
                                   (abs(simple[-2][1]-simple[-1][1])<1e-5 and abs(p[1]-simple[-1][1])<1e-5)):
                simple[-1]=p
            else:simple.append(p)
        return simple

    def supported(self,x,y,axis):
        for a,b,_ in self.edges:
            if axis=='x' and abs(a[1]-b[1])<1e-5 and abs(y-a[1])<1e-4 and min(a[0],b[0])-1e-4<=x<=max(a[0],b[0])+1e-4:return True
            if axis=='y' and abs(a[0]-b[0])<1e-5 and abs(x-a[0])<1e-4 and min(a[1],b[1])-1e-4<=y<=max(a[1],b[1])+1e-4:return True
        return False

LAYOUT=DesignLayout()

if __name__=='__main__':
    print('Slots:',len(LAYOUT.slots),'Front-access slots:',len(LAYOUT.front_slots),'Graph nodes:',len(LAYOUT.graph),'Edges:',len(LAYOUT.edges))
    for j,sid in enumerate(LAYOUT.job_slots):
        p=LAYOUT.data['ports']['in'][j%2];out=LAYOUT.data['ports']['out'][j%2]
        route=LAYOUT.path(p['buffer'],LAYOUT.slot_point(sid))+LAYOUT.path(LAYOUT.slot_point(sid),out['buffer'])
        print(j,sid,len(route),LAYOUT.slot_point(sid))
