"""SQLite storage. Migrations preserve existing devices and history."""
import ipaddress
import os
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

DATABASE = os.environ.get('NETWORK_DATABASE', str(Path(__file__).with_name('network_monitor.db')))
DEFAULT_DEVICES = [('Google DNS', '8.8.8.8'), ('Cloudflare DNS', '1.1.1.1'), ('Localhost', '127.0.0.1')]

def get_connection():
    c = sqlite3.connect(DATABASE, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def create_table():
    with closing(get_connection()) as c, c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute("CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY AUTOINCREMENT, device_name TEXT NOT NULL, ip_address TEXT NOT NULL, status TEXT DEFAULT 'NOT TESTED', rtt REAL)")
        c.execute('CREATE TABLE IF NOT EXISTS monitoring_history (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER NOT NULL, status TEXT NOT NULL, rtt REAL, checked_at TEXT)')
        migrations = {
            'devices': {'consecutive_failures': 'INTEGER DEFAULT 0', 'last_checked': 'TEXT', 'incident_open': 'INTEGER DEFAULT 0', 'last_error': 'TEXT', 'first_miss_at': 'TEXT', 'paused': 'INTEGER DEFAULT 0', 'revision': 'INTEGER DEFAULT 0', 'archived_at': 'TEXT'},
            'monitoring_history': {'success': 'INTEGER', 'error': 'TEXT', 'device_name': 'TEXT', 'ip_address': 'TEXT', 'revision': 'INTEGER DEFAULT 0'},
        }
        for table, fields in migrations.items():
            existing = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            for name, definition in fields.items():
                if name not in existing:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        c.execute("UPDATE devices SET incident_open=1 WHERE status='DOWN' AND archived_at IS NULL")
        c.execute('CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER, device_name TEXT, ip_address TEXT, kind TEXT, message TEXT, created_at TEXT)')
        c.execute('''CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER,
            device_name TEXT, ip_address TEXT, started_at TEXT NOT NULL,
            confirmed_at TEXT NOT NULL, recovered_at TEXT,
            acknowledged_at TEXT, note TEXT DEFAULT '', retired_at TEXT)''')
        if 'retired_at' not in {r['name'] for r in c.execute('PRAGMA table_info(incidents)')}:
            c.execute('ALTER TABLE incidents ADD COLUMN retired_at TEXT')
        index=c.execute("SELECT sql FROM sqlite_master WHERE name='one_open_incident'").fetchone()
        if index and 'retired_at' not in index['sql']:
            c.execute('DROP INDEX one_open_incident')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident ON incidents(device_id) WHERE recovered_at IS NULL AND retired_at IS NULL')
        if 'closed_reason' not in {r['name'] for r in c.execute('PRAGMA table_info(incidents)')}:
            c.execute("ALTER TABLE incidents ADD COLUMN closed_reason TEXT DEFAULT 'Device removed'")
        c.execute('''UPDATE monitoring_history SET
            device_name=(SELECT device_name FROM devices WHERE id=device_id),
            ip_address=(SELECT ip_address FROM devices WHERE id=device_id)
            WHERE device_name IS NULL OR ip_address IS NULL''')
        c.execute('''INSERT INTO incidents(device_id,device_name,ip_address,started_at,confirmed_at)
            SELECT id,device_name,ip_address,COALESCE(last_checked,?),COALESCE(last_checked,?)
            FROM devices d WHERE incident_open=1 AND archived_at IS NULL AND NOT EXISTS
            (SELECT 1 FROM incidents i WHERE i.device_id=d.id AND i.recovered_at IS NULL)''',(now(),now()))
        c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        c.execute('CREATE INDEX IF NOT EXISTS history_device_time ON monitoring_history(device_id,id)')
        c.execute("CREATE INDEX IF NOT EXISTS history_revision_date ON monitoring_history(device_id,revision,datetime(checked_at))")
        c.execute('CREATE INDEX IF NOT EXISTS history_revision_time ON monitoring_history(device_id,revision,id)')
        c.execute("CREATE INDEX IF NOT EXISTS history_check_date ON monitoring_history(datetime(checked_at))")
        if not c.execute("SELECT 1 FROM settings WHERE key='seeded'").fetchone():
            for name, address in DEFAULT_DEVICES:
                c.execute('INSERT INTO devices(device_name,ip_address) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM devices WHERE ip_address=?)', (name,address,address))
            c.execute("INSERT INTO settings VALUES ('seeded','1')")

def seed_devices():
    # Seeding is migrated once; deleting a default device is respected.
    pass

def validate_ip(address):
    try:
        ipaddress.ip_address(address)
        return True
    except ValueError:
        return False

def add_device(name,address):
    address = str(ipaddress.ip_address(address))
    with closing(get_connection()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if any(str(ipaddress.ip_address(r[0])) == address for r in c.execute('SELECT ip_address FROM devices WHERE archived_at IS NULL')):
            return False
        c.execute('INSERT INTO devices(device_name,ip_address) VALUES (?,?)',(name,address))
        return True

def get_devices():
    with closing(get_connection()) as c:
        return [dict(r) for r in c.execute('SELECT * FROM devices WHERE archived_at IS NULL ORDER BY id')]

def get_device_by_id(device_id):
    with closing(get_connection()) as c:
        r=c.execute('SELECT * FROM devices WHERE id=? AND archived_at IS NULL',(device_id,)).fetchone()
        return dict(r) if r else None

def record_probe(device_id, alive, rtt, error=None, expected_revision=None):
    """Atomically record a probe, incident state, and transition notification."""
    timestamp=now()
    with closing(get_connection()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM devices WHERE id=? AND archived_at IS NULL',(device_id,)).fetchone()
        if not row or row['paused'] or (expected_revision is not None and row['revision']!=expected_revision):
            return
        # Local socket/configuration errors are not missed Echo Replies.
        failures=row['consecutive_failures'] or 0
        opened=row['incident_open']
        kind=None
        first_miss=row['first_miss_at']
        if error:
            status='ERROR'
            # An unperformed probe cannot complete a consecutive-miss sequence.
            if not opened:
                failures=0
                first_miss=None
        elif alive:
            failures=0
            first_miss=None
            status='RECOVERED' if opened else 'UP'
            if opened:
                kind='RECOVERY'
            opened=0
        else:
            failures+=1
            first_miss=first_miss or timestamp
            status='DOWN' if failures>=3 else 'DEGRADED'
            if failures>=3 and not opened:
                kind='FAILURE'
                opened=1
        c.execute('UPDATE devices SET status=?,rtt=?,consecutive_failures=?,last_checked=?,incident_open=?,last_error=?,first_miss_at=? WHERE id=?', (status,rtt,failures,timestamp,opened,error,first_miss,device_id))
        c.execute('INSERT INTO monitoring_history(device_id,status,rtt,checked_at,success,error,device_name,ip_address,revision) VALUES (?,?,?,?,?,?,?,?,?)',(device_id,status,rtt,timestamp,None if error else int(alive),error,row['device_name'],row['ip_address'],row['revision']))
        if kind == 'FAILURE':
            c.execute('INSERT OR IGNORE INTO incidents(device_id,device_name,ip_address,started_at,confirmed_at) VALUES (?,?,?,?,?)',
                      (device_id,row['device_name'],row['ip_address'],first_miss,timestamp))
        elif kind == 'RECOVERY':
            c.execute('UPDATE incidents SET recovered_at=? WHERE device_id=? AND recovered_at IS NULL AND retired_at IS NULL',(timestamp,device_id))
        if kind:
            message='ICMP reachable again.' if alive else 'Three consecutive ICMP probes missed; target is unreachable.'
            c.execute('INSERT INTO alerts(device_id,device_name,ip_address,kind,message,created_at) VALUES (?,?,?,?,?,?)',(device_id,row['device_name'],row['ip_address'],kind,message,timestamp))
        return status

def delete_device(device_id):
    # Archive configuration while retaining immutable probe and incident records.
    with closing(get_connection()) as c, c:
        c.execute('UPDATE incidents SET retired_at=?,closed_reason=? WHERE device_id=? AND recovered_at IS NULL AND retired_at IS NULL',(now(),'Device removed',device_id))
        c.execute('UPDATE devices SET archived_at=?,incident_open=0 WHERE id=?',(now(),device_id))


def get_history(limit=100, device_id=None, status=None, before=None):
    clauses=[];args=[]
    if device_id is not None:clauses.append('device_id=?');args.append(device_id)
    if status:clauses.append('status=?');args.append(status)
    if before is not None:clauses.append('id<?');args.append(before)
    sql='SELECT * FROM monitoring_history'
    if clauses:sql+=' WHERE '+' AND '.join(clauses)
    args.append(limit)
    with closing(get_connection()) as c:
        return [dict(r) for r in c.execute(sql+' ORDER BY id DESC LIMIT ?',args)]


def get_alerts():
    with closing(get_connection()) as c:
        return [dict(r) for r in c.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 100')]

def get_report():
    with closing(get_connection()) as c:
        # Legacy rows lack measured success and are excluded from packet statistics.
        return [dict(r) for r in c.execute('''SELECT d.id,d.device_name,d.ip_address,
            COUNT(h.success) AS probes, SUM(CASE WHEN h.success=0 THEN 1 ELSE 0 END) AS lost,
            ROUND(100.0*SUM(CASE WHEN h.success=0 THEN 1 ELSE 0 END)/NULLIF(COUNT(h.success),0),2) AS packet_loss,
            ROUND(AVG(CASE WHEN h.success=1 THEN h.rtt END),2) AS avg_rtt,
            MIN(h.checked_at) AS first_check,MAX(h.checked_at) AS last_check
            FROM devices d LEFT JOIN monitoring_history h ON d.id=h.device_id AND d.revision=h.revision WHERE d.archived_at IS NULL GROUP BY d.id ORDER BY d.id''')]

def get_trends():
    with closing(get_connection()) as c:
        return {str(d['id']): [dict(r) for r in reversed(c.execute('SELECT checked_at,rtt,status FROM monitoring_history WHERE device_id=? AND revision=? ORDER BY id DESC LIMIT 40',(d['id'],d['revision'])).fetchall())] for d in get_devices()}


DEFAULT_SETTINGS={'interval':10,'timeout':2.0,'slow_rtt':100.0}

def get_settings():
    with closing(get_connection()) as c:
        row=c.execute("SELECT value FROM settings WHERE key='monitor_config'").fetchone()
    return {**DEFAULT_SETTINGS,**(json.loads(row['value']) if row else {})}


def save_settings(config):
    with closing(get_connection()) as c,c:
        c.execute("INSERT INTO settings(key,value) VALUES ('monitor_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(config),))


def edit_device(device_id, name, address, paused):
    address=str(ipaddress.ip_address(address))
    with closing(get_connection()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM devices WHERE id=? AND archived_at IS NULL',(device_id,)).fetchone()
        if not row:return 'missing'
        changed=str(ipaddress.ip_address(row['ip_address']))!=address
        if changed:
            for other in c.execute('SELECT id,ip_address FROM devices WHERE archived_at IS NULL AND id!=?',(device_id,)):
                if str(ipaddress.ip_address(other['ip_address']))==address:return 'duplicate'
        if changed:
            c.execute('UPDATE incidents SET retired_at=?,closed_reason=? WHERE device_id=? AND recovered_at IS NULL AND retired_at IS NULL',(now(),'IP address changed',device_id))
            c.execute("UPDATE devices SET revision=revision+1,status='NOT TESTED',rtt=NULL,last_checked=NULL,last_error=NULL,incident_open=0,first_miss_at=NULL,consecutive_failures=0 WHERE id=?",(device_id,))
        elif row['paused']!=paused and not row['incident_open']:
            c.execute("UPDATE devices SET status='NOT TESTED',rtt=NULL,last_checked=NULL,last_error=NULL,first_miss_at=NULL,consecutive_failures=0 WHERE id=?",(device_id,))
        c.execute('UPDATE devices SET device_name=?,ip_address=?,paused=? WHERE id=?',(name,address,int(paused),device_id))
        return 'updated'


def storage_info():
    with closing(get_connection()) as c:
        count=c.execute('SELECT COUNT(*) FROM monitoring_history').fetchone()[0]
        archived=c.execute('SELECT COUNT(*) FROM devices WHERE archived_at IS NOT NULL').fetchone()[0]
        size=c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0]
    return {'probe_records':count,'archived_devices':archived,'database_bytes':size}
