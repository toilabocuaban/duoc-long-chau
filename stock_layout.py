"""Illustrative occupancy with explicit reservations for the demonstration routes."""
import json
import math
from pathlib import Path
from cad_navigation import LAYOUT, initial_stock_cell

def clear_for_routes(x,y):
    return any(abs(s['x']-x)<.005 and abs(s['y']-y)<.005 and s['id'] not in LAYOUT.reserved for s in LAYOUT.slots.values())

def stock_positions(levels):
    # Cells holding real (movable) initial pallets are skipped so the static decoration never overlaps them.
    n=int(json.loads((Path(__file__).parent/'config.json').read_text()).get('initial_outbound_pallets',0))
    used={initial_stock_cell(i) for i in range(n)}
    result=[]
    for level,z in enumerate(levels):
        for i,s in enumerate(LAYOUT.slots.values()):
            if s['id'] in LAYOUT.reserved or (s['id'],level) in used or (i*7+level*3)%10>=4:continue
            result.append((s['x'],s['y'],z))
    return result
