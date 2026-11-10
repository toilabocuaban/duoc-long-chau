import time
from gz.transport13 import Node
node=Node()
deadline=time.monotonic()+180
start=time.monotonic()
last_report=0
print('[1/3] Đang nạp mô hình 3D nhánh test (445 models: Kho 9 tầng + Nhà 4 tầng + Dock xe tải)...',flush=True)
while time.monotonic()<deadline:
    elapsed=int(time.monotonic()-start)
    if elapsed>=last_report+5 and elapsed>0:
        last_report=elapsed
        print(f'   -> Đang tải môi trường 3D... ({elapsed}s)',flush=True)
    if '/gui/move_to/pose' in node.service_list():
        print(f'[2/3] Warehouse camera and rendering ready. ({elapsed}s)',flush=True)
        break
    time.sleep(1)
else: raise SystemExit('Gazebo GUI did not initialize within 180 seconds. See gazebo.log.')
