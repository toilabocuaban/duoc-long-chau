"""Illustrative occupancy with explicit reservations for the demonstration routes."""
import math
from cad_navigation import LAYOUT

def clear_for_routes(x,y):
    return any(abs(s['x']-x)<.005 and abs(s['y']-y)<.005 and s['id'] not in LAYOUT.reserved for s in LAYOUT.slots.values())

def stock_positions(levels, threshold=None):
    if threshold is None:
        threshold = 2 if len(levels) == 1 else 1
    result=[]
    for level,z in enumerate(levels):
        for i,s in enumerate(LAYOUT.slots.values()):
            if s['id'] in LAYOUT.reserved or (i*7+level*3)%10 >= threshold:
                continue
            result.append((s['x'],s['y'],z))
    return result
