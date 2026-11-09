"""Shared rail/chassis geometry. All dimensions are metres."""
WHEEL_RADIUS=.105
WHEEL_CENTRE_Z=-.16
RAIL_TOP_OFFSET=WHEEL_CENTRE_Z-WHEEL_RADIUS
RAIL_HEIGHT=.10
X_GAUGE=.98
Y_GAUGE=.96
from cad_navigation import LAYOUT
X_LINES=LAYOUT.data['lane_x']+LAYOUT.data['trunk_x']
Y_LINES=LAYOUT.data['road_y']

def deck_name(s): return s.replace('shuttle_','shuttle_deck_')

def wheel_name(s,axis): return s.replace('shuttle_',f'shuttle_wheels_{axis}_')

def supported(x,y,axis):
    return LAYOUT.supported(x,y,axis)
