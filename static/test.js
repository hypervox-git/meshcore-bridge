// Neighbours + Test mode UI (link test, traceroute). Loaded after app.js and relies on
// its globals: socket, nodes, aliases, leafletMap, initMap, showPage, escHtml,
// GATEWAY_LAT/LON, setStatus.

// ---- shared ----------------------------------------------------------------
let tmTargets = [];
const SNR_GOOD = 5, SNR_OK = 0;                       // dB thresholds for colouring
function snrColor(v){ return v == null ? '#4a5568' : v >= SNR_GOOD ? '#00e5a0' : v >= SNR_OK ? '#ff9500' : '#ff3b3b'; }
function fmt(v, unit, d){ return v == null ? '-' : (d != null ? Number(v).toFixed(d) : v) + (unit || ''); }
function post(url, body){
  return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})})
    .then(r => r.json());
}
function tgtLabel(t){ return escHtml((aliases[t.id] || t.name || t.id) + ' (' + t.type + ')'); }

function loadTargets(){
  fetch('/api/test/targets').then(r => r.json()).then(d => {
    if (!d.ok) return;
    tmTargets = d.targets;
    const fill = (id, filter) => {
      const el = document.getElementById(id); if (!el) return;
      const keep = el.value;
      el.innerHTML = tmTargets.filter(filter).map(t => '<option value="' + t.id + '">' + tgtLabel(t) + '</option>').join('')
        || '<option value="">no matching contacts yet</option>';
      if (keep) el.value = keep;
    };
    fill('lt-target', t => t.type !== 'client');
    fill('tr-target', () => true);
    fill('nb-remote-target', t => t.type === 'repeater');
  }).catch(() => {});
}

(function hookShowPage(){
  const orig = showPage;
  showPage = function(name){
    orig(name);
    if (name === 'test' || name === 'neighbors') loadTargets();
  };
})();

function dutyHtml(d){
  if (!d) return '';
  const bar = (label, used) => {
    const pct = d.budget_s ? Math.min(100, used / d.budget_s * 100) : 0;
    const col = pct > 85 ? 'var(--danger)' : pct > 60 ? 'var(--warn)' : 'var(--accent)';
    return '<div class="duty-row"><span class="duty-label">' + label + '</span>'
      + '<div class="duty-bar"><div style="width:' + pct.toFixed(1) + '%;background:' + col + '"></div></div>'
      + '<span class="duty-num">' + used.toFixed(1) + ' / ' + d.budget_s + ' s</span></div>';
  };
  return '<div class="duty-note">Duty cycle: legal limit ' + d.limit_pct + '%, test budget ' + d.share_pct
    + '% of that (' + d.budget_s + ' s per rolling hour, per transmitter)</div>'
    + bar('Our node TX', d.local_used_s) + (d.target_used_s != null ? bar('Target TX', d.target_used_s) : '');
}

// ---- Neighbours ------------------------------------------------------------
function discoverNeighbors(){
  const types = Array.from(document.querySelectorAll('.nb-type:checked')).map(c => c.value);
  const btn = document.getElementById('nb-btn');
  btn.disabled = true;
  setStatus('nb-status', 'Listening ~8 s...', '');
  document.getElementById('nb-status').textContent = 'Listening ~8 s...';
  post('/api/neighbors/discover', {types, wait_s: 8}).then(d => {
    btn.disabled = false;
    document.getElementById('nb-status').textContent = '';
    if (!d.ok) { setStatus('nb-status', d.error, 'err'); return; }
    renderNeighbors(d);
  }).catch(e => { btn.disabled = false; setStatus('nb-status', 'Error: ' + e, 'err'); });
}

function renderNeighbors(d){
  document.getElementById('nb-count').textContent = d.neighbours.length + ' found';
  const el = document.getElementById('nb-results');
  if (!d.neighbours.length){
    el.innerHTML = '<div class="empty-state">No zero-hop neighbours answered. Only nodes in direct radio range reply.</div>';
    return;
  }
  el.innerHTML = '<table class="nb-table"><tr><th>Node</th><th>Type</th><th>RSSI</th><th>SNR (rx)</th><th>SNR (they heard us)</th><th></th></tr>'
    + d.neighbours.map(n => {
      const nm = escHtml(aliases[n.id] || n.name || n.id);
      return '<tr><td><div class="nb-name">' + nm + '</div><div class="nb-id">' + n.id + (n.known ? '' : ' &middot; not in contacts') + '</div></td>'
        + '<td>' + escHtml(n.type) + '</td>'
        + '<td>' + fmt(n.rssi, ' dBm') + '</td>'
        + '<td style="color:' + snrColor(n.snr) + '">' + fmt(n.snr, ' dB', 1) + '</td>'
        + '<td style="color:' + snrColor(n.snr_in) + '">' + fmt(n.snr_in, ' dB', 1) + '</td>'
        + '<td>' + (n.known ? '<button class="btn" style="padding:3px 8px;font-size:9px" onclick="jumpToTest(\'' + n.id + '\')">Test</button>' : '') + '</td></tr>';
    }).join('') + '</table>'
    + '<div class="duty-wrap">' + dutyHtml(d.duty) + '</div>';
}

function remoteNeighbors(){
  const to = document.getElementById('nb-remote-target').value;
  if (!to) { setStatus('nb-remote-status', 'Pick a repeater', 'err'); return; }
  const btn = document.getElementById('nb-remote-btn'); btn.disabled = true;
  document.getElementById('nb-remote-status').textContent = 'Asking repeater...';
  post('/api/neighbors/remote', {to, password: document.getElementById('nb-remote-pass').value}).then(d => {
    btn.disabled = false;
    document.getElementById('nb-remote-status').textContent = '';
    const el = document.getElementById('nb-remote-results');
    if (!d.ok) { el.innerHTML = '<div class="empty-state">' + escHtml(d.error) + '</div>'; return; }
    el.innerHTML = '<div class="duty-note">' + d.shown + ' of ' + d.total + ' neighbours of ' + escHtml(d.repeater.name) + '</div>'
      + '<table class="nb-table"><tr><th>Node</th><th>Last heard</th><th>SNR</th></tr>'
      + d.neighbours.map(n => '<tr><td><div class="nb-name">' + escHtml(aliases[n.id] || n.name || n.id) + '</div><div class="nb-id">' + n.id + '</div></td>'
        + '<td>' + (n.secs_ago == null ? '-' : n.secs_ago + ' s ago') + '</td>'
        + '<td style="color:' + snrColor(n.snr) + '">' + fmt(n.snr, ' dB', 1) + '</td></tr>').join('') + '</table>'
      + '<div class="duty-wrap">' + dutyHtml(d.duty) + '</div>';
  }).catch(e => { btn.disabled = false; setStatus('nb-remote-status', 'Error: ' + e, 'err'); });
}

function jumpToTest(id){
  showPage('test');
  setTimeout(() => {
    const sel = document.getElementById('lt-target'); if (sel && Array.from(sel.options).some(o => o.value === id)) sel.value = id;
    const tr = document.getElementById('tr-target'); if (tr) tr.value = id;
  }, 400);
}

// ---- Live link test --------------------------------------------------------
let ltChart = null;
const LT_MAX_POINTS = 60;

function ltInitChart(){
  if (ltChart || typeof Chart === 'undefined') return;
  const ds = (label, color, dash) => ({label, data: [], borderColor: color, borderWidth: 1.5, pointRadius: 2,
    fill: false, tension: .25, spanGaps: true, borderDash: dash || []});
  ltChart = new Chart(document.getElementById('lt-chart'), {
    type: 'line',
    data: {labels: [], datasets: [
      ds('Target RSSI', '#00e5a0'), ds('Target noise floor', '#00e5a0', [4, 3]),
      ds('Our RSSI (reply)', '#0077ff'), ds('Our noise floor', '#0077ff', [4, 3])]},
    options: {animation: false, responsive: true, maintainAspectRatio: false,
      plugins: {legend: {labels: {color: '#c8d0dc', font: {size: 10}}}},
      scales: {x: {ticks: {color: '#4a5568', maxTicksLimit: 8}, grid: {color: '#1e2530'}},
               y: {title: {display: true, text: 'dBm', color: '#4a5568'}, ticks: {color: '#4a5568'}, grid: {color: '#1e2530'}}}}
  });
}

function ltPush(label, vals){
  ltInitChart(); if (!ltChart) return;
  ltChart.data.labels.push(label);
  vals.forEach((v, i) => ltChart.data.datasets[i].data.push(v == null ? null : v));
  if (ltChart.data.labels.length > LT_MAX_POINTS){
    ltChart.data.labels.shift(); ltChart.data.datasets.forEach(d => d.data.shift());
  }
  ltChart.update('none');
}

function ltTile(id, val, unit, color){
  const el = document.getElementById(id); if (!el) return;
  el.textContent = val == null ? '-' : val + (unit || '');
  el.style.color = color || '';
}

function startLinkTest(){
  const to = document.getElementById('lt-target').value;
  if (!to) { setStatus('lt-status', 'Pick a target', 'err'); return; }
  const pw = document.getElementById('lt-pass').value;
  document.getElementById('lt-start').disabled = true;
  if (ltChart){ ltChart.data.labels = []; ltChart.data.datasets.forEach(d => d.data = []); ltChart.update('none'); }
  post('/api/test/link/start', {to, password: pw}).then(d => {
    document.getElementById('lt-pass').value = '';           // never keep it in the page
    if (!d.ok){ document.getElementById('lt-start').disabled = false; setStatus('lt-status', d.error, 'err'); return; }
    ltRunning(true, 'Starting' + (pw ? ' (logging in)' : '') + '...');
  }).catch(e => { document.getElementById('lt-start').disabled = false; setStatus('lt-status', 'Error: ' + e, 'err'); });
}

function stopLinkTest(){
  document.getElementById('lt-stop').disabled = true;
  post('/api/test/link/stop').then(() => {}).catch(() => {});
}

function ltRunning(on, msg){
  document.getElementById('lt-start').disabled = on;
  document.getElementById('lt-stop').disabled = !on;
  document.getElementById('lt-target').disabled = on;
  const el = document.getElementById('lt-state');
  el.textContent = msg || (on ? 'Running' : 'Idle');
  el.className = 'badge' + (on ? ' on' : '');
}

socket.on('test_state', s => {
  if (s.running){
    ltRunning(true, 'Running - ' + (s.target ? s.target.name : '') + (s.interval_s ? ' - radio poll every ' + s.interval_s + ' s (duty-cycle paced)' : ''));
  } else {
    ltRunning(false, 'Stopped' + (s.reason ? ': ' + s.reason : ''));
  }
  if (s.duty) document.getElementById('lt-duty').innerHTML = dutyHtml(s.duty);
});

socket.on('test_wait', w => {
  document.getElementById('lt-state').textContent = 'Paused for duty cycle - resuming in ~' + w.wait_s + ' s';
  if (w.duty) document.getElementById('lt-duty').innerHTML = dutyHtml(w.duty);
});

function ltLocal(l){
  if (!l) return;
  ltTile('lt-l-nf', l.noise_floor, ' dBm');
  ltTile('lt-l-rssi', l.last_rssi, ' dBm');
  ltTile('lt-l-snr', l.last_snr, ' dB', snrColor(l.last_snr));
}

socket.on('test_local', d => ltLocal(d.local));

socket.on('test_sample', s => {
  const t = new Date(s.ts * 1000).toLocaleTimeString('en-GB', {hour12: false});
  ltLocal(s.local);
  const r = s.remote;
  if (s.timeout || !r){
    document.getElementById('lt-state').textContent = 'No reply to request #' + s.seq + ' (is a login needed / out of range?)';
  } else {
    ltTile('lt-r-nf', r.noise_floor, ' dBm');
    ltTile('lt-r-rssi', r.last_rssi, ' dBm');
    ltTile('lt-r-snr', r.last_snr, ' dB', snrColor(r.last_snr));
    ltTile('lt-r-margin', r.margin_db, ' dB');
    ltTile('lt-r-queue', r.tx_queue_len, '');
    document.getElementById('lt-state').textContent = 'Running - sample #' + s.seq + ' at ' + t + ', next in ~' + s.next_in + ' s';
  }
  ltPush(t, [r && r.last_rssi, r && r.noise_floor, s.local && s.local.last_rssi, s.local && s.local.noise_floor]);
  if (s.duty) document.getElementById('lt-duty').innerHTML = dutyHtml(s.duty);
});

// ---- Traceroute ------------------------------------------------------------
let traceLayer = null;
let lastTrace = null;

function runTrace(){
  const to = document.getElementById('tr-target').value;
  if (!to) { setStatus('tr-status', 'Pick a target', 'err'); return; }
  const btn = document.getElementById('tr-btn'); btn.disabled = true;
  document.getElementById('tr-status').textContent = 'Discovering route + tracing (up to ~60 s)...';
  document.getElementById('tr-status').className = 'send-status';
  post('/api/test/trace', {to}).then(d => {
    btn.disabled = false;
    document.getElementById('tr-status').textContent = '';
    renderTrace(d);
  }).catch(e => { btn.disabled = false; setStatus('tr-status', 'Error: ' + e, 'err'); });
}

function renderTrace(d){
  lastTrace = d.ok ? d : null;
  document.getElementById('tr-show').disabled = !d.ok;
  const el = document.getElementById('tr-results');
  if (!d.ok){
    el.innerHTML = '<div class="empty-state">' + escHtml(d.error || 'trace failed') + '</div>'
      + (d.trace_path ? '<div class="duty-note">Tried path: <code>' + escHtml(d.trace_path) + '</code> (route ' + escHtml(d.path_source) + ')</div>' : '')
      + (d.duty ? '<div class="duty-wrap">' + dutyHtml(d.duty) + '</div>' : '');
    return;
  }
  const rows = d.legs.map((l, i) => {
    const nm = l.name || (l.resolved === false ? 'unknown repeater' : l.id) || l.hash;
    return '<tr><td>' + (i + 1) + '</td><td>' + escHtml(l.leg) + '</td><td><div class="nb-name">' + escHtml(nm) + '</div><div class="nb-id">' + escHtml(l.hash) + '</div></td>'
      + '<td style="color:' + snrColor(l.snr) + '">' + fmt(l.snr, ' dB', 1) + '</td></tr>';
  }).join('');
  const unresolved = d.points.filter(p => p.kind === 'repeater' && p.lat == null).length;
  el.innerHTML = '<div class="duty-note">Route to <b>' + escHtml(d.target.name) + '</b> (' + escHtml(d.path_source) + ' path, '
      + d.hops.length + ' repeater hop' + (d.hops.length === 1 ? '' : 's') + '), round trip ' + d.rtt_ms + ' ms, final SNR at us '
      + fmt(d.final_snr, ' dB', 1) + '<br>Scope: ' + escHtml(d.scope) + '<br>Trace path: <code>' + escHtml(d.trace_path) + '</code></div>'
    + '<table class="nb-table"><tr><th>#</th><th>Leg</th><th>Node</th><th>SNR at node</th></tr>' + rows + '</table>'
    + (unresolved ? '<div class="duty-note" style="color:var(--warn)">' + unresolved + ' hop(s) have no known location - drawn as a dashed gap on the map.</div>' : '')
    + '<div class="duty-wrap">' + dutyHtml(d.duty) + '</div>';
  drawTrace(d, true);
}

function clearTrace(){
  if (traceLayer && leafletMap){ leafletMap.removeLayer(traceLayer); }
  traceLayer = null;
}

function drawTrace(d, jump){
  initMap();
  if (!leafletMap) return;
  clearTrace();
  const grp = L.layerGroup();
  const pts = d.points;
  let prev = null, prevIdx = -1, gap = false;
  const bounds = [];
  pts.forEach((p, i) => {
    const known = p.lat != null && p.lon != null;
    if (!known){ gap = true; return; }
    bounds.push([p.lat, p.lon]);
    if (prev){
      L.polyline([prev, [p.lat, p.lon]], {
        color: gap ? '#4a5568' : snrColor(p.snr), weight: gap ? 3 : 5, opacity: 0.9,
        dashArray: gap ? '6,8' : null
      }).addTo(grp);
    }
    const label = p.kind === 'gateway' ? 'GW' : p.kind === 'target' ? '&#9873;' : String(i);
    const col = p.kind === 'gateway' ? '#00e5a0' : p.kind === 'target' ? '#b48cff' : snrColor(p.snr);
    L.marker([p.lat, p.lon], {icon: L.divIcon({className: '', iconSize: [22, 22], iconAnchor: [11, 11],
      html: '<div class="hop-marker" style="border-color:' + col + ';color:' + col + '">' + label + '</div>'})})
      .bindPopup('<b>' + escHtml(p.label) + '</b>' + (p.id ? '<br>' + p.id : '') + (p.hash ? '<br>hash ' + escHtml(p.hash) : '')
        + (p.snr != null ? '<br>SNR out ' + p.snr.toFixed(1) + ' dB' : '')
        + (p.snr_back != null ? '<br>SNR back ' + p.snr_back.toFixed(1) + ' dB' : '')
        + (p.ambiguous ? '<br><i>hash also matches ' + p.ambiguous.length + ' other repeater(s)</i>' : ''))
      .addTo(grp);
    prev = [p.lat, p.lon]; prevIdx = i; gap = false;
  });
  grp.addTo(leafletMap);
  traceLayer = grp;
  d._bounds = bounds;
  if (jump) showTraceOnMap();
}

function showTraceOnMap(){
  if (!lastTrace) return;
  showPage('map');
  setTimeout(() => {
    if (!traceLayer) drawTrace(lastTrace, false);
    const b = lastTrace._bounds || [];
    if (leafletMap && b.length > 1) leafletMap.fitBounds(b, {padding: [50, 50], maxZoom: 16});
    else if (leafletMap && b.length === 1) leafletMap.setView(b[0], 15);
  }, 450);
}

// restore state if the page is (re)loaded while a test is running
fetch('/api/test/status').then(r => r.json()).then(s => {
  if (s && s.link_test && s.link_test.running){
    ltRunning(true, 'Running - ' + s.link_test.target.name);
    if (s.duty) document.getElementById('lt-duty').innerHTML = dutyHtml(s.duty);
  }
}).catch(() => {});

// Deep links (/#test, /#neighbors, /#map, /#log, ...) so other pages - e.g. the Home
// Assistant dashboard iframes - can open the bridge straight on a tab.
(function deepLinks(){
  const go = () => {
    const p = (location.hash || '').replace('#', '');
    if (p && document.getElementById('page-' + p)) showPage(p);
  };
  window.addEventListener('hashchange', go);
  go();
})();
