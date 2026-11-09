"""Rest-to-rest quintic profiles with bounded speed, acceleration and jerk."""
import math

def blend(t):
    t=max(0.,min(1.,t))
    return t*t*t*(10+t*(-15+6*t))

def duration(distance,speed,acceleration,jerk):
    if min(speed,acceleration,jerk)<=0:raise ValueError('Motion limits must be positive')
    d=abs(distance)
    return max(.05,1.875*d/speed,math.sqrt((10/math.sqrt(3))*d/acceleration),(60*d/jerk)**(1/3))

def bounds(points):
    """Conservative swept rectangle including forks and load for an AGV leg."""
    xs=[];ys=[]
    for base in points:
        x,y,_,yaw=base;c=math.cos(yaw);s=math.sin(yaw)
        for forward in [-.95,2.6]:
            for side in [-.85,.85]:
                xs.append(x+forward*c-side*s);ys.append(y+forward*s+side*c)
    return min(xs),min(ys),max(xs),max(ys)

def intersects(a,b,margin=.15):
    return not (a[2]+margin<=b[0] or b[2]+margin<=a[0] or a[3]+margin<=b[1] or b[3]+margin<=a[1])
