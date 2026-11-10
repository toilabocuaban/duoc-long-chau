"""Isolated full dock-to-storage / W2 integration, not run by unittest discovery."""
import json,os,signal,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def main():
    env={**os.environ,'GZ_PARTITION':f'dock_test_{os.getpid()}'}
    with (ROOT/'dock-test-server.log').open('w') as log:
        server=subprocess.Popen(['gz','sim','-s','-r',str(ROOT/'warehouse.sdf')],env=env,stdout=log,stderr=log,start_new_session=True)
        try:
            with (ROOT/'dock-test-controller.log').open('w') as ctl:
                result=subprocess.run(['python3',str(ROOT/'controller.py'),'--fast','--jobs','8'],env=env,stdout=ctl,stderr=ctl,timeout=240)
            if result.returncode:raise RuntimeError((ROOT/'dock-test-controller.log').read_text()[-6000:])
            r=json.loads((ROOT/'validation.json').read_text());c=json.loads((ROOT/'config.json').read_text())
            assert r['complete']==8 and r['inbound_stored']==8,r
            assert r['loose_cartons_delivered']==c['loose_pallets']*c['loose_cartons_per_pallet'],r
            assert r['dock_flow']['scanned_even']==8*18,r
            assert r['dock_flow']['scanned_odd']==r['loose_cartons_delivered'],r
            assert r['dock_flow']['empty_pallet_returns']==c['loose_pallets'],r
            print(json.dumps(r,indent=2),flush=True)
        finally:
            if server.poll() is None:
                os.killpg(server.pid,signal.SIGTERM)
                try:server.wait(10)
                except subprocess.TimeoutExpired:os.killpg(server.pid,signal.SIGKILL);server.wait()
if __name__=='__main__':main()
