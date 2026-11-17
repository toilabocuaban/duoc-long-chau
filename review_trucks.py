"""Render the generated warehouse with Gazebo camera sensors in an isolated run.
Upper storeys are hidden only in the review to expose floor-1 dock alignment.
"""
import math
import os
import signal
import subprocess
import time
from pathlib import Path
import xml.etree.ElementTree as E

ROOT=Path(__file__).resolve().parent
out=ROOT/'previews'/'truck-review';out.mkdir(parents=True,exist_ok=True)
tree=E.parse(ROOT/'warehouse.sdf');w=tree.getroot().find('world')
w.remove(w.find('gui'))
for m in list(w.findall('model')):
    if not m.attrib['name'].startswith(('ff1_', 'truck_dock_', 'cad_dock_apron', 'site')):
        w.remove(m)
E.SubElement(w,'plugin',filename='gz-sim-sensors-system',name='gz::sim::systems::Sensors').append(E.fromstring('<render_engine>ogre</render_engine>'))
views={'detail':((-22,-33,7),(-30,-16,.8)),
       'layout':((-26,-47,40),(-26,-8,0))}
for name,(pos,target) in views.items():
    folder=out/name;folder.mkdir(exist_ok=True)
    yaw=math.atan2(target[1]-pos[1],target[0]-pos[0]); pitch=math.atan2(pos[2]-target[2],math.hypot(pos[0]-target[0],pos[1]-target[1]))
    w.append(E.fromstring(f'''<model name="review_{name}"><static>true</static><pose>{pos[0]} {pos[1]} {pos[2]} 0 {pitch} {yaw}</pose><link name="camera"><sensor name="{name}" type="camera"><topic>/review/{name}</topic><always_on>true</always_on><update_rate>1</update_rate><camera><horizontal_fov>1.05</horizontal_fov><image><width>1600</width><height>1000</height></image><clip><near>.1</near><far>180</far></clip><save enabled="true"><path>{folder.as_posix()}</path></save></camera></sensor></link></model>'''))
path=out/'review.sdf';tree.write(path,encoding='utf-8',xml_declaration=True)
env={**os.environ,'GZ_PARTITION':f'truck_review_{os.getpid()}','LIBGL_ALWAYS_SOFTWARE':'1','GALLIUM_DRIVER':'llvmpipe','QT_QPA_PLATFORM':'xcb'}
env.pop('MESA_LOADER_DRIVER_OVERRIDE',None)
os.environ['GZ_PARTITION']=env['GZ_PARTITION']
from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image
import struct
import zlib
def save_png(path,msg):
    def chunk(tag,data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    raw=b''.join(b'\0'+msg.data[y*msg.step:y*msg.step+msg.width*3] for y in range(msg.height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',msg.width,msg.height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))
node=Node();received={};callbacks=[]
for name in views:
    def receive(msg,name=name):
        if msg.width and msg.height:
            save_png(out/f'{name}.png',msg)
            received[name]=True
    callbacks.append(receive);node.subscribe(Image,f'/review/{name}',receive)
started=time.time()
with (out/'render.log').open('w') as log:
    proc=subprocess.Popen(['gz','sim','-s','-r','-v','4',str(path)],env=env,stdout=log,stderr=log,start_new_session=True)
    try:
        while time.time()-started<100:
            frames={n:[p for p in (out/n).glob('*.png') if p.stat().st_mtime>=started] for n in views}
            if len(received)==len(views):
                for n in views: print(out/f'{n}.png',flush=True)
                break
            if all(len(f)>=2 for f in frames.values()):
                import shutil
                for n,f in frames.items():
                    dest=out/f'{n}.png';shutil.copy2(sorted(f,key=lambda p:p.stat().st_mtime)[-1],dest);print(dest,flush=True)
                break
            if proc.poll() is not None: raise RuntimeError(f'Gazebo rendering exited {proc.returncode}: {out / "render.log"}')
            time.sleep(1)
        else:raise TimeoutError(f'Gazebo camera capture timed out: {out / "render.log"}')
    finally:
        for name in views:node.unsubscribe(f'/review/{name}')
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(10)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
