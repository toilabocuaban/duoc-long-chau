"""CAD-anchored trucks, local origin at rear sill; +X points toward cab.

Appearance is a procedural approximation of the user's red tractor/trailer photo,
not a manufacturer model. Road is lowered by deck height so cargo meets floor 0.
"""
import json
import math
from pathlib import Path
from sdf_parts import box, cylinder, strut, merge_boxes

ROOT=Path(__file__).resolve().parent
CAD=json.loads((ROOT/'dock_layout.json').read_text(encoding='utf-8'))
DECK=1.15
RED='.72 .025 .035 1'; BLACK='.025 .03 .035 1'; STEEL='.48 .53 .57 1'
GLASS='.025 .09 .14 1'; GREEN='.12 .40 .26 1'; WOOD='.65 .48 .24 1'

def selected_docks(c):
    order=c.get('dock_active_ids',[4,5,9,10])
    if len(set(order))!=len(order) or any(i not in range(1,12) for i in order):
        raise ValueError('dock_active_ids must contain unique CAD door IDs 1..11')
    order=list(order)+[i for i in range(1,12) if i not in order]
    count=min(int(c.get('dock_active_count',4)),int(c.get('truck_count_visible',4)))
    if not 0<=count<=11: raise ValueError('Truck/dock count must be 0..11')
    return [next(d for d in CAD['doors'] if d['id']==i) for i in order[:count]]

def truck_pose(d):
    return (d['x'],d['rear_y'],-DECK,-math.pi/2)

def transparent_panel(name,x,y,z,sx,sy,sz,t):
    # SDF transparency belongs directly to visual, never material.
    return box(name,x,y,z,sx,sy,sz,'.45 .75 .65 1').replace(
        '</visual>',f'<transparency>{t}</transparency><cast_shadows>false</cast_shadows></visual>')

def visuals(c):
    L=float(c.get('truck_length_m',12)); W=float(c.get('truck_width_m',2.5)); H=float(c.get('truck_body_height_m',3.8))
    t=float(c.get('truck_cargo_transparency',.85))
    if not (8<=L<=15 and 2<=W<=2.8 and 2.5<=H<=4.5 and 0<=t<=1):
        raise ValueError('Truck limits: length 8..15, width 2..2.8, body height 2.5..4.5, transparency 0..1')
    v=[]; rounds=[]; transparent=[]; n=0
    def b(label,x,y,z,sx,sy,sz,color):
        nonlocal n
        n+=1;v.append(box(f'{label}_{n}',x,y,z,sx,sy,sz,color))
    end=L-3.3; top=DECK+H; cab=L-1.5
    # Longitudinal beams, crossmembers, articulated tractor frame and fifth wheel.
    for y in (-.72,.72): b('rail',L/2,y,.82,L-.25,.15,.28,BLACK)
    for k in range(int(L)): b('crossmember',k+.35,0,.85,.10,W-.3,.12,STEEL)
    b('deck',end/2,0,DECK-.06,end,W,.12,STEEL)
    b('fifth_wheel',end-.25,0,1.02,1.0,.9,.12,BLACK)
    # Separate thin walls and roof, open rear; opaque perimeter shows box outline.
    for side in (-1,1):
        transparent.append(transparent_panel(f'cargo_side_{side}',end/2,side*W/2,(DECK+top)/2,end,.025,H,t))
        for z in (DECK+.04,top): b('edge',end/2,side*W/2,z,end,.055,.07,GREEN)
        for x in (0,end): b('post',x,side*W/2,(DECK+top)/2,.065,.065,H,GREEN)
        b('side_guard',end*.54,side*(W/2-.06),.55,end*.42,.07,.20,STEEL)
        for x in (1,end*.45,end-.5): b('amber_marker',x,side*(W/2+.018),DECK+.12,.12,.025,.06,'1 .45 .015 1')
    transparent.append(transparent_panel('cargo_roof',end/2,0,top,end,W,.025,t))
    transparent.append(transparent_panel('cargo_front',end,0,(DECK+top)/2,.025,W,H,t))
    for z in (DECK,top): b('rear_header',0,0,z,.07,W,.07,GREEN)
    # Open doors folded flat against the outside of both side walls.
    for side in (-1,1):
        transparent.append(transparent_panel(f'open_door_{side}',W/4,side*(W/2+.045),(DECK+top)/2,W/2,.025,H,t))
        b('door_latch',W*.22,side*(W/2+.065),(DECK+top)/2,.025,.025,H*.72,STEEL)
        for z in (DECK+.35,top-.35): b('hinge',.07,side*(W/2+.07),z,.12,.06,.12,STEEL)
    # Decorative stock stays deep in the trailer while the animated batch near
    # the rear is created by dock_flow. It has no process or collision role.
    decorative_rows=(range(int(c.get('truck_decorative_pallet_rows',2))) if c.get('dock_flow_enabled',True)
                     else range(max(0,int((end-3.3)/1.35))))
    for row in decorative_rows:
        x=(5.25+row*1.45) if c.get('dock_flow_enabled',True) else (3.45+row*1.35)
        for side in (-1,1):
            y=side*W*.245
            for k in range(5): b('pallet_slat',x,y+(k-2)*.18,DECK+.12,1.1,.15,.04,WOOD)
            for xx in (-.4,.4): b('pallet_foot',x+xx,y,DECK+.05,.14,.8,.10,WOOD)
            for a in (-1,1):
                for by in (-1,1):
                    for layer in range(3):
                        xx=x+a*.265; yy=y+by*.205; z=DECK+.15+(layer+.5)*.52
                        b('carton',xx,yy,z,.51,.39,.50,'.74 .55 .30 1')
                        b('tape',xx,yy,z+.253,.055,.39,.008,'.92 .80 .57 1')
                        b('label',xx,yy+side*.197,z,.17,.006,.12,'.93 .92 .82 1')
    # Rounded cab shell with actual bevelled silhouette (shared generated mesh).
    # Cab bevel uses a small OBJ mesh, avoiding dependence on build_world import.
    cab_visual=bevel_cab(cab,W)
    b('cab_roof',cab-.12,0,3.72,2.25,W-.20,.16,RED)
    b('visor',L-.14,0,3.34,.22,W-.18,.12,BLACK)
    b('windscreen',L-.09,0,2.79,.025,W-.36,.98,GLASS)
    b('screen_split',L-.065,0,2.78,.035,.035,.99,BLACK)
    # Wipers, grille, trim, headlights and front registration plate.
    for y in (-W*.22,W*.22):
        v.append(strut(f'wiper_{y}',(L-.048,y-.28,2.37),(L-.048,y+.20,2.46),.027,BLACK))
    b('grille',L-.012,0,1.60,.07,W-.3,1.00,BLACK)
    for k in range(6): b('grille_slat',L+.03,0,1.23+k*.14,.03,W-.4,.037,STEEL)
    b('badge',L+.055,0,1.98,.025,.30,.10,STEEL)
    b('bumper',L+.035,0,.78,.18,W,.28,STEEL)
    b('bumper_lower',L+.08,0,.58,.15,W-.20,.12,BLACK)
    b('plate',L+.14,0,.78,.012,.48,.12,'.94 .94 .90 1')
    for side in (-1,1):
        y=side*W/2
        b('side_window',cab+.25,y+.005*side,2.80,1.45,.025,.86,GLASS)
        b('door_seam',cab-.63,y+.018*side,2.05,.025,.025,2.0,BLACK)
        b('handle',cab-.32,y+.04*side,2.06,.22,.05,.055,BLACK)
        b('mirror_arm',L-.5,side*(W/2+.15),2.98,.07,.35,.05,BLACK)
        b('mirror',L-.50,side*(W/2+.33),2.74,.19,.13,.53,BLACK)
        b('mirror_glass',L-.60,side*(W/2+.33),2.74,.012,.12,.43,STEEL)
        for z in (.6,.85,1.10): b('step',cab-.25,side*(W/2-.01),z,.65,.28,.09,STEEL)
        b('headlight',L+.045,side*(W/2-.30),1.04,.05,.42,.20,'.95 .97 1 1')
        b('indicator',L+.055,side*(W/2-.14),1.18,.05,.13,.08,'1 .45 .015 1')
        b('fuel_tank',end-.10,side*.90,.66,1.1,.5,.48,STEEL)
        for x in (end-.4,end+.2): b('tank_strap',x,side*1.155,.65,.05,.02,.45,BLACK)
        b('rear_light',-.045,side*(W/2-.25),.89,.06,.35,.11,'.85 .015 .015 1')
    # Three trailer axles, tractor rear axle and steering axle; tyre, rim, hub and bolts.
    axles=[1.25,2.25,3.25,end-.55,L-1.0]
    for a,x in enumerate(axles):
        rounds.append(cylinder(f'axle{a}',x,0,.5,.09,W-.35,BLACK,math.pi/2))
        for side in (-1,1):
            y=side*(W/2-.18)
            rounds.append(cylinder(f'tyre{a}_{side}',x,y,.5,.50,.32,BLACK,math.pi/2))
            rounds.append(cylinder(f'rim{a}_{side}',x,y+side*.166,.5,.315,.025,STEEL,math.pi/2))
            rounds.append(cylinder(f'hub{a}_{side}',x,y+side*.19,.5,.13,.06,BLACK,math.pi/2))
            for k in range(8):
                q=k*math.pi/4
                rounds.append(cylinder(f'bolt{a}_{side}_{k}',x+.225*math.cos(q),y+side*.19,.5+.225*math.sin(q),.025,.035,BLACK,math.pi/2))
            b('mudguard',x,side*(W/2-.13),1.045,1.12,.43,.09,BLACK)
            b('mudflap',x-.57,side*(W/2-.13),.38,.035,.38,.52,BLACK)
    return merge_boxes('truck_details',v+rounds)+[cab_visual]+transparent

def bevel_cab(cx,w):
    """Bevelled red shell; +X is front, corners narrowed for wraparound screen."""
    verts=[]
    for z,sx,sy in [(1.18,1.42,w/2),(3.42,1.30,w/2),(3.80,1.04,w/2-.12)]:
        cut=.18
        verts.extend([(cx+x,y,z) for x,y in [(-sx+cut,-sy),(sx-cut,-sy),(sx,-sy+cut),(sx,sy-cut),(sx-cut,sy),(-sx+cut,sy),(-sx,sy-cut),(-sx,-sy+cut)]])
    faces=[]
    for layer in (0,1):
        for k in range(8):
            a=layer*8+k;b=layer*8+(k+1)%8
            faces.extend([(a,b,b+8),(a,b+8,a+8)])
    for k in range(1,7): faces.extend([(0,k+1,k),(16,16+k,17+k)])
    import struct
    path=ROOT/'assets'/'truck_cab.stl';path.parent.mkdir(exist_ok=True)
    with path.open('wb') as f:
        f.write(b'Procedural truck cab'.ljust(80,b'\0')); f.write(struct.pack('<I',len(faces)))
        for face in faces:
            a,b,c=[verts[i] for i in face]; u=[b[i]-a[i] for i in range(3)];v=[c[i]-a[i] for i in range(3)]
            n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];l=math.sqrt(sum(q*q for q in n))
            f.write(struct.pack('<12fH',*[q/l for q in n],*a,*b,*c,0))
    return f'<visual name="cab_shell"><geometry><mesh><uri>{path.as_posix()}</uri></mesh></geometry><material><ambient>{RED}</ambient><diffuse>{RED}</diffuse><specular>.5 .5 .5 1</specular></material></visual>'

def add_scene(c,model):
    selected=selected_docks(c); truck=visuals(c)
    for i,d in enumerate(selected): model(f'truck_dock_{i}',truck_pose(d),truck)
    parts=[]
    for d in CAD['doors']:
        x,y=d['x'],d['rear_y']; width=3.0
        # Dock seal, open door frame and level bridge to the open truck rear.
        for side in (-1,1):
            parts.append(box(f'seal{d["id"]}_{side}',x+side*1.53,y+.10,1.85,.16,.18,3.7,BLACK))
            parts.append(box(f'bumper{d["id"]}_{side}',x+side*1.35,y-.05,-.30,.18,.18,.65,BLACK))
        parts.append(box(f'header{d["id"]}',x,y+.1,3.72,3.22,.2,.18,STEEL))
        if d in selected:
            span=d['door_y']-y+.15
            parts.append(box(f'bridge{d["id"]}',x,y+span/2-.04,-.025,2.35,span,.05,STEEL))
        for side in (-1,1): parts.append(box(f'bay{d["id"]}_{side}',x+side*1.65,y-6,-DECK+.006,.055,12,.012,'.95 .8 .2 1'))
    model('cad_dock_apron',(0,0,0),merge_boxes('dock_apron',parts))
