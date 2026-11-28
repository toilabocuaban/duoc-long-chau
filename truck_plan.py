"""Daily inbound truck plan, from the Excel sheet 'Duoc Long Chau Zone C 03 Inbound Docking'.

Per day (config.json):
  inbound_trucks_per_day            60 trucks
  inbound_cartons_per_truck        200 cartons (mixed even + odd goods on every truck)
  warehouse_inbound_target         150 even pallets (1000x1200x1400)
  warehouse_inbound_odd_gt10_target 130 odd pallets, 3-5 SKUs, > 10 cartons per SKU
  warehouse_inbound_odd_lt10_target 120 odd pallets, 3-5 SKUs, < 10 cartons per SKU
  inbound_truck_interval_s         spacing of truck arrivals (default: shift length / trucks)

400 pallets over 60 trucks is 6.67 pallets per truck, so 40 trucks bring 7 pallets and 20 bring 6.
A truck's 200 cartons are split evenly over its pallets (28-29 or 33-34 cartons per pallet), so the
day totals match the Excel exactly: 60 trucks, 12,000 cartons, 150 even + 130 + 120 odd pallets.
Every split uses the same even spreading, so the plan is deterministic and identical every day.
"""


def spread(total, n):
    """n integers summing to total, as equal as possible and evenly interleaved (Bresenham)."""
    if n <= 0:
        return []
    return [((k + 1) * total) // n - (k * total) // n for k in range(n)]


def _pick(k, chosen, n):
    """True when item k of n is one of the `chosen` items spread evenly over the n."""
    return n > 0 and ((k + 1) * chosen) // n > (k * chosen) // n


def day_targets(c):
    return {
        'trucks': max(1, int(c.get('inbound_trucks_per_day', 60))),
        'cartons_per_truck': max(1, int(c.get('inbound_cartons_per_truck', 200))),
        'even': max(0, int(c.get('warehouse_inbound_target', 150))),
        'odd_gt10': max(0, int(c.get('warehouse_inbound_odd_gt10_target', 130))),
        'odd_lt10': max(0, int(c.get('warehouse_inbound_odd_lt10_target', 120))),
    }


def build_day_plan(c, day=0, first_truck_id=0):
    """List of trucks for one day: {'truck_id', 'day', 'arrival_s', 'cartons', 'pallets': [...]},
    each pallet {'kind': 'even'|'odd', 'odd_type': None|'gt10'|'lt10', 'cartons': n}.
    The pallet type is fixed here, before the truck is unloaded (it is known from the delivery)."""
    t = day_targets(c)
    trucks, cartons = t['trucks'], t['cartons_per_truck']
    n_odd = t['odd_gt10'] + t['odd_lt10']
    day_s = float(c.get('cycle_duration_s', 28800.0) or 28800.0)
    interval = float(c.get('inbound_truck_interval_s', day_s / trucks) or (day_s / trucks))
    pallets_per_truck = spread(t['even'] + n_odd, trucks)
    even_per_truck = spread(t['even'], trucks)
    odd_types = ['gt10' if _pick(o, t['odd_gt10'], n_odd) else 'lt10' for o in range(n_odd)]
    plan, o = [], 0
    for k in range(trucks):
        n = max(1, pallets_per_truck[k])
        e = min(even_per_truck[k], n)
        pallets = []
        for p, size in enumerate(spread(cartons, n)):
            if _pick(p, e, n):
                pallets.append({'kind': 'even', 'odd_type': None, 'cartons': size})
            else:
                pallets.append({'kind': 'odd', 'odd_type': odd_types[o] if o < n_odd else 'lt10', 'cartons': size})
                o += 1
        plan.append({'truck_id': first_truck_id + k, 'day': day, 'arrival_s': day * day_s + k * interval,
                     'cartons': cartons, 'pallets': pallets})
    return plan


def max_cartons_per_pallet(c):
    return max(p['cartons'] for tr in build_day_plan(c) for p in tr['pallets'])
