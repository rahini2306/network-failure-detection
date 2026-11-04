# Network Failure Detection using ICMP

## Setup on macOS / Python 3.14

Open this folder in VS Code. In its terminal:

```sh
source venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8765. Stop with Control+C. If port 8765 is busy, stop the previous app first or run `PORT=8766 python app.py`.

Use `python app.py` to run monitoring. Importing the Flask app or using `flask run` only serves the dashboard. The development reloader is disabled, and a macOS file lock prevents two instances from probing the same database. Do not run multiple server processes for this local project.

## Behavior

- Up to 16 devices are checked concurrently. Defaults: one ICMP Echo Request per 10-second cycle, with a 2-second timeout. Change these in Settings. Very large target lists extend the cycle; cycles never overlap.
- One or two consecutive missed replies: DEGRADED. Three: DOWN (ICMP unreachable). Additional misses do not repeat the failure alert.
- A successful reply after DOWN generates one recovery alert and displays RECOVERED for one cycle, then UP. Success resets the miss counter.
- Local probe exceptions show ERROR and do not count as packet loss or failure evidence. Check the displayed error for socket permissions or configuration problems.
- ICMP unreachability can mean filtering, routing trouble, packet loss, or an unavailable device. It does not prove a device is switched off.
- Every check, RTT, timestamp, and transition alert is saved to SQLite. Timestamps use UTC and the browser displays local time. History is retained indefinitely: plan maintenance for long deployments.
- Browser polls every 3 seconds. Alerts are always visible in the dashboard. Optional browser notifications require permission and an open dashboard; no email/SMS service is configured.
- Reports show aggregate measured packet loss (`lost / sent * 100`) for each active target’s current IP revision. Legacy rows have unknown probe outcomes and are excluded. One probe is sent per check, so the consecutive-miss threshold refers to probes as well as cycles.
- Existing devices/history are preserved by additive migrations; default devices seed once. Existing duplicate IP rows are preserved for review, while new duplicate targets are rejected.
- Removing a device archives its configuration and retains its probe history and incidents. CSV exports current devices' aggregate probe statistics. RTT trends show each device's last 40 checks with gaps for unavailable RTTs.

## Demo

1. Start the app and wait for Localhost to show UP and a measured RTT.
2. Add a lab target you control and verify it replies. Disconnect that target or temporarily block ICMP on it.
3. Observe DEGRADED on misses 1–2 and DOWN on miss 3 (roughly 20–30 seconds, depending on cycle timing). Observe one FAILURE alert.
4. Restore connectivity. The next successful check shows RECOVERED and one RECOVERY alert, followed by UP.
5. Inspect RTT trends, recent checks, packet-loss report, and download CSV.
6. Enable browser notifications if desired. Public DNS targets can be filtered on your network; use a controlled lab device for a repeatable demonstration.

## Verification

Run `python -m unittest discover -s tests -v`. Tests use temporary databases and simulated replies; they do not alter saved monitoring data. Real ICMP behavior still depends on host permissions and network filtering.

## Review features

- **Connection map:** clickable observer-to-target relationships for your configured devices. It is not automatic device discovery or a physical topology map.
- **Availability timeline:** the last 40 checks within the last 24 hours, with reply/miss/error blocks. Availability is the percentage of measured probes receiving replies, not continuous time-based uptime. Probe errors and unknown legacy samples are excluded.
- **Latency metrics:** nearest-rank 95th percentile RTT and average absolute RTT difference between adjacent successful checks (a simple latency variation measure).
- **Incident workflow:** one incident begins on the third consecutive missed probe, ends on observed recovery, and can be acknowledged with an investigation note. Deleting a target retires its open incident without claiming recovery.
- **Mean recovery time:** average observed interval from failure confirmation to recovery across resolved incidents. Monitoring gaps can delay observations; this is not guaranteed physical outage duration.
- **Evidence-based hints:** explanations based on probe errors and active failure counts. They suggest checks and do not diagnose root cause.
- **Review report:** open “Review report” and choose Print / save as PDF for a snapshot of metrics and incidents. The report uses real records, not demo data.

### Useful monitoring controls

- **Check now:** sends one real ICMP probe for that device immediately. Concurrent checks for the same device are prevented. A manual miss contributes to the same three-miss threshold as automatic monitoring.
- **Stale data detection:** readings older than the expected monitoring window are labelled STALE and excluded from fresh UP / DOWN counts. This prevents an old successful check from appearing to prove current reachability.
- **Export log:** downloads the latest 10,000 timestamped probe records as CSV, including errors and outcomes, for independent analysis.
- No synthetic results or demonstration mode are included.

New incident tracking starts at upgrade. For targets already marked DOWN, migration uses their last recorded check as the initial known confirmation time; it does not reconstruct an unknown historical outage start. The timeline reports up to 200 recent incidents; the summary covers all recorded incidents.

## Device editing, maintenance, and backups

- Use **Edit** beside a device to change its name/IP or pause monitoring for maintenance.
- IP changes start a new measurement revision. Current reports/trends use the new revision; probe history keeps the original name and IP snapshot. Renaming preserves the existing measurement series.
- Paused devices receive no automatic or manual probes. They appear PAUSED and do not count as currently UP, DOWN, or stale. An incident already open before a pause stays open until an observed recovery, target change, or removal. Maintenance can therefore extend observed incident durations.
- Resuming an unconfirmed target resets its partial miss streak. Already confirmed incidents remain intact.
- **Settings** persists interval (5–300s), timeout (0.5–5s), and slow-response threshold (10–5000ms). Three consecutive missed replies remain the failure rule. SLOW is a latency warning, not a reachability failure.
- **Probe history** supports device/status filters and cursor pagination, 100 rows per page. All records, including archived devices and older IP revisions, remain available in the All devices view and database backup.
- **Settings → Download database backup** creates a consistent SQLite snapshot, including committed WAL data. Restore only while the app is stopped: preserve the current database and any WAL/SHM files together first; place the downloaded snapshot in the project as `network_monitor.db` without stale WAL/SHM files alongside it, then restart. If unfamiliar with SQLite restore, retain the backup and request help rather than replacing live files.
- History is not automatically pruned. Database size and record count are shown in Settings; store periodic backups outside the project for long-term use.
- The server remains local at **http://127.0.0.1:8765**, rejects unexpected Host headers and cross-origin write requests, and does not provide remote login or public deployment.

## Reliability verification

Run `python -m unittest discover -s tests -v`: covers migration, three-miss failures/recovery, local errors, stale data, duplicate/concurrent checks, revision-safe editing, maintenance pause, history preservation, IPv6 duplicates, settings validation, consistent backups, cursor pagination, origin checks, and metric calculations.

Optional: `python benchmark.py` measures analytics/report performance using a temporary database with 100 configured targets and 20,000 generated stored probes. It sends no ICMP packets and changes no real monitoring data. This measures database/analytics performance, not network throughput.
