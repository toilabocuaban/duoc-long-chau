"""Floor-1 loose-goods unpacking: geometry of the carton routes (agreed process).

Two operators at the carton inbound gate (floor 1) unpack loose-goods pallets standing on two
pallet spots next to them and put the cartons on the carton belt (*U758). A carton then follows:
  carton belt -> roller module -> incline 'Bang tai tang 1' (x = -3.55)
  floor 1      : straight on to the floor-1 sorting loop
  floor k > 1  : roller module (y ~ 5.06) -> cross belt *U752 -> 90-degree In curve -> spiral 1->2,
                 then on every intermediate floor the Out conveyor *U748 diverts at its roller
                 module onto that floor's *U752 and the next spiral, up to the destination floor
then along the lower sorting belt to a sorting branch (*U632) and up to the sorting head (operator
+ AGV rack), where the carton is put into a tray (removed from the simulation).

Every point is (x, y, z) of the carton centre in the world frame; geometry comes from
four_floor_scene so the cartons ride exactly on the modelled belts and spirals.
"""
import math
import four_floor_scene as FF

L = FF.L
LEVELS = FF.LEVELS
CARTON = (.40, .30, .25)          # ASSUMPTION: loose-goods carton L x W x H (m), 3 x 3 x 2 per pallet
STACK = (3, 3)                    # cartons per layer along pallet x, y

def _rect(block): return next(c['rect'] for c in L['conveyors'] if c['block'] == block)
U758, U753, U427, U429 = _rect('*U758'), _rect('*U753'), _rect('*U427'), _rect('*U429')
FEED_Y = FF.cy(L['_feeder'])
X_INC = FF.cx(U753)
Y758 = FF.cy(U758)
Y_MAIN, Y_UPPER = FF.cy(U427), FF.cy(U429)
BRANCHES = sorted((c['rect'] for c in L['conveyors'] if c['block'] == '*U632'), key=FF.cx)
SPIRAL_FROM = {s['from_floor']: s for s in L['spirals']}

def zc(f, belt=FF.BELT_Z):
    """Carton centre height on a belt of floor f."""
    return LEVELS[f - 1] + belt + CARTON[2] / 2

def floors():
    return list(range(1, len(LEVELS) + 1))

def stack_positions(spot, n):
    """Carton centres on a pallet at spot (x, y, yaw), top layer first (unpack order)."""
    x, y, yaw = spot; c, s = math.cos(yaw), math.sin(yaw)
    per = STACK[0] * STACK[1]; layers = math.ceil(n / per); out = []
    for k in range(n):
        layer, i = divmod(k, per); a, b = divmod(i, STACK[1])
        lx, ly = (a - (STACK[0] - 1) / 2) * CARTON[0], (b - (STACK[1] - 1) / 2) * CARTON[1]
        out.append((x + lx * c - ly * s, y + lx * s + ly * c, .15 + CARTON[2] * (layer + .5)))
    return list(reversed(out))   # last stacked (top) first

def _spiral(sp):
    """In curve (from the cross belt) -> In tangent -> CCW helix -> Out tangent end, carton centre points."""
    h = CARTON[2] / 2; zb, zt = LEVELS[sp['from_floor'] - 1], LEVELS[sp['to_floor'] - 1]
    x_in, x_out = sp['x'] - FF.SPIRAL_TANGENT, sp['x'] + FF.SPIRAL_TANGENT
    cc = (x_in + FF.CURVE_R, FEED_Y - FF.CURVE_R)
    pts = [(cc[0] + FF.CURVE_R * math.cos(a), cc[1] + FF.CURVE_R * math.sin(a), zb + FF.BELT_Z + h)
           for a in (math.pi / 2 + math.pi / 2 * k / 8 for k in range(9))]
    rc = (sp['r_inner'] + sp['r_outer']) / 2; total = FF.SPIRAL_TURNS * 2 * math.pi; n = int(36 * FF.SPIRAL_TURNS)
    pts.append((x_in, sp['y'], zb + FF.BELT_Z + h))
    for k in range(1, n + 1):
        t = k / n; a = math.pi + total * t
        pts.append((sp['x'] + rc * math.cos(a), sp['y'] + rc * math.sin(a), zb + FF.BELT_Z + h + (zt - zb) * t))
    pts.append((x_out, FEED_Y, zt + FF.BELT_Z + h))   # Out tangent + *U748 up to the roller module
    return pts

def route(dest, x_start, branch):
    """Carton centre polyline from the carton belt (in front of the operator) to the sorting head."""
    if dest not in floors(): raise ValueError(f'Destination floor {dest} out of range')
    h = CARTON[2] / 2
    pts = [(x_start, Y758, zc(1)), (X_INC, Y758, zc(1))]
    for y, z in FF.incline_profile(U753):
        if y > Y758 + 1e-6 and (dest == 1 or y < FEED_Y): pts.append((X_INC, y, LEVELS[0] + z + h))
    if dest == 1:
        entry = X_INC; pts.append((X_INC, Y_MAIN, zc(1)))
    else:
        pts.append((X_INC, FEED_Y, zc(1)))
        for f in range(1, dest):
            sp = SPIRAL_FROM[f]
            pts.append((sp['x'] - FF.SPIRAL_TANGENT + FF.CURVE_R, FEED_Y, zc(f)))   # along *U752 to the In curve
            pts += _spiral(sp)
        entry = SPIRAL_FROM[dest - 1]['x'] + FF.SPIRAL_TANGENT
        pts.append((entry, Y_MAIN, zc(dest)))
    b = BRANCHES[branch % len(BRANCHES)]; bx = FF.cx(b)
    if bx > entry + .3: raise ValueError(f'Branch x={bx:.2f} is east of the floor-{dest} loop end {entry:.2f}')
    pts += [(bx, Y_MAIN, zc(dest)), (bx, Y_UPPER, zc(dest)), (bx, b[3] - .3, zc(dest))]
    return pts

# ---------------------------------------------------------------- operator animation (kinematic, agreed)
# Each unpacking operator is four rigid parts posed every frame: torso+head (pivot at the hip, bends
# forward), both arms (pivot at the shoulders), left / right leg (pivot at the hip, swing when walking).
HIP = .86
SHOULDER = (.02, .55)          # shoulder offset from the hip in the torso frame (forward, up)
ARM = .54                      # shoulder -> hand
LEG_Y = .09
WORKER_PARTS = {               # local boxes (x, y, z, sx, sy, sz, colour) per part
    'torso': [(0, 0, .30, .24, .42, .62, FF.SHIRT), (.01, 0, .725, .20, .18, .22, FF.SKIN), (-.02, 0, .84, .21, .19, .05, FF.HAIR)],
    'arms': [(0, -.25, -.26, .10, .09, .56, FF.SHIRT), (0, .25, -.26, .10, .09, .56, FF.SHIRT),
             (.0, -.25, -.56, .08, .08, .07, FF.SKIN), (.0, .25, -.56, .08, .08, .07, FF.SKIN)],
    'legl': [(0, 0, -.43, .13, .14, .86, FF.PANTS)],
    'legr': [(0, 0, -.43, .13, .14, .86, FF.PANTS)],
}
OPERATORS = sorted((w for w in L['workers'] if w['area'] == 'carton_inbound'), key=lambda w: w['x'])

def part_names(i): return {p: f'op{i}_{p}' for p in WORKER_PARTS}

def station(op):
    """Operator position at the carton belt, facing it (north)."""
    return (op['x'], U758[1] - .38, math.pi / 2)

def pallet_side(op, spot):
    """Where the operator stands to reach into the pallet: on the pallet side facing the belt station."""
    x, y, _ = spot
    return (x + .6 + .42, y, math.pi) if x < op['x'] else (x - .6 - .42, y, 0.)

def _rot(yaw, v):
    c, s = math.cos(yaw), math.sin(yaw)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)

def operator_pose(i, x, y, yaw, bend=0., arm=0., swing=0., z0=0., upper_only=False):
    """Part poses (x, y, z, roll, pitch, yaw). bend: torso forward lean; arm: raise of the arms relative to
    the torso (0 = hanging along it); swing: leg swing angle; z0: floor base height."""
    n = part_names(i)
    sx = SHOULDER[0] * math.cos(bend) + SHOULDER[1] * math.sin(bend)
    sz = -SHOULDER[0] * math.sin(bend) + SHOULDER[1] * math.cos(bend)
    dx, dy = _rot(yaw, (sx, 0))
    out = {n['torso']: (x, y, z0 + HIP, 0., bend, yaw),
           n['arms']: (x + dx, y + dy, z0 + HIP + sz, 0., bend - arm, yaw)}
    if not upper_only:
        lx, ly = _rot(yaw, (0, LEG_Y))
        out[n['legl']] = (x + lx, y + ly, z0 + HIP, 0., swing, yaw)
        out[n['legr']] = (x - lx, y - ly, z0 + HIP, 0., -swing, yaw)
    return out

def held_carton(x, y, yaw, bend, arm, z0=0.):
    """Carton centre carried between the hands."""
    sx = SHOULDER[0] * math.cos(bend) + SHOULDER[1] * math.sin(bend)
    sz = -SHOULDER[0] * math.sin(bend) + SHOULDER[1] * math.cos(bend)
    th = bend - arm
    hx, hz = sx - ARM * math.sin(th) + .08, z0 + HIP + sz - ARM * math.cos(th) + CARTON[2] / 2 - .04
    dx, dy = _rot(yaw, (hx, 0))
    return (x + dx, y + dy, hz, yaw)

def reach_for(z_carton):
    """Bend / arm raise to reach a carton whose centre is at height z (lower layers -> deeper bend)."""
    bend = min(1.1, max(.3, 1.4 - 1.2 * z_carton))
    return bend, bend * .55

# ---------------------------------------------------------------- U1 sorting-branch stations -> Kho 04
U1_WORKER_Y = 11.981
U1_RACK_Y = FF.U1_RACK_Y
U1_RACKS_PER_BRANCH = FF.U1_RACKS_PER_BRANCH
U1_BRANCH_CELLS = FF.U1_BRANCH_CELLS
U1_RACK_CARTON_Z = 1.24
U1_MED_PER_CARTON = 3
MED_YELLOW = '.98 .78 .10 1'
MED_WHITE = '.96 .96 .94 1'
MED_ORANGE = '.95 .42 .08 1'

def med_box_parts():
    """2x2 pack of small yellow medicine boxes (fits 3 packs per 580x415x260 mm hollow blue tray)."""
    out = []
    for sx in (-.038, .038):
        for sy in (-.078, .078):
            out.append((sx, sy, .035, .068, .145, .15, MED_YELLOW))
            out.append((sx, sy, -.065, .068, .145, .05, MED_WHITE))
            out.append((sx, sy, -.030, .070, .147, .02, MED_ORANGE))
    return out

def u1_worker_id(floor, branch):
    """Unique integer ID for animated U1 sorting worker on (floor 1..4, branch 0..5)."""
    return 100 + floor * 10 + (branch % len(BRANCHES))

def u1_station(floor, branch):
    """(x, y, yaw, z0) of the U1 sorting worker at the north end of branch (*U632), facing south."""
    bx = FF.cx(BRANCHES[branch % len(BRANCHES)])
    return (bx, U1_WORKER_Y, -math.pi / 2, LEVELS[floor - 1])

def u1_rack_pose(floor, branch):
    """Initial pose (x, y, z, yaw) of the U1 rack directly behind the U1 sorting worker."""
    bx = FF.cx(BRANCHES[branch % len(BRANCHES)])
    return (bx, U1_RACK_Y, LEVELS[floor - 1], 0.)

def u1_med_in_carton(carton_pose, m):
    """World pose (x, y, z, yaw) of yellow medicine box m (0..2) inside an opened carton at belt end."""
    cx, cy, cz = carton_pose[:3]
    dx = (m - (U1_MED_PER_CARTON - 1) / 2) * .10
    return (cx + dx, cy, cz + .05, 0.)

def u1_med_on_rack(rack_pose, m, tray_slot=0):
    """World pose (x, y, z, yaw) of yellow medicine box m (0..2) inside a hollow blue tray on a U1 rack."""
    rx, ry, rz = rack_pose[:3]
    ryaw = rack_pose[3] if len(rack_pose) > 3 else 0.
    tx = -.295 if tray_slot % 2 == 0 else .295
    dx_local = tx + (m - (U1_MED_PER_CARTON - 1) / 2) * .165
    dx, dy = _rot(ryaw, (dx_local, 0.))
    return (rx + dx, ry + dy, rz + U1_RACK_CARTON_Z, ryaw)

def u1_carton_on_rack(rack_pose, slot=0):
    """World pose (x, y, z, yaw) of the first medicine box / slot in a hollow blue tray on a U1 rack."""
    return u1_med_on_rack(rack_pose, slot % U1_MED_PER_CARTON, slot // U1_MED_PER_CARTON)

def _u1_corridor_y(y):
    return 13.795 if y < 16.5 else 17.959

def u1_agv_route(start_xyz, end_xyz, branch):
    """3D polyline along CAD agv_paths connecting U1 station and Kho 04 rack_cells on the same floor."""
    x1, y1, z = start_xyz[:3]
    x2, y2 = end_xyz[:2]
    b = branch % len(BRANCHES)
    c1, c2 = _u1_corridor_y(y1), _u1_corridor_y(y2)
    aisle_x = -26.627 if b < 3 else -11.585
    raw = [(x1, y1, z), (x1, c1, z)]
    if abs(c1 - c2) < 1e-3:
        raw += [(x2, c2, z), (x2, y2, z)]
    else:
        raw += [(aisle_x, c1, z), (aisle_x, c2, z), (x2, c2, z), (x2, y2, z)]
    pts = [raw[0]]
    for p in raw[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-4:
            pts.append(p)
    return pts

# ---------------------------------------------------------------- Zone 05 (Unpack Pallet) -> U2 -> Kho 04
Z05_WORKERS = sorted((w for w in FF.people() if w['area'] == 'unpack'), key=lambda w: -w['y'])
Z05_CARTONS_PER_PALLET = 18
U2_WORKERS = FF.U2_WORKERS
U2_RACK_X = FF.U2_RACK_X
U2_RACKS_Y = FF.U2_RACKS_Y
U2_CORRIDORS_Y = FF.U2_CORRIDORS_Y
U2_RACKS_PER_STATION = FF.U2_RACKS_PER_STATION
U2_STATION_CELLS = FF.U2_STATION_CELLS
U2_DROP_X = -7.42

def z05_worker_id(floor, zone):
    """Unique integer ID for animated Zone 05 unpack worker on (floor 1..4, zone 0..1)."""
    return 200 + floor * 10 + (zone % len(Z05_WORKERS))

def z05_station(floor, zone):
    """(x, y, yaw, z0) of the Zone 05 unpack worker on (floor 1..4, zone 0..1), facing east."""
    w = Z05_WORKERS[zone % len(Z05_WORKERS)]
    return (w['x'], w['y'], 0., LEVELS[floor - 1])

def z05_pallet_side(spot_xy, worker_idx=0):
    """Standing position (x, y, yaw) on the west side of a Zone 05 pallet spot, facing east.
    Two workers sharing the same pallet stand slightly offset along y (+/-0.28 m) so they never overlap."""
    dy = .28 if (worker_idx % 2) == 0 else -.28
    return (spot_xy[0] - .6 - .42, spot_xy[1] + dy, 0.)

def z05_stack_positions(spot_xy, floor, n=Z05_CARTONS_PER_PALLET):
    """World poses (x, y, z, yaw) of the n=18 cartons stacked 3x3x2 on a Zone 05 pallet deck on floor."""
    z0 = LEVELS[floor - 1]
    return [(px, py, z0 + pz, 0.) for px, py, pz in stack_positions((spot_xy[0], spot_xy[1], 0.), n)]

def u2_worker_id(floor, u2_idx):
    """Unique integer ID for animated U2 put-away worker on (floor 1..4, u2_idx 0..5)."""
    return 300 + floor * 10 + (u2_idx % len(U2_WORKERS))

def u2_station(floor, u2_idx):
    """(x, y, yaw, z0) of the U2 worker on (floor 1..4, u2_idx 0..5), facing east towards Zone 05."""
    w = U2_WORKERS[u2_idx % len(U2_WORKERS)]
    return (w['x'], w['y'], 0., LEVELS[floor - 1])

def u2_carton_drop_pose(floor, u2_idx):
    """World pose (x, y, z, yaw) of a carton placed on the floor directly in front of U2 worker u2_idx."""
    w = U2_WORKERS[u2_idx % len(U2_WORKERS)]
    return (U2_DROP_X, w['y'], LEVELS[floor - 1] + CARTON[2] / 2, math.pi / 2)

def u2_rack_pose(floor, u2_idx):
    """Initial pose (x, y, z, yaw) of the U2 rack directly behind U2 worker u2_idx."""
    idx = u2_idx % len(U2_RACKS_Y)
    return (U2_RACK_X, U2_RACKS_Y[idx], LEVELS[floor - 1], math.pi / 2)

def u2_med_in_carton(carton_pose, m):
    """World pose (x, y, z, yaw) of yellow medicine box m (0..2) inside an opened carton on the U2 floor."""
    cx, cy, cz = carton_pose[:3]
    dy = (m - (U1_MED_PER_CARTON - 1) / 2) * .10
    return (cx, cy + dy, cz + .05, math.pi / 2)

def u2_agv_route(start_xyz, end_xyz, u2_idx):
    """3D polyline along CAD agv_paths connecting U2 station and Kho 04 rack_cells on the same floor."""
    x1, y1, z = start_xyz[:3]
    x2, y2 = end_xyz[:2]
    idx = u2_idx % len(U2_RACKS_Y)
    sy, cy = U2_RACKS_Y[idx], U2_CORRIDORS_Y[idx]
    raw = [(x1, y1, z)]
    if abs(y1 - sy) < 1e-3 and abs(sy - cy) > 1e-3:
        raw += [(-10.067, sy, z), (-10.067, cy, z)]
    elif abs(y1 - cy) > 1e-3:
        raw.append((x1, cy, z))
    if abs(y2 - sy) < 1e-3 and abs(sy - cy) > 1e-3:
        raw += [(-10.067, cy, z), (-10.067, sy, z), (x2, y2, z)]
    else:
        raw += [(x2, cy, z), (x2, y2, z)]
    pts = [raw[0]]
    for p in raw[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-4:
            pts.append(p)
    return pts

# ---------------------------------------------------------------- Empty Carton Trolleys <-> Khu 06 & Khu P1 (14 Stations)
TROLLEY_CAPACITY = FF.TROLLEY_CAPACITY
K06_SLOTS_XY = FF.K06_SLOTS_XY
P1_STATIONS = FF.P1_STATIONS
P1_RACKS_PER_STATION = FF.P1_RACKS_PER_STATION
P1_CELL_MAP = FF.P1_CELL_MAP
P1_STATION_CELLS = FF.P1_STATION_CELLS

_TROLLEY_SLOTS_LOCAL = (
    (-.24, 0., .30 + CARTON[2] * .5),
    (+.24, 0., .30 + CARTON[2] * .5),
    (-.24, 0., .30 + CARTON[2] * 1.5),
    (+.24, 0., .30 + CARTON[2] * 1.5),
    (0.,   0., .30 + CARTON[2] * 2.5),
)

def trolley_carton_pose(trolley_pose, slot=0):
    """World pose (x, y, z, yaw) of empty carton slot (0..4) stacked inside a wire-mesh trolley."""
    tx, ty, tz = trolley_pose[:3]
    tyaw = trolley_pose[3] if len(trolley_pose) > 3 else 0.
    lx, ly, lz = _TROLLEY_SLOTS_LOCAL[slot % len(_TROLLEY_SLOTS_LOCAL)]
    dx, dy = _rot(tyaw, (lx, ly))
    return (tx + dx, ty + dy, tz + lz, tyaw)

def k06_slot_pose(floor, slot_idx=0):
    """World pose (x, y, z, yaw) of a trolley staging slot in Khu 06 ([-4.31, 13.6])."""
    kx, ky = K06_SLOTS_XY[slot_idx % len(K06_SLOTS_XY)]
    return (kx, ky, LEVELS[floor - 1], 0.)

def u1_trolley_pose(floor, branch):
    """Initial pose (x, y, z, yaw) of the empty-carton trolley beside U1 sorting worker."""
    bx = FF.cx(BRANCHES[branch % len(BRANCHES)])
    return (bx + .95, 12.05, LEVELS[floor - 1], 0.)

def u1_k06_route(start_xyz, end_xyz):
    """3D polyline along CAD agv_paths connecting a U1 trolley position and Khu 06."""
    x1, y1, z = start_xyz[:3]
    x2, y2 = end_xyz[:2]
    raw = [(x1, y1, z), (x1, 13.795, z), (x2, 13.795, z), (x2, y2, z)]
    pts = [raw[0]]
    for p in raw[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-4:
            pts.append(p)
    return pts

def u2_trolley_pose(floor, u2_idx):
    """Initial pose (x, y, z, yaw) of the empty-carton trolley beside U2 put-away worker."""
    idx = u2_idx % len(U2_WORKERS)
    wy = U2_WORKERS[idx]['y']
    ty = wy + .95 if idx == 0 else wy - .95
    return (-7.95, ty, LEVELS[floor - 1], math.pi / 2)

def u2_k06_route(start_xyz, end_xyz, u2_idx=0):
    """3D polyline along CAD agv_paths connecting a U2 trolley position and Khu 06."""
    x1, y1, z = start_xyz[:3]
    x2, y2 = end_xyz[:2]
    if abs(x1 - (-7.95)) < .5:
        raw = [(x1, y1, z), (-10.067, y1, z), (-10.067, 13.795, z), (x2, 13.795, z), (x2, y2, z)]
    else:
        raw = [(x1, y1, z), (x1, 13.795, z), (-10.067, 13.795, z), (-10.067, y2, z), (x2, y2, z)]
    pts = [raw[0]]
    for p in raw[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-4:
            pts.append(p)
    return pts

def p1_worker_id(floor, st_idx):
    """Unique integer ID for animated P1 picking worker on (floor 1..4, st_idx 0..13)."""
    return 400 + floor * 20 + (st_idx % len(P1_STATIONS))

def p1_station(floor, st_idx):
    """(x, y, yaw, z0) of P1 picking worker on (floor 1..4, st_idx 0..13), standing between desk and trolley."""
    idx = st_idx % len(P1_STATIONS)
    st = P1_STATIONS[idx]
    dx, dy = FF.cx(st['desk']), FF.cy(st['desk'])
    z0 = LEVELS[floor - 1]
    if idx < 7:
        return (dx + .98, dy, -math.pi / 2, z0)
    return (dx, dy - .98, 0., z0)

def p1_rack_pose(floor, st_idx):
    """Initial pose (x, y, z, yaw) of the P1 rack at st['rack'] on (floor 1..4, st_idx 0..13)."""
    idx = st_idx % len(P1_STATIONS)
    st = P1_STATIONS[idx]
    return (FF.cx(st['rack']), FF.cy(st['rack']), LEVELS[floor - 1], 0. if idx < 7 else math.pi / 2)

def p1_trolley_pose(floor, st_idx):
    """Initial pose (x, y, z, yaw) of the P1 trolley at st['mark'] on (floor 1..4, st_idx 0..13)."""
    idx = st_idx % len(P1_STATIONS)
    tx, ty, tyaw = FF.p1_trolley_xy_yaw(idx)
    return (tx, ty, LEVELS[floor - 1], tyaw)

def p1_active_carton_pose(floor, st_idx):
    """World pose (x, y, z, yaw) on the working edge of the P1 desk where the worker packs 3 medicine boxes."""
    idx = st_idx % len(P1_STATIONS)
    st = P1_STATIONS[idx]
    dx, dy = FF.cx(st['desk']), FF.cy(st['desk'])
    z = LEVELS[floor - 1] + .85 + CARTON[2] / 2
    if idx < 7:
        return (dx + .45, dy - .18, z, 0.)
    return (dx + .18, dy - .45, z, math.pi / 2)

def p1_aside_carton_pose(floor, st_idx):
    """World pose (x, y, z, yaw) further onto the P1 desk where the packed carton is set aside before disappearing."""
    idx = st_idx % len(P1_STATIONS)
    st = P1_STATIONS[idx]
    dx, dy = FF.cx(st['desk']), FF.cy(st['desk'])
    z = LEVELS[floor - 1] + .85 + CARTON[2] / 2
    if idx < 7:
        return (dx - .15, dy + .10, z, 0.)
    return (dx - .10, dy + .15, z, math.pi / 2)

def p1_med_in_carton(carton_pose, m):
    """World pose (x, y, z, yaw) of yellow medicine box m (0..2) packed inside a carton on the P1 desk."""
    cx, cy, cz = carton_pose[:3]
    cyaw = carton_pose[3] if len(carton_pose) > 3 else 0.
    offset = (m - (U1_MED_PER_CARTON - 1) / 2) * .10
    dx, dy = _rot(cyaw, (offset, 0.))
    return (cx + dx, cy + dy, cz + .05, cyaw)

def p1_agv_route(start_xyz, end_xyz, st_idx):
    """3D polyline along CAD agv_paths connecting P1 station rack spot and Kho 04 rack_cells on the same floor."""
    idx = st_idx % len(P1_STATIONS)
    x1, y1, z = start_xyz[:3]
    x2, y2 = end_xyz[:2]
    _, _, corr_y = P1_CELL_MAP[idx]
    st = P1_STATIONS[idx]
    rx, ry = FF.cx(st['rack']), FF.cy(st['rack'])
    raw = [(x1, y1, z)]
    if idx >= 7:
        if abs(x1 - rx) < 1e-2 and abs(y1 - corr_y) > 1e-2:
            raw += [(-45.706, y1, z), (-45.706, corr_y, z)]
        elif abs(y1 - corr_y) > 1e-2:
            raw.append((x1, corr_y, z))
        if abs(x2 - rx) < 1e-2 and abs(y2 - corr_y) > 1e-2:
            raw += [(-45.706, corr_y, z), (-45.706, y2, z), (x2, y2, z)]
        else:
            raw += [(x2, corr_y, z), (x2, y2, z)]
    else:
        raw += [(x1, corr_y, z), (x2, corr_y, z), (x2, y2, z)]
    pts = [raw[0]]
    for p in raw[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-4:
            pts.append(p)
    return pts

def p1_k06_route(start_xyz, end_xyz, st_idx):
    """3D polyline along CAD perimeter agv_paths connecting P1 trolley mark and Khu 06."""
    idx = st_idx % len(P1_STATIONS)
    x1, y1, z = start_xyz[:3]
    x2, y2 = end_xyz[:2]
    if idx < 7:
        if y1 > 40.:
            raw = [(x1, y1, z), (x1, 45.291, z), (-10.067, 45.291, z), (-10.067, 13.795, z), (x2, 13.795, z), (x2, y2, z)]
        else:
            raw = [(x1, y1, z), (x1, 13.795, z), (-10.067, 13.795, z), (-10.067, 45.291, z), (x2, 45.291, z), (x2, y2, z)]
    else:
        if x1 < -40.:
            raw = [(x1, y1, z), (-45.706, y1, z), (-45.706, 45.291, z), (-10.067, 45.291, z), (-10.067, 13.795, z), (x2, 13.795, z), (x2, y2, z)]
        else:
            raw = [(x1, y1, z), (x1, 13.795, z), (-10.067, 13.795, z), (-10.067, 45.291, z), (-45.706, 45.291, z), (-45.706, y2, z), (x2, y2, z)]
    pts = [raw[0]]
    for p in raw[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-4:
            pts.append(p)
    return pts

def length(pts):
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))

def along(pts, s):
    """Pose (x, y, z, yaw) at arc length s along the polyline (yaw = horizontal heading)."""
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        if s <= d or b is pts[-1]:
            t = 0 if d == 0 else min(1., s / d)
            yaw = math.atan2(b[1] - a[1], b[0] - a[0]) if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-6 else 0.
            return tuple(a[j] + (b[j] - a[j]) * t for j in range(3)) + (yaw,)
        s -= d
    return tuple(pts[-1]) + (0.,)
