#!/bin/bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source ./graphics.sh
export GZ_SIM_RESOURCE_PATH="$PWD:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_PARTITION=warehouse_equipment
python3 build_world.py
exec gz sim -r --render-engine ogre equipment-detail.sdf
