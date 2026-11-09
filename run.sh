#!/bin/bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source ./graphics.sh
export GZ_SIM_RESOURCE_PATH="$PWD:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_PARTITION=warehouse_v1
if [[ ! -s warehouse.sdf || config.json -nt warehouse.sdf || build_world.py -nt warehouse.sdf || cad_scene.py -nt warehouse.sdf || equipment_visuals.py -nt warehouse.sdf ]]; then
    echo 'Building warehouse model...'
    python3 build_world.py
else
    echo 'Using cached warehouse model.'
fi
gz sim -r --render-engine ogre warehouse.sdf > gazebo.log 2>&1 &
sim_pid=$!
trap 'kill "$sim_pid" 2>/dev/null || true' EXIT
python3 wait_gui.py
python3 controller.py | tee controller.log
wait "$sim_pid"
