"""Optional local performance check. Uses a temporary DB and sends no ICMP packets."""
import os
import tempfile
import time
from contextlib import closing

def run():
    with tempfile.TemporaryDirectory() as temp:
        os.environ['NETWORK_DATABASE']=temp+'/benchmark.sqlite'
        import database as db
        import insights
        db.create_table()
        with closing(db.get_connection()) as c,c:
            for index in range(100):
                c.execute('INSERT INTO devices(device_name,ip_address) VALUES (?,?)',(f'Benchmark target {index}',f'10.20.0.{index+1}'))
            c.execute('UPDATE devices SET archived_at=? WHERE id<=3',(db.now(),))
            for device in c.execute('SELECT * FROM devices WHERE archived_at IS NULL').fetchall():
                c.executemany('INSERT INTO monitoring_history(device_id,status,rtt,checked_at,success,device_name,ip_address,revision) VALUES (?,?,?,?,?,?,?,?)',[(device['id'],'UP',float(i%20+1),db.now(),1,device['device_name'],device['ip_address'],0) for i in range(200)])
        started=time.perf_counter()
        metrics=insights.snapshot()
        reports=db.get_report()
        elapsed=time.perf_counter()-started
        assert len(metrics['metrics'])==100 and len(reports)==100
        assert sum(m['probes'] for m in metrics['metrics'])==20000
        print(f'100 targets / 20,000 stored probes: analytics + report {elapsed:.3f}s')
        print('Temporary database removed on exit. No network probes sent.')

if __name__=='__main__':run()
