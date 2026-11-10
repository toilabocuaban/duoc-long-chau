#!/bin/bash
# Opens the 4-floor picking building scene (layout review; no process controller yet).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source ./graphics.sh
export GZ_SIM_RESOURCE_PATH="$PWD:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_PARTITION=four_floor
if [[ ! -s four-floor.sdf || four_floor_layout.json -nt four-floor.sdf || four_floor_scene.py -nt four-floor.sdf || config.json -nt four-floor.sdf || sdf_parts.py -nt four-floor.sdf ]]; then
    echo 'Building 4-floor scene...'
    python3 four_floor_scene.py
else
    echo 'Using cached 4-floor scene.'
fi
gz sim -r --render-engine ogre four-floor.sdf > four-floor.log 2>&1
