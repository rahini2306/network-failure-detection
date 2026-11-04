"""Measured analytics, independent of UI rendering. No inferred physical topology."""
from contextlib import closing
from datetime import datetime, timezone
from math import ceil
import database as db


def elapsed(start, end):
    def parse(value):
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
    return max(0,round((parse(end)-parse(start)).total_seconds(),1))


def incident_rows():
    with closing(db.get_connection()) as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM incidents ORDER BY id DESC LIMIT 200')]
    for row in rows:
        row['duration_seconds']=elapsed(row['confirmed_at'],row['recovered_at'] or row['retired_at'] or db.now())
    return rows


def snapshot():
    devices=db.get_devices()
    metrics=[]
    with closing(db.get_connection()) as c:
        counts={r['device_id']:dict(r) for r in c.execute("""SELECT h.device_id,COUNT(h.success) probes,
            SUM(CASE WHEN h.success=1 THEN 1 ELSE 0 END) replies,
            SUM(CASE WHEN h.success=1 AND h.rtt IS NOT NULL THEN 1 ELSE 0 END) rtt_count
            FROM monitoring_history h JOIN devices d ON d.id=h.device_id AND d.revision=h.revision
            AND d.archived_at IS NULL WHERE datetime(h.checked_at)>=datetime('now','-24 hours') GROUP BY h.device_id""")}
        for device in devices:
            count=counts.get(device['id'],{'probes':0,'replies':0,'rtt_count':0})
            args=(device['id'],device['revision'])
            where="device_id=? AND revision=? AND datetime(checked_at)>=datetime('now','-24 hours')"
            recent=[dict(r) for r in reversed(c.execute('SELECT success,rtt,checked_at,error FROM monitoring_history WHERE '+where+' ORDER BY id DESC LIMIT 40',args).fetchall())]
            p95=None
            if count['rtt_count']:
                p95=c.execute('SELECT rtt FROM monitoring_history WHERE '+where+' AND success=1 AND rtt IS NOT NULL ORDER BY rtt LIMIT 1 OFFSET ?',(*args,ceil(.95*count['rtt_count'])-1)).fetchone()[0]
            jitter=c.execute('''SELECT AVG(ABS(rtt-previous_rtt)) FROM (
                SELECT rtt,success,LAG(rtt) OVER(ORDER BY id) previous_rtt,
                LAG(success) OVER(ORDER BY id) previous_success FROM monitoring_history WHERE '''+where+
                ') WHERE success=1 AND previous_success=1',args).fetchone()[0]
            metrics.append({'id':device['id'],'device_name':device['device_name'],
                'availability':round(100*count['replies']/count['probes'],2) if count['probes'] else None,
                'probes':count['probes'],'p95_rtt':round(p95,2) if p95 is not None else None,
                'jitter':round(jitter,2) if jitter is not None else None,
                'checks':[{**r,'status':'ERROR' if r['error'] else 'UNKNOWN' if r['success'] is None else 'UP' if r['success'] else 'MISS'} for r in recent]})
    incidents=incident_rows()
    # Aggregate totals cover all recorded incidents, independently of the 200-row UI limit.
    with closing(db.get_connection()) as c:
        summary=dict(c.execute('''SELECT COUNT(*) total,
          SUM(CASE WHEN recovered_at IS NULL AND retired_at IS NULL THEN 1 ELSE 0 END) open,
          SUM(CASE WHEN acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) acknowledged,
          AVG(CASE WHEN recovered_at IS NOT NULL THEN MAX(0,(julianday(recovered_at)-julianday(confirmed_at))*86400) END) mttr_seconds
          FROM incidents''').fetchone())
    summary={key:(round(value,1) if value is not None else None) if key=='mttr_seconds' else value or 0 for key,value in summary.items()}
    down=[d for d in devices if d['incident_open'] and not d['paused']]
    errors=[d for d in devices if d['status']=='ERROR' and not d['paused']]
    if errors:
        hint={'title':'Check the probe environment','detail':f'{len(errors)} target(s) report local probe errors. Inspect the error details before diagnosing network failures.'}
    elif len(down)>=2:
        hint={'title':'Several targets are unreachable','detail':'Check your monitoring laptop’s connection and shared network path. This pattern suggests a shared issue, but ICMP alone cannot identify its cause.'}
    elif down:
        hint={'title':'One target needs attention','detail':'Check its IP address, route, and ICMP firewall policy. Unreachable does not establish whether it is powered off.'}
    else:
        hint={'title':'No confirmed failures','detail':'The monitor has not recorded an active three-miss incident. Review individual readings for degraded or untested targets.'}
    return {'window':'Last 24 hours','metrics':metrics,'incidents':incidents,'summary':summary,'hint':hint}


def acknowledge(incident_id, note):
    with closing(db.get_connection()) as c,c:
        result=c.execute('UPDATE incidents SET acknowledged_at=COALESCE(acknowledged_at,?),note=? WHERE id=?',(db.now(),note,incident_id))
        return bool(result.rowcount)
