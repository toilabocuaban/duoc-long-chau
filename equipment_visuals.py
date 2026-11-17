"""Detailed visual assemblies; running gear datums remain in rail_geometry.py."""
import math

ORANGE='.94 .27 .025 1';STEEL='.64 .68 .70 1';DARK='.065 .08 .095 1'
BLUE='.045 .17 .37 1';WHITE='.83 .85 .84 1';YELLOW='.97 .70 .06 1'
SHUTTLE_BLUE='.015 .42 .75 1'
BATCH_BOXES=None
_CHAIN_CACHE={}
SEGMENTS={'0':'abcdef','1':'bc','2':'abdeg','3':'abcdg','4':'bcfg','5':'acdfg','6':'acdefg','7':'abc','8':'abcdefg','9':'abcdfg'}

def fit_cad_lane(items):
    """Fit the body/deck to the CAD's 1.4m clear channel; wheel gauges stay fixed."""
    import xml.etree.ElementTree as ET
    out=[]
    for item in items:
        e=ET.fromstring(item);p=e.find('pose');v=list(map(float,p.text.split()));v[0]*=.9;v[1]*=.9;p.text=' '.join(map(str,v))
        size=e.find('geometry/box/size')
        if size is not None:
            v=list(map(float,size.text.split()));v[0]*=.9;v[1]*=.9;size.text=' '.join(map(str,v))
        mesh=e.find('geometry/mesh')
        if mesh is not None:ET.SubElement(mesh,'scale').text='.9 .9 1'
        out.append(ET.tostring(e,encoding='unicode'))
    return out

def digits(box,prefix,text,x,y,z,scale=.12):
    v=[]
    locations={'a':(0,1,.8,.12),'g':(0,0,.8,.12),'d':(0,-1,.8,.12),
               'f':(-.4,.5,.12,.8),'b':(.4,.5,.12,.8),'e':(-.4,-.5,.12,.8),'c':(.4,-.5,.12,.8)}
    for i,char in enumerate(text):
        for key in SEGMENTS[char]:
            dx,dz,sx,sz=locations[key]
            v.append(box(f'{prefix}{i}{key}',x+(i-(len(text)-1)/2)*scale*1.2+dx*scale,y,z+dz*scale,sx*scale,.006,sz*scale,DARK))
    return v

def shuttle_body(i,box,cylinder,chamfer):
    v=[chamfer(f'shuttle_chassis_{i}',0,0,-.145,1.4,1.1,.15,DARK),box('underframe',0,0,-.202,1.12,.84,.035,DARK)]
    # Split covers leave visible wheel cut-outs rather than hiding all wheels.
    for y in [-.535,.535]:
        v.extend([box(f'central_panel{y}',0,y,-.105,.59,.045,.15,SHUTTLE_BLUE),box(f'top_lip{y}',0,y,-.028,1.36,.14,.045,SHUTTLE_BLUE)])
        for x in [-.63,.63]: v.append(box(f'corner_panel{x}{y}',x,y,-.11,.13,.045,.13,SHUTTLE_BLUE))
        for q in range(6): v.append(box(f'cooling_slot{y}{q}',-.20+q*.07,y*1.003,-.12,.028,.008,.045,DARK))
    for x in [-.69,.69]:
        v.extend([box(f'end_bumper{x}',x,0,-.155,.04,.94,.06,DARK),box(f'scanner_recess{x}',x*1.025,0,-.09,.016,.22,.075,DARK),box(f'scanner_glass{x}',x*1.04,0,-.09,.008,.16,.035,'.10 .32 .42 1')])
        for y in [-.40,.40]:
            v.append(box(f'statuslamp{x}{y}',x*1.035,y,-.09,.012,.08,.028,'.88 .93 .95 1'))
    v.extend([box('id_plate',0,-.563,-.075,.30,.012,.115,WHITE),*digits(box,'id',f'{i+1:02}',0,-.573,-.075,.043)])
    for x in [-.58,.58]:
        for y in [-.40,.40]: v.append(cylinder(f'case_bolt{x}{y}',x,y,-.028,.014,.012,STEEL))
    v=fit_cad_lane(v)
    return BATCH_BOXES(f'shuttle_body_{i}',v) if BATCH_BOXES is not None else v

_DECK_CACHE=None
def shuttle_deck(box,cylinder):
    global _DECK_CACHE
    if BATCH_BOXES is not None and _DECK_CACHE is not None: return list(_DECK_CACHE)
    v=[box('black_deck',0,0,-.025,1.13,.88,.04,DARK)]
    for y in [-.30,.30]:
        v.append(box(f'panel_seam{y}',0,y,-.003,1.08,.007,.003,'.16 .18 .19 1'))
        for x in [-.43,.43]: v.append(cylinder(f'pad_bolt{x}{y}',x,y,-.002,.009,.003,DARK))
    for x in [-.40,.40]:
        v.append(box(f'lift_crossmember{x}',x,0,-.075,.08,.73,.05,STEEL))
        for y in [-.28,.28]: v.append(cylinder(f'lift_plunger{x}{y}',x,y,-.105,.025,.08,STEEL))
    v=fit_cad_lane(v)
    if BATCH_BOXES is not None:
        _DECK_CACHE=tuple(BATCH_BOXES('shuttle_deck',v))
        return list(_DECK_CACHE)
    return v

def chain_conveyor(box,cylinder,length=2,width=1.35,axis='y',height=.3,legs=True):
    """Two-strand chain conveyor. Local z=0 is the pallet support surface."""
    import xml.etree.ElementTree as ET
    key=(length,width,axis,height,legs)
    if key in _CHAIN_CACHE: return list(_CHAIN_CACHE[key])
    v=[];gauge=.42;half=length/2;pitch=.055
    for x in [-gauge,gauge]:
        v.extend([box(f'chain_channel{x}',x,0,-.09,.17,length,.16,STEEL),box(f'wear_strip{x}',x,0,-.026,.10,length,.035,DARK)])
        for k in range(max(2,int(length/pitch))):
            y=-half+.025+k*pitch
            v.append(box(f'chain_link{x}_{k}',x,y,-.010,.075,.035,.02,'.28 .31 .33 1'))
            for dx in [-.043,.043]:v.append(box(f'link_plate{x}_{k}_{dx}',x+dx,y,-.008,.014,.044,.023,STEEL))
        for y in [-half,half]:
            v.append(cylinder(f'sprocket{x}{y}',x,y,-.075,.075,.11,STEEL,pitch=math.pi/2))
            for k in range(12):
                a=k*math.tau/12
                v.append(box(f'tooth{x}{y}{k}',x,y+.075*math.cos(a),-.075+.075*math.sin(a),.115,.018,.018,DARK))
            v.append(box(f'end_cover{x}{y}',x,y,-.083,.19,.05,.17,STEEL))
    for y in [-half+.16,0,half-.16]:
        v.append(box(f'crossmember{y}',0,y,-.18,width,.08,.10,STEEL))
    # Shaft, shaft bearings, flexible couplings and geared drive.
    dy=-half+.09
    v.append(cylinder('drive_shaft',0,dy,-.10,.027,width+.45,STEEL,pitch=math.pi/2))
    for x in [-.65,.65]:
        v.extend([box(f'bearing{x}',x,dy,-.105,.13,.15,.13,DARK),cylinder(f'coupling{x}',x,dy,-.10,.06,.12,STEEL,pitch=math.pi/2)])
    v.extend([box('gearbox',-.88,dy,-.13,.28,.25,.24,STEEL),cylinder('drive_motor',-.96,dy+.22,-.10,.09,.32,'.30 .35 .38 1',roll=math.pi/2)])
    for k in range(5):v.append(cylinder(f'motor_fin{k}',-.96,dy+.10+k*.045,-.10,.105,.018,STEEL,roll=math.pi/2))
    if legs:
        h=max(.06,height-.20)
        for x in [-.57,.57]:
            for y in [-half+.24,half-.24]:
                v.extend([box(f'leg{x}{y}',x,y,-.20-h/2,.075,.075,h,STEEL),box(f'foot{x}{y}',x,y,-height+.012,.18,.16,.025,DARK)])
    for y in [-half+.12,half-.12]:
        for x in [-.61,.61]:v.append(box(f'sensor{x}{y}',x,y,.045,.07,.07,.08,DARK))
    if axis=='x':
        rotated=[]
        for item in v:
            e=ET.fromstring(item);p=e.find('pose');x,y,z,r,pt,yaw=map(float,p.text.split())
            p.text=f'{y} {-x} {z} {r} {pt} {yaw-math.pi/2}'
            rotated.append(ET.tostring(e,encoding='unicode'))
        v=rotated
    if BATCH_BOXES is not None:
        import hashlib
        v=BATCH_BOXES('chain_'+hashlib.sha1(repr(key).encode()).hexdigest()[:12],v)
    _CHAIN_CACHE[key]=tuple(v)
    return list(v)

_WHEEL_CACHE={}
def shuttle_wheels(axis,box,cylinder):
    if BATCH_BOXES is not None and axis in _WHEEL_CACHE: return list(_WHEEL_CACHE[axis])
    v=[]
    for x in [-.48,.48]:
        for y in [-.49,.49]:
            roll=math.pi/2 if axis=='x' else 0;pitch=math.pi/2 if axis=='y' else 0
            v.extend([cylinder(f'tyre{x}{y}',x,y,-.16,.105,.08,DARK,roll,pitch),
                      cylinder(f'flange{x}{y}',x,y,-.16,.105,.025,STEEL,roll,pitch),
                      cylinder(f'hub{x}{y}',x+(math.copysign(.05,x) if axis=='y' else 0),y+(math.copysign(.05,y) if axis=='x' else 0),-.16,.052,.018,STEEL,roll,pitch)])
    if BATCH_BOXES is not None:
        _WHEEL_CACHE[axis]=tuple(BATCH_BOXES(f'shuttle_wheels_{axis}',v))
        return list(_WHEEL_CACHE[axis])
    return v

def tower(prefix,box,cylinder,strut):
    v=[];half=1.10
    for x in [-half,half]:
        for y in [-half,half]:
            # Three plates produce a visible channel-section upright.
            v.extend([box(f'upright_web{x}{y}',x,y,9,.075,.16,18,STEEL),box(f'upright_flangeA{x}{y}',x-.07,y,9,.10,.035,18,STEEL),box(f'upright_flangeB{x}{y}',x+.07,y,9,.10,.035,18,STEEL),box(f'base{x}{y}',x,y,.055,.40,.40,.11,STEEL)])
            for dx in [-.13,.13]:
                for dy in [-.13,.13]: v.append(cylinder(f'anchor{x}{y}{dx}{dy}',x+dx,y+dy,.125,.022,.04,DARK))
        v.append(box(f'guide_rail{x}',x,0,9,.06,.10,18,'.36 .40 .43 1'))
        v.append(box(f'guide_face{x}',x-.04*math.copysign(1,x),0,9,.02,.075,18,STEEL))
        # Schematic dual-chain lift transmission, kept clear of the transfer faces.
        for y in [.72,.78]:
            v.append(box(f'chain_run{x}{y}',x*.88,y,9,.035,.022,17.8,DARK))
        for z in [.23,17.85]:
            v.extend([cylinder(f'pulley{x}{z}',x*.88,.75,z,.18,.12,DARK,math.pi/2),cylinder(f'pulley_hub{x}{z}',x*.88,.68,z,.075,.025,STEEL,math.pi/2)])
    for k,z in enumerate([.16,2.15,4.15,6.15,8.15,10.15,12.15,14.15,16.15,17.98]):
        for x in [-half,half]:
            v.append(box(f'side_tie{k}{x}',x,0,z,.12,2.3,.12,BLUE))
            if k<9: v.append(strut(f'diagonal{k}{x}',(x,-1.05,z+.18),(x,1.05,min(z+1.75,17.9)),.055,STEEL))
    for y in [-1.1,1.1]: v.append(box(f'top_header{y}',0,y,18.02,2.36,.18,.20,STEEL))
    v.extend([box('drive_gearbox',0,.78,18.22,.48,.40,.32,BLUE),cylinder('motor',.43,.78,18.22,.16,.5,DARK,pitch=math.pi/2),cylinder('cross_shaft',0,.75,17.85,.045,2,STEEL,pitch=math.pi/2)])
    for q in range(5): v.append(cylinder(f'motor_fin{q}',.25+q*.075,.78,18.22,.17,.025,STEEL,pitch=math.pi/2))
    v.extend([box('control_cabinet',1.62,-.55,.76,.52,.42,1.45,WHITE),box('cabinet_door',1.62,-.77,.76,.46,.025,1.32,STEEL),box('hmi',1.62,-.79,1.15,.23,.02,.15,DARK),box('hmi_screen',1.62,-.805,1.15,.18,.008,.10,'.10 .35 .48 1'),cylinder('beacon',1.62,-.55,1.57,.055,.14,YELLOW)])
    for i,color in enumerate(['.16 .7 .3 1','.95 .2 .08 1']):v.append(box(f'button{i}',1.55+i*.14,-.80,.93,.045,.015,.045,color))
    ident=int(prefix[-1])+1+(2 if prefix.startswith('out') else 0)
    v.extend([box('lift_id_plate',0,-1.21,17.3,.86,.035,.50,BLUE),box('lift_id_background',0,-1.235,17.3,.70,.01,.39,WHITE),*digits(box,'lift_id',f'{ident:02}',0,-1.245,17.3,.14)])
    return BATCH_BOXES(f'tower_{prefix}',v) if BATCH_BOXES is not None else v

_CABIN_CACHE=None
def cabin(box,cylinder,roller_deck):
    global _CABIN_CACHE
    if BATCH_BOXES is not None and _CABIN_CACHE is not None: return list(_CABIN_CACHE)
    v=roller_deck('cabin')
    for x in [-.94,.94]:
        for y in [-.91,.91]:v.append(box(f'cabin_post{x}{y}',x,y,.91,.085,.085,2.02,STEEL))
        v.append(box(f'base_side{x}',x,0,-.43,.13,2,.22,ORANGE))
        v.append(box(f'top_side{x}',x,0,1.94,.10,1.92,.10,STEEL))
        for z in [.25,1.6]:
            v.append(box(f'guide_bracket{x}{z}',x,0,z,.2,.22,.15,BLUE))
            v.append(cylinder(f'guide_roller{x}{z}',x*1.10,0,z,.075,.05,DARK,pitch=math.pi/2))
    for y in [-.91,.91]:
        v.extend([box(f'base_cross{y}',0,y,-.43,1.88,.12,.22,ORANGE),box(f'top_cross{y}',0,y,1.94,1.96,.10,.10,STEEL)])
    for x in [-.78,.78]:
        v.append(box(f'side_guard{x}',x,0,.14,.045,1.5,.22,STEEL))
        for y in [-.62,.62]:v.append(box(f'photoeye{x}{y}',x,y,.24,.08,.08,.08,DARK))
    v.append(box('cabin_base',0,0,-.48,1.8,1.8,.12,BLUE))
    if BATCH_BOXES is not None:
        _CABIN_CACHE=tuple(BATCH_BOXES('lift_cabin',v))
        return list(_CABIN_CACHE)
    return v

_CW_CACHE=None
def counterweight(box):
    global _CW_CACHE
    if BATCH_BOXES is not None and _CW_CACHE is not None: return list(_CW_CACHE)
    v=[box('weight_stack',0,0,0,.18,.64,1.05,'.28 .31 .34 1')]+[box(f'weight_seam{k}',.098,0,-.45+k*.15,.01,.60,.012,DARK) for k in range(7)]
    if BATCH_BOXES is not None:
        _CW_CACHE=tuple(BATCH_BOXES('lift_cw',v))
        return list(_CW_CACHE)
    return v
