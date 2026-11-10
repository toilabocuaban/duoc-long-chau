#!/bin/bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
pkill -9 -f "gz sim.*warehouse.sdf" 2>/dev/null || true
pkill -9 -f "controller.py" 2>/dev/null || true
source ./graphics.sh
if [[ dock_flow.py -nt warehouse.sdf ]]; then python3 build_world.py; fi
export GZ_SIM_RESOURCE_PATH="$PWD:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_PARTITION=warehouse_v1
if [[ ! -s warehouse.sdf || config.json -nt warehouse.sdf || build_world.py -nt warehouse.sdf || cad_scene.py -nt warehouse.sdf || equipment_visuals.py -nt warehouse.sdf || four_floor_scene.py -nt warehouse.sdf || four_floor_layout.json -nt warehouse.sdf || sdf_parts.py -nt warehouse.sdf || loose_goods.py -nt warehouse.sdf || truck_scene.py -nt warehouse.sdf || dock_layout.json -nt warehouse.sdf ]] || grep -qE '<uri>[A-Za-z]:[\\/]' warehouse.sdf; then
    echo 'Building warehouse model...'
    python3 build_world.py
else
    echo 'Using cached warehouse model.'
fi
gz sim -r --render-engine ogre warehouse.sdf > gazebo.log 2>&1 &
sim_pid=$!
ctl_pid=''
trap 'kill "$sim_pid" ${ctl_pid:+"$ctl_pid"} 2>/dev/null || true' EXIT
python3 wait_gui.py
python3 controller.py > >(tee controller.log) 2>&1 &
ctl_pid=$!
# Closing the Gazebo window must also end this session's controller; otherwise it keeps running and
# attaches to the next window opened on the same partition (two controllers driving one world).
while kill -0 "$sim_pid" 2>/dev/null && kill -0 "$ctl_pid" 2>/dev/null; do sleep 1; done
if ! kill -0 "$sim_pid" 2>/dev/null; then
    kill "$ctl_pid" 2>/dev/null || true
    echo 'Gazebo window closed: controller stopped.'
fi
wait "$ctl_pid" 2>/dev/null || true
wait "$sim_pid" 2>/dev/null || true
