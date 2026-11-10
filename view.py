import math
import sys
import time
from gz.transport13 import Node
from gz.msgs10.gui_camera_pb2 import GUICamera
from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.pose_pb2 import Pose

views={'agv':(-18,-16,8,(-28,-5,1)), 'building':(-72,-28,36,(-26,20,5)), 'loose':(0.5,-6,6,(-7,1.5,1.5)), 'overview':(40,-45,35,(8,16,5)), 'rack':(30,-30,23,(25,23,8)), 'rail':(26,-4,8,(20.30108,3.4192,4.3)), 'lane':(20.30108,6.5,5.5,(20.30108,24,4.7)), 'cad_top':(26.3,25,70,(26.3,25,0)), 'equipment':(6,-6,3,(2,0,.7)), 'shuttle_detail':(5.6,-2,1.6,(3.7,0,.3)), 'lift_detail':(3.4,-4,2.7,(0,0,1.1)), 'inbound_detail':(4,-6,3.2,(0,-1.3,.7)), 'outbound_detail':(2.5,9,4.5,(6.3,5.3,.45))}
views.update({'docks':(-26,-47,26,(-25,-10,0)), 'truck':(-22,-33,7,(-30,-16,.8))})
x,y,z,target=views[sys.argv[1] if len(sys.argv)>1 else 'overview']
yaw=math.atan2(target[1]-y,target[0]-x)
if len(sys.argv)>1 and sys.argv[1]=='cad_top': yaw=math.pi/2
pitch=math.atan2(z-target[2],math.hypot(target[0]-x,target[1]-y))
m=GUICamera();m.pose.position.x=x;m.pose.position.y=y;m.pose.position.z=z
m.pose.orientation.w=math.cos(yaw/2)*math.cos(pitch/2)
m.pose.orientation.x=-math.sin(yaw/2)*math.sin(pitch/2)
m.pose.orientation.y=math.cos(yaw/2)*math.sin(pitch/2)
m.pose.orientation.z=math.sin(yaw/2)*math.cos(pitch/2)
node=Node();last=[]
def observe(msg): last[:]=[msg.position.x,msg.position.y,msg.position.z]
ok,response=node.request('/gui/move_to/pose',m,GUICamera,Boolean,5000)
if not ok or not response.data: raise SystemExit('Camera unavailable: open Warehouse V1 first.')
node.subscribe(Pose,'/gui/camera/pose',observe)
deadline=time.monotonic()+15
while time.monotonic()<deadline:
    if last and math.dist(last,[x,y,z])<.1: break
    time.sleep(.1)
else: raise SystemExit(f'Camera transition not confirmed. Last pose: {last}')
