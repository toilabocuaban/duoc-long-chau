import time
from gz.transport13 import Node
node=Node()
deadline=time.monotonic()+180
while time.monotonic()<deadline:
    if '/gui/move_to/pose' in node.service_list():
        print('Warehouse camera and rendering ready.',flush=True)
        break
    time.sleep(1)
else: raise SystemExit('Gazebo GUI did not initialize within 180 seconds. See gazebo.log.')
