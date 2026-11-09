"""Single-thread cooperative motion scheduler; one writer for all Gazebo poses."""
from dataclasses import dataclass
import math

@dataclass
class Motion:
    targets:dict
    duration:float
    validate:object=None
    start:object=None
    done:object=None

class Scheduler:
    def __init__(self):self.time=0.;self.tasks={};self.completed=set()
    def add(self,name,sequence):
        if name in self.tasks:raise RuntimeError(f'Task already active: {name}')
        self.completed.discard(name)
        self.tasks[name]={'iterator':iter(sequence),'motion':None,'elapsed':0.,'starts':{}}
    def active(self,name=None):return name in self.tasks if name is not None else bool(self.tasks)
    def advance(self,dt,poses):
        state=dict(poses);changed={};writers={};end=self.time+dt
        for name,task in list(self.tasks.items()):
            remaining=dt;now=self.time
            while True:
                if task['motion'] is None:
                    try:m=next(task['iterator'])
                    except StopIteration:
                        del self.tasks[name];self.completed.add(name);break
                    task['motion']=m;task['elapsed']=0
                    task['starts']={k:tuple(state[k]) for k in m.targets}
                    if m.start:m.start(now)
                m=task['motion'];duration=max(1e-9,m.duration)
                used=min(remaining,max(0,duration-task['elapsed']))
                task['elapsed']+=used;remaining-=used;now+=used
                t=min(1,task['elapsed']/duration)
                update={}
                for entity,target in m.targets.items():
                    if entity in writers and writers[entity]!=name:raise RuntimeError(f'Concurrent writers for {entity}')
                    writers[entity]=name
                    start=task['starts'][entity];start=start+(0,)*(len(target)-len(start))
                    update[entity]=tuple(a+(b-a)*t for a,b in zip(start,target))
                prospective={**state,**update}
                if m.validate:m.validate(prospective)
                state.update(update);changed.update(update)
                if t>=1:
                    if m.done:m.done(now)
                    task['motion']=None
                if remaining<=1e-9:break
        self.time=end
        return changed

def forks_clear(base,pallet,reach=2.4,margin=.12):
    # This handshake is for the north-facing inbound docking pose.
    if abs(math.atan2(math.sin(base[3]-math.pi/2),math.cos(base[3]-math.pi/2)))>.02:return False
    return base[1]+reach<=pallet[1]-.5-margin+1e-6

class InboundInterlock:
    def __init__(self):self.owner=None;self.buffer_occupied=False
    def reserve(self,pallet,*,placed,clear,lift_ready,buffer_free):
        if self.owner is not None or self.buffer_occupied or not all([placed,clear,lift_ready,buffer_free]):
            raise RuntimeError('Inbound handover interlock not satisfied')
        self.owner=pallet
    def received(self,pallet):
        if self.owner!=pallet:raise RuntimeError('Unexpected pallet at buffer')
        self.buffer_occupied=True
    def picked(self,pallet):
        if self.owner!=pallet or not self.buffer_occupied:raise RuntimeError('Buffer is not ready for pickup')
        self.buffer_occupied=False;self.owner=None
