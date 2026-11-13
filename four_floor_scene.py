"""4-floor picking building (west of the 4-way store) built from four_floor_layout.json.

Floors 2-4 repeat the floor-1 CAD plan, except items that exist on floor 1 only (agreed):
the loose-goods area with the carton inbound gate (incline 'Bang tai tang 1', carton belt,
weighing / barcode / profile check, 2 operators) and the full-case palletising area (4 pallet spots,
1 operator). Floor elevations come from config.json output_levels (4.5 m: the spirals rise
H=0.6 m -> 5.1 m, i.e. they arrive at belt height 0.6 m on the next floor).

Spiral conveyors (CAD): In on the left tangent (x = centre - 1.3) fed through a 90-degree curve from
the cross belt *U752 (y ~ 5.06); Out on the right tangent (x = centre + 1.3) into *U748 and the
sorting belts. Spiral k->k+1 has its In on floor k and its Out on floor k+1, so each floor only gets
the connections that really exist there. On floors 2-4 the cross belt *U752 takes part of the Out
flow of the spiral arriving from below (roller transfer at the crossing) to the next spiral's In.

Static content is batched into shared meshes. AGVs, carried racks and F4s are batched too for the
layout review; build with --movable to make them individual models for the process step.

Dimensions
* Hikrobot Q3-600D latent AGV : 950 x 650 x 250 mm, lift platform 850 x 600 mm (datasheet).
* Hikrobot F4-1000 forklift    : 1672 x 990 x 1915 mm, forks 1200 x 180 x 65 mm, outer width 680 mm.
* Rack 1200 x 800 x 1700 mm, 4 shelves x 2 plastic trays 580 x 415 x 260 mm (8 trays / rack).
Assumptions not drawn in the 2D CAD are marked "ASSUMPTION".
"""
import json
import math
import sys
from pathlib import Path
from sdf_parts import box, merge_boxes

MOVABLE = '--movable' in sys.argv
ROOT = Path(__file__).resolve().parent
C = json.loads((ROOT / 'config.json').read_text())
L = json.loads((ROOT / 'four_floor_layout.json').read_text(encoding='utf-8'))
LEVELS = C['output_levels']                  # floor elevations (m)
FLOOR_H = LEVELS[1] - LEVELS[0]
BELT_Z = .6                                  # CAD: "Bang tai Belt H=0.6m"
SPIRAL_TANGENT = 1.3                         # In/Out tangents at centre -/+ 1.3 m (CAD)
CURVE_R = .96                                # 90-degree In curve, centreline radius (CAD)
CURVE_W = .77
FEEDER_Y = (4.5, 5.6)                        # *U752 cross-belt band (CAD y 4.674 .. 5.445)
SPIRAL_TURNS = 4.5                           # ASSUMPTION: 1.0 m pitch over the 4.5 m rise

CONCRETE = '.62 .63 .64 1'; WALL = '.20 .22 .26 1'; DOOR = '.25 .45 .75 1'; FIXTURE = '.52 .55 .58 1'
ROUTE = '.98 .62 .05 1'; TAPE_Y = '.95 .80 .05 1'; TAPE_G = '.10 .70 .20 1'
RACK_POST = '.28 .31 .35 1'; RACK_BEAM = '.93 .45 .10 1'; TRAY = '.05 .30 .78 1'; CARTON = '.82 .68 .44 1'
AGV_BODY = '.95 .45 .08 1'; AGV_TOP = '.12 .13 .15 1'; AGV_PLATE = '.30 .32 .35 1'
F4_BODY = '.66 .68 .70 1'; F4_DARK = '.14 .15 .17 1'; F4_FORK = '.36 .37 .40 1'
SHIRT = '.10 .38 .72 1'; PANTS = '.15 .16 .20 1'; SKIN = '.86 .67 .52 1'; HAIR = '.10 .08 .07 1'
DESK = '.20 .40 .75 1'; DESK_TOP = '.86 .88 .90 1'; BIN = '.10 .48 .88 1'; SCREEN = '.06 .07 .09 1'
FRAME = '.55 .58 .61 1'; BELT = '.13 .13 .14 1'; ROLLER = '.78 .80 .82 1'; LEG = '.42 .45 .48 1'; RED = '.85 .12 .10 1'
SLAB = '.80 .82 .84'

_n = [0]
def nm(p):
    _n[0] += 1
    return f'{p}{_n[0]}'

def cx(r): return (r[0] + r[2]) / 2
def cy(r): return (r[1] + r[3]) / 2

def place(parts, x, y, yaw, z=0.):
    c, s = math.cos(yaw), math.sin(yaw)
    return [box(nm('p'), x + px * c - py * s, y + px * s + py * c, z + pz, sx, sy, sz, col, yaw + pyaw)
            for px, py, pz, sx, sy, sz, col, pyaw in parts]

def rect_box(r, z0, z1, col, inset=0.):
    return box(nm('r'), cx(r), cy(r), (z0 + z1) / 2, max(.005, r[2] - r[0] - 2 * inset), max(.005, r[3] - r[1] - 2 * inset), z1 - z0, col)

def line_box(s, w, h, col, z0=0.):
    x0, y0, x1, y1 = s; ln = math.hypot(x1 - x0, y1 - y0)
    return box(nm('l'), (x0 + x1) / 2, (y0 + y1) / 2, z0 + h / 2, ln + w, w, h, col, math.atan2(y1 - y0, x1 - x0))

def tilted(x, y, z, sx, sy, sz, col, yaw, pitch):
    return box(nm('t'), x, y, z, sx, sy, sz, col, yaw).replace(f'0 0 {yaw}</pose>', f'0 {pitch} {yaw}</pose>')

# ---------------------------------------------------------------- part libraries (local frames)
def hollow_tray_parts(tx, z, col=TRAY):
    """5-sided open-top hollow blue plastic tote (580 x 415 x 260 mm) at (tx, 0) on shelf height z."""
    wx, wy, h, tw, tb = .58, .415, .26, .022, .020
    return [
        (tx, 0., z + tb / 2, wx, wy, tb, col, 0),
        (tx, -(wy - tw) / 2, z + h / 2, wx, tw, h, col, 0),
        (tx, +(wy - tw) / 2, z + h / 2, wx, tw, h, col, 0),
        (tx - (wx - tw) / 2, 0., z + h / 2, tw, wy - 2 * tw, h, col, 0),
        (tx + (wx - tw) / 2, 0., z + h / 2, tw, wy - 2 * tw, h, col, 0),
    ]

def rack_parts(hollow_trays=False):
    p = []
    for sx in (-1, 1):
        for sy in (-1, 1): p.append((sx * .58, sy * .38, .85, .04, .04, 1.7, RACK_POST, 0))
    for z in (.40, .74, 1.08, 1.42):
        for sy in (-1, 1): p.append((0, sy * .38, z - .025, 1.2, .04, .05, RACK_BEAM, 0))
        p.append((0, 0, z - .0075, 1.16, .76, .015, RACK_POST, 0))
        for tx in (-.295, .295):
            if hollow_trays:
                p.extend(hollow_tray_parts(tx, z))
            else:
                p.append((tx, 0, z + .13, .58, .415, .26, TRAY, 0))
                p.append((tx, 0, z + .264, .52, .36, .008, CARTON, 0))
    for sy in (-1, 1): p.append((0, sy * .38, 1.675, 1.2, .04, .05, RACK_BEAM, 0))
    return p

def u1_rack_parts():
    """U1 inbound rack: 4-shelf rack with hollow open-top blue plastic trays (580x415x260 mm)."""
    return rack_parts(hollow_trays=True)

def p1_rack_parts():
    """P1 picking rack: hollow blue plastic trays with pre-stocked yellow medicine boxes on the lower shelves."""
    p = rack_parts(hollow_trays=True)
    med_col = '.95 .76 .08 1'; strip_col = '.12 .42 .78 1'; cap_col = '.96 .97 .98 1'
    for z in (.40, .74, 1.08):
        for tx in (-.295, .295):
            for m in range(3):
                mx = tx + (m - 1) * .17
                p.append((mx, 0, z + .095, .14, .26, .15, med_col, 0))
                p.append((mx, 0, z + .172, .142, .262, .006, cap_col, 0))
                p.append((mx, -.131, z + .105, .11, .004, .045, strip_col, 0))
    return p

def trolley_parts(with_cartons=False):
    """Wire-mesh empty-carton trolley (1200 x 800 x 1700 mm): blue base deck at 300 mm, 4 caster wheels,
    and 4-sided steel wire-mesh cage up to 1700 mm."""
    p = [
        (0, 0, .27, 1.20, .80, .06, TRAY, 0),
        (0, -.385, .315, 1.20, .03, .03, TRAY, 0),
        (0, +.385, .315, 1.20, .03, .03, TRAY, 0),
        (-.585, 0, .315, .03, .74, .03, TRAY, 0),
        (+.585, 0, .315, .03, .74, .03, TRAY, 0),
    ]
    for sx in (-1, 1):
        for sy in (-1, 1):
            p.append((sx * .48, sy * .30, .185, .08, .08, .11, ROLLER, 0))
            p.append((sx * .48, sy * .30, .08, .16, .06, .16, BELT, 0))
            p.append((sx * .58, sy * .38, 1.00, .035, .035, 1.40, ROLLER, 0))
        p.append((0, sx * .38, 1.00, .025, .025, 1.40, ROLLER, 0))
    for z in (.45, .65, .85, 1.05, 1.25, 1.45, 1.68):
        for sy in (-1, 1):
            p.append((0, sy * .385, z, 1.18, .012, .012, ROLLER, 0))
        for sx in (-1, 1):
            p.append((sx * .585, 0, z, .012, .78, .012, ROLLER, 0))
    for x in (-.44, -.29, -.14, .14, .29, .44):
        for sy in (-1, 1):
            p.append((x, sy * .385, 1.00, .010, .010, 1.38, ROLLER, 0))
    for y in (-.24, -.08, .08, .24):
        for sx in (-1, 1):
            p.append((sx * .585, y, 1.00, .010, .010, 1.38, ROLLER, 0))
    if with_cartons:
        slots = ((-.24, 0., .425), (.24, 0., .425), (-.24, 0., .675), (.24, 0., .675), (0., 0., .925))
        for lx, ly, lz in slots:
            p.append((lx, ly, lz, .40, .30, .246, CARTON, 0))
            p.append((lx, ly, lz + .122, .06, .302, .006, '.9 .8 .57 1', 0))
    return p

def agv_parts():
    return [(0, 0, .0975, .95, .65, .145, AGV_BODY, 0), (0, 0, .2075, .95, .65, .075, AGV_TOP, 0),
            (0, 0, .25, .85, .60, .01, AGV_PLATE, 0), (.476, 0, .11, .01, .32, .04, AGV_TOP, 0),
            (-.476, 0, .11, .01, .32, .04, AGV_TOP, 0)]

def f4_parts():   # origin at the body/fork junction, forks along +x
    return [(-.236, 0, .60, .472, .99, 1.20, F4_BODY, 0), (-.236, 0, 1.25, .40, .90, .10, F4_DARK, 0),
            (-.236, 0, 1.0, .48, 1.0, .06, AGV_BODY, 0),
            (.03, -.30, .9575, .08, .08, 1.915, F4_DARK, 0), (.03, .30, .9575, .08, .08, 1.915, F4_DARK, 0),
            (.03, 0, 1.88, .08, .68, .07, F4_DARK, 0), (.06, 0, .40, .05, .68, .60, F4_DARK, 0),
            (.66, -.25, .1135, 1.2, .18, .065, F4_FORK, 0), (.66, .25, .1135, 1.2, .18, .065, F4_FORK, 0),
            (-.30, .42, 1.35, .05, .18, .14, SCREEN, 0)]

def person_parts():  # ~1.68 m operator facing +x
    return [(0, -.09, .43, .13, .14, .86, PANTS, 0), (0, .09, .43, .13, .14, .86, PANTS, 0),
            (0, 0, 1.16, .24, .42, .62, SHIRT, 0), (.02, -.25, 1.15, .10, .09, .56, SHIRT, 0),
            (.02, .25, 1.15, .10, .09, .56, SHIRT, 0), (.01, 0, 1.585, .20, .18, .22, SKIN, 0),
            (-.02, 0, 1.70, .21, .19, .05, HAIR, 0)]

# ---------------------------------------------------------------- conveyors (top of belt / rollers = 0.6 m)
def belt(r, axis, z0=0., legs=True):
    v = [rect_box(r, z0 + BELT_Z - .12, z0 + BELT_Z - .005, FRAME), rect_box(r, z0 + BELT_Z - .005, z0 + BELT_Z, BELT, .03)]
    if legs:
        ln = (r[2] - r[0]) if axis == 'x' else (r[3] - r[1]); n = max(2, math.ceil(ln / 2) + 1)
        for k in range(n):
            t = min(.1, ln / 4) + (ln - 2 * min(.1, ln / 4)) * k / (n - 1)
            for side in (0, 1):
                if axis == 'x': x, y = r[0] + t, (r[1] + .05 if side == 0 else r[3] - .05)
                else: x, y = (r[0] + .05 if side == 0 else r[2] - .05), r[1] + t
                v.append(box(nm('lg'), x, y, z0 + (BELT_Z - .12) / 2, .05, .05, BELT_Z - .12, LEG))
    return v

def roller_module(t, flow_axis, z0=0.):
    """Right-angle roller transfer module, rollers flush with the belt top (0.6 m)."""
    v = [rect_box(t, z0 + BELT_Z - .12, z0 + BELT_Z - .045, FRAME), box(nm('rl'), cx(t), cy(t), z0 + (BELT_Z - .12) / 2, .08, .08, BELT_Z - .12, LEG)]
    ln = (t[2] - t[0]) if flow_axis == 'x' else (t[3] - t[1]); wd = (t[3] - t[1]) if flow_axis == 'x' else (t[2] - t[0])
    n = max(3, int(ln / .075))
    for k in range(n):
        u = (k + .5) / n * ln
        x, y = (t[0] + u, cy(t)) if flow_axis == 'x' else (cx(t), t[1] + u)
        sx, sy = (.045, wd - .04) if flow_axis == 'x' else (wd - .04, .045)
        v.append(box(nm('ro'), x, y, z0 + BELT_Z - .0225, sx, sy, .045, ROLLER))
    return v

def overlap(a, b):
    return max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))

def conveyor_set(belts, transfers, z0=0.):
    """Belts are cut where a roller module sits so the two never overlap; modules take the belt's flow axis."""
    v = []
    for r, axis in belts:
        lo, hi = (0, 2) if axis == 'x' else (1, 3)
        cuts = sorted((t[lo], t[hi]) for t in transfers if overlap(r, t) > 1e-4)
        start = r[lo]
        for a, b in cuts + [(r[hi], r[hi])]:
            if a - start > .05:
                seg = list(r); seg[lo], seg[hi] = start, a
                v += belt(seg, axis, z0)
            start = max(start, b)
    for t in transfers:
        hosts = [(overlap(r, t), axis) for r, axis in belts if overlap(r, t) > 1e-4]
        v += roller_module(t, max(hosts)[1] if hosts else 'x', z0)
    return v

INCLINE_TOP = 3.0   # CAD: 'Bang tai tang 1 H=0.6m -> 3m -> 0.6m'

def incline_profile(r):
    """(y, belt-top z) breakpoints of the floor-1 incline at x = centre of r.

    It must be at 0.6 m on both roller modules on its line: the one taking cartons from the carton belt
    (y ~ -0.75) and the one diverting to the spiral feeder (y ~ 5.06). ASSUMPTION: between them it
    rises / runs level / falls in equal thirds (the CAD gives only the heights).
    """
    mods = sorted((t for t in L['transfers'] if abs(cx(t) - cx(r)) < .3), key=cy)
    lo = mods[0][3] + .05 if mods and cy(mods[0]) < FEEDER_Y[0] else r[1]
    hi = next((t[1] - .05 for t in mods if FEEDER_Y[0] < cy(t) < FEEDER_Y[1]), r[3])
    third = (hi - lo) / 3
    return [(r[1], BELT_Z), (lo, BELT_Z), (lo + third, INCLINE_TOP), (hi - third, INCLINE_TOP), (hi, BELT_Z), (r[3], BELT_Z)]

def incline(r):
    x = cx(r); w = r[2] - r[0]; prof = incline_profile(r)
    v = []
    for (ya, za), (yb, zb) in zip(prof, prof[1:]):
        if yb - ya < .01: continue
        seg = math.hypot(yb - ya, zb - za); pitch = -math.atan2(zb - za, yb - ya)
        v.append(tilted(x, (ya + yb) / 2, (za + zb) / 2 - .06, seg, w, .12, FRAME, math.pi / 2, pitch))
        v.append(tilted(x, (ya + yb) / 2, (za + zb) / 2 + .003, seg, w - .06, .006, BELT, math.pi / 2, pitch))
        if za > BELT_Z + .01 and zb > BELT_Z + .01:   # supports under the raised level run
            for yy in (ya, yb): v.append(box(nm('il'), x, yy, (za - .12) / 2, .08, .08, za - .12, LEG))
    return v

def spiral(sp):
    """Absolute-frame spiral: In curve + In tangent on floor 'from', CCW helix, Out tangent on floor 'to'."""
    v = []
    ri, ro = sp['r_inner'], sp['r_outer']; rc = (ri + ro) / 2; w = ro - ri
    zb, zt = LEVELS[sp['from_floor'] - 1], LEVELS[sp['to_floor'] - 1]
    x_in, x_out = sp['x'] - SPIRAL_TANGENT, sp['x'] + SPIRAL_TANGENT
    curve_c = (x_in + CURVE_R, cy(L['_feeder']) - CURVE_R)   # quarter turn from the tangent up to the *U752 axis
    y_tan = curve_c[1]
    # In: curve from the cross belt down into the left tangent, then the tangent to the ring (floor 'from').
    n = 10
    for k in range(n):
        a0 = math.pi / 2 + (k / n) * math.pi / 2; a1 = math.pi / 2 + ((k + 1) / n) * math.pi / 2; am = (a0 + a1) / 2
        ch = 2 * CURVE_R * math.sin((a1 - a0) / 2) * 1.04
        px, py = curve_c[0] + CURVE_R * math.cos(am), curve_c[1] + CURVE_R * math.sin(am)
        v.append(box(nm('cf'), px, py, zb + BELT_Z - .0625, ch, CURVE_W, .115, FRAME, am + math.pi / 2))
        v.append(box(nm('cb'), px, py, zb + BELT_Z - .0025, ch, CURVE_W - .06, .005, BELT, am + math.pi / 2))
    v += belt([x_in - w / 2, sp['y'], x_in + w / 2, y_tan], 'y', zb)
    # Helix: starts at angle pi heading -y (CCW), ends at angle 0 heading +y.
    total = SPIRAL_TURNS * 2 * math.pi; n = int(40 * SPIRAL_TURNS)
    z0, z1 = zb + BELT_Z, zt + BELT_Z
    arc = total * rc / n; pitch = -math.atan2((z1 - z0) / n, arc)
    for k in range(n):
        t = (k + .5) / n; ang = math.pi + total * t; z = z0 + (z1 - z0) * t
        x, y = sp['x'] + rc * math.cos(ang), sp['y'] + rc * math.sin(ang); yaw = ang + math.pi / 2
        v.append(tilted(x, y, z - .06, arc * 1.06, w, .10, FRAME, yaw, pitch))
        v.append(tilted(x, y, z - .0025, arc * 1.06, w - .05, .005, BELT, yaw, pitch))
        for rr in (ri, ro):
            v.append(tilted(sp['x'] + rr * math.cos(ang), sp['y'] + rr * math.sin(ang), z + .05, arc * 1.06, .025, .08, TAPE_Y, yaw, pitch))
    # Out: right tangent from the ring to *U748 (floor 'to').
    out_end = min((c['rect'][1] for c in L['conveyors'] if c['block'] == '*U748' and abs(cx(c['rect']) - x_out) < .3), default=sp['y'] + 2.4)
    v += belt([x_out - w / 2, sp['y'], x_out + w / 2, out_end], 'y', zt)
    v.append(box(nm('sc'), sp['x'], sp['y'], (zb + z1 + .4) / 2, .3, .3, z1 + .4 - zb, LEG))
    for k in range(4):
        a = math.pi / 4 + k * math.pi / 2
        v.append(box(nm('sp'), sp['x'] + (ro + .12) * math.cos(a), sp['y'] + (ro + .12) * math.sin(a), (zb + z1) / 2, .07, .07, z1 - zb, LEG))
    return v

# ---------------------------------------------------------------- floor content
FLOOR1_ONLY_BLOCKS = {'*U753', '*U758', '*U757'}       # carton inbound line (floor 1)
SPIRAL_ZONE_BLOCKS = {'*U752', '*U748'}               # rebuilt per floor from the spiral In/Out
SORT_LOOP_BLOCKS = {'*U427', '*U429', '*U572'}        # two main sorting belts + cross pieces, rebuilt per floor
SORT_BAND_Y = (6.3, 10.0)                              # roller modules sitting on the main sorting belts
L['_feeder'] = next(c['rect'] for c in L['conveyors'] if c['block'] == '*U752')

def on_feeder(t): return FEEDER_Y[0] < cy(t) < FEEDER_Y[1]
def on_sort_loop(t): return SORT_BAND_Y[0] < cy(t) < SORT_BAND_Y[1]

def loop_end_x(f):
    """The CAD plan overlays every floor: the east end of the sorting loop is where that floor's goods enter it
    (floor 1: the carton incline; floor k: the Out of spiral k-1 -> k)."""
    if f == 1: return next(cx(c['rect']) for c in L['conveyors'] if c['block'] == '*U753')
    return next(s['x'] + SPIRAL_TANGENT for s in L['spirals'] if s['to_floor'] == f)

def sort_loop(f):
    branch_x = [cx(c['rect']) for c in L['conveyors'] if c['block'] == '*U632']
    ends = [loop_end_x(k) for k in range(1, len(LEVELS) + 1)]
    end = loop_end_x(f)
    def keep(x):   # a cross piece / module at another floor's loop end is a projection artefact
        return any(abs(x - b) < .3 for b in branch_x) or abs(x - end) < .3 or not any(abs(x - e) < .3 for e in ends)
    crosses = [c['rect'] for c in L['conveyors'] if c['block'] == '*U572' and keep(cx(c['rect'])) and cx(c['rect']) < end + .3]
    east = max(r[2] for r in crosses if abs(cx(r) - end) < .3)
    belts = [([c['rect'][0], c['rect'][1], min(c['rect'][2], east), c['rect'][3]], 'x') for c in L['conveyors'] if c['block'] in ('*U427', '*U429')]
    belts += [(r, 'y') for r in crosses]
    mods = [t for t in L['transfers'] if on_sort_loop(t) and keep(cx(t)) and cx(t) < east]
    return belts, mods

def carton_area(t): return cy(t) < FEEDER_Y[0] - .3   # roller modules on the floor-1 carton line

def people():
    agv_xy = [(a['x'], a['y']) for a in L['agvs']]
    out = [dict(w) for w in L['workers']]
    for z in L['unpack_zones']:
        spots = [s for s in L['pallet_spots'] if z[0] <= cx(s) <= z[2] and z[1] <= cy(s) <= z[3]]
        y = sum(cy(s) for s in spots) / len(spots) if spots else cy(z)
        out.append({'x': min(s[0] for s in spots) - .8 if spots else z[0] + .6, 'y': y, 'area': 'unpack', 'yaw': 0.})
    pick = [s for s in L['pallet_spots'] if cy(s) < 0]
    if pick:
        out.append({'x': sum(cx(s) for s in pick) / len(pick), 'y': max(s[3] for s in pick) + .9, 'area': 'pallet_pick', 'yaw': -math.pi / 2})
    for w in out:
        if 'yaw' not in w:
            near = min(agv_xy, key=lambda a: math.dist(a, (w['x'], w['y'])))
            w['yaw'] = math.atan2(near[1] - w['y'], near[0] - w['x']) if math.dist(near, (w['x'], w['y'])) < 2.5 else math.pi / 2
    return out

FLOOR1_AREAS = {'carton_inbound', 'pallet_pick'}
ANIMATED_WORKER_AREAS = {'carton_inbound', 'pallet_pick', 'sorting', 'unpack', 'put_away', 'picking_station'} if C.get('loose_pallet_spots') else set()
TROLLEY_CAPACITY = 5
K06_SLOTS_XY = [
    (-6.20, 14.895), (-4.80, 14.895), (-3.40, 14.895), (-2.00, 14.895),
    (-6.20, 12.695), (-4.80, 12.695), (-3.40, 12.695), (-2.00, 12.695),
]
U1_RACK_Y = 12.691              # CAD x1 rack station directly behind each sorting worker (y=11.981)
U1_BRANCHES = sorted((c['rect'] for c in L['conveyors'] if c['block'] == '*U632'), key=cx)
U1_RACKS_PER_BRANCH = 2         # 1 rack at U1 station + 1 replacement rack in Kho 04

def _u1_kho04_pool():
    c160 = sorted((c for c in L['rack_cells'] if round(c['y'], 3) == 16.056), key=lambda c: c['x'])
    c169 = sorted((c for c in L['rack_cells'] if round(c['y'], 3) == 16.951), key=lambda c: c['x'])
    c205 = sorted((c for c in L['rack_cells'] if round(c['y'], 3) == 20.561
                   and round(c['x'], 3) in (-32.269, -30.968, -29.667, -24.813)), key=lambda c: c['x'])
    return c160 + c169 + c205

_U1_POOL = _u1_kho04_pool()
U1_BRANCH_CELLS = {b: [_U1_POOL[b], _U1_POOL[6 + b]]
                   for b in range(len(U1_BRANCHES))}
U1_RESERVED_CELL_XY = {(round(c['x'], 3), round(c['y'], 3)) for cells in U1_BRANCH_CELLS.values() for c in cells}

# U2 put-away stations (6 stations per floor opposite Zone 05 unpack zones) -> Kho 04
U2_WORKERS = sorted((w for w in L['workers'] if w['area'] == 'put_away'), key=lambda w: -w['y'])
U2_RACK_X = -8.773
U2_RACKS_Y = [40.881, 36.121, 32.121, 28.121, 24.121, 17.959]
U2_CORRIDORS_Y = [40.881, 36.121, 32.121, 28.121, 24.121, 19.059]
U2_CELL_ROWS_Y = [41.879, 37.115, 33.123, 29.122, 25.122, 20.561]
U2_CELL_XS = (-13.105, -14.406)
U2_RACKS_PER_STATION = 2
U2_STATION_CELLS = {u2_idx: [next(c for c in L['rack_cells']
                                  if round(c['x'], 3) == rx and round(c['y'], 3) == U2_CELL_ROWS_Y[u2_idx])
                             for rx in U2_CELL_XS]
                    for u2_idx in range(len(U2_WORKERS))}
U2_RESERVED_CELL_XY = {(round(c['x'], 3), round(c['y'], 3)) for cells in U2_STATION_CELLS.values() for c in cells}

# P1 picking stations (14 stations per floor: 0..6 North row, 7..13 West row) <-> Kho 04 & Khu 06
P1_STATIONS = L['stations']
P1_RACKS_PER_STATION = 2
P1_CELL_MAP = {
    0: (42.782, (-32.269, -30.968), 44.191),
    1: (42.782, (-36.164, -34.863), 44.191),
    2: (42.782, (-40.067, -38.766), 44.191),
    3: (42.782, (-29.667, -24.813), 44.191),
    4: (42.782, (-23.512, -22.212), 44.191),
    5: (42.782, (-20.911, -19.610), 44.191),
    6: (42.782, (-17.008, -15.707), 44.191),
    7: (34.025, (-42.668, -41.367), 35.021),
    8: (38.025, (-42.668, -41.367), 39.021),
    9: (42.782, (-42.668, -41.367), 44.191),
    10: (30.025, (-40.067, -38.766), 31.021),
    11: (26.024, (-42.668, -41.367), 27.021),
    12: (21.455, (-42.668, -41.367), 22.464),
    13: (20.561, (-40.067, -38.766), 19.059),
}
P1_STATION_CELLS = {
    st_idx: [next(c for c in L['rack_cells'] if round(c['x'], 3) == rx and round(c['y'], 3) == cy_row) for rx in xs]
    for st_idx, (cy_row, xs, _) in P1_CELL_MAP.items()
}
P1_RESERVED_CELL_XY = {(round(c['x'], 3), round(c['y'], 3)) for cells in P1_STATION_CELLS.values() for c in cells}

def spot_marks(s):
    x0, y0, x1, y1 = s
    return [line_box(e, .05, .004, TAPE_G) for e in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0), (x0, y0, x1, y1), (x0, y1, x1, y0))]

def east_interface(s):
    """Wall / post on the east face towards the 4-way store, south part (y < 0): open on floor 1 (agreed)."""
    return min(s[0], s[2]) > -1.3 and max(s[1], s[3]) < .3

def floor_common():
    v = []
    for s in L['plan_lines'].get('wall', []):
        if not east_interface(s): v.append(line_box(s, .06, .05, WALL))
    for s in L['plan_lines'].get('door', []): v.append(line_box(s, .03, .02, DOOR))
    for s in L['plan_lines'].get('fixture', []): v.append(line_box(s, .02, .015, FIXTURE))
    for s in L['agv_paths']: v.append(line_box(s, .05, .004, ROUTE))
    for c in L['columns']: v.append(rect_box(c, 0, FLOOR_H - .15, CONCRETE))         # structural, 8.3 m grid
    for c in L.get('wall_posts', []):
        if not east_interface(c): v.append(rect_box(c, 0, FLOOR_H - .15, CONCRETE))
    for r in L['rack_cells']:
        if (round(r['x'], 3), round(r['y'], 3)) not in (U1_RESERVED_CELL_XY | U2_RESERVED_CELL_XY | P1_RESERVED_CELL_XY):
            v += place(rack_parts(), r['x'], r['y'], r['yaw'])
    for st in L['stations']:
        if st['desk']: v.append(rect_box(st['desk'], 0, .83, DESK)); v.append(rect_box(st['desk'], .83, .85, DESK_TOP))
        for c in st['desk_cells']: v.append(rect_box(c, .85, 1.07, BIN, .015))   # ASSUMPTION: bins 220 mm on an 850 mm desk
        if st.get('desk_ext'): v.append(rect_box(st['desk_ext'], 0, .85, DESK))
        if st['monitor']:
            m = st['monitor']
            v.append(box(nm('mp'), cx(m), cy(m), .6, .04, .04, 1.2, LEG)); v.append(rect_box(m, 1.2, 1.55, SCREEN))
        for q in st['posts']:
            if max(q[2] - q[0], q[3] - q[1]) > .4: v.append(rect_box(q, .95, 1.0, TAPE_Y))
            else: v.append(rect_box(q, 0, 1.1, TAPE_Y))
        for q in st['mark']: v.append(rect_box(q, 0, .004, TAPE_Y))
    for w in people():
        if w['area'] not in FLOOR1_AREAS and w['area'] not in ANIMATED_WORKER_AREAS:
            v += place(person_parts(), w['x'], w['y'], w['yaw'])
    for s in L['pallet_spots']:
        if cy(s) >= 0: v += spot_marks(s)
    for s_idx, (kx, ky) in enumerate(K06_SLOTS_XY):
        v += spot_marks([kx - .62, ky - .42, kx + .62, ky + .42])
        v += place(trolley_parts(with_cartons=(s_idx >= 4)), kx, ky, 0.)
    for z in L['unpack_zones']:
        x0, y0, x1, y1 = z
        for e in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)): v.append(line_box(e, .06, .004, TAPE_Y))
    belts = [(c['rect'], c['axis']) for c in L['conveyors'] if c['block'] not in FLOOR1_ONLY_BLOCKS | SPIRAL_ZONE_BLOCKS | SORT_LOOP_BLOCKS]
    v += conveyor_set(belts, [t for t in L['transfers'] if not on_feeder(t) and not carton_area(t) and not on_sort_loop(t)])
    if not MOVABLE:
        for a in L['agvs']:
            if a.get('source') in ('x1', 'station', 'g5'):
                continue
            v += place(agv_parts(), a['x'], a['y'], a['yaw'])
            if a['rack']: v += place(rack_parts(), a['x'], a['y'], a['yaw'])
        fl = L['forklift']; v += place(f4_parts(), fl['fork_tip_x'] - 1.2, fl['y'], fl['yaw'])
    for d in L['devices']:
        if d['kind'] == 'port_gate': v.append(rect_box(d['rect'], 0, 1.8, RED))
    return v

def floor_extra(f):
    """Per-floor spiral connections; floor 1 also gets the carton inbound line and the palletising area."""
    v = []
    if f > 1:   # floors 2-4 keep the east wall that is open on floor 1
        v += [line_box(s, .06, .05, WALL) for s in L['plan_lines'].get('wall', []) if east_interface(s)]
        v += [rect_box(c, 0, FLOOR_H - .15, CONCRETE) for c in L.get('wall_posts', []) if east_interface(c)]
    sp_out = next((s for s in L['spirals'] if s['to_floor'] == f), None)
    sp_in = next((s for s in L['spirals'] if s['from_floor'] == f), None)
    feeder = L['_feeder']; belts = []; trs = []
    if sp_out:
        x_out = sp_out['x'] + SPIRAL_TANGENT
        belts += [(c['rect'], 'y') for c in L['conveyors'] if c['block'] == '*U748' and abs(cx(c['rect']) - x_out) < .3]
        split = next((t for t in L['transfers'] if on_feeder(t) and abs(cx(t) - x_out) < .3), None)
        if sp_in and split: trs.append(split)       # part of the arriving flow diverted to the next spiral
    if sp_in:
        left = sp_in['x'] - SPIRAL_TANGENT + CURVE_R
        if sp_out and trs: right = trs[0][0]
        else: right = min((c['rect'][0] for c in L['conveyors'] if c['block'] == '*U757'), default=feeder[2])
        belts.append(([left, feeder[1], right, feeder[3]], 'x'))
    if f == 1:
        belts += [(c['rect'], 'x' if c['block'] != '*U753' else 'y') for c in L['conveyors'] if c['block'] in ('*U757', '*U758')]
        trs += [t for t in L['transfers'] if carton_area(t) or (on_feeder(t) and not any(
            abs(cx(t) - s['x'] - SPIRAL_TANGENT) < .3 for s in L['spirals']))]
        for c in L['conveyors']:
            if c['block'] == '*U753': v += incline(c['rect'])
        for w in people():
            if w['area'] in FLOOR1_AREAS and w['area'] not in ANIMATED_WORKER_AREAS: v += place(person_parts(), w['x'], w['y'], w['yaw'])
        for s in L['pallet_spots']:
            if cy(s) < 0: v += spot_marks(s)
        for x, y, yaw in C.get('loose_pallet_spots', []):   # loose-goods unpack spots (agreed, floor 1)
            hx, hy = (.6, .5) if abs(math.sin(yaw)) < .5 else (.5, .6)
            v += spot_marks([x - hx, y - hy, x + hx, y + hy])
        for d in L['devices']:
            r = d['rect']
            if d['kind'] == 'weighing': v.append(rect_box(r, BELT_Z - .15, BELT_Z + .02, '.25 .27 .30 1'))
            elif d['kind'] == 'barcode_reader':
                v.append(box(nm('bp'), cx(r), cy(r), .65, .05, .05, 1.3, LEG)); v.append(box(nm('bh'), cx(r), cy(r), 1.33, .12, .2, .1, SCREEN))
            elif d['kind'] == 'profile_check':
                lng = 'y' if r[3] - r[1] > r[2] - r[0] else 'x'
                for k in (0, 1):
                    px = cx(r) if lng == 'y' else (r[0] if k == 0 else r[2]); py = (r[1] if k == 0 else r[3]) if lng == 'y' else cy(r)
                    v.append(box(nm('pc'), px, py, .7, .06, .06, 1.4, '.70 .70 .72 1'))
                v.append(box(nm('pt'), cx(r), cy(r), 1.42, (r[2] - r[0]) if lng == 'x' else .08, (r[3] - r[1]) if lng == 'y' else .08, .06, '.70 .70 .72 1'))
    loop_belts, loop_mods = sort_loop(f)
    v += conveyor_set(belts + loop_belts, trs + loop_mods)
    return v

# ---------------------------------------------------------------- world assembly
def model(name, pose, visuals, static=True):
    x, y, z, yaw = pose
    return (f'<model name="{name}"><static>{"true" if static else "false"}</static><pose>{x} {y} {z} 0 0 {yaw}</pose>'
            f'<link name="body">{"".join(visuals)}</link></model>')

def p1_trolley_xy_yaw(st_idx):
    st = P1_STATIONS[st_idx]
    mx = (min(q[0] for q in st['mark']) + max(q[2] for q in st['mark'])) / 2
    my = (min(q[1] for q in st['mark']) + max(q[3] for q in st['mark'])) / 2
    if st_idx < 7:
        return (mx + .35, my + .06, math.pi / 2)
    return (mx - .05, my - .35, 0.)

def models():
    out = []
    common = merge_boxes('ff_floor', floor_common())
    u1_agv_mesh = merge_boxes('ff_u1_agv', place(agv_parts(), 0, 0, 0))
    u1_rack_mesh = merge_boxes('ff_u1_rack', place(u1_rack_parts(), 0, 0, 0))
    p1_rack_mesh = merge_boxes('ff_p1_rack', place(p1_rack_parts(), 0, 0, 0))
    unpack_rack_mesh = merge_boxes('ff_unpack_rack', place(rack_parts(), 0, 0, 0))
    trolley_empty_mesh = merge_boxes('ff_trolley_empty', place(trolley_parts(with_cartons=False), 0, 0, 0))
    if MOVABLE:
        agv_mesh = merge_boxes('ff_agv', place(agv_parts(), 0, 0, 0))
        rack_mesh = unpack_rack_mesh
        f4_mesh = merge_boxes('ff_f4', place(f4_parts(), 0, 0, 0))
    b = L['building']
    for k, zf in enumerate(LEVELS):
        f = k + 1
        out.append(model(f'ff{f}_static', (0, 0, zf, 0), common))
        extra = floor_extra(f)
        if extra: out.append(model(f'ff{f}_extra', (0, 0, zf, 0), merge_boxes(f'ff_extra{f}', extra)))
        if k == 0:
            out.append(model('ff1_floor', (0, 0, 0, 0), [rect_box(b, -.012, -.002, '.83 .84 .85 1')]))
        else:
            out.append(model(f'ff{f}_slab', (0, 0, 0, 0), [rect_box(b, zf - .15, zf - .012, SLAB + ' 1').replace(
                '</material>', '</material><transparency>0.7</transparency>')]))
        if MOVABLE:
            for i, a in enumerate(L['agvs']):
                out.append(model(f'ff{f}_agv_{i}', (a['x'], a['y'], zf, a['yaw']), agv_mesh, static=False))
                if a['rack']: out.append(model(f'ff{f}_agvrack_{i}', (a['x'], a['y'], zf, a['yaw']), rack_mesh, static=False))
            fl = L['forklift']
            out.append(model(f'ff{f}_f4', (fl['fork_tip_x'] - 1.2, fl['y'], zf, fl['yaw']), f4_mesh, static=False))
        # U1 sorting-branch stations: 1 AGV + 1 active rack + 1 spare rack in Kho 04 + 1 empty trolley beside worker.
        for br_idx, br_rect in enumerate(U1_BRANCHES):
            bx = cx(br_rect)
            out.append(model(f'u1_agv_{f}_{br_idx}', (bx, U1_RACK_Y, zf, math.pi / 2), u1_agv_mesh, static=True))
            out.append(model(f'u1_rack_{f}_{br_idx}_0', (bx, U1_RACK_Y, zf, 0.), u1_rack_mesh, static=True))
            for r_idx in range(1, U1_RACKS_PER_BRANCH):
                cell = U1_BRANCH_CELLS[br_idx][r_idx]
                out.append(model(f'u1_rack_{f}_{br_idx}_{r_idx}', (cell['x'], cell['y'], zf, cell.get('yaw', 0.)), u1_rack_mesh, static=True))
            out.append(model(f'u1_trolley_{f}_{br_idx}', (bx + .95, 12.05, zf, 0.), trolley_empty_mesh, static=True))
        # U2 put-away stations opposite Zone 05: 1 AGV + 1 active rack + 1 spare rack in Kho 04 + 1 empty trolley beside worker.
        for u2_idx, ry in enumerate(U2_RACKS_Y):
            out.append(model(f'u2_agv_{f}_{u2_idx}', (U2_RACK_X, ry, zf, 0.), u1_agv_mesh, static=True))
            out.append(model(f'u2_rack_{f}_{u2_idx}_0', (U2_RACK_X, ry, zf, math.pi / 2), u1_rack_mesh, static=True))
            for r_idx in range(1, U2_RACKS_PER_STATION):
                cell = U2_STATION_CELLS[u2_idx][r_idx]
                out.append(model(f'u2_rack_{f}_{u2_idx}_{r_idx}', (cell['x'], cell['y'], zf, cell.get('yaw', 0.)), u1_rack_mesh, static=True))
            wy = U2_WORKERS[u2_idx]['y']
            ty = wy + .95 if u2_idx == 0 else wy - .95
            out.append(model(f'u2_trolley_{f}_{u2_idx}', (-7.95, ty, zf, math.pi / 2), trolley_empty_mesh, static=True))
        # P1 picking stations (14 stations: 7 North + 7 West): 1 AGV + 1 active rack + 1 spare rack in Kho 04 + 1 trolley at mark.
        for st_idx, st in enumerate(P1_STATIONS):
            rx, ry = cx(st['rack']), cy(st['rack'])
            ryaw = 0. if st_idx < 7 else math.pi / 2
            agv_yaw = -math.pi / 2 if st_idx < 7 else 0.
            out.append(model(f'p1_agv_{f}_{st_idx}', (rx, ry, zf, agv_yaw), u1_agv_mesh, static=True))
            out.append(model(f'p1_rack_{f}_{st_idx}_0', (rx, ry, zf, ryaw), p1_rack_mesh, static=True))
            for r_idx in range(1, P1_RACKS_PER_STATION):
                cell = P1_STATION_CELLS[st_idx][r_idx]
                out.append(model(f'p1_rack_{f}_{st_idx}_{r_idx}', (cell['x'], cell['y'], zf, cell.get('yaw', 0.)), p1_rack_mesh, static=True))
            tx, ty, tyaw = p1_trolley_xy_yaw(st_idx)
            out.append(model(f'p1_trolley_{f}_{st_idx}', (tx, ty, zf, tyaw), trolley_empty_mesh, static=True))
    for j, sp in enumerate(L['spirals']):
        out.append(model(f'ff_spiral_{sp["from_floor"]}_{sp["to_floor"]}', (0, 0, 0, 0), merge_boxes(f'ff_spiral{j}', spiral(sp))))
    return out

def context():
    """Ground, 4-way store outline and its two output gates on every level (connection check)."""
    v = [box('ground', -5, 15, -.12, 130, 100, .2, '.55 .58 .60 1')]
    for s in ((0, 0, 52.6, 0), (52.6, 0, 52.6, 50), (52.6, 50, 0, 50), (0, 50, 0, 0)): v.append(line_box(s, .12, .01, '.20 .35 .60 1'))
    try:
        lay = json.loads((ROOT / 'design_layout.json').read_text(encoding='utf-8'))
        for port in lay['ports']['out']:
            gx, gy = port['gate']
            for zf in LEVELS: v.append(box(nm('g'), gx, gy, zf + .3, port['gate_length'], 1.35, .1, '.10 .65 .30 1'))
    except (OSError, KeyError):
        pass
    return model('ff_context', (0, 0, 0, 0), merge_boxes('ff_context', v))

HEADER = '''<?xml version="1.0"?><sdf version="1.9"><world name="four_floor">
<physics name="physics" type="ignored"><max_step_size>0.01</max_step_size><real_time_factor>1</real_time_factor></physics>
<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
<plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
<plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
<scene><ambient>.55 .55 .55 1</ambient><background>.72 .76 .80 1</background><shadows>false</shadows></scene>
<light name="sun" type="directional"><pose>0 0 40 0 0 0</pose><diffuse>.9 .9 .9 1</diffuse><direction>-.3 .2 -1</direction><cast_shadows>false</cast_shadows></light>
<gui fullscreen="false"><plugin filename="MinimalScene" name="3D View"><gz-gui><title>Four Floor</title><property type="bool" key="showTitleBar">false</property><property type="string" key="state">docked</property></gz-gui><engine>ogre</engine><scene>scene</scene><ambient_light>.55 .55 .55</ambient_light><background_color>.72 .76 .80</background_color><camera_pose>-66 -30 30 0 .42 .70</camera_pose></plugin><plugin filename="GzSceneManager" name="Scene Manager"/><plugin filename="InteractiveViewControl" name="View control"/><plugin filename="CameraTracking" name="Camera Tracking"/><plugin filename="WorldControl" name="World control"/><plugin filename="WorldStats" name="World stats"/><plugin filename="Screenshot" name="Screenshot"/></gui>
'''

if __name__ == '__main__':
    parts = models() + [context()]
    for old in (ROOT / 'assets').glob('ff_*.stl'):   # drop stale batches from a previous build
        if not any(old.name in p for p in parts): old.unlink()
    (ROOT / 'four-floor.sdf').write_text(HEADER + ''.join(parts) + '</world></sdf>')
    print(f'four-floor.sdf: {len(parts)} models | floors at {LEVELS} | per floor: {len(L["agvs"])} AGV+rack, '
          f'{len(L["rack_cells"])} storage racks, {len(L["stations"])} stations, {len(L["columns"])} columns | '
          f'spirals {[(s["from_floor"], s["to_floor"]) for s in L["spirals"]]}')
