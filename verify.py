import json
import os
from pathlib import Path
import signal
import subprocess
import sys

root=Path(__file__).resolve().parent
os.chdir(root)
os.environ['GZ_PARTITION']='warehouse_v1_verify'
with (root/'test-server.log').open('w') as log:
    server=subprocess.Popen(['gz','sim','-s','-r','warehouse.sdf'],stdout=log,stderr=log,start_new_session=True)
    try:
        result=subprocess.run([sys.executable,'controller.py','--fast','--jobs','18'],capture_output=True,text=True,timeout=240)
        (root/'test-controller.log').write_text(result.stdout+result.stderr)
        assert result.returncode==0, result.stderr
        result_data=json.loads((root/'validation.json').read_text())
        assert result_data['complete']==18 and result_data['inbound_stored']==18, result_data
        assert result_data.get('dual_agv_motion_ticks',0)>0,result_data
        assert result_data.get('same_direction_following_ticks',0)>0,result_data
        assert result_data.get('multiple_shuttle_motion_ticks',0)>0,result_data
        events=[json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()]
        config=json.loads((root/'config.json').read_text())
        def stamp(job,*states):return next(e['demo_time_s'] for e in events if e['job']==job and e['state'] in states)
        # An AGV either leaves its parking box or is redispatched on the return lane.
        assert stamp(1,'ALIGN_DEPARTURE','TURN_TOWARDS_PICKUP')<stamp(0,'AGV_HANDOVER_CHAIN')
        assert stamp(1,'AGV_CARRY_TO_INBOUND')<stamp(0,'AGV_RETURN_COMPLETE','AGV_REDISPATCH')
        # Every inbound job must release its AGV one way or the other.
        released=[e['job'] for e in events if e['state'] in ('AGV_RETURN_COMPLETE','AGV_REDISPATCH')]
        assert sorted(released)==list(range(18)),released
        ready={}
        for e in events:
            if e['state']=='LIFT_READY':
                assert e['settle_elapsed_s']>=config['lift_settle_s']-1e-4,e
                assert e['velocity_m_s']==0,e
                ready[(e['job'],e['lift'])]=e
            if e['state'] in ['INPUT_BUFFER_TRANSFER','OUTPUT_CABIN_TRANSFER','OUTPUT_GATE_TRANSFER']:
                prefix='in_lift_' if e['state']=='INPUT_BUFFER_TRANSFER' else 'out_lift_'
                matches=[r for (job,lift),r in ready.items() if job==e['job'] and lift.startswith(prefix)]
                assert matches and max(r['demo_time_s'] for r in matches)<=e['demo_time_s'],e

        for state,key in [('INBOUND_ARRIVAL','inbound_interval_s'),('OUTBOUND_REQUEST_QUEUED','outbound_interval_s')]:
            stamps=[e['demo_time_s'] for e in events if e['state']==state]
            assert len(stamps)==18,(state,stamps)
            assert all(abs((b-a)-config[key])<.04 for a,b in zip(stamps,stamps[1:])),(state,stamps)
        assert result_data.get('max_concurrent_motions',0)>=2,result_data
        n_loose=config.get('loose_pallets',0)*config.get('loose_cartons_per_pallet',0) if config.get('loose_pallet_spots') else 0
        assert result_data.get('loose_cartons_delivered',0)==n_loose,result_data
        if n_loose>=len(config['output_levels']):   # cartons reach every floor of the 4-floor building
            assert len(result_data['loose_cartons_per_floor'])==len(config['output_levels']),result_data
        assert result_data.get('inbound_outbound_overlap_ticks',0)>0,result_data
        (root/'verified-result.json').write_text(json.dumps(result_data,indent=2))
        print(json.dumps(result_data))
    finally:
        try:os.killpg(server.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:os.killpg(server.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            server.wait(timeout=5)
