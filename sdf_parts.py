"""Shared SDF visual helpers: primitive visuals and material-batched box meshes (binary STL)."""
import math
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent

def box(name, x, y, z, sx, sy, sz, color, yaw=0):
    return f'<visual name="{name}"><pose>{x} {y} {z} 0 0 {yaw}</pose><geometry><box><size>{sx} {sy} {sz}</size></box></geometry><material><ambient>{color}</ambient><diffuse>{color}</diffuse></material></visual>'

def cylinder(name,x,y,z,r,length,color,roll=0,pitch=0):
    return f'<visual name="{name}"><pose>{x} {y} {z} {roll} {pitch} 0</pose><geometry><cylinder><radius>{r}</radius><length>{length}</length></cylinder></geometry><material><ambient>{color}</ambient><diffuse>{color}</diffuse><specular>.25 .25 .25 1</specular></material></visual>'

def strut(name,a,b,width,color):
    dx,dy,dz=[b[i]-a[i] for i in range(3)]
    length=math.sqrt(dx*dx+dy*dy+dz*dz)
    yaw=math.atan2(dy,dx);pitch=-math.atan2(dz,math.hypot(dx,dy))
    v=box(name,*[(a[i]+b[i])/2 for i in range(3)],length,width,width,color)
    return v.replace(f'0 0 0</pose>',f'0 {pitch} {yaw}</pose>')

def merge_boxes(name,visuals):
    """Batch thousands of structural members (boxes and cylinders) by material for responsive rendering."""
    import xml.etree.ElementTree as ET
    import struct
    groups={}
    passthrough=[]
    for visual in visuals:
        e=ET.fromstring(visual)
        if e.find('transparency') is not None or e.find('geometry/mesh') is not None:
            passthrough.append(visual)
            continue
        pose_el=e.find('pose')
        px,py,pz,r,p,y=map(float,pose_el.text.split()) if pose_el is not None else (0.,0.,0.,0.,0.,0.)
        cr,sr,cp,sp,cy,syaw=math.cos(r),math.sin(r),math.cos(p),math.sin(p),math.cos(y),math.sin(y)
        def xform(x0,y0,z0):
            y1,z1=cr*y0-sr*z0,sr*y0+cr*z0
            x2,z2=cp*x0+sp*z1,-sp*x0+cp*z1
            return (px+cy*x2-syaw*y1,py+syaw*x2+cy*y1,pz+z2)
        color=e.find('material/diffuse').text
        tris=groups.setdefault(color,[])
        size=e.find('geometry/box/size')
        if size is not None:
            sx,sy,sz=map(float,size.text.split())
            points=[xform(xx*sx/2,yy*sy/2,zz*sz/2) for xx,yy,zz in [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]]
            for f in [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]:
                tris.append([points[i] for i in f])
            continue
        cyl=e.find('geometry/cylinder')
        if cyl is not None:
            rad=float(cyl.find('radius').text);length=float(cyl.find('length').text);hz=length/2
            seg=12
            tc=xform(0.,0.,hz);bc=xform(0.,0.,-hz)
            ring_t=[xform(rad*math.cos(2*math.pi*k/seg),rad*math.sin(2*math.pi*k/seg),hz) for k in range(seg)]
            ring_b=[xform(rad*math.cos(2*math.pi*k/seg),rad*math.sin(2*math.pi*k/seg),-hz) for k in range(seg)]
            for k in range(seg):
                kn=(k+1)%seg
                tris.append([ring_b[k],ring_b[kn],ring_t[kn]])
                tris.append([ring_b[k],ring_t[kn],ring_t[k]])
                tris.append([tc,ring_t[k],ring_t[kn]])
                tris.append([bc,ring_b[kn],ring_b[k]])
            continue
        raise ValueError('Box or cylinder geometry required for structural batching')
    result=list(passthrough)
    batches=[(color,tris[a:a+21000]) for color,tris in groups.items() for a in range(0,len(tris),21000)]
    for i,(color,tris) in enumerate(batches):
        path=ROOT/'assets'/f'{name}_{i}.stl';path.parent.mkdir(exist_ok=True)
        with path.open('wb') as f:
            f.write(b'Warehouse structural members'.ljust(80,b'\0'));f.write(struct.pack('<I',len(tris)))
            for a,b,c in tris:
                u=[b[j]-a[j] for j in range(3)];v=[c[j]-a[j] for j in range(3)]
                n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
                norm=math.sqrt(sum(x*x for x in n)) or 1.0;n=[x/norm for x in n]
                f.write(struct.pack('<12fH',*n,*a,*b,*c,0))
        result.append(f'<visual name="{name}_{i}"><geometry><mesh><uri>{escape(str(path))}</uri></mesh></geometry><material><ambient>{color}</ambient><diffuse>{color}</diffuse><specular>.35 .35 .35 1</specular></material></visual>')
    return result
