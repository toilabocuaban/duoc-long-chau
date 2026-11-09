#!/bin/bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
export GZ_SIM_RESOURCE_PATH="$PWD:${GZ_SIM_RESOURCE_PATH:-}"
python3 -m unittest test_agv_traffic test_motion_profile test_scheduler test_chain_conveyor test_stock test_rails test_agv_motion -v
python3 build_world.py
python3 verify.py
