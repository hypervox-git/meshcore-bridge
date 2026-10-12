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

// ---- Repeater (serial CLI) mode --------------------------------------------
let repChart = null;
function repApply(r){
  if (!r || r.mode !== 'repeater') return;
  document.getElementById('rep-card').style.display = '';
  ['lt-card', 'tr-card'].forEach(id => { const e = document.getElementById(id); if (e) e.style.display = 'none'; });
  const nb = document.getElementById('nb-btn'); if (nb) nb.disabled = true;
  const st = document.getElementById('nb-status');
  if (st) st.textContent = 'Repeater mode: discovery needs a Companion-USB node (see Test tab)';
  document.getElementById('rep-info').textContent = (r.ver ? 'Firmware ' + r.ver + ' - ' : '') + r.port
    + (r.connected ? '' : ' - NOT CONNECTED: ' + (r.error || ''));
  const q = r.radio; if (!q) return;
  ltTile('rp-nf', q.noise_floor, ' dBm'); ltTile('rp-rssi', q.last_rssi, ' dBm');
  ltTile('rp-snr', q.last_snr, ' dB', snrColor(q.last_snr));
  document.getElementById('rp-air').textContent = q.tx_air_secs + ' s / ' + q.rx_air_secs + ' s';
  const p = r.packets;
  if (p){
    document.getElementById('rp-pk').textContent = p.recv + ' / ' + p.sent;
    document.getElementById('rp-err').textContent = p.recv_errors;
  }
  if (r.neighbours) document.getElementById('rp-nb').textContent =
    r.neighbours.length ? r.neighbours.map(n => n.raw).join('\n') : 'none heard';
  if (!repChart && window.Chart){
    repChart = new Chart(document.getElementById('rp-chart'), {type: 'line',
      data: {labels: [], datasets: [
        {label: 'RSSI', data: [], borderColor: '#4aa3ff', pointRadius: 0, tension: .2},
        {label: 'Noise floor', data: [], borderColor: '#ff9f40', pointRadius: 0, tension: .2}]},
      options: {animation: false, maintainAspectRatio: false, scales: {y: {title: {display: true, text: 'dBm'}}}}});
  }
  if (repChart){
    const t = new Date().toLocaleTimeString('en-GB', {hour12: false});
    repChart.data.labels.push(t); repChart.data.datasets[0].data.push(q.last_rssi);
    repChart.data.datasets[1].data.push(q.noise_floor);
    if (repChart.data.labels.length > 150){ repChart.data.labels.shift(); repChart.data.datasets.forEach(d => d.data.shift()); }
    repChart.update('none');
  }
}
socket.on('rep_sample', repApply);
fetch('/api/repeater').then(r => r.json()).then(repApply).catch(() => {});

// ---- Repeater test mode ------------------------------------------------------
let rtInit = false;
function rtFmt(sec){ const m = Math.floor(sec / 60), s = sec % 60; return m + ':' + String(s).padStart(2, '0'); }

function rtRender(v){
  if (!v) return;
  const running = !!v.running;
  if (!rtInit){
    rtInit = true;
    document.getElementById('rt-min').value = v.minutes_default;
    document.getElementById('rt-off').checked = !!v.offline_default;
    document.getElementById('rt-tx').value = v.assumed_tx_dbm;
    document.getElementById('rt-max').textContent = 'max ' + v.minutes_max + ' min';
  }
  ['rt-min', 'rt-off', 'rt-tx'].forEach(id => document.getElementById(id).disabled = running);
  document.getElementById('rt-start').disabled = running || !v.connected;
  document.getElementById('rt-stop').disabled = !running;
  document.getElementById('rt-disc').disabled = !running;
  const st = document.getElementById('rt-state');
  if (running){
    st.textContent = (v.offline ? 'Repeater OFFLINE - returns to the mesh in ' : 'Testing (still forwarding) - ends in ') + rtFmt(v.remaining_s);
    st.className = 'badge on';
  } else {
    st.textContent = v.restore_pending ? 'Restoring repeater to the mesh...' : (v.reason ? 'Ended: ' + v.reason : 'Idle');
    st.className = 'badge';
  }
  const el = document.getElementById('rt-results');
  if (!v.neighbours && !v.heard){ el.innerHTML = ''; return; }
  const nb = (v.neighbours || []).map(n => '<tr><td><div class="nb-name">' + escHtml(n.name || (n.known === false ? 'unknown node' : '')) + '</div><div class="nb-id">' + escHtml(n.id) + '</div></td>'
    + '<td style="color:' + snrColor(n.snr) + '">' + fmt(n.snr, ' dB', 2) + '</td><td>' + fmt(n.rssi, ' dBm', 0) + '</td>'
    + '<td>' + fmt(n.pathloss_db, ' dB', 1) + '</td><td>' + n.heard + '</td><td>' + (n.age == null ? '-' : Math.round(n.age) + ' s') + '</td></tr>').join('');
  const hv = (v.heard || []).slice().reverse().map(h => '<tr><td>' + new Date(h.ts * 1000).toLocaleTimeString('en-GB', {hour12: false})
    + '</td><td>' + fmt(h.rssi, ' dBm', 0) + '</td><td style="color:' + snrColor(h.snr) + '">' + fmt(h.snr, ' dB', 1) + '</td><td>' + h.n + '</td></tr>').join('');
  const dv = v.discover;
  const dnote = !dv ? '' : dv.supported === false
    ? '<div class="duty-note" style="color:var(--warn)">discover.neighbors is not supported by this firmware (' + escHtml(dv.reply) + ') - discovery is passive: neighbours appear when their advert is heard.</div>'
    : dv.supported === null ? '<div class="duty-note" style="color:var(--warn)">Discovery not sent: ' + escHtml(dv.reply) + '</div>'
    : '<div class="duty-note">discover.neighbors sent at ' + new Date(dv.ts * 1000).toLocaleTimeString('en-GB', {hour12: false}) + (dv.tx_air_s != null ? ' (' + dv.tx_air_s + ' s airtime)' : '') + (dv.reply ? ' - ' + escHtml(dv.reply) : '') + '</div>';
  el.innerHTML = dnote + '<div class="duty-note">Packets heard this test: <b>' + v.packets_heard + '</b>'
    + (v.noise ? ' - noise floor min/avg/max ' + v.noise.min + ' / ' + v.noise.avg + ' / ' + v.noise.max + ' dBm' : '')
    + (v.our_tx_dbm != null ? ' - our TX power ' + v.our_tx_dbm + ' dBm' : '')
    + (v.radio_cfg ? ' - radio ' + escHtml(v.radio_cfg) : '') + '</div>'
    + '<table class="nb-table"><tr><th>Neighbour</th><th>SNR</th><th>RSSI*</th><th>Path loss (est.)</th><th>Adverts heard</th><th>Last heard</th></tr>'
    + (nb || '<tr><td colspan="6"><div class="empty-state">No neighbours heard yet. Discovery is passive: a neighbour appears when its zero-hop advert arrives (repeaters advert every few hours by default).</div></td></tr>') + '</table>'
    + '<div class="config-hint">*RSSI = repeater\'s last-packet RSSI when that neighbour\'s entry refreshed. Path loss = assumed neighbour TX (' + v.assumed_tx_dbm + ' dBm) - RSSI; antenna gains and real TX power unknown, so treat as an estimate.</div>'
    + '<div class="config-title" style="margin-top:.6rem">Every packet heard (last 20)</div>'
    + '<table class="nb-table"><tr><th>Time</th><th>RSSI</th><th>SNR</th><th>Pkts</th></tr>' + (hv || '<tr><td colspan="4">none yet</td></tr>') + '</table>';
}

function rtStart(){
  const body = {minutes: parseFloat(document.getElementById('rt-min').value),
                offline: document.getElementById('rt-off').checked,
                assumed_tx_dbm: parseFloat(document.getElementById('rt-tx').value)};
  if (body.offline && !confirm('This takes the repeater out of the mesh for ' + body.minutes + ' min. It returns automatically. Continue?')) return;
  post('/api/rtest/start', body).then(d => { if (!d.ok) setStatus('rt-status', d.error, 'err'); else { setStatus('rt-status', '', ''); rtRender(d); } })
    .catch(e => setStatus('rt-status', 'Error: ' + e, 'err'));
}
function rtDiscover(){ post('/api/rtest/discover').then(d => setStatus('rt-status', d.ok ? 'Discovery sent' : d.error, d.ok ? 'ok' : 'err')).catch(() => {}); }
function rtStop(){ post('/api/rtest/stop').then(rtRender).catch(() => {}); }

socket.on('rtest_state', rtRender);
socket.on('rtest_update', rtRender);
setInterval(() => { if (document.getElementById('rep-card').style.display !== 'none') fetch('/api/rtest/status').then(r => r.json()).then(rtRender).catch(() => {}); }, 5000);
fetch('/api/rtest/status').then(r => r.json()).then(rtRender).catch(() => {});
