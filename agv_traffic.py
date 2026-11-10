"""Time-space reservations allow same-direction following without changing motion profiles."""
from dataclasses import dataclass
import math
from motion_profile import blend,bounds,intersects
from agv_motion import rect,rigid,overlaps

@dataclass
class Plan:
    start:tuple
    end:tuple
    began:float
    duration:float
    loaded:bool=False
    shape:object=None  # normalised motion law of the leg (S-curve from the controller)
    def at(self,time):
        q=(self.shape or blend)((time-self.began)/self.duration)
        return tuple(a+(b-a)*q for a,b in zip(self.start,self.end))

def compatible(candidate,peer,now,following_gap=1.0):
    # Broad phase rejects separated aisles quickly; narrow phase compares occupancy
    # at the SAME time, including a stopped vehicle after its planned leg ends.
    a=bounds([candidate.at(now+k*candidate.duration/40) for k in range(41)])
    remaining=max(0,peer.began+peer.duration-now)
    b=bounds([peer.at(now+k*remaining/40) for k in range(41)])
    if not intersects(a,b,max(.25,following_gap)):return True
    horizon=max(candidate.began+candidate.duration,peer.began+peer.duration)-now
    steps=max(1,math.ceil(horizon/.05))
    for k in range(steps+1):
        t=now+horizon*k/steps;x=candidate.at(t);y=peer.at(t)
        same_heading=abs(math.atan2(math.sin(x[3]-y[3]),math.cos(x[3]-y[3])))<.02
        lateral=abs((y[0]-x[0])*math.sin(x[3])-(y[1]-x[1])*math.cos(x[3]))
        gap=following_gap if same_heading and lateral<.3 else .25
        if (x[0]-y[0])**2+(x[1]-y[1])**2>(6+gap)**2:continue
        if not intersects(bounds([x]),bounds([y]),gap):continue
        def parts(base,loaded):
            result=[rect(base,.83,.65),rect(rigid(base,1.55),.85,.4)]
            if loaded:result.append(rect(rigid(base,1.8),.6,.6))
            return result
        if any(overlaps(a,b,gap) for a in parts(x,candidate.loaded) for b in parts(y,peer.loaded)):return False
    return True
