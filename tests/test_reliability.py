import importlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.saved_env=os.environ.get('NETWORK_DATABASE')
        os.environ['NETWORK_DATABASE']=self.temp.name+'/test.db'
        import database,app
        self.db=importlib.reload(database)
        self.app=importlib.reload(app)
        self.client=self.app.app.test_client()
        self.id=self.db.get_devices()[0]['id']

    def tearDown(self):
        if self.saved_env is None:os.environ.pop('NETWORK_DATABASE',None)
        else:os.environ['NETWORK_DATABASE']=self.saved_env
        self.temp.cleanup()

    def edit(self,**changes):
        device=self.db.get_device_by_id(self.id)
        data={'device_name':device['device_name'],'ip_address':device['ip_address'],'paused':False,**changes}
        return self.client.patch('/api/devices/'+str(self.id),json=data)

    def test_rename_preserves_historical_identity(self):
        self.db.record_probe(self.id,True,5)
        self.assertEqual(self.edit(device_name='Renamed').status_code,200)
        self.db.record_probe(self.id,True,10)
        history=self.db.get_history()
        self.assertEqual([r['device_name'] for r in history],['Renamed','Google DNS'])
        self.assertEqual(self.db.get_report()[0]['probes'],2)

    def test_ip_edit_separates_measurements_and_incidents(self):
        for _ in range(3):self.db.record_probe(self.id,False,None)
        self.assertEqual(self.edit(ip_address='192.0.2.8').status_code,200)
        self.assertEqual(self.db.get_report()[0]['probes'],0)
        self.assertIsNone(self.db.record_probe(self.id,True,5,expected_revision=0))
        self.db.record_probe(self.id,True,2,expected_revision=1)
        self.assertEqual(self.db.get_report()[0]['packet_loss'],0)
        self.assertEqual(self.db.get_history()[-1]['ip_address'],'8.8.8.8')
        for _ in range(3):self.db.record_probe(self.id,False,None)
        incidents=self.client.get('/api/insights').json['incidents']
        self.assertEqual(len(incidents),2)
        self.assertEqual(incidents[1]['closed_reason'],'IP address changed')
        self.assertIsNone(incidents[0]['retired_at'])
        self.db.create_table()
        self.assertEqual(self.client.get('/api/insights').json['summary']['open'],1)

    def test_pause_stops_checks_and_resume_resets_streak(self):
        self.db.record_probe(self.id,False,None)
        self.assertEqual(self.edit(paused=True).status_code,200)
        with patch.object(self.app,'check_device') as ping:
            self.assertFalse(self.app.monitor_device(self.db.get_device_by_id(self.id)))
            self.assertEqual(self.client.post('/api/devices/'+str(self.id)+'/check').status_code,409)
            ping.assert_not_called()
        self.assertEqual(self.client.get('/api/devices').json['statistics']['paused'],1)
        self.edit(paused=False)
        self.assertEqual(self.db.get_device_by_id(self.id)['consecutive_failures'],0)

    def test_archive_keeps_history(self):
        self.db.record_probe(self.id,True,1)
        self.client.post('/delete/'+str(self.id))
        self.assertIsNone(self.db.get_device_by_id(self.id))
        self.assertEqual(len(self.db.get_history()),1)
        self.assertEqual(self.db.storage_info()['archived_devices'],1)
        self.assertTrue(self.db.add_device('Replacement','8.8.8.8'))
        self.db.create_table()
        self.assertEqual(len(self.db.get_history()),1)

    def test_invalid_duplicate_and_ipv6_canonicalization(self):
        self.assertEqual(self.edit(ip_address='1.1.1.1').status_code,409)
        self.assertEqual(self.edit(ip_address='invalid').status_code,400)
        self.assertEqual(self.edit(paused='yes').status_code,400)
        self.assertEqual(self.edit(ip_address='2001:0db8:0000::1').status_code,200)
        self.assertEqual(self.db.get_device_by_id(self.id)['ip_address'],'2001:db8::1')
        self.assertFalse(self.db.add_device('Duplicate IPv6','2001:db8::1'))

    def test_settings_validation_and_persistence(self):
        config={'interval':15,'timeout':1.5,'slow_rtt':80}
        self.assertEqual(self.client.post('/api/settings',json=config).status_code,200)
        self.db.create_table()
        self.assertEqual(self.client.get('/api/settings').json['config'],config)
        self.assertEqual(self.client.get('/api/devices').json['interval'],15)
        for invalid in [{'interval':0},{'timeout':float('nan')},{'slow_rtt':True}]:
            self.assertEqual(self.client.post('/api/settings',json={**config,**invalid}).status_code,400)
        self.assertEqual(self.db.get_settings(),config)

    def test_backup_is_valid_consistent_sqlite(self):
        self.db.record_probe(self.id,True,12)
        response=self.client.get('/backup.sqlite')
        path=self.temp.name+'/restore.sqlite'
        with open(path,'wb') as f:f.write(response.data)
        with closing(sqlite3.connect(path)) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM monitoring_history').fetchone()[0],1)
        self.assertIn('attachment',response.headers['Content-Disposition'])

    def test_cursor_pagination_and_filters(self):
        for _ in range(110):self.db.record_probe(self.id,True,3)
        page=self.client.get('/api/history?device_id='+str(self.id)+'&status=UP').json
        self.assertEqual(len(page['history']),100)
        older=self.client.get('/api/history?before='+str(page['next_cursor'])).json
        self.assertEqual(len(older['history']),10)
        self.assertFalse(set(r['id'] for r in page['history'])&set(r['id'] for r in older['history']))
        self.assertEqual(self.client.get('/api/history?status=NOPE').status_code,400)

    def test_cross_origin_and_rebinding_rejected(self):
        config={'interval':10,'timeout':2,'slow_rtt':100}
        self.assertEqual(self.client.post('/api/settings',json=config,headers={'Origin':'https://example.com'}).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Host':'untrusted.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/settings',json=config,headers={'Origin':'http://localhost'}).status_code,200)

    def test_p95_and_latency_variation(self):
        for value in range(1,21):self.db.record_probe(self.id,True,value)
        metric=self.client.get('/api/insights').json['metrics'][0]
        self.assertEqual(metric['p95_rtt'],19)
        self.assertEqual(metric['jitter'],1)
        self.assertEqual(metric['availability'],100)

    def test_simultaneous_requests_do_not_overlap(self):
        import threading
        entered=threading.Event();release=threading.Event()
        def held_probe(device):
            entered.set();release.wait(2)
            return True,1.0,None
        with patch.object(self.app,'check_device',side_effect=held_probe) as probe:
            thread=threading.Thread(target=self.app.monitor_device,args=(self.db.get_device_by_id(self.id),))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.client.post('/api/devices/'+str(self.id)+'/check').status_code,409)
            finally:
                release.set();thread.join(3)
            self.assertEqual(probe.call_count,1)
        self.assertEqual(len(self.db.get_history()),1)

    def test_legacy_duplicates_can_still_be_renamed(self):
        with closing(self.db.get_connection()) as c,c:
            c.execute("INSERT INTO devices(device_name,ip_address) VALUES ('Legacy duplicate','8.8.8.8')")
        self.assertEqual(self.edit(device_name='Primary DNS').status_code,200)
        self.assertEqual(self.db.get_device_by_id(self.id)['device_name'],'Primary DNS')
