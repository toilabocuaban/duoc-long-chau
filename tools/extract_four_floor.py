"""Extract the 4-floor picking building (west of the 4-way store) from the CAD layout.

Run on Windows (needs ezdxf + pymupdf):
    python -I tools/extract_four_floor.py --dxf layout.dxf --pdf "Layout ... -Model.pdf" --out four_floor_layout.json

* layout.dxf : DXF saved by progeCAD from "Layout DCL-ETEK-4-Ways version 4.dwg" (DWG itself is not read).
* The PDF is the "Model" export of the same drawing. It is used only for dynamic-block arrays
  (rack grid, pallet X spots) whose array copies are lost in the DXF export. It is registered to
  the DXF with the AGV+rack symbols (x1); the residual is stored in the output.

All coordinates are metres in the simulation frame (origin = 4-way block origin, same as
design_layout.json). Every item is floor-local; the scene builder stacks floors.
"""
import argparse, collections, json, math, statistics as st, logging
logging.disable(logging.CRITICAL)
import ezdxf
from ezdxf import bbox
import pymupdf

OX, OY = 351421.53136008, 239481.1168709311
ZONE = (-52.5, -10.5, 0.5, 50.5)

def m(b):
    return ((b.extmin.x - OX) / 1000, (b.extmin.y - OY) / 1000, (b.extmax.x - OX) / 1000, (b.extmax.y - OY) / 1000)

def inside(r, z=ZONE):
    return r[0] >= z[0] and r[2] <= z[2] and r[1] >= z[1] and r[3] <= z[3]

def rnd(v, n=3):
    return [round(x, n) for x in v]

def extent(e):
    try:
        b = bbox.extents([e])
        return m(b) if b.has_data else None
    except Exception:
        return None

def segments(e):
    """2D segments (metres) of a primitive; curves are flattened to <= 5 cm chords."""
    t = e.dxftype(); pts = []
    try:
        if t == 'LINE':
            pts = [[e.dxf.start, e.dxf.end]]
        elif t in ('LWPOLYLINE', 'POLYLINE'):
            v = list(e.flattening(50)) if hasattr(e, 'flattening') else [p for p in e.vertices()]
            pts = [v]
        elif t in ('ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE'):
            pts = [list(e.flattening(50))]
    except Exception:
        return []
    out = []
    for poly in pts:
        p = [((q[0] - OX) / 1000, (q[1] - OY) / 1000) for q in poly]
        out += [(p[i][0], p[i][1], p[i + 1][0], p[i + 1][1]) for i in range(len(p) - 1)
                if math.dist(p[i], p[i + 1]) > .005]
    return out

def clip(s, z=ZONE):
    """Liang-Barsky clip of a segment to the zone rectangle."""
    x0, y0, x1, y1 = s; dx, dy = x1 - x0, y1 - y0; t0, t1 = 0., 1.
    for p, q in ((-dx, x0 - z[0]), (dx, z[2] - x0), (-dy, y0 - z[1]), (dy, z[3] - y0)):
        if p == 0:
            if q < 0: return None
        else:
            r = q / p
            if p < 0: t0 = max(t0, r)
            else: t1 = min(t1, r)
            if t0 > t1: return None
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)

def walk(entities, depth=0, path=()):
    for e in entities:
        if e.dxftype() == 'INSERT' and depth < 5:
            try: yield from walk(e.virtual_entities(), depth + 1, path + (e.dxf.name,))
            except Exception: pass
        else:
            yield e, path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dxf', required=True); ap.add_argument('--pdf', required=True); ap.add_argument('--out', required=True)
    a = ap.parse_args()
    doc = ezdxf.readfile(a.dxf); msp = doc.modelspace()
    out = {'source': 'Layout DCL-ETEK-4-Ways version 4.dwg (progeCAD DXF export) + Model PDF for dynamic arrays',
           'origin_mm': [OX, OY], 'zone': ZONE}

    tops = []  # top-level inserts fully inside the zone
    for e in msp.query('INSERT'):
        r = extent(e)
        if r and inside(r): tops.append((e, r))
    by_name = collections.defaultdict(list)
    for e, r in tops: by_name[e.dxf.name].append((e, r))

    # ---- Architecture plan lines (block *U162) clipped to the zone, grouped by layer family.
    families = {'wall': ('Arc-Net tuong', 'Arc- Tuong panel', 'Arc-Net hatch tuong'), 'door': ('Arc-Net cua',),
                'column': ('Arc-Be tong',), 'grid': ('Arc-Net truc', 'AXIS_TEXT'),
                'fixture': ('Arc-Net noi that', 'Arc-Net noi that 2', 'Arc-Net thay 1', 'Arc-Net thay 2', 'Arc-Net khuat', 'Arc-Net manh')}
    fam_of = {lay: f for f, ls in families.items() for lay in ls}
    plan = collections.defaultdict(list); columns = []; structural = []
    for e in msp.query('INSERT[name=="*U162"]'):
        for s, path in walk([e]):
            if s.dxftype() in ('TEXT', 'MTEXT', 'DIMENSION', 'HATCH', 'INSERT', 'WIPEOUT', 'LEADER'): continue
            # Structural columns: closed outlines ~330 x 460 mm on layer 'Arc-Net thay 2' (8.3 m grid).
            if s.dxf.layer == 'Arc-Net thay 2' and s.dxftype() == 'LWPOLYLINE':
                r = extent(s)
                if r and inside(r) and .25 < r[2] - r[0] < .75 and .25 < r[3] - r[1] < .75:
                    structural.append(rnd(r))
                continue
            fam = fam_of.get(s.dxf.layer)
            if not fam or fam == 'grid': continue
            if fam == 'column':   # 200 x 200 mm posts inside the masonry walls
                r = extent(s)
                if r and inside(r) and .2 < r[2] - r[0] < 1.2 and .2 < r[3] - r[1] < 1.2:
                    columns.append(rnd(r))
                continue
            for seg in segments(s):
                c = clip(seg)
                if c and math.dist(c[:2], c[2:]) > .01: plan[fam].append(rnd(c))
    # De-duplicate column outlines (outline + hatch boundary).
    def dedupe(cs):
        u = []
        for c in cs:
            if not any(abs(c[0] - q[0]) < .05 and abs(c[1] - q[1]) < .05 for q in u): u.append(c)
        return u
    uniq = dedupe(columns)
    # A column drawn with two outlines: keep the outer one (centres closer than 0.3 m).
    structural.sort(key=lambda c: -(c[2] - c[0]) * (c[3] - c[1])); cols = []
    for c in structural:
        cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
        if not any(math.dist((cx, cy), ((q[0] + q[2]) / 2, (q[1] + q[3]) / 2)) < .3 for q in cols): cols.append(c)
    out['plan_lines'] = dict(plan); out['wall_posts'] = uniq; out['columns'] = cols
    walls = plan['wall']
    xs = [v for s in walls for v in (s[0], s[2])]; ys = [v for s in walls for v in (s[1], s[3])]
    out['building'] = rnd([max(min(xs), ZONE[0]), max(min(ys), ZONE[1]), min(max(xs), 0.), min(max(ys), ZONE[3])])

    # ---- AGV route lines (orange, colour 40) in model space.
    paths = []
    for e in msp.query('LWPOLYLINE'):
        if e.dxf.color != 40: continue
        for seg in segments(e):
            c = clip(seg)
            if c: paths.append(rnd(c))
    out['agv_paths'] = paths

    # ---- PDF registration with the x1 symbols, then dynamic-array cells.
    x1c = [((r[0] + r[2]) / 2, (r[1] + r[3]) / 2) for e, r in by_name['x1']]
    page = pymupdf.open(a.pdf)[0]
    green = []; green_lines = []
    for d in page.get_drawings():
        c = d.get('color')
        if not c or not (c[1] > .6 and c[0] < .5 and c[2] < .5): continue
        for it in d['items']:
            if it[0] == 'qu': green.append(it[1].rect)
            elif it[0] == 're': green.append(it[1])
            elif it[0] == 'l': green_lines.append((it[1].x, it[1].y, it[2].x, it[2].y))
    portrait = [((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2) for r in green if 4.8 < r.width < 7 and 7.5 < r.height < 10.5]
    s, ax, ay = 7.41, 105.0 + 42.02 * 7.41, 92.1 + 48.03 * 7.41
    for _ in range(8):
        pairs = []
        for x, y in x1c:
            px, py = ax + s * x, ay - s * y
            b = min(portrait, key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)
            if (b[0] - px) ** 2 + (b[1] - py) ** 2 < (2.5 * s) ** 2: pairs.append(((x, y), b))
        mx = st.mean(p[0][0] for p in pairs); my = st.mean(p[0][1] for p in pairs)
        mpx = st.mean(p[1][0] for p in pairs); mpy = st.mean(p[1][1] for p in pairs)
        sx = sum((p[1][0] - mpx) * (p[0][0] - mx) for p in pairs) / sum((p[0][0] - mx) ** 2 for p in pairs)
        sy = -sum((p[1][1] - mpy) * (p[0][1] - my) for p in pairs) / sum((p[0][1] - my) ** 2 for p in pairs)
        s = (sx + sy) / 2; ax = mpx - s * mx; ay = mpy + s * my
    res = [max(abs(ax + s * p[0][0] - p[1][0]), abs(ay - s * p[0][1] - p[1][1])) / s for p in pairs]
    out['pdf_registration'] = {'pt_per_m': s, 'ax': ax, 'ay': ay, 'pairs': len(pairs), 'max_residual_m': round(max(res), 4)}
    def to_sim(r): return ((r.x0 - ax) / s, (ay - r.y1) / s, (r.x1 - ax) / s, (ay - r.y0) / s)
    def cells_of(wmin, wmax, hmin, hmax):
        cand = [to_sim(r) for r in green if wmin < r.width / s < wmax and hmin < r.height / s < hmax]
        cand = [c for c in cand if inside(c)]
        cand.sort(key=lambda c: -(c[2] - c[0]) * (c[3] - c[1]))
        keep = []
        for c in cand:
            cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
            if not any(abs(cx - (k[0] + k[2]) / 2) < .3 and abs(cy - (k[1] + k[3]) / 2) < .3 for k in keep): keep.append(c)
        return keep

    # ---- AGVs with racks: x1 symbols and the g5 rotation circle (contains one x1).
    agvs = []
    def agv_from_green(r, source):
        w, h = r[2] - r[0], r[3] - r[1]
        agvs.append({'x': round((r[0] + r[2]) / 2, 3), 'y': round((r[1] + r[3]) / 2, 3),
                     'yaw': 0.0 if w >= h else round(math.pi / 2, 6), 'rack': True, 'source': source})
    occupied = []
    for name in ('x1', 'g5'):
        for e, r in by_name[name]:
            g = [extent(c) for c, p in walk([e]) if c.dxftype() == 'LWPOLYLINE' and c.dxf.color == 3]
            g = [q for q in g if q and .7 < max(q[2] - q[0], q[3] - q[1]) < 1.4]
            rr = max(g, key=lambda q: (q[2] - q[0]) * (q[3] - q[1])) if g else r
            agv_from_green(rr, name); occupied.append(rr)

    # ---- Picking stations (*U563): put-wall desk, monitor, light-curtain posts, floor U-mark, worker, AGV+rack.
    stations = []; workers = []
    for e, r in by_name['*U563']:
        st_ = {'insert': rnd(((e.dxf.insert.x - OX) / 1000, (e.dxf.insert.y - OY) / 1000)), 'rot_deg': e.dxf.rotation,
               'bbox': rnd(r), 'desk': None, 'desk_cells': [], 'monitor': None, 'posts': [], 'mark': []}
        greens = []
        for c in e.virtual_entities():
            q = extent(c)
            if not q: continue
            t = c.dxftype(); col = c.dxf.color
            if t == 'INSERT':
                n = c.dxf.name
                if n == '*U742':
                    workers.append({'x': round((q[0] + q[2]) / 2, 3), 'y': round((q[1] + q[3]) / 2, 3), 'area': 'picking_station'})
                elif n == '壁挂式显示器': st_['monitor'] = rnd(q)
                elif n in ('1', '1B', '2', 'b3'): st_['posts'].append(rnd(q))
            elif t == 'LWPOLYLINE' and col == 5:
                if (q[2] - q[0]) * (q[3] - q[1]) > .5: st_['desk'] = rnd(q)
                elif (q[2] - q[0]) * (q[3] - q[1]) > .05: st_['desk_cells'].append(rnd(q))
                else: st_['desk_ext'] = rnd(q)
            elif t == 'LWPOLYLINE' and col == 2: st_['mark'].append(rnd(q))
            elif t == 'LWPOLYLINE' and col == 3 and (q[2] - q[0]) * (q[3] - q[1]) > .5: greens.append(q)
        if greens:
            g = max(greens, key=lambda q: (q[2] - q[0]) * (q[3] - q[1]))
            agv_from_green(g, 'station'); occupied.append(g); st_['rack'] = rnd(g)
        stations.append(st_)
    out['stations'] = stations

    # ---- Storage rack cells (landscape 1.2 x 0.8 m, PDF) minus cells already holding an AGV rack.
    cells = cells_of(1.1, 1.3, .7, .9)
    def taken(c):
        cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
        return any(o[0] - .1 < cx < o[2] + .1 and o[1] - .1 < cy < o[3] + .1 for o in occupied)
    out['rack_cells'] = [{'x': round((c[0] + c[2]) / 2, 3), 'y': round((c[1] + c[3]) / 2, 3), 'yaw': 0.0} for c in cells if not taken(c)]
    out['agvs'] = agvs

    # ---- Other workers: top-level person blocks (0.78-0.92 m, one head circle of ~0.3 m).
    for e, r in tops:
        w, h = r[2] - r[0], r[3] - r[1]
        if not (.75 < w < .95 and .75 < h < .95) or e.dxf.name in ('x1', 'g5', '*U563'): continue
        heads = [extent(c) for c, p in walk([e]) if c.dxftype() == 'CIRCLE']
        heads = [q for q in heads if q and .25 < q[2] - q[0] < .35]
        if len(heads) == 1:
            y = r[1] + h / 2
            area = 'sorting' if 11 < y < 13 else 'put_away' if r[0] > -9 and y > 15 else 'carton_inbound' if y < 0 else 'other'
            workers.append({'x': round(r[0] + w / 2, 3), 'y': round(y, 3), 'area': area})
    out['workers'] = workers

    # ---- Conveyors at H=0.6 m: straight belts from block footprints; transfers; curves; incline; spirals.
    conv = []; transfers = []; curves = []
    for e, r in tops:
        n = e.dxf.name; w, h = r[2] - r[0], r[3] - r[1]
        if n in ('*U427', '*U429', '*U572', '*U632', '*U748', '*U752', '*U757', '*U758'):
            conv.append({'block': n, 'rect': rnd(r), 'axis': 'x' if w >= h else 'y'})
        elif n == '*U753':
            conv.append({'block': n, 'rect': rnd(r), 'axis': 'y', 'profile': 'incline_0.6_3.0_0.6'})
        elif n == '90 degree curve belt':
            curves.append({'rect': rnd(r)})
        elif .55 < min(w, h) and max(w, h) < .7 and n.startswith('*U'):
            transfers.append(rnd(r))  # right-angle transfer modules (red '+' symbol) at belt junctions
    out['conveyors'] = conv; out['conveyor_curves'] = curves; out['transfers'] = transfers
    sp = []
    for e, r in by_name['Băng tải xoắn ốc 2']:
        rings = [v for v in e.virtual_entities() if v.dxftype() in ('CIRCLE', 'ARC')]
        outer = max(rings, key=lambda v: v.dxf.radius)
        c = outer.ocs().to_wcs(outer.dxf.center)  # mirrored insert: centre is in OCS
        radii = sorted({round(v.dxf.radius / 1000, 3) for v in rings if v.dxf.radius > 900})
        sp.append(((c.x - OX) / 1000, (c.y - OY) / 1000, r, radii))
    sp.sort(key=lambda t: -t[0])
    # Labels: rightmost "Tang 1->2", middle "2->3", leftmost "3->4"; H=0.6 m -> 5.1 m above the lower floor.
    out['spirals'] = [{'x': round(x, 3), 'y': round(y, 3), 'r_inner': radii[0], 'r_outer': radii[-1], 'rect': rnd(r),
                       'from_floor': k + 1, 'to_floor': k + 2, 'z_from': .6, 'z_to': 5.1}
                      for k, (x, y, r, radii) in enumerate(sp)]

    # ---- F4 forklift (block ádasd): forks (yellow) point towards the pallet port.
    for e, r in by_name['ádasd']:
        forks = [extent(c) for c, p in walk([e]) if c.dxf.color == 2]
        forks = [q for q in forks if q]
        fx = max(q[2] for q in forks)
        out['forklift'] = {'fork_tip_x': round(fx, 3), 'y': round((r[1] + r[3]) / 2, 3), 'yaw': 0.0, 'bbox': rnd(r)}

    # ---- Pallet spots (green X, 1.0 x 1.2 m): drawn as loose lines in the PDF -> pair parallel sides.
    hor = []
    for x0, y0, x1, y1 in green_lines:
        a0, b0, a1, b1 = (x0 - ax) / s, (ay - y0) / s, (x1 - ax) / s, (ay - y1) / s
        if abs(b0 - b1) < .02 and .9 < abs(a1 - a0) < 1.3 and ZONE[0] < min(a0, a1) and max(a0, a1) < ZONE[2] and ZONE[1] < b0 < ZONE[3]:
            hor.append((round(min(a0, a1), 2), round(max(a0, a1), 2), round(b0, 2)))
    hor = sorted(set(hor)); spots = []
    for i, (xa, xb, ya) in enumerate(hor):
        for xc, xd, yb in hor[i + 1:]:
            if abs(xa - xc) < .03 and abs(xb - xd) < .03 and .9 < yb - ya < 1.3:
                c = (xa, ya, xb, yb)
                if not taken(c) and not any(abs(c[0] - q[0]) < .1 and abs(c[1] - q[1]) < .1 for q in spots): spots.append(c)
    out['pallet_spots'] = [rnd(c) for c in spots]
    out['unpack_zones'] = []
    for e in msp.query('LWPOLYLINE'):
        r = extent(e)
        if r and inside(r) and r[0] > -4.2 and 5 < r[3] - r[1] < 7.5 and 2.5 < r[2] - r[0] < 3.5: out['unpack_zones'].append(rnd(r))
    dev = []
    for n, kind in (('称重装置', 'weighing'), ('条码扫描', 'barcode_reader'), ('*U391', 'profile_check'), ('hhhh', 'port_gate')):
        for e, r in by_name[n]: dev.append({'kind': kind, 'rect': rnd(r)})
    out['devices'] = dev
    # Areas (text anchors) for reference.
    out['labels'] = []
    for e, path in walk(msp):
        if e.dxftype() not in ('TEXT', 'MTEXT'): continue
        r = extent(e)
        if not r or not inside(r): continue
        t = (e.plain_text() if e.dxftype() == 'MTEXT' else e.dxf.text).replace('\n', ' ').strip()
        if len(t) > 4 and not t.startswith(('A0000', 'mln')): out['labels'].append({'text': t, 'at': rnd(r[:2], 2)})

    json.dump(out, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('building', out['building'], '| plan', {k: len(v) for k, v in plan.items()}, '| columns', len(out['columns']), 'wall posts', len(uniq),
          '| agv paths', len(paths), '| stations', len(stations), '| AGVs', collections.Counter(x['source'] for x in agvs),
          '| storage racks', len(out['rack_cells']), '| workers', collections.Counter(w['area'] for w in workers),
          '| conveyors', len(conv), 'curves', len(curves), 'transfers', len(transfers), '| spirals', len(out['spirals']),
          '| forklift', out.get('forklift'), '| pallet spots', len(spots), '| unpack', len(out['unpack_zones']),
          '| devices', len(dev), '| PDF residual', out['pdf_registration'])

if __name__ == '__main__':
    main()
