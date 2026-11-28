import json
import math
import equipment_visuals as EQ
from pathlib import Path
from xml.sax.saxutils import escape
from agv_motion import PARKS, pickup_position, assembly
from rail_geometry import RAIL_TOP_OFFSET, RAIL_HEIGHT, X_LINES, Y_LINES
from cad_navigation import LAYOUT

ROOT = Path(__file__).resolve().parent
C = json.loads((ROOT / 'config.json').read_text())
parts = []
COLORS = {'rack': '.65 .69 .72 1', 'beam': '.18 .34 .57 1', 'floor': '.48 .51 .53 1', 'in': '.12 .40 .35 1', 'out': '.55 .30 .17 1'}
STEEL='.45 .49 .52 1'; DARK='.10 .13 .15 1'; WHITE='.80 .83 .82 1'; TEAL='.08 .32 .35 1'
ORANGE='.95 .28 .025 1'; BLUE='.035 .24 .68 1'; CARTON='.82 .70 .43 1'

from sdf_parts import box, cylinder, strut, merge_boxes, HIDDEN_Z, HIDDEN_BELOW, CAMERA_FAR_CLIP

EQ.BATCH_BOXES=merge_boxes

def chamfer(name,x,y,z,sx,sy,sz,color):
    # Actual bevelled mesh silhouette rather than a stack of featureless cubes.
    path=ROOT/'assets'; path.mkdir(exist_ok=True)
    b=min(sx,sy)*.16
    corners=[(-sx/2+b,-sy/2),(sx/2-b,-sy/2),(sx/2,-sy/2+b),(sx/2,sy/2-b),(sx/2-b,sy/2),(-sx/2+b,sy/2),(-sx/2,sy/2-b),(-sx/2,-sy/2+b)]
    verts=[(a,c,h) for h in [-sz/2,sz/2] for a,c in corners]
    faces=[]
    for i in range(1,7): faces.extend([(0,i+1,i),(8,8+i,8+i+1)])
    for i in range(8):
        j=(i+1)%8;faces.extend([(i,j,8+j),(i,8+j,8+i)])
    mesh=path/f'{name}.stl'
    lines=['solid body']
    for f in faces:
        lines+=['facet normal 0 0 0','outer loop']+['vertex '+' '.join(map(str,verts[k])) for k in f]+['endloop','endfacet']
    lines+=['endsolid body'];mesh.write_text('\n'.join(lines))
    return f'<visual name="{name}"><pose>{x} {y} {z} 0 0 0</pose><geometry><mesh><uri>{escape(str(mesh))}</uri></mesh></geometry><material><ambient>{color}</ambient><diffuse>{color}</diffuse><specular>.3 .3 .3 1</specular></material></visual>'

def model(name, xyz, visuals):
    yaw=xyz[3] if len(xyz)>3 else 0
    z=HIDDEN_Z if xyz[2]<HIDDEN_BELOW else xyz[2]   # pooled objects "hidden" at z=-5: park them beyond the camera far clip
    parts.append(f'<model name="{name}"><static>true</static><pose>{xyz[0]} {xyz[1]} {z} 0 0 {yaw}</pose><link name="body">' + ''.join(visuals) + '</link></model>')

def roller_deck(prefix,sx=1.8,sy=1.8):
    v=[]
    for dx in [-sx/2,sx/2]: v.append(box(f'{prefix}frame{dx}',dx,0,-.14,.09,sy,.22,DARK))
    for k in range(10): v.append(cylinder(f'{prefix}roller{k}',0,(k-4.5)*sy/10,-.045,.045,sx,STEEL,pitch=math.pi/2))
    for dx in [-.48,.48]: v.append(box(f'{prefix}shuttle_yrail{dx}',dx,0,RAIL_TOP_OFFSET-RAIL_HEIGHT/2,.10,sy,RAIL_HEIGHT,STEEL))
    for dy in [-.49,.49]: v.append(box(f'{prefix}shuttle_xrail{dy}',0,dy,RAIL_TOP_OFFSET-RAIL_HEIGHT/2,sx,.10,RAIL_HEIGHT,STEEL))
    return v

def lift_tower(prefix,width=2.2):
    return EQ.tower(prefix,box,cylinder,strut)

def lift_cabin(axis='y'):
    return EQ.cabin(box,cylinder,lambda prefix: EQ.chain_conveyor(box,cylinder,1.8,1.6,axis,legs=False)+
        [box(f'{prefix}rail{dx}',dx,0,RAIL_TOP_OFFSET-RAIL_HEIGHT/2,.10,1.8,RAIL_HEIGHT,STEEL) for dx in [-.48,.48]])

def shuttle_lift_cabin():
    """Shuttle transfer lift cabin with integrated X/Y rail locks so shuttles ride safely between levels."""
    return EQ.cabin(box,cylinder,lambda prefix:
        [box(f'{prefix}yrail{dx}',dx,0,RAIL_TOP_OFFSET-RAIL_HEIGHT/2,.10,1.8,RAIL_HEIGHT,STEEL) for dx in [-.48,.48]]+
        [box(f'{prefix}xrail{dy}',0,dy,RAIL_TOP_OFFSET-RAIL_HEIGHT/2,1.8,.10,RAIL_HEIGHT,STEEL) for dy in [-.49,.49]]+
        [box(f'{prefix}lock_x{sx}',sx*.82,0,RAIL_TOP_OFFSET+.04,.08,.90,.06,'.88 .22 .10 1') for sx in (-1,1)]+
        [box(f'{prefix}lock_y{sy}',0,sy*.82,RAIL_TOP_OFFSET+.04,.90,.08,.06,'.88 .22 .10 1') for sy in (-1,1)])

def pallet_deck():
    v=[]
    # Moulded plastic lattice deck with fork pockets and nine support blocks.
    for a in range(9): v.append(box(f'deckrib{a}',0,(a-4)*.115,.13,1.2,.085,.04,BLUE))
    for a in range(9): v.append(box(f'crossrib{a}',(a-4)*.14,0,.12,.055,1,.04,BLUE))
    for a in [-.58,.58]: v.append(box(f'rimx{a}',a,0,.125,.04,1,.05,BLUE))
    for a in [-.48,.48]: v.append(box(f'rimy{a}',0,a,.125,1.2,.04,.05,BLUE))
    for a in [-.51,0,.51]:
        for b in [-.4,0,.4]: v.append(box(f'foot{a}_{b}',a,b,.055,.16,.12,.11,BLUE))
    return v

def pallet(name, xyz):
    v=pallet_deck()
    for a in range(3):
        for b in range(2):
            for c in range(5):
                bx,by,bz=(a-1)*.39,(b-.5)*.49,.15+(c+.5)*1.25/5
                v.append(box(f'box{a}{b}{c}',bx,by,bz,.38,.48,1.25/5-.012,CARTON))
                v.append(box(f'tape{a}{b}{c}',bx,by,bz+1.25/10-.005,.055,.478,.004,'.9 .8 .57 1'))
                if b==0:
                    v.append(box(f'label{a}{c}',bx,by-.242,bz,.14,.004,.1,'.86 .86 .79 1'))
                    for q in range(6): v.append(box(f'barcode{a}{c}{q}',bx-.05+q*.018,by-.245,bz,.006,.003,.065,'.12 .12 .12 1'))
    for x in [-.33,.33]:
        v.append(box(f'wrapfront{x}',x,-.49,.775,.018,.005,1.25,'.92 .89 .74 1'))
        v.append(box(f'wraptop{x}',x,0,1.402,.018,.98,.004,'.92 .89 .74 1'))
    # All pallets are identical: batch the 134 boxes into one shared mesh per colour
    # (a few draw calls per pallet instead of 134), generated once and reused.
    global PALLET_MESH
    if PALLET_MESH is None: PALLET_MESH=merge_boxes('pallet_mesh',v)
    model(name,xyz,PALLET_MESH)

PALLET_MESH=None

import truck_scene
# Warehouse slab ends at CAD south facade. Recessed truck yard aligns trailer
# deck with warehouse floor; no truck intersects the AGV lanes at y=-8.4/-6.4.
v=[box('ground',1,22.89,-.12,114,66.22,.2,COLORS['floor']),
   box('truck_yard',1,-22.11,-truck_scene.DECK-.10,114,23.78,.20,'.28 .30 .32 1'),
   box('dock_retaining_face',-26,-10.20,-.6,52,.08,1.10,'.52 .54 .56 1')]
for x in range(-56,57,4): v.append(box(f'jointx{x}',x,22.89,-.015,.012,66.22,.004,'.38 .41 .42 1'))
for y in range(-8,57,4): v.append(box(f'jointy{y}',1,y,-.015,112,.012,.004,'.38 .41 .42 1'))
# Inbound AGV lanes on floor 1 (4-floor building) continuing into the 4-way store; pallet spots are drawn by the 4-floor scene.
from agv_motion import LOAD_Y, RETURN_Y
_HUMAN_FORKLIFT = C.get('forklift_type', 'human_driven') == 'human_driven'
for y in (LOAD_Y, RETURN_Y): v.append(box(f'aisle{y}',-4,y,.004,92,.10,.008,'.86 .70 .19 1'))
for x,y,_,_ in PARKS:
    for dx in [-.7,.7]: v.append(box(f'park{x}_{dx}',x+dx,y,.005,.06,1.9,.01,'.16 .42 .35 1'))
    for dy in [-.95,.95]: v.append(box(f'parkend{x}_{dy}',x,y+dy,.005,1.46,.06,.01,'.16 .42 .35 1'))
    if not _HUMAN_FORKLIFT:
        # Integrated floor charging pad + north-side charger bollard at each AGV Forklift parking bay
        v.append(box(f'park_chg_pad{x}',x,y,.008,.70,.70,.012,'.85 .62 .12 1'))
        v.append(box(f'park_chg_post{x}',x,y+1.15,.55,.28,.22,1.10,'.18 .22 .26 1'))
        v.append(box(f'park_chg_led{x}',x,y+1.03,.92,.14,.02,.08,'.10 .85 .35 1'))
v.extend([box('back_wall',26,51,10,54,.18,20,'.64 .67 .69 1'),box('right_wall',53,25,10,.18,52,20,'.59 .63 .65 1')])
for x in range(0,54,6): v.append(box(f'building_column{x}',x,50.8,10,.25,.3,20,'.35 .39 .42 1'))
model('site',(0,0,0),merge_boxes('site_mesh',v))
truck_scene.add_scene(C,model)
# 4-floor picking building west of the store (CAD, four_floor_scene.py); replaces the old placeholder zone.
import four_floor_scene
import loose_goods
_spots=C.get('loose_pallet_spots',[])
if _spots: four_floor_scene.ANIMATED_WORKER_AREAS={'carton_inbound','pallet_pick','sorting','unpack','put_away','picking_station'}
parts.extend(four_floor_scene.models())
import dock_flow
dock_flow.scene(C,model,pallet_deck)
# Floor-1 loose goods: one pallet deck per unpack spot, one model per carton of the demo (start hidden)
# and the two unpacking operators as four animated parts each (torso+head, arms, left / right leg).
if _spots:
    _part_mesh={p:merge_boxes(f'op_{p}',[box(f'{p}{k}',*b) for k,b in enumerate(bs)]) for p,bs in loose_goods.WORKER_PARTS.items()}
    for i,op in enumerate(loose_goods.OPERATORS):
        for name,pose in loose_goods.operator_pose(i,*loose_goods.station(op)).items():
            model(name,(pose[0],pose[1],pose[2],pose[5]),_part_mesh[name.split('_',1)[1]])
    for f in loose_goods.floors():
        for b in range(len(loose_goods.BRANCHES)):
            wx,wy,wyaw,wz=loose_goods.u1_station(f,b)
            for name,pose in loose_goods.operator_pose(loose_goods.u1_worker_id(f,b),wx,wy,wyaw,z0=wz).items():
                model(name,(pose[0],pose[1],pose[2],pose[5]),_part_mesh[name.split('_',1)[1]])
        for z_idx in range(len(loose_goods.Z05_WORKERS)):
            wx,wy,wyaw,wz=loose_goods.z05_station(f,z_idx)
            for name,pose in loose_goods.operator_pose(loose_goods.z05_worker_id(f,z_idx),wx,wy,wyaw,z0=wz).items():
                model(name,(pose[0],pose[1],pose[2],pose[5]),_part_mesh[name.split('_',1)[1]])
        for u2_idx in range(len(loose_goods.U2_WORKERS)):
            wx,wy,wyaw,wz=loose_goods.u2_station(f,u2_idx)
            for name,pose in loose_goods.operator_pose(loose_goods.u2_worker_id(f,u2_idx),wx,wy,wyaw,z0=wz).items():
                model(name,(pose[0],pose[1],pose[2],pose[5]),_part_mesh[name.split('_',1)[1]])
        for st_idx in range(len(loose_goods.P1_STATIONS)):
            wx,wy,wyaw,wz=loose_goods.p1_station(f,st_idx)
            for name,pose in loose_goods.operator_pose(loose_goods.p1_worker_id(f,st_idx),wx,wy,wyaw,z0=wz).items():
                model(name,(pose[0],pose[1],pose[2],pose[5]),_part_mesh[name.split('_',1)[1]])
    _deck=merge_boxes('loose_deck',pallet_deck())
    for s,(x,y,yaw) in enumerate(_spots): model(f'loose_pallet_{s}',(x,y,-5,yaw),_deck)
    cl,cw,ch=loose_goods.CARTON
    _carton=merge_boxes('loose_carton',[box('c',0,0,0,cl,cw,ch-.004,CARTON),box('t',0,0,ch/2-.003,.06,cw+.002,.006,'.9 .8 .57 1')])
    _med_box=merge_boxes('med_box',[box(f'mb{k}',*b) for k,b in enumerate(loose_goods.med_box_parts())])
    _med_pack=merge_boxes('med_pack',[box(f'mp{k}',*b) for k,b in enumerate(loose_goods.med_tray_pack_parts())])
    _loose_pool=max(180,int(C.get('loose_carton_pool',0)),int(C.get('loose_pallets',0))*int(C.get('loose_cartons_per_pallet',0)))
    for i in range(_loose_pool):
        model(f'carton_{i}',(_spots[0][0],_spots[0][1],-5),_carton)
    _z05_pool=max(80,int(C.get('z05_carton_pool',0)),len(loose_goods.Z05_WORKERS)*int(C.get('z05_cartons_per_pallet',loose_goods.Z05_CARTONS_PER_PALLET))*2)
    _u1_mpc=int(C.get('u1_med_boxes_per_carton',loose_goods.U1_MED_PER_CARTON))
    _u2_mpc=int(C.get('u2_med_boxes_per_carton',loose_goods.U1_MED_PER_CARTON))
    _p1_mpc=int(C.get('p1_med_boxes_per_carton',loose_goods.U1_MED_PER_CARTON))
    _u1_med_total=max(1,int(C.get('u1_cartons_per_rack',8)))*_u1_mpc
    _u2_med_total=max(1,int(C.get('u2_cartons_per_rack',8)))*_u2_mpc
    _p1_med_total=max(1,int(C.get('p1_cartons_per_rack',8)))*_p1_mpc
    _p1_med_mesh=_med_pack if _p1_mpc==1 else _med_box
    for r_idx in range(8):
        model(f'z05_ret_deck_{r_idx}',(_spots[0][0],_spots[0][1],-5,0),_deck)
    for f in loose_goods.floors():
        z0=loose_goods.LEVELS[f-1]
        for b in range(len(loose_goods.BRANCHES)):
            rx,ry,_,_=loose_goods.u1_rack_pose(f,b)
            for r_idx in range(loose_goods.U1_RACKS_PER_BRANCH):
                for m in range(_u1_med_total):
                    model(f'u1_med_{f}_{b}_{r_idx}_{m}',(rx,ry,-5,0),_med_pack if _u1_mpc==1 else _med_box)
        for z_idx in range(len(loose_goods.Z05_WORKERS)):
            wx,wy,_,_=loose_goods.z05_station(f,z_idx)
            model(f'z05_deck_{f}_{z_idx}',(wx,wy,-5,0),_deck)
        w0x,w0y,_,_=loose_goods.z05_station(f,0)
        for c_idx in range(_z05_pool):
            model(f'z05_carton_{f}_{c_idx}',(w0x,w0y,-5,0),_carton)
        for u2_idx in range(len(loose_goods.U2_WORKERS)):
            rx,ry,_,_=loose_goods.u2_rack_pose(f,u2_idx)
            for r_idx in range(loose_goods.U2_RACKS_PER_STATION):
                for m in range(_u2_med_total):
                    model(f'u2_med_{f}_{u2_idx}_{r_idx}_{m}',(rx,ry,-5,0),_med_pack if _u2_mpc==1 else _med_box)
        for st_idx in range(len(loose_goods.P1_STATIONS)):
            r0_pose=loose_goods.p1_rack_pose(f,st_idx)
            for m in range(_p1_med_total):
                tray_slot=m//_p1_mpc
                box_i=1 if _p1_mpc==1 else (m%_p1_mpc)
                model(f'p1_med_{f}_{st_idx}_{m}',loose_goods.u1_med_on_rack(r0_pose,box_i,tray_slot),_p1_med_mesh)
            t_pose=loose_goods.p1_trolley_pose(f,st_idx)
            for c_slot in range(loose_goods.TROLLEY_CAPACITY):
                model(f'p1_carton_{f}_{st_idx}_{c_slot}',loose_goods.trolley_carton_pose(t_pose,c_slot),_carton)
from cad_scene import build_cad_scene
stock_count=build_cad_scene(C,box,strut,merge_boxes,model)
print(f'CAD slots per level: {len(LAYOUT.slots)}; illustrative stock: {stock_count}')
for i,port in enumerate(LAYOUT.data['ports']['in']):
    x,y=port['lift'];bx,by=port['buffer'];cx,cy=port['infeed']
    model(f'in_tower_{i}',(x,y,0),lift_tower(f'in{i}'))
    model(f'in_lift_{i}',(x,y,.3),lift_cabin())
    model(f'in_counterweight_{i}',(x+1.45,y+.75,17.3),EQ.counterweight(box))
    model(f'chain_in_{i}',(cx,cy,.3),EQ.chain_conveyor(box,cylinder,port['infeed_length'],1.35,'y'))
    for k,z in enumerate(C['storage_levels']):model(f'in_buffer_{i}_{k}',(bx,by,z),EQ.chain_conveyor(box,cylinder,1.6,1.35,'y',legs=False))
for i,port in enumerate(LAYOUT.data['ports']['out']):
    x,y=port['lift'];bx,by=port['buffer'];gx,gy=port['gate'];tx,ty=port['corner']
    model(f'out_tower_{i}',(x,y,0),lift_tower(f'out{i}'))
    model(f'out_lift_{i}',(x,y,.3),lift_cabin())
    model(f'out_counterweight_{i}',(x+1.45,y+.75,17.3),EQ.counterweight(box))
    for k,floor in enumerate(C['output_levels']):
        z=floor+.3
        model(f'out_link_{i}_{k}',(x,(y+ty)/2,z),EQ.chain_conveyor(box,cylinder,ty-y,1.35,'y'))
        model(f'out_corner_{i}_{k}',(tx,ty,z),EQ.chain_conveyor(box,cylinder,1.5,1.35,'x'))
        model(f'out_gate_{i}_{k}',(gx,gy,z),EQ.chain_conveyor(box,cylinder,port['gate_length'],1.35,'x'))
    for k,z in enumerate(C['storage_levels']):model(f'out_buffer_{i}_{k}',(bx,by,z),EQ.chain_conveyor(box,cylinder,1.6,1.35,'y',legs=False))

# Dedicated Shuttle Transfer Lift (with X/Y rail locks) at north-east rail intersection (51.80108, 44.75)
_sh_lx, _sh_ly = 51.80108, 44.75
model('shuttle_lift_tower', (_sh_lx, _sh_ly, 0), lift_tower('sh4'))
model('shuttle_lift_cabin', (_sh_lx, _sh_ly, C['storage_levels'][0]), shuttle_lift_cabin())
model('shuttle_lift_cw', (_sh_lx + 1.45, _sh_ly + .75, 17.3), EQ.counterweight(box))
# Outer-edge Lithium battery charging stations for Shuttles along east technical aisle (next to right_wall x=53)
for k, z in enumerate(C['storage_levels']):
    chg_v = []
    for bay_idx, cy_chg in enumerate((36.75, 28.75)):
        chg_v.extend([
            box(f'sh_chg_cab_{k}_{bay_idx}', 52.55, cy_chg, .55, .45, .70, 1.10, '.18 .22 .26 1'),
            box(f'sh_chg_fin_{k}_{bay_idx}', 52.79, cy_chg, .65, .04, .56, .70, '.45 .48 .52 1'),
            box(f'sh_chg_led_{k}_{bay_idx}', 52.31, cy_chg, .95, .03, .22, .10, '.10 .85 .35 1'),
            box(f'sh_chg_pad_{k}_{bay_idx}', 51.80108, cy_chg, .02, .90, .80, .03, '.85 .62 .12 1'),
        ])
    model(f'shuttle_chargers_{k}', (0, 0, z), merge_boxes(f'sh_chg_{k}', chg_v))

_fork_mesh=merge_boxes('agv_fork',[box('carriage',0,0,.25,.1,.98,.5,DARK),box('forkL',.8,-.23,.04,1.6,.12,.08,STEEL),box('forkR',.8,.23,.04,1.6,.12,.08,STEEL)])
for i in range(len(PARKS)):
    v=[chamfer(f'chassis{i}',-.08,0,.48,1.5,1.12,.56,WHITE),chamfer(f'counterweight{i}',-.46,0,.82,.65,1.08,.3,TEAL),box('bumper',-.82,0,.29,.1,1.05,.18,DARK),box('battery',-.1,0,.83,.5,.75,.18,DARK)]
    for a in [-.48,.48]:
        for b in [-.56,.56]:
            v.extend([cylinder(f'tire{a}{b}',a,b,.25,.25,.14,'.055 .06 .065 1',math.pi/2),cylinder(f'hub{a}{b}',a,b*1.14,.25,.13,.03,STEEL,math.pi/2)])
    for b in [-.45,.45]:
        v.extend([box(f'mast{b}',.68,b,1.3,.12,.10,2.6,DARK),box(f'rail{b}',.755,b,1.3,.025,.05,2.4,STEEL),cylinder(f'hydraulic{b}',.58,b*.65,1.2,.045,1.8,STEEL)])
    for h in [.2,1.3,2.5]: v.append(box(f'mastcross{h}',.68,0,h,.13,1,.08,DARK))
    if _HUMAN_FORKLIFT:
        # Human-driven forklift: overhead guard, seat, steering wheel, and seated operator.
        for b in [-.42,.42]:
            v.append(box(f'guard_post_f{b}',.38,b,1.45,.05,.05,1.35,DARK))
            v.append(box(f'guard_post_r{b}',-.55,b,1.45,.05,.05,1.35,DARK))
        v.append(box('guard_roof',-.08,0,2.14,1.02,.92,.05,DARK))
        v.extend([
            box('seat_base',-.15,0,.92,.38,.42,.10,DARK),
            box('seat_back',-.32,0,1.18,.10,.40,.46,DARK),
            box('steer_col',.26,0,1.05,.07,.07,.55,DARK),
            cylinder('steer_wheel',.22,0,1.32,.16,.03,DARK,0.35),
            # Seated human driver (pants, safety vest, arms, head + hard hat)
            box('drv_thighs',-.02,0,1.04,.34,.32,.14,'.14 .20 .32 1'),
            box('drv_legs',.14,0,.86,.14,.30,.32,'.14 .20 .32 1'),
            box('drv_torso',-.18,0,1.25,.24,.34,.44,'.95 .45 .05 1'),
            box('drv_arm_l',.02,-.18,1.30,.32,.09,.09,'.95 .45 .05 1'),
            box('drv_arm_r',.02,.18,1.30,.32,.09,.09,'.95 .45 .05 1'),
            box('drv_head',-.16,0,1.58,.18,.18,.20,'.85 .68 .55 1'),
            box('drv_hat',-.16,0,1.70,.22,.21,.08,'.98 .82 .08 1'),
            cylinder('beacon',-.50,0,2.20,.06,.10,'.95 .48 .03 1'),
        ])
    else:
        v.extend([box('scanner',-.25,0,1.0,.24,.35,.16,DARK),box('scanwindow',-.25,-.18,1,.2,.015,.06,'.2 .45 .6 1'),cylinder('beacon',-.5,0,1.12,.06,.10,'.95 .48 .03 1')])
        for b in [-.48,.48]: v.append(box(f'status{b}',-.25,b,.67,.45,.025,.035,'.12 .8 .55 1'))
    model(f'agv_{i}',PARKS[i],merge_boxes(f'agv_body_{i}',v))
    model(f'fork_{i}',assembly(i,PARKS[i],.02)[f'fork_{i}'],_fork_mesh)
for i in range(10):
    z=C['storage_levels'][i%9];px,py=LAYOUT.slot_point(LAYOUT.parking[i//9])
    model(f'shuttle_{i}',(px,py,z),EQ.shuttle_body(i,box,cylinder,chamfer))
    model(f'shuttle_deck_{i}',(px,py,z-.06),EQ.shuttle_deck(box,cylinder))
    for axis in ['x','y']:
        model(f'shuttle_wheels_{axis}_{i}',(px,py,z+(0 if axis=='y' else .04)),EQ.shuttle_wheels(axis,box,cylinder))
_seq=C.get('inbound_type_sequence',['even','odd']) or ['even']
_use_barcode=C.get('inbound_classification')=='barcode' and bool(_spots)
for i in range(C['jobs']):
    if _use_barcode and str(_seq[i%len(_seq)]).lower()!='even':
        continue
    pallet(f'pallet_{i}',(pickup_position(i)[0],-17,-5,pickup_position(i)[3]))
from cad_navigation import initial_stock_cell
for i in range(C.get('initial_outbound_pallets',0)):
    sid,level=initial_stock_cell(i)
    pallet(f'pallet_out_{i}',LAYOUT.slot_point(sid)+(C['storage_levels'][level],0))
for i in range(max(0,int(C.get('continuous_extra_pallets',0)))):
    pallet(f'pallet_pool_{i}',(pickup_position(i)[0],-17,-5,pickup_position(i)[3]))

header='''<?xml version="1.0"?><sdf version="1.9"><world name="warehouse">
<physics name="physics" type="ignored"><max_step_size>0.05</max_step_size><real_time_factor>1</real_time_factor></physics>
<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
<plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
<plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
<scene><ambient>.5 .5 .5 1</ambient><background>.72 .76 .80 1</background><shadows>false</shadows></scene>
<light name="sun" type="directional"><pose>0 0 30 0 0 0</pose><diffuse>.9 .9 .9 1</diffuse><direction>-.3 .2 -1</direction><cast_shadows>false</cast_shadows></light>
<gui fullscreen="false"><plugin filename="MinimalScene" name="3D View"><gz-gui><title>Warehouse V1</title><property type="bool" key="showTitleBar">false</property><property type="string" key="state">docked</property></gz-gui><engine>ogre</engine><scene>scene</scene><ambient_light>.5 .5 .5</ambient_light><background_color>.72 .76 .80</background_color><camera_pose>8 -50 45 0 .53 1.76</camera_pose><camera_clip><near>0.1</near><far>__FAR_CLIP__</far></camera_clip></plugin><plugin filename="GzSceneManager" name="Scene Manager"/><plugin filename="CameraTracking" name="Camera Tracking"/><plugin filename="InteractiveViewControl" name="View control"/><plugin filename="EntityContextMenuPlugin" name="Entity menu"/><plugin filename="WorldControl" name="World control"/><plugin filename="WorldStats" name="World stats"/><plugin filename="Screenshot" name="Screenshot"/></gui>
'''
header=header.replace('__FAR_CLIP__',str(CAMERA_FAR_CLIP))
(ROOT/'warehouse.sdf').write_text(header+''.join(parts)+'</world></sdf>')
print(f'Generated warehouse.sdf: {len(parts)} models')

# A separate inspection scene uses exactly the same assemblies as the warehouse.
# It provides an unobstructed close-up instead of enlarging real equipment sizes.
import xml.etree.ElementTree as ET
out_offset=LAYOUT.data['ports']['out'][0]['corner'][1]-LAYOUT.data['ports']['out'][0]['lift'][1]
placements={'in_tower_0':(0,0,0),'in_lift_0':(0,0,.3),'in_counterweight_0':(1.45,.75,17.3),
            'chain_in_0':(0,LAYOUT.data['ports']['in'][0]['infeed'][1]-LAYOUT.data['ports']['in'][0]['lift'][1],.3),'in_buffer_0_0':(0,LAYOUT.data['ports']['in'][0]['buffer'][1]-LAYOUT.data['ports']['in'][0]['lift'][1],.3),
            'out_tower_0':(7,4,0),'out_lift_0':(7,4,.3),'out_counterweight_0':(8.45,4.75,17.3),
            'out_buffer_0_0':(7,4+LAYOUT.data['ports']['out'][0]['buffer'][1]-LAYOUT.data['ports']['out'][0]['lift'][1],.3),'out_link_0_0':(7,4+out_offset/2,.3),
            'out_corner_0_0':(7,4+out_offset,.3),'out_gate_0_0':(5.4,4+out_offset,.3),
            'shuttle_0':(3.7,0,.3),'shuttle_deck_0':(3.7,0,.24),
            'shuttle_wheels_x_0':(3.7,0,.3),'shuttle_wheels_y_0':(3.7,0,.34)}
inspection=[]
for part in parts:
    e=ET.fromstring(part)
    if e.attrib['name'] in placements:
        e.find('pose').text=' '.join(map(str,placements[e.attrib['name']]))+' 0 0 0'
        inspection.append(ET.tostring(e,encoding='unicode'))
floor=box('floor',4,1,-.12,18,16,.2,'.40 .44 .46 1')
for y in [-.49,.49]:floor+=box(f'sample_track{y}',3.7,y,.3+RAIL_TOP_OFFSET-RAIL_HEIGHT/2,2.5,.10,RAIL_HEIGHT,'.68 .075 .045 1')
inspection.append('<model name="inspection_floor"><static>true</static><link name="body">'+floor+'</link></model>')
detail_header=header.replace('world name="warehouse"','world name="equipment_detail"').replace('40 -45 35 0 .52 2.1','6 -6 3 0 .3 2.15').replace('Warehouse V1','Equipment Detail')
(ROOT/'equipment-detail.sdf').write_text(detail_header+''.join(inspection)+'</world></sdf>')
