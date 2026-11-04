"""Local ICMP monitor. Run with python app.py (one monitoring process)."""
import atexit
import csv
import fcntl
import io
import logging
import os
import threading
import time
import math
import sqlite3
import tempfile
from contextlib import closing
from urllib.parse import urlsplit
from datetime import datetime, timezone
from math import ceil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response, send_file
from icmplib import ping
import database as db
import insights

app=Flask(__name__)
app.secret_key=os.environ.get('FLASK_SECRET_KEY',os.urandom(32))
MONITOR_INTERVAL=10
PING_TIMEOUT=2
FAILURE_THRESHOLD=3
monitor_lock=threading.Lock()
stop_event=threading.Event()
monitor_thread=None
lock_file=None
health={'last_cycle': None, 'error': None,'cycle_seconds':None}
wake_event=threading.Event()
probe_locks={}
probe_locks_guard=threading.Lock()

def device_probe_lock(device_id):
    with probe_locks_guard:
        return probe_locks.setdefault(device_id,threading.Lock())
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
db.create_table()

def check_device(device):
    try:
        result=ping(device['ip_address'],count=1,timeout=db.get_settings()['timeout'],privileged=False)
        return result.is_alive, result.avg_rtt if result.is_alive else None, None
    except Exception as error:
        return False,None,str(error)

def monitor_device(device):
    lock=device_probe_lock(device['id'])
    if not lock.acquire(blocking=False):
        return False
    try:
        current=db.get_device_by_id(device['id'])
        if current is None or current['paused']:
            return False
        alive,rtt,error=check_device(current)
        state=db.record_probe(current['id'],alive,rtt,error,expected_revision=current['revision'])
        logging.info('%s (%s): %s%s',current['device_name'],current['ip_address'],state, f' / {error}' if error else '')
        return state is not None
    finally:
        lock.release()


def monitoring_loop():
    with ThreadPoolExecutor(max_workers=16,thread_name_prefix='icmp') as pool:
        while not stop_event.is_set():
            wake_event.clear()
            started=time.monotonic()
            config=db.get_settings()
            try:
                futures=[pool.submit(monitor_device,d) for d in db.get_devices() if not d['paused']]
                for future in futures:
                    future.result()
                health.update(last_cycle=db.now(),error=None,cycle_seconds=round(time.monotonic()-started,3))
            except Exception as error:
                health['error']=str(error)
                logging.exception('Monitoring cycle failed; retrying next cycle')
            wake_event.wait(max(0.1, config['interval']-(time.monotonic()-started)))

def start_monitor():
    global monitor_thread, lock_file
    with monitor_lock:
        if monitor_thread and monitor_thread.is_alive():
            return False
        handle=open(str(Path(db.DATABASE).resolve())+'.monitor.lock','a')
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        lock_file=handle
        stop_event.clear()
        monitor_thread=threading.Thread(target=monitoring_loop,daemon=True,name='network-monitor')
        monitor_thread.start()
        return True

atexit.register(stop_event.set)
atexit.register(wake_event.set)

@app.before_request
def local_request_guard():
    # Prevent hostile browser origins and DNS-rebinding hosts from modifying this local app.
    if urlsplit(request.host_url).hostname not in {'127.0.0.1','localhost','::1'}:
        return jsonify(error='Use the local dashboard address.'),403
    if request.method in {'POST','PUT','PATCH','DELETE'}:
        origin=request.headers.get('Origin')
        if request.headers.get('Sec-Fetch-Site')=='cross-site' or (origin and origin.rstrip('/')!=request.host_url.rstrip('/')):
            return jsonify(error='Cross-origin changes are not allowed.'),403

@app.route('/api/devices/<int:device_id>',methods=['PATCH'])
def edit_device(device_id):
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify(error='Enter valid device details.'),400
    name=data.get('device_name');address=data.get('ip_address');paused=data.get('paused',False)
    if not isinstance(name,str) or not 1<=len(name.strip())<=100 or not isinstance(address,str) or not db.validate_ip(address.strip()) or type(paused) is not bool:
        return jsonify(error='Enter a name, valid IP address, and a valid pause setting.'),400
    with device_probe_lock(device_id):
        result=db.edit_device(device_id,name.strip(),address.strip(),paused)
    if result=='missing':return jsonify(error='Device not found.'),404
    if result=='duplicate':return jsonify(error='That IP address is already configured.'),409
    wake_event.set()
    return jsonify(ok=True,device=db.get_device_by_id(device_id))

@app.route('/api/settings',methods=['GET','POST'])
def monitoring_settings():
    if request.method=='GET':return jsonify(config=db.get_settings(),storage=db.storage_info(),failure_threshold=3)
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify(error='Invalid monitoring settings.'),400
    interval=data.get('interval');timeout=data.get('timeout');slow=data.get('slow_rtt')
    if type(interval) is not int or not 5<=interval<=300 or type(timeout) not in [int,float] or not math.isfinite(timeout) or not .5<=timeout<=5 or type(slow) not in [int,float] or not math.isfinite(slow) or not 10<=slow<=5000:
        return jsonify(error='Interval: 5–300 seconds; timeout: 0.5–5 seconds; slow RTT: 10–5000 ms.'),400
    db.save_settings({'interval':interval,'timeout':float(timeout),'slow_rtt':float(slow)})
    wake_event.set()
    return jsonify(ok=True,config=db.get_settings())

@app.route('/backup.sqlite')
def download_backup():
    # SQLite backup includes committed WAL data without copying an inconsistent file.
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'network-monitor-backup.sqlite'
        with closing(db.get_connection()) as src,closing(sqlite3.connect(path)) as dst:
            src.backup(dst)
        data=io.BytesIO(path.read_bytes())
    return send_file(data,mimetype='application/vnd.sqlite3',as_attachment=True,download_name='network-monitor-backup.sqlite',max_age=0)

@app.route('/api/health')
def monitor_health():
    return jsonify(database='ok',monitor_running=bool(monitor_thread and monitor_thread.is_alive()),**health,storage=db.storage_info())

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/devices')
def api_devices():
    devices=db.get_devices()
    config=db.get_settings()
    stale_after=max(30,config['interval']*2+ceil(len(devices)/16)*config['timeout']+5)
    current=datetime.now(timezone.utc)
    for device in devices:
        value=device['last_checked']
        try:
            checked=datetime.fromisoformat(value.replace('Z','+00:00')) if value else None
            if checked and checked.tzinfo is None:checked=checked.replace(tzinfo=timezone.utc)
            device['is_stale']=bool(not device['paused'] and checked and (current-checked).total_seconds()>stale_after)
        except ValueError:
            device['is_stale']=True
    statistics={key:sum(d['status'] in states and not d['is_stale'] and not d['paused'] for d in devices) for key,states in {'up':('UP','RECOVERED'),'degraded':('DEGRADED',),'down':('DOWN',),'error':('ERROR',),'untested':('NOT TESTED',)}.items()}
    statistics['total']=len(devices)
    statistics['paused']=sum(bool(d['paused']) for d in devices)
    statistics['stale']=sum(d['is_stale'] for d in devices)
    return jsonify(devices=devices,statistics=statistics,interval=config['interval'],monitor=health,stale_after=stale_after,config=config)

@app.route('/api/devices/<int:device_id>/check',methods=['POST'])
def check_now(device_id):
    device=db.get_device_by_id(device_id)
    if device is None:return jsonify(error='Device not found.'),404
    if device['paused']:return jsonify(error='Monitoring is paused for this device.'),409
    if not monitor_device(device):return jsonify(error='A check is already running or the device was removed.'),409
    return jsonify(ok=True,device=db.get_device_by_id(device_id))


@app.route('/add',methods=['POST'])
def add():
    name=request.form.get('device_name','').strip()
    address=request.form.get('ip_address','').strip()
    if not name or len(name)>100 or not db.validate_ip(address):
        flash('Enter a device name (1–100 characters) and a valid IPv4 or IPv6 address.')
    elif db.add_device(name,address):
        flash('Device added. Monitoring begins next cycle.')
    else:
        flash('That IP address is already configured.')
    return redirect(url_for('home'))

@app.route('/delete/<int:device_id>',methods=['POST'])
def remove_device(device_id):
    with device_probe_lock(device_id):
        db.delete_device(device_id)
    flash('Device archived. Its monitoring history and incidents are retained.')
    return redirect(url_for('home'))

@app.route('/api/history')
def api_history():
    try:
        device_id=int(request.args['device_id']) if request.args.get('device_id') else None
        before=int(request.args['before']) if request.args.get('before') else None
    except ValueError:return jsonify(error='Invalid history filter.'),400
    status=request.args.get('status') or None
    if status and status not in ['UP','RECOVERED','DOWN','DEGRADED','ERROR','NOT TESTED']:
        return jsonify(error='Invalid history status.'),400
    rows=db.get_history(101,device_id,status,before)
    return jsonify(history=rows[:100],next_cursor=rows[99]['id'] if len(rows)>100 else None,alerts=db.get_alerts(),report=db.get_report(),trends=db.get_trends())

@app.route('/report.csv')
def report_csv():
    rows=db.get_report()
    output=io.StringIO()
    fields=['id','device_name','ip_address','probes','lost','packet_loss','avg_rtt','first_check','last_check']
    writer=csv.DictWriter(output,fieldnames=fields)
    writer.writeheader()
    for row in rows:
        # Prevent spreadsheet formula execution in user-supplied device names.
        if row['device_name'].lstrip().startswith(('=','+','-','@')):
            row['device_name']="'"+row['device_name']
        writer.writerow(row)
    return Response(output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=network-report.csv'})

@app.route('/api/insights')
def api_insights():
    return jsonify(insights.snapshot())

@app.route('/api/incidents/<int:incident_id>/acknowledge',methods=['POST'])
def acknowledge_incident(incident_id):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict) or not isinstance(payload.get('note',''),str) or len(payload.get('note',''))>500:
        return jsonify(error='Enter a note of at most 500 characters.'),400
    if not insights.acknowledge(incident_id,payload.get('note','').strip()):
        return jsonify(error='Incident not found.'),404
    return jsonify(ok=True)

@app.route('/history.csv')
def export_history():
    output=io.StringIO()
    fields=['id','device_id','device_name','ip_address','status','rtt','checked_at','success','error']
    writer=csv.DictWriter(output,fieldnames=fields,extrasaction='ignore')
    writer.writeheader()
    for row in db.get_history(10000):
        for key in ['device_name','ip_address','error']:
            value=row.get(key)
            if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):row[key]="'"+value
        writer.writerow(row)
    return Response(output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=probe-history.csv'})

@app.route('/review')
def review_report():
    return render_template('review.html',data=insights.snapshot(),generated=db.now())

if __name__=='__main__':
    start_monitor()
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT','8765')),debug=False,use_reloader=False)
