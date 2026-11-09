"""3D geometry generated from the same CAD map used by navigation."""
import math
from cad_navigation import LAYOUT
from stock_layout import stock_positions
from rail_geometry import RAIL_TOP_OFFSET,RAIL_HEIGHT

def build_cad_scene(C,box,strut,merge_boxes,model):
    blue='.055 .20 .43 1';steel='.62 .66 .69 1';red='.68 .075 .045 1'
    frame=[];rail=[];grating=[];goods=[]
    for i,r in enumerate(LAYOUT.barriers):
        x=(r[0]+r[2])/2;y0,y1=r[1],r[3]
        n=max(1,math.ceil((y1-y0)/2.2))
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
        length=math.dist(aa,bb)+(.98 if kind in ['slot_entry','storage_channel'] else .02)
        for lev,z in enumerate(C['storage_levels']):
            for side in [-1,1]:
                x=(aa[0]+bb[0])/2+(0 if horizontal else side*.48)
                y=(aa[1]+bb[1])/2+(side*.49 if horizontal else 0)
                rail.append(box(f'rail{i}_{lev}_{side}',x,y,z+RAIL_TOP_OFFSET-RAIL_HEIGHT/2,length if horizontal else .085,.085 if horizontal else length,RAIL_HEIGHT,red if kind in ['main_lane','shared_grating'] else steel))
    for lev,z in enumerate(C['storage_levels']):
        for i,r in enumerate(LAYOUT.data['walkways']):
            x0=max(0,r[0]);x1=min(LAYOUT.data['size'][0],r[2]);y0,y1=r[1],r[3]
            for k in range(max(1,int((x1-x0)/.20))+1):
                x=x0+k*.20
                grating.append(box(f'gridcross{lev}_{i}_{k}',x,(y0+y1)/2,z-.34,.022,y1-y0,.045,steel))
            for k in range(8):grating.append(box(f'gridlong{lev}_{i}_{k}',(x0+x1)/2,y0+k*(y1-y0)/7,z-.34,x1-x0,.022,.045,steel))
        for s in LAYOUT.slots.values():
            for dy in [-.4,.4]:frame.append(box(f'ledge_{s["id"]}_{lev}_{dy}',s['x'],s['y']+dy,z-.015,1.28,.07,.03,steel))
    model('cad_rack_structure',(0,0,0),merge_boxes('cad_frame',frame))
    model('cad_running_rails',(0,0,0),merge_boxes('cad_rails',rail))
    model('shared_maintenance_grating',(0,0,0),merge_boxes('cad_grating',grating))
    stock=stock_positions(C['storage_levels'])
    for i,(x,y,z) in enumerate(stock):
        for dx in [-.51,0,.51]:
            for dy in [-.4,.4]:goods.append(box(f'feet{i}_{dx}_{dy}',x+dx,y+dy,z+.055,.16,.12,.11,'.025 .22 .65 1'))
        goods.append(box(f'pallet{i}',x,y,z+.13,1.2,1,.04,'.025 .22 .65 1'))
        for a in range(3):
            for b in range(2):
                for k in range(3):goods.append(box(f'carton{i}_{a}_{b}_{k}',x+(a-1)*.39,y+(b-.5)*.49,z+.15+(k+.5)*1.55/3,.38,.48,1.55/3-.008,'.82 .70 .43 1'))
    model('illustrative_stock',(0,0,0),merge_boxes('cad_stock',goods))
    return len(stock)
