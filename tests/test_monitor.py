import importlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        os.environ['NETWORK_DATABASE']=self.tmp.name+'/test.db'
        import database
        self.db=importlib.reload(database)
        self.db.create_table()
        import app
        self.app=importlib.reload(app)
        self.client=self.app.app.test_client()
        self.id=self.db.get_devices()[0]['id']
    def tearDown(self):
        self.tmp.cleanup()
    def test_failure_recovery(self):
        self.assertEqual([self.db.record_probe(self.id,False,None) for _ in range(4)],['DEGRADED','DEGRADED','DOWN','DOWN'])
        self.assertEqual(len(self.db.get_alerts()),1)
        self.assertEqual(self.db.record_probe(self.id,False,None,'permission denied'),'ERROR')
        self.assertEqual(self.db.record_probe(self.id,True,1.2),'RECOVERED')
        self.assertEqual(self.db.record_probe(self.id,True,2),'UP')
        self.assertEqual(len(self.db.get_alerts()),2)
        report=self.db.get_report()[0]
        self.assertEqual((report['probes'],report['lost'],report['packet_loss']),(6,4,66.67))
    def test_routes_and_validation(self):
        for route in ['/','/api/devices','/api/history','/report.csv']:
            self.assertEqual(self.client.get(route).status_code,200)
        before=len(self.db.get_devices())
        self.client.post('/add',data={'device_name':'bad','ip_address':'invalid'})
        self.client.post('/add',data={'device_name':'duplicate','ip_address':'8.8.8.8'})
        self.assertEqual(len(self.db.get_devices()),before)
        self.client.post('/add',data={'device_name':'Lab','ip_address':'192.0.2.1'})
        target=self.db.get_devices()[-1]
        self.client.post('/delete/'+str(target['id']))
        self.assertIsNone(self.db.get_device_by_id(target['id']))
    def test_legacy_migration(self):
        self.db.DATABASE=self.tmp.name+'/legacy.db'
        with closing(sqlite3.connect(self.db.DATABASE)) as c, c:
            c.execute('CREATE TABLE devices(id INTEGER PRIMARY KEY,device_name TEXT,ip_address TEXT,status TEXT,rtt REAL)')
            c.execute("INSERT INTO devices VALUES(1,'Existing','8.8.8.8','UP',1)")
        self.db.create_table()
        self.db.create_table()
        self.assertEqual(len(self.db.get_devices()),3)
        self.assertEqual(self.db.get_devices()[0]['device_name'],'Existing')
        self.db.record_probe(1,False,None)
        self.assertEqual(self.db.get_devices()[0]['consecutive_failures'],1)
    def test_monitor_integration_and_start_guard(self):
        with patch.object(self.app,'check_device',return_value=(True,0.5,None)):
            self.app.monitor_device(self.db.get_devices()[0])
        self.assertEqual(self.client.get('/api/devices').json['statistics']['up'],1)
        with patch.object(self.app,'monitoring_loop',side_effect=lambda:self.app.stop_event.wait(2)):
            self.assertTrue(self.app.start_monitor())
            self.assertFalse(self.app.start_monitor())
            self.app.stop_event.set()
            self.app.monitor_thread.join()
            self.app.lock_file.close()

    def test_incidents_metrics_and_notes(self):
        from datetime import datetime,timedelta,timezone
        from insights import snapshot
        base=datetime.now(timezone.utc)-timedelta(minutes=2)
        for seconds,alive in [(0,False),(10,False),(20,False),(60,True)]:
            with patch.object(self.db,'now',return_value=(base+timedelta(seconds=seconds)).isoformat()):
                self.db.record_probe(self.id,alive,5 if alive else None)
        data=snapshot()
        self.assertEqual(data['summary']['total'],1)
        self.assertEqual(data['summary']['open'],0)
        self.assertEqual(data['summary']['mttr_seconds'],40)
        metric=next(m for m in data['metrics'] if m['id']==self.id)
        self.assertEqual(metric['availability'],25)
        self.assertEqual(metric['p95_rtt'],5)
        incident=data['incidents'][0]
        response=self.client.post('/api/incidents/'+str(incident['id'])+'/acknowledge',json={'note':'Cable checked'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(snapshot()['incidents'][0]['note'],'Cable checked')
        self.assertEqual(self.client.post('/api/incidents/999/acknowledge',json={'note':[]}).status_code,400)
        self.assertEqual(self.client.get('/review').status_code,200)

    def test_probe_error_and_retired_incident(self):
        from insights import snapshot
        self.db.record_probe(self.id,False,None,'socket denied')
        self.assertEqual(snapshot()['summary']['total'],0)
        for _ in range(3):self.db.record_probe(self.id,False,None)
        self.assertEqual(snapshot()['summary']['open'],1)
        self.db.delete_device(self.id)
        data=snapshot()
        self.assertEqual(data['summary']['open'],0)
        self.assertIsNotNone(data['incidents'][0]['retired_at'])
        self.assertIsNone(data['incidents'][0]['recovered_at'])

    def test_errors_break_unconfirmed_miss_sequence(self):
        self.db.record_probe(self.id,False,None)
        self.db.record_probe(self.id,False,None)
        self.db.record_probe(self.id,False,None,'local socket error')
        self.assertEqual(self.db.record_probe(self.id,False,None),'DEGRADED')
        self.assertEqual(self.db.get_device_by_id(self.id)['consecutive_failures'],1)
        self.assertEqual(self.client.get('/api/insights').json['summary']['total'],0)

    def test_manual_check_and_overlap_guard(self):
        with patch.object(self.app,'check_device',return_value=(True,3.5,None)):
            response=self.client.post('/api/devices/'+str(self.id)+'/check')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['device']['rtt'],3.5)
        lock=self.app.device_probe_lock(self.id)
        with lock:
            self.assertEqual(self.client.post('/api/devices/'+str(self.id)+'/check').status_code,409)
        self.assertEqual(len(self.db.get_history()),1)
        self.assertEqual(self.client.post('/api/devices/99999/check').status_code,404)
        self.assertEqual(self.client.get('/history.csv').status_code,200)

    def test_stale_success_not_counted_as_current_up(self):
        from datetime import datetime,timedelta,timezone
        with patch.object(self.db,'now',return_value=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()):
            self.db.record_probe(self.id,True,1.0)
        data=self.client.get('/api/devices').json
        self.assertTrue(data['devices'][0]['is_stale'])
        self.assertEqual(data['statistics']['up'],0)
        self.assertEqual(data['statistics']['stale'],1)
        self.assertEqual(self.client.get('/api/demo').status_code,404)

if __name__=='__main__':
    unittest.main()
