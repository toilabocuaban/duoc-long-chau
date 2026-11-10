"""Rest-to-rest 7-segment (jerk-limited S-curve) profiles with bounded speed, acceleration and jerk.

Segments: jerk+, constant accel, jerk-, cruise at the configured speed, jerk-, constant decel, jerk+.
Short moves that cannot reach the configured speed (or acceleration) drop the cruise
(or constant-acceleration) segments, so the limits are never exceeded.
"""
import math

MIN_DURATION=.05

def blend(t):
    """Legacy normalised quintic shape; kept for stationary/placeholder plans."""
    t=max(0.,min(1.,t))
    return t*t*t*(10+t*(-15+6*t))

class SCurve:
    def __init__(self,distance,speed,acceleration,jerk):
        if min(speed,acceleration,jerk)<=0:raise ValueError('Motion limits must be positive')
        self.d=d=abs(distance);v,a,j=speed,acceleration,jerk
        def accel_time(vp):
            # Time from rest to vp (and its peak acceleration), jerk-limited.
            return (vp/a+a/j,a) if vp>=a*a/j else (2*math.sqrt(vp/j),math.sqrt(vp*j))
        ta,_=accel_time(v)
        if d>=v*ta:vp=v
        else:
            vp=a/2*(-(a/j)+math.sqrt((a/j)**2+4*d/a))
            if vp<a*a/j:vp=(d*math.sqrt(j)/2)**(2/3)
        self.vp=vp;self.ta,self.ap=accel_time(vp) if vp>0 else (0.,0.)
        self.tj=self.ap/j if j else 0.;self.tca=max(0.,self.ta-2*self.tj);self.j=j
        self.tc=(d-vp*self.ta)/vp if vp>0 else 0.
        self.tc=max(0.,self.tc)
        self.T=2*self.ta+self.tc
        self.duration=max(MIN_DURATION,self.T)
    def _accel_distance(self,t):
        j,ap,tj,tca=self.j,self.ap,self.tj,self.tca
        t=max(0.,min(self.ta,t))
        if t<=tj:return j*t**3/6
        v1=j*tj*tj/2;s1=j*tj**3/6
        if t<=tj+tca:
            u=t-tj;return s1+v1*u+ap*u*u/2
        v2=v1+ap*tca;s2=s1+v1*tca+ap*tca*tca/2;u=t-tj-tca
        return s2+v2*u+ap*u*u/2-j*u**3/6
    def position(self,t):
        """Distance travelled after t seconds (0..T)."""
        if self.d==0:return 0.
        t=max(0.,min(self.T,t))
        if t<=self.ta:return self._accel_distance(t)
        if t<=self.ta+self.tc:return self._accel_distance(self.ta)+self.vp*(t-self.ta)
        return self.d-self._accel_distance(self.T-t)
    def shape(self,u):
        """Normalised progress 0..1 for normalised time u 0..1 (time-stretching keeps limits)."""
        u=max(0.,min(1.,u))
        if self.d==0 or self.T<=0:return u
        return min(1.,max(0.,self.position(u*self.T)/self.d))

def duration(distance,speed,acceleration,jerk):
    return SCurve(distance,speed,acceleration,jerk).duration

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
