"""3D geometry generated from the same CAD map used by navigation."""
import math
from cad_navigation import LAYOUT
from stock_layout import stock_positions
from rail_geometry import RAIL_TOP_OFFSET,RAIL_HEIGHT

def _merge_intervals(intervals,gap=0.05):
    """Merge sorted 1D intervals whose gap does not exceed `gap`."""
    if not intervals:return []
    ordered=sorted(intervals)
    merged=[list(ordered[0])]
    for start,end in ordered[1:]:
        if start<=merged[-1][1]+gap:
            merged[-1][1]=max(merged[-1][1],end)
        else:
            merged.append([start,end])
    return merged

def build_cad_scene(C,box,strut,merge_boxes,model):
    blue='.055 .20 .43 1';steel='.62 .66 .69 1';red='.68 .075 .045 1'
    frame=[];rail=[];grating=[];goods=[]
    for i,r in enumerate(LAYOUT.barriers):
        x=(r[0]+r[2])/2;y0,y1=r[1],r[3]
        n=max(1,math.ceil((y1-y0)/4.4))
        for k in range(n+1):
            y=y0+(y1-y0)*k/n
            frame.extend([box(f'post{i}_{k}',x,y,9,.10,.08,18,blue),box(f'foot{i}_{k}',x,y,.035,.22,.22,.07,steel)])
        for lev,z in enumerate(C['storage_levels']):
            frame.append(box(f'longitudinal{i}_{lev}',x,(y0+y1)/2,z-.38,.10,y1-y0,.16,blue))
            for k in range(n):
                ya=y0+(y1-y0)*k/n;yb=y0+(y1-y0)*(k+1)/n
                if yb-ya<.25:continue
                frame.append(strut(f'brace{i}_{lev}_{k}',(x,ya,z+.06),(x,yb,min(z+1.68,17.95)),.04,steel))
    for i,c in enumerate(LAYOUT.data['columns']):
        r=c['rect'];x=(r[0]+r[2])/2;y=(r[1]+r[3])/2
        frame.extend([box(f'factory_column_{i}',x,y,10,r[2]-r[0],r[3]-r[1],20,'.43 .45 .46 1'),
                      box(f'column_base_{i}',x,y,.09,r[2]-r[0]+.16,r[3]-r[1]+.16,.18,'.32 .34 .35 1')])
    # Add rails under deep storage channels as physical geometry. The first
    # demonstration allocates front slots, avoiding travel through stored loads.
    physical=list(LAYOUT.edges)
    for x in LAYOUT.data['lane_x']:
        points=sorted({(x,s['y']) for s in LAYOUT.slots.values() if abs(s['x']-x)<.001}|
                      {(x,y) for y in LAYOUT.data['road_y']},key=lambda p:p[1])
        for a,b in zip(points,points[1:]):
            if b[1]-a[1]<=2.7 and LAYOUT.clear(a,b):physical.append((a,b,'storage_channel'))
    seen=set()
    rail_groups={}
    for i,(a,b,kind) in enumerate(physical):
        signature=tuple(sorted((a,b)))
        if signature in seen:continue
        seen.add(signature)
        aa,bb=list(a),list(b)
        # Fixed rail stops at the input cabin boundary, leaving the lift shaft free.
        if kind=='lift_entry':
            bb[1]=max(bb[1],aa[1])
            port=min(LAYOUT.data['ports']['in'],key=lambda p:abs(p['lift'][0]-aa[0]))
            aa=[aa[0],port['lift'][1]+.9];bb=[bb[0],port['buffer'][1]]
        horizontal=abs(aa[1]-bb[1])<1e-5
        pad=.98 if kind in ['slot_entry','storage_channel'] else .02
        fixed=round((aa[1]+bb[1])/2 if horizontal else (aa[0]+bb[0])/2,4)
        s0,s1=(min(aa[0],bb[0])-pad/2,max(aa[0],bb[0])+pad/2) if horizontal else (min(aa[1],bb[1])-pad/2,max(aa[1],bb[1])+pad/2)
        color=red if kind in ['main_lane','shared_grating'] else steel
        rail_groups.setdefault((horizontal,fixed,color),[]).append((s0,s1))
    rail_idx=0
    for (horizontal,fixed,color),spans in rail_groups.items():
        for s0,s1 in _merge_intervals(spans,gap=0.05):
            center=(s0+s1)/2;length=s1-s0
            for lev,z in enumerate(C['storage_levels']):
                for side in [-1,1]:
                    x=center if horizontal else fixed+side*.48
                    y=fixed+side*.49 if horizontal else center
                    rail.append(box(f'rail{rail_idx}_{lev}_{side}',x,y,z+RAIL_TOP_OFFSET-RAIL_HEIGHT/2,length if horizontal else .085,.085 if horizontal else length,RAIL_HEIGHT,color))
            rail_idx+=1
    ledge_groups={}
    for s in LAYOUT.slots.values():
        for dy in [-.4,.4]:
            ledge_groups.setdefault(round(s['y']+dy,3),[]).append((s['x']-.64,s['x']+.64))
    merged_ledges=[(ly,(x0+x1)/2,x1-x0) for ly,spans in ledge_groups.items() for x0,x1 in _merge_intervals(spans,gap=0.35)]
    for lev,z in enumerate(C['storage_levels']):
        for i,r in enumerate(LAYOUT.data['walkways']):
            x0=max(0,r[0]);x1=min(LAYOUT.data['size'][0],r[2]);y0,y1=r[1],r[3]
            for k in range(max(1,int((x1-x0)/.80))+1):
                x=x0+k*.80
                grating.append(box(f'gridcross{lev}_{i}_{k}',x,(y0+y1)/2,z-.34,.022,y1-y0,.045,steel))
            for k in range(4):grating.append(box(f'gridlong{lev}_{i}_{k}',(x0+x1)/2,y0+k*(y1-y0)/3,z-.34,x1-x0,.022,.045,steel))
        for k,(ly,lx,llen) in enumerate(merged_ledges):
            frame.append(box(f'ledge_{k}_{lev}',lx,ly,z-.015,llen,.07,.03,steel))
    model('cad_rack_structure',(0,0,0),merge_boxes('cad_frame',frame))
    model('cad_running_rails',(0,0,0),merge_boxes('cad_rails',rail))
    model('shared_maintenance_grating',(0,0,0),merge_boxes('cad_grating',grating))
    thresh=C.get('stock_threshold',1)
    stock=stock_positions(C['storage_levels'],threshold=thresh)
    for i,(x,y,z) in enumerate(stock):
        goods.append(box(f'pallet{i}',x,y,z+.075,1.2,1.0,.15,'.025 .22 .65 1'))
        goods.append(box(f'load{i}',x,y,z+.925,1.16,.97,1.54,'.82 .70 .43 1'))
    model('illustrative_stock',(0,0,0),merge_boxes('cad_stock',goods))
    return len(stock)
