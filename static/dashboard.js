'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const rtt = value => value == null ? '—' : `${Number(value).toFixed(2)} ms`;
const timestamp = value => {
  if (!value) return 'Awaiting probe';
  const date = new Date(value.includes('T') ? value : value.replace(' ', 'T') + 'Z');
  return Number.isNaN(date.getTime()) ? 'Unknown time' : date.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
};
const knownStatuses = ['UP','RECOVERED','DOWN','DEGRADED','ERROR','NOT TESTED','STALE','PAUSED'];
function badge(status) {
  const safe = knownStatuses.includes(status) ? status : 'NOT TESTED';
  return `<span class="badge ${safe.replace(' ','-')}">${escapeHTML(safe)}</span>`;
}
let insightsData=null;
let ackId=null;
let historyRequest=0;
let insightRequest=0;
let editId=null;
let historyCursor=null;
let nextHistoryCursor=null;
let slowLimit=100;
let devices = [];
let historyData = {history:[], alerts:[], report:[], trends:{}};
let activeFilter = 'ALL';
let latestAlert = null;
let lastDataRefresh = null;
let deleting = null;
let detailsId = null;
let autoRefresh = true;
let inFlight = false;
let toastTimer;

function renderDevices() {
  const query = $('device-search').value.trim().toLowerCase();
  const rows = devices.filter(d => `${d.device_name} ${d.ip_address}`.toLowerCase().includes(query)).filter(d => {
    if (activeFilter === 'UP') return !d.paused && !d.is_stale && ['UP','RECOVERED'].includes(d.status);
    if (activeFilter === 'DOWN') return !d.paused && !d.is_stale && d.status === 'DOWN';
    if (activeFilter === 'ATTENTION') return !d.paused && (d.is_stale || ['DEGRADED','ERROR','NOT TESTED'].includes(d.status));
    return true;
  });
  $('device-table').innerHTML = rows.map(d => `<tr><td><button class="target-name target-button" data-details="${Number(d.id)}" aria-label="Inspect ${escapeHTML(d.device_name)}"><span class="device-icon" aria-hidden="true">▱</span>${escapeHTML(d.device_name)}</button></td><td>${escapeHTML(d.ip_address)}</td><td>${badge(d.paused ? 'PAUSED' : d.is_stale ? 'STALE' : d.status)}${d.last_error ? `<small class="probe-error">${escapeHTML(d.last_error)}</small>` : ''}</td><td class="rtt">${rtt(d.rtt)}${!d.paused&&!d.is_stale&&d.rtt>slowLimit?'<small class="slow-label">SLOW</small>':''}</td><td>${d.consecutive_failures ?? 0}<span style="color:#777e8b"> / 3</span></td><td>${timestamp(d.last_checked)}</td><td><button class="check-button" data-check="${Number(d.id)}" ${d.paused?'disabled':''} aria-label="Check ${escapeHTML(d.device_name)} now">↻ Check now</button><button class="check-button" data-edit="${Number(d.id)}" aria-label="Edit ${escapeHTML(d.device_name)}">Edit</button><button class="delete-button" data-delete="${Number(d.id)}" aria-label="Remove ${escapeHTML(d.device_name)}" title="Remove device">×</button></td></tr>`).join('') || `<tr><td colspan="7" class="empty-cell">${devices.length ? 'No targets match this view. Try another filter.' : 'Your network starts here. Add your first device below.'}</td></tr>`;
}
$('device-search').addEventListener('input',renderDevices);
function selectFilter(value) {
 activeFilter=value;
 document.querySelectorAll('[data-filter]').forEach(b=>{const selected=b.dataset.filter===value;b.classList.toggle('active',selected);b.setAttribute('aria-pressed',String(selected));});
 document.querySelectorAll('[data-stat-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.statFilter===value)));
 renderDevices();
}
document.querySelectorAll('[data-stat-filter]').forEach(card=>{card.addEventListener('click',()=>{selectFilter(card.dataset.statFilter);$('devices').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});});card.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();card.click();}});});
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
  selectFilter(button.dataset.filter);
}));
$('device-table').addEventListener('click', event => {
  const inspect=event.target.closest('[data-details]');
  if(inspect){detailsId=Number(inspect.dataset.details);renderDetails();$('device-details').showModal();return;}
  const button = event.target.closest('[data-delete]');
  if (!button) return;
  const device = devices.find(d => d.id === Number(button.dataset.delete));
  if (!device) return;
  deleting = device.id;
  $('delete-description').textContent = `${device.device_name} · ${device.ip_address}`;
  $('delete-dialog').showModal();
});
$('delete-dialog').addEventListener('close', () => {
  if ($('delete-dialog').returnValue !== 'delete' || deleting === null) return;
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = `/delete/${deleting}`;
  document.body.append(form);
  form.submit();
});
$('history-toggle').addEventListener('click', () => {
  const open = $('history-content').hidden;
  $('history-content').hidden = !open;
  $('history-toggle').setAttribute('aria-expanded',String(open));
  $('history-toggle').textContent = open ? 'Hide recent checks −' : 'Show recent checks +';
});
$('notifications').addEventListener('click',async () => {
  if (!('Notification' in window)) {
    $('notifications').textContent = 'Notifications unavailable';
    return;
  }
  try {
    const result = await Notification.requestPermission();
    $('notifications').textContent = result === 'granted' ? 'Notifications enabled ✓' : 'Notifications blocked';
  } catch {
    $('notifications').textContent = 'Notifications unavailable';
  }
});

async function fetchJSON(path) {
  const response = await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(10000)});
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}
async function loadDevices() {
  try {
    const data = await fetchJSON('/api/devices');
    devices = data.devices;
    slowLimit=data.config.slow_rtt;
    updateHistoryDevices();
    ['total','up','degraded','down'].forEach(key => { $(`${key}-count`).textContent = String(key==='degraded' ? data.statistics.degraded+data.statistics.error+data.statistics.untested+(data.statistics.stale||0) : data.statistics[key]).padStart(2,'0'); });
    $('nav-count').textContent = data.statistics.total;
    $('filter-total').textContent = data.statistics.total;
    $('interval').textContent = data.interval;
    $('extra-counts').textContent = `${data.statistics.error} probe errors / ${data.statistics.untested} untested / ${data.statistics.stale||0} stale / ${data.statistics.paused||0} paused`;
    const fresh = data.monitor.last_cycle && Date.now()-Date.parse(data.monitor.last_cycle) < Math.max(30000,data.interval*3000);
    const label = data.monitor.error ? 'MONITOR ERROR' : fresh ? 'MONITOR ONLINE' : 'AWAITING MONITOR';
    $('monitor-label').innerHTML = `<i class="connection-dot ${data.monitor.error ? 'error' : fresh ? 'live' : ''}"></i>${label}`;
    $('monitor-label').title = data.monitor.error || (data.monitor.last_cycle ? `Last cycle: ${timestamp(data.monitor.last_cycle)}` : 'Start python app.py to run the monitor');
    $('refresh-state').textContent = fresh ? 'LIVE UPDATES' : 'AWAITING PROBES';
    $('refresh-state').classList.toggle('fresh',Boolean(fresh));
    lastDataRefresh = new Date();
    $('last-refresh').textContent = `Updated ${lastDataRefresh.toLocaleTimeString()}`;
    renderDevices();
    if($('device-details').open)renderDetails();
    return true;
  } catch (error) {
    $('monitor-label').innerHTML = '<i class="connection-dot error"></i> CONNECTION LOST';
    $('refresh-state').textContent = 'DATA UNAVAILABLE';
    $('refresh-state').classList.remove('fresh');
    $('last-refresh').textContent = `Refresh failed. Retrying…${lastDataRefresh ? ' Last sync: '+lastDataRefresh.toLocaleTimeString() : ''}`;
    if (!lastDataRefresh) $('device-table').innerHTML = '<tr><td colspan="7" class="empty-cell">Cannot reach the monitor. Start the app and keep this page open.</td></tr>';
    console.error(error);
    return false;
  }
}
async function loadHistory() {
  const requestId=++historyRequest;
  try {
    const query=new URLSearchParams();
    if($('history-device').value)query.set('device_id',$('history-device').value);
    if($('history-status').value)query.set('status',$('history-status').value);
    if(historyCursor)query.set('before',historyCursor);
    const data = await fetchJSON('/api/history?'+query);
    if(requestId!==historyRequest)return;
    nextHistoryCursor=data.next_cursor;
    $('history-older').disabled=!nextHistoryCursor;
    historyData = data;
    if($('device-details').open)renderDetails();
    renderEvents(data.alerts);
    renderReport(data.report);
    updateTrendOptions(data.report);
    renderTrend();
    $('history-table').innerHTML = data.history.map(h => `<tr><td>${escapeHTML(h.device_name)}</td><td>${escapeHTML(h.ip_address)}</td><td>${badge(h.status)}</td><td>${rtt(h.rtt)}</td><td>${timestamp(h.checked_at)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty-cell">No probes recorded yet.</td></tr>';
    if (latestAlert !== null && 'Notification' in window && Notification.permission === 'granted') {
      for (const alert of data.alerts.filter(a => a.id > latestAlert).reverse()) {
        try { new Notification(`${alert.kind}: ${alert.device_name}`,{body:alert.message}); } catch { /* In-page alerts remain available. */ }
      }
    }
    latestAlert = Math.max(latestAlert || 0,...data.alerts.map(a => a.id));
  } catch (error) {
    $('alerts').innerHTML = '<div class="empty-state">Event stream unavailable. Retrying automatically…</div>';
    console.error(error);
  }
}
function renderEvents(alerts) {
  const eventType=$('event-filter').value;
  const visible=alerts.filter(a=>eventType==='ALL'||a.kind===eventType);
  $('alerts').innerHTML = visible.slice(0,30).map(a => {
    const recovery = a.kind === 'RECOVERY';
    return `<article class="event ${recovery ? 'recovery' : ''}"><span class="event-symbol" aria-hidden="true">${recovery ? '↗' : '↘'}</span><div><div class="event-title"><span>${escapeHTML(a.device_name)} ${recovery ? 'recovered' : 'unreachable'}</span><time>${timestamp(a.created_at)}</time></div><p>${escapeHTML(a.message)}</p><small>${escapeHTML(a.ip_address)}</small></div></article>`;
  }).join('') || '<div class="empty-state">No events to show.</div>';
}
function renderReport(report) {
  $('report').innerHTML = report.map(r => `<tr><td class="rtt">${escapeHTML(r.device_name)}</td><td>${r.probes}</td><td>${r.lost}</td><td><span class="loss-meter"><span>${r.packet_loss == null ? '—' : Number(r.packet_loss).toFixed(1)+'%'}</span><span class="loss-track" aria-hidden="true"><i style="width:${Math.max(0,Math.min(100,r.packet_loss || 0))}%"></i></span></span></td><td class="rtt">${rtt(r.avg_rtt)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty-cell">Configure a target to start a report.</td></tr>';
}
function updateTrendOptions(report) {
  const selected = $('trend-device').value;
  // Preserve the native menu while it is being used; only rebuild on device changes.
  const signature = report.map(r => `${r.id}:${r.device_name}`).join('|');
  if ($('trend-device').dataset.signature !== signature) {
    $('trend-device').innerHTML = report.map(r => `<option value="${Number(r.id)}">${escapeHTML(r.device_name)}</option>`).join('') || '<option value="">No devices configured</option>';
    if (report.some(r => String(r.id) === selected)) $('trend-device').value = selected;
    $('trend-device').dataset.signature = signature;
  }
}
$('trend-device').addEventListener('change',renderTrend);
function renderTrend() {
  const id = $('trend-device').value;
  const report = historyData.report.find(r => String(r.id) === id);
  const points = historyData.trends[id] || [];
  $('trend-average').textContent = rtt(report?.avg_rtt);
  $('trend-loss').textContent = report?.packet_loss == null ? '—' : report.packet_loss+'%';
  $('trend-checks').textContent = points.length;
  if (!points.length) {
    $('trend-chart').innerHTML = '<div class="empty-state">No checks yet.<br>The first probe will start your trace.</div>';
    return;
  }
  const measured = points.filter(p => p.rtt != null);
  const maximum = Math.max(1,...measured.map(p => p.rtt))*1.15;
  const x = i => 38 + i*440/Math.max(1,points.length-1);
  const y = value => 155-value/maximum*130;
  const grid = [0,1,2,3].map(i => {
    const value = maximum*i/3;
    return `<path d="M38 ${y(value)}H485" stroke="#2c3139" stroke-dasharray="2 4"/><text x="28" y="${y(value)+3}" text-anchor="end">${value.toFixed(1)}</text>`;
  }).join('');
  const lines = points.map((p,i) => i && p.rtt != null && points[i-1].rtt != null ? `<line x1="${x(i-1)}" y1="${y(points[i-1].rtt)}" x2="${x(i)}" y2="${y(p.rtt)}" stroke="#6c9cff" stroke-width="1.7"/>` : '').join('');
  const dots = points.map((p,i) => p.rtt != null ? `<circle cx="${x(i)}" cy="${y(p.rtt)}" r="2.4" fill="#b5cdff"><title>${escapeHTML(timestamp(p.checked_at))}: ${rtt(p.rtt)}</title></circle>` : `<path d="M${x(i)-2} 150l4 5m-4 0l4-5" stroke="#c68a8a"><title>${escapeHTML(timestamp(p.checked_at))}: ${escapeHTML(p.status)}, no RTT</title></path>`).join('');
  $('trend-chart').innerHTML = `<svg viewBox="0 0 500 190" role="img" aria-label="${escapeHTML(report?.device_name)} RTT trend. ${points.length} checks; ${measured.length} replies. Gaps indicate missing RTTs."><title>RTT in milliseconds; oldest to newest checks</title>${grid}${lines}${dots}<text x="38" y="179">${escapeHTML(timestamp(points[0].checked_at))}</text><text x="485" y="179" text-anchor="end">Latest</text></svg>`;
}

function updateClock() { $('clock').textContent = new Date().toLocaleTimeString('en-GB',{hour12:false}); }
updateClock();
setInterval(updateClock,1000);
const sections = [...document.querySelectorAll('main>section[id],main[id]')];
const navLinks = [...document.querySelectorAll('.nav-link')];
navLinks.forEach(link => link.addEventListener('click',() => navLinks.forEach(l => l.classList.toggle('active',l === link))));
// The animation is a decorative signal illustration, not a live topology map.
(function drawNetwork() {
  const canvas = $('network-art');
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let width=0,height=0,frame=0,visible=true,animation=null;
  function resize() {
    const rect=canvas.getBoundingClientRect();
    width=rect.width;height=rect.height;
    const ratio=Math.min(devicePixelRatio || 1,2);
    canvas.width=width*ratio;canvas.height=height*ratio;
    ctx.setTransform(ratio,0,0,ratio,0,0);
    draw();
  }
  function dot(x,y,r=1.05,alpha=1,color='#e7e8e4') {
    ctx.globalAlpha=alpha;ctx.fillStyle=color;
    ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();
  }
  function draw() {
    if (!width || !height) return;
    ctx.clearRect(0,0,width,height);
    const radius=Math.min(height*.34,width*.18);
    const cx=width*.27,cy=height*.50;
    const rotate=reduced.matches ? .5 : frame*.002;
    // A spherical dot grid with rotating, continent-like patches.
    for(let lat=-1.42;lat<1.43;lat+=.083) {
      const ring=Math.cos(lat);
      const count=Math.max(6,Math.round(80*ring));
      for(let j=0;j<count;j++) {
        const lon=j/count*Math.PI*2;
        const angle=lon+rotate;
        const z=Math.cos(angle)*ring;
        if(z<-.05)continue;
        const land=Math.sin(lon*3+Math.sin(lat*4)*1.5)+Math.cos(lat*7-lon*2)+Math.sin(lon*5+lat*3)>.38;
        const edge=z<.22;
        if(!land && !edge && j%5!==0)continue;
        const x=cx+Math.sin(angle)*ring*radius;
        const y=cy+Math.sin(lat)*radius;
        dot(x,y,land?1.15:.7,(land?.4:.12)+Math.max(0,z)*.55);
      }
    }
    ctx.globalAlpha=.7;
    // Dot-matrix workstation, deliberately built from points.
    const scale=radius/43;
    const ox=width*.78-35*scale,oy=cy-38*scale;
    const dotStep=3.2;
    for(let y0=0;y0<78;y0+=dotStep) for(let x0=0;x0<75;x0+=dotStep) {
      const monitor=x0>=9&&x0<=65&&y0<=48;
      const screen=x0>=16&&x0<=58&&y0>=7&&y0<=36;
      const screenBorder=screen&&(x0<20||x0>54||y0<11||y0>32);
      const stand=x0>=31&&x0<=43&&y0>48&&y0<58;
      const base=y0>=59&&y0<=72&&x0>=(12-(y0-59)*.7)&&x0<=(62+(y0-59)*.7);
      const keyboard=base&&y0<68&&y0>62&&x0>17&&x0<59;
      if((monitor&&(!screen||screenBorder))||stand||(base&&!keyboard)) dot(ox+x0*scale,oy+y0*scale,1*scale,.82);
    }
    // Dashed echo path and moving packets bridge the two systems.
    const start=cx+radius+16,end=ox-10,center=cy;
    for(let x0=start;x0<end;x0+=9)dot(x0,center,.65,.35);
    for(let p=0;p<3;p++) {
      const progress=((reduced.matches ? .18 : frame*.007)+p/3)%1;
      const x0=start+(end-start)*progress;
      const alpha=.35+Math.sin(progress*Math.PI)*.65;
      for(let dx=-1;dx<=1;dx++) for(let dy=-1;dy<=1;dy++)dot(x0+dx*3,center+dy*3,1,alpha,'#6c9cff');
    }
    ctx.globalAlpha=1;
    ctx.font='10px monospace';ctx.fillStyle='#92969e';ctx.textAlign='center';
    ctx.fillText('THE NETWORK',cx,height-14);ctx.fillText('YOUR DEVICE',width*.78,height-14);
    ctx.textAlign='left';
  }
  function tick() {
    animation=null;
    if(!visible || document.hidden || reduced.matches)return;
    frame++;draw();animation=requestAnimationFrame(tick);
  }
  function resume() { if(animation===null && visible && !document.hidden && !reduced.matches)animation=requestAnimationFrame(tick); }
  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(entries => { visible=entries[0].isIntersecting;resume(); }).observe(canvas);
  document.addEventListener('visibilitychange',resume);
  reduced.addEventListener('change',() => {draw();resume();});
  resize();resume();
})();
function toast(message) {
 $('toast').textContent=message;$('toast').hidden=false;
 clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},3500);
}
function renderDetails() {
 const d=devices.find(d=>d.id===detailsId);
 if(!d){$('device-details').close();return;}
 const report=historyData.report.find(r=>r.id===d.id);
 $('detail-name').textContent=d.device_name;
 $('detail-body').innerHTML=`<dl class="detail-grid"><div><dt>IP address</dt><dd>${escapeHTML(d.ip_address)}</dd></div><div><dt>Status</dt><dd>${badge(d.paused ? 'PAUSED' : d.is_stale ? 'STALE' : d.status)}</dd></div><div><dt>Latest RTT</dt><dd>${rtt(d.rtt)}</dd></div><div><dt>Packet loss</dt><dd>${report?.packet_loss==null?'—':report.packet_loss+'%'}</dd></div><div><dt>Consecutive misses</dt><dd>${d.consecutive_failures || 0}</dd></div><div><dt>Last checked</dt><dd>${timestamp(d.last_checked)}</dd></div></dl>${d.last_error?`<p class="probe-error">${escapeHTML(d.last_error)}</p>`:''}`;
}
$('close-details').onclick=()=>$('device-details').close();
$('detail-chart').onclick=()=>{
 const id=String(detailsId);$('device-details').close();
 if([...$('trend-device').options].some(o=>o.value===id)){$('trend-device').value=id;renderTrend();}
 document.querySelector('.trend-panel').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
};
$('event-filter').addEventListener('change',()=>renderEvents(historyData.alerts));
$('auto-refresh').addEventListener('click',()=>{
 autoRefresh=!autoRefresh;
 $('auto-refresh').setAttribute('aria-pressed',String(autoRefresh));
 $('auto-refresh').textContent=autoRefresh?'Auto-refresh on':'Auto-refresh paused';
 toast(autoRefresh?'Live updates resumed.':'Dashboard updates paused. Monitoring continues.');
 if(autoRefresh)refreshData();
});
async function refreshData(manual=false){
 if(inFlight)return;inFlight=true;
 $('refresh-now').disabled=true;$('refresh-now').textContent='Refreshing…';
 try{const [success]=await Promise.all([loadDevices(),loadHistory(),loadInsights()]);if(insightsData)renderInsights();if(manual)toast(success?'Dashboard updated.':'Could not connect. Check that the app is running.');}
 finally{inFlight=false;$('refresh-now').disabled=false;$('refresh-now').textContent='↻ Refresh';}
}
$('refresh-now').addEventListener('click',()=>refreshData(true));
function duration(seconds) {
 if(seconds==null)return '—';
 if(seconds<60)return Math.round(seconds)+'s';
 if(seconds<3600)return Math.floor(seconds/60)+'m '+Math.round(seconds%60)+'s';
 return Math.floor(seconds/3600)+'h '+Math.floor(seconds%3600/60)+'m';
}
async function loadInsights(){
 const requestId=++insightRequest;
 try{const data=await fetchJSON('/api/insights');if(requestId!==insightRequest)return;insightsData=data;renderInsights();}
 catch(error){$('hint-title').textContent='Insights unavailable';$('hint-detail').textContent='Retrying automatically. Check the monitor connection.';console.error(error);}
}
function renderInsights(){
 const data=insightsData;
 $('incident-open').textContent=data.summary.open;
 $('incident-total').textContent=data.summary.total;
 $('incident-mttr').textContent=duration(data.summary.mttr_seconds);
 $('hint-title').textContent=data.hint.title;
 $('hint-detail').textContent=data.hint.detail;
 $('insight-window').textContent='Last 24 hours · last 40 checks. Availability = replies / measured probes.';
 $('connection-map').innerHTML=`<div class="map-observer">▦ Monitor<small>This laptop</small></div><div class="map-targets">${devices.map(d=>`<button class="map-target" data-map-id="${d.id}">${escapeHTML(d.device_name)}<span>${escapeHTML(d.ip_address)}</span>${badge(d.paused ? 'PAUSED' : d.is_stale ? 'STALE' : d.status)}</button>`).join('')||'<p>No targets configured.</p>'}</div>`;
 $('availability-table').innerHTML=data.metrics.map(m=>`<tr><td class="rtt">${escapeHTML(m.device_name)}</td><td><div class="timeline" role="img" aria-label="${escapeHTML(m.device_name)}: ${m.checks.length} recent checks">${m.checks.map(c=>`<span class="${knownStatuses.includes(c.status)||['MISS','UNKNOWN'].includes(c.status)?c.status:'UNKNOWN'}" title="${escapeHTML(timestamp(c.checked_at))} · ${escapeHTML(c.status)}${c.rtt==null?'':' · '+rtt(c.rtt)}"></span>`).join('')||'No checks'}</div></td><td>${m.availability==null?'—':m.availability+'%'}</td><td>${rtt(m.p95_rtt)}</td><td>${rtt(m.jitter)}</td></tr>`).join('')||'<tr><td colspan="5" class="empty-cell">Configure a target to start measuring.</td></tr>';
 $('incident-list').innerHTML=data.incidents.map(i=>`<article class="incident-item"><div><strong>${escapeHTML(i.device_name)}</strong><span class="badge ${i.retired_at?'UNKNOWN':i.recovered_at?'RECOVERED':'DOWN'}">${i.retired_at?'MONITORING ENDED':i.recovered_at?'RESOLVED':'OPEN'}</span><p>Confirmed ${timestamp(i.confirmed_at)}<br>${i.retired_at?escapeHTML(i.closed_reason||'Device removed')+' '+timestamp(i.retired_at):i.recovered_at?'Recovered '+timestamp(i.recovered_at):'Awaiting observed recovery'} · ${duration(i.duration_seconds)}</p>${i.acknowledged_at?`<p class="note">Reviewed ${timestamp(i.acknowledged_at)}${i.note?'<br>'+escapeHTML(i.note):''}</p>`:''}</div><button class="button button-outline" data-ack-id="${i.id}">${i.acknowledged_at?'Edit note':'Acknowledge'}</button></article>`).join('')||'<div class="empty-state">No confirmed incidents recorded.</div>';
}
$('connection-map').addEventListener('click',e=>{const b=e.target.closest('[data-map-id]');if(b){detailsId=Number(b.dataset.mapId);renderDetails();$('device-details').showModal();}});
$('incident-list').addEventListener('click',e=>{const b=e.target.closest('[data-ack-id]');if(!b)return;ackId=Number(b.dataset.ackId);$('incident-note').value=insightsData.incidents.find(i=>i.id===ackId)?.note||'';$('ack-error').textContent='';$('ack-dialog').showModal();});
$('ack-cancel').onclick=()=>$('ack-dialog').close();
$('ack-form').addEventListener('submit',async e=>{
 e.preventDefault();
 $('ack-save').disabled=true;
 try{const response=await fetch('/api/incidents/'+ackId+'/acknowledge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:$('incident-note').value})});if(!response.ok)throw new Error('Could not save the note.');$('ack-dialog').close();await loadInsights();toast('Incident review saved.');}
 catch(error){$('ack-error').textContent=error.message;}
 finally{$('ack-save').disabled=false;}
});
$('device-table').addEventListener('click',async e=>{
 const b=e.target.closest('[data-check]');if(!b)return;
 b.disabled=true;b.textContent='Checking…';
 try{const response=await fetch('/api/devices/'+Number(b.dataset.check)+'/check',{method:'POST'});const result=await response.json();if(!response.ok)throw new Error(result.error||'Check failed.');await refreshData();toast('Probe recorded. One manual check counts toward the three-miss threshold.');}
 catch(error){toast(error.message);}
 finally{b.disabled=false;b.textContent='↻ Check now';}
});
function updateHistoryDevices(){
 const selected=$('history-device').value;
 const signature=devices.map(d=>d.id+':'+d.device_name).join('|');
 if($('history-device').dataset.signature===signature)return;
 $('history-device').innerHTML='<option value="">All devices</option>'+devices.map(d=>`<option value="${d.id}">${escapeHTML(d.device_name)}</option>`).join('');
 if(devices.some(d=>String(d.id)===selected))$('history-device').value=selected;
 $('history-device').dataset.signature=signature;
}
for(const id of ['history-device','history-status'])$(id).addEventListener('change',()=>{historyCursor=null;loadHistory();});
$('history-latest').onclick=()=>{historyCursor=null;loadHistory();};
$('history-older').onclick=()=>{if(nextHistoryCursor){historyCursor=nextHistoryCursor;loadHistory();}};
$('device-table').addEventListener('click',e=>{
 const button=e.target.closest('[data-edit]');if(!button)return;
 const device=devices.find(d=>d.id===Number(button.dataset.edit));if(!device)return;
 editId=device.id;$('edit-name').value=device.device_name;$('edit-address').value=device.ip_address;$('edit-paused').checked=Boolean(device.paused);$('edit-error').textContent='';$('edit-dialog').showModal();
});
$('edit-cancel').onclick=()=>$('edit-dialog').close();
$('edit-form').addEventListener('submit',async e=>{
 e.preventDefault();$('edit-save').disabled=true;
 try{const response=await fetch('/api/devices/'+editId,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_name:$('edit-name').value,ip_address:$('edit-address').value,paused:$('edit-paused').checked})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to update device.');$('edit-dialog').close();await refreshData();toast('Device updated. Previous records preserved.');}
 catch(error){$('edit-error').textContent=error.message;}
 finally{$('edit-save').disabled=false;}
});
$('open-settings').onclick=async()=>{
 try{const data=await fetchJSON('/api/settings');$('setting-interval').value=data.config.interval;$('setting-timeout').value=data.config.timeout;$('setting-slow').value=data.config.slow_rtt;$('storage-summary').textContent=`${data.storage.probe_records.toLocaleString()} saved probes · ${(data.storage.database_bytes/1048576).toFixed(2)} MB · ${data.storage.archived_devices} archived devices`;$('settings-error').textContent='';$('settings-dialog').showModal();}
 catch{toast('Unable to load monitoring settings.');}
};
$('settings-cancel').onclick=()=>$('settings-dialog').close();
$('settings-form').addEventListener('submit',async e=>{
 e.preventDefault();$('settings-save').disabled=true;
 try{const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval:Number($('setting-interval').value),timeout:Number($('setting-timeout').value),slow_rtt:Number($('setting-slow').value)})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to save settings.');$('settings-dialog').close();await refreshData();toast('Monitoring settings saved.');}
 catch(error){$('settings-error').textContent=error.message;}
 finally{$('settings-save').disabled=false;}
});
async function poll(){if(autoRefresh)await refreshData();setTimeout(poll,3000);}
poll();
