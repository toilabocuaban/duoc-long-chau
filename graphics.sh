#!/bin/bash
export QT_QPA_PLATFORM=xcb
if [[ "${WAREHOUSE_SOFTWARE_RENDERING:-0}" == "1" || ! -e /dev/dxg ]]; then
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
else
    export MESA_LOADER_DRIVER_OVERRIDE=d3d12
    export GALLIUM_DRIVER=d3d12
fi
