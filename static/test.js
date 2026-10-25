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
  }).catch(() => {});
}

(function hookShowPage(){
  const orig = showPage;
  showPage = function(name){
    orig(name);
    try { history.replaceState(null, '', '#' + name); } catch (e) {}       // a reload (or a bookmark) reopens the same tab
    if (name === 'test' || name === 'neighbors') loadTargets();
    if (name === 'test') testNav();
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

// ---- persistent neighbour table (survives reboots; filled from the public database) ----
let nbtData = null, nbtAt = 0;
function nbtAge(s){
  if (s < 60) return Math.round(s) + ' s';
  if (s < 3600) return Math.floor(s / 60) + ' min';
  if (s < 86400) return Math.floor(s / 3600) + ' h ' + Math.floor(s % 3600 / 60) + ' min';
  return Math.floor(s / 86400) + ' d ' + Math.floor(s % 86400 / 3600) + ' h';
}
function nbtRender(d){
  if (d) { nbtData = d; nbtAt = Date.now(); }
  d = nbtData;
  const el = document.getElementById('nbt-results'); if (!el || !d) return;
  const el_s = d.now ? (Date.now() - nbtAt) / 1000 : 0;   // seconds since the server stamped the data
  const rows = d.entries || [];
  const live = rows.filter(e => e.heard_ts != null && (d.now - e.heard_ts) + el_s <= d.active_s).length;
  let h = '<div class="duty-note">' + rows.length + ' repeaters, ' + live + ' heard in the last ' + nbtAge(d.active_s)
    + (d.last_sync_ts ? '. Public database updated ' + nbtAge(d.now - d.last_sync_ts + el_s) + ' ago' : '. Not yet updated from the public database')
    + (d.our_name ? ' (range/bearing from ' + escHtml(d.our_name) + ')' : '')
    + (d.last_error ? '. <span style="color:var(--danger,#e55)">' + escHtml(d.last_error) + '</span>' : '') + '</div>';
  if (!rows.length) { el.innerHTML = h + '<div class="empty-state">Press "Update from public database" to list the repeaters near this one.</div>'; return; }
  h += '<table class="nb-table"><tr><th>Node</th><th>Range</th><th>Bearing</th><th>Heard</th><th>RSSI</th><th>SNR</th></tr>';
  h += rows.map(e => {
    const age = e.heard_ts != null ? (d.now - e.heard_ts) + el_s : null;
    const on = age != null && age <= d.active_s;
    const via = e.heard_via ? ' (' + escHtml(e.heard_via) + ')' : '';
    return '<tr class="' + (on ? 'nbt-live' : 'nbt-muted') + '"><td><div class="nb-name">' + escHtml(aliases[e.key] || e.name || e.key.slice(0, 8))
      + '</div><div class="nb-id">' + escHtml(e.key.slice(0, 8)) + '</div></td>'
      + '<td>' + (e.range_km == null ? '-' : e.range_km.toFixed(1) + ' km') + '</td>'
      + '<td>' + (e.bearing_deg == null ? '-' : Math.round(e.bearing_deg) + '&deg;') + '</td>'
      + '<td>' + (age == null ? 'never' : nbtAge(age) + ' ago' + via) + '</td>'
      + '<td>' + (e.rssi == null ? '-' : fmt(e.rssi, ' dBm', 0)) + '</td>'
      + '<td>' + (e.snr == null ? '-' : fmt(e.snr, ' dB', 1)) + '</td></tr>';
  }).join('') + '</table>';
  el.innerHTML = h;
}
function nbtLoad(){ return fetch('/api/nbtable').then(r => r.json()).then(d => { if (d.ok) nbtRender(d); }).catch(() => {}); }
function nbSync(){
  const btns = ['nbt-btn', 'nbt-btn2'].map(i => document.getElementById(i)).filter(Boolean);
  const msg = t => ['nbt-status', 'nbt-status2'].forEach(i => { const e = document.getElementById(i); if (e) e.textContent = t; });
  btns.forEach(b => b.disabled = true); msg('Updating...');
  return post('/api/nbtable/sync', {}).then(d => {
    btns.forEach(b => b.disabled = false);
    if (!d.ok) { msg('Failed: ' + d.error); return; }
    msg('Done: ' + d.added + ' new, ' + d.count + ' listed');
    nbtRender(d.table);
  }).catch(e => { btns.forEach(b => b.disabled = false); msg('Error: ' + e); });
}
socket.on('nbtable', nbtRender);
nbtLoad();
setInterval(() => nbtRender(), 30000);        // keep the ages moving

function jumpToTest(id){
  showPage('test'); testTab('run');
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
  const nb = document.getElementById('nb-btn');
  if (nb){ nb.disabled = false; nb.onclick = repDiscover; nb.removeAttribute('onclick'); nb.addEventListener('click', repDiscover); }
  document.querySelectorAll('.nb-type').forEach(c => { if (c.parentElement) c.parentElement.style.display = 'none'; });
  repNeighbours(r.neighbours, r.connected);
  if (r.radio) window.repGwPopup = '<b>Local repeater</b><br>noise floor ' + r.radio.noise_floor + ' dBm<br>last RSSI ' + r.radio.last_rssi
    + ' dBm, SNR ' + r.radio.last_snr + ' dB' + (r.neighbours ? '<br>' + r.neighbours.length + ' neighbour(s) heard' : '');
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
  window.rtLast = v;
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

// Neighbours tab in repeater mode: the repeater's own neighbour table, live.
let repNbLast = null;
function repNeighbours(rows, connected){
  repNbLast = rows || repNbLast;
  const el = document.getElementById('nb-results'); if (!el) return;
  const rt = (window.rtLast && window.rtLast.neighbours) || [];
  const byId = {}; rt.forEach(n => byId[n.id] = n);
  document.getElementById('nb-count').textContent = (rows ? rows.length : 0) + ' found';
  if (!rows || !rows.length){ el.innerHTML = ''; return; }
  el.innerHTML = '<table class="nb-table"><tr><th>Node</th><th>SNR</th><th>RSSI*</th><th>Path loss (est.)</th><th>Last heard</th><th>Position</th></tr>'
    + rows.map(r => { const t = byId[r.id] || {};
      return '<tr><td><div class="nb-name">' + escHtml(r.name || 'unknown node') + '</div><div class="nb-id">' + escHtml(r.id) + '</div></td>'
        + '<td style="color:' + snrColor(r.snr_db) + '">' + fmt(r.snr_db, ' dB', 2) + '</td><td>' + fmt(t.rssi, ' dBm', 0) + '</td><td>' + fmt(t.pathloss_db, ' dB', 1) + '</td>'
        + '<td>' + (r.age_s == null ? '-' : r.age_s + ' s ago') + '</td><td>' + (r.lat != null ? r.lat.toFixed(4) + ', ' + r.lon.toFixed(4) : '-') + '</td></tr>'; }).join('')
    + '</table><div class="config-hint" style="margin:.4rem 1rem">*RSSI and path loss are filled in when the neighbour\'s advert is heard while the bridge is watching.</div>';
}
function repDiscover(){
  const st = document.getElementById('nb-status'); st.textContent = 'Sending discover.neighbors...'; st.className = 'send-status';
  post('/api/rtest/discover').then(d => {
    if (!d.ok){ setStatus('nb-status', d.error, 'err'); return; }
    const x = d.discover || {};
    st.textContent = x.supported ? 'Discovery sent' + (x.tx_air_s != null ? ' (' + x.tx_air_s + ' s airtime)' : '') + ' - results appear as neighbours answer'
                                 : 'Firmware has no discover.neighbors - listening passively (' + (x.reply || '') + ')';
    if (d.neighbours) repNeighbours(d.neighbours, true);
  }).catch(e => setStatus('nb-status', 'Error: ' + e, 'err'));
}

setInterval(() => { try { if (window.repGwPopup && typeof gatewayMarker !== 'undefined' && gatewayMarker) gatewayMarker.setPopupContent(window.repGwPopup); } catch (e) {} }, 2000);

// ---- Map: public repeaters at their known positions + simulated link lines --------------
let simLayer = null, simTimer = null;
window.simKeys = new Set();
function simColour(rssi){ return rssi >= -100 ? '#00e5a0' : rssi >= -115 ? '#ff9500' : '#ff3b3b'; }

function simPopup(it, loc){
  const m = it.measured || {}, dir = !!(loc.antenna && loc.antenna.directional), idx = (window.simItems || []).indexOf(it);
  return '<b>' + escHtml(it.name || it.key.slice(0, 8)) + '</b> (' + (it.source === 'discovered' ? 'heard by us' : 'public directory') + ')<br>' + escHtml(it.key.slice(0, 8))
    + '<br>Distance ' + it.distance_km + ' km @ ' + it.freq_mhz + ' MHz'
    + '<br>Free-space loss ' + it.fspl_db + ' dB'
    + '<br><b>Boresight to point at this repeater: ' + it.bearing_deg + '&deg; true</b> (from ' + escHtml(loc.name || 'our repeater') + ')'
    + (dir ? '<br>' + it.off_boresight_deg + '&deg; off the current ' + escHtml(String(loc.antenna.type)) + ' boresight ' + loc.antenna.azimuth_deg + '&deg;; gain ' + it.gain_local_dbi + ' &rarr; ' + loc.antenna.gain_dbi + ' dBi if aimed' : '<br>Omni antenna - no steering needed')
    + (dir && idx >= 0 ? '<br><button class="btn btn-send" onclick="steerIdx(' + idx + ')">' + steerLabel(it.bearing_deg) + '</button> <span class="send-status" id="steer-msg"></span>' : '')
    + '<br>Antenna gain: ' + it.gain_local_dbi + ' dBi here' + (loc.antenna && loc.antenna.directional ? ' (peak ' + loc.antenna.gain_dbi + ', F/B ' + loc.antenna.front_back_db + ' dB)' : '') + ' + ' + it.gain_remote_dbi + ' dBi there (' + escHtml(it.gain_src) + ')'
    + '<br><b>Simulated path loss ' + it.pathloss_db + ' dB</b> &rarr; ' + it.rssi_dbm + ' dBm at ' + it.tx_dbm + ' dBm TX'
    + (m.pathloss_db != null ? '<br>Measured path loss ~' + m.pathloss_db + ' dB (' + (it.excess_db >= 0 ? '+' : '') + it.excess_db + ' dB vs free space)' : '')
    + (m.snr != null ? '<br>SNR ' + m.snr + ' dB' : '');
}

function destPoint(lat, lon, brgDeg, km){
  const R = 6371.0088, b = brgDeg * Math.PI / 180, p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180, dr = km / R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(dr) + Math.cos(p1) * Math.sin(dr) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(dr) * Math.cos(p1), Math.cos(dr) - Math.sin(p1) * Math.sin(p2));
  return [p2 * 180 / Math.PI, l2 * 180 / Math.PI];
}

// Relative field strength at `t` deg off boresight (mirrors bridge/linkmodel._field): cardioid from F/B,
// or cos(t/2)^n set by the half-power beamwidth when one is given.
function patField(t, fb, hpbw){
  const r = Math.pow(10, -fb / 20);
  if (hpbw){
    const h = Math.max(10, Math.min(hpbw, 270)), n = Math.log(Math.SQRT1_2) / Math.log(Math.cos(h / 4 * Math.PI / 180));
    const a = ((t % 360) + 360) % 360, off = a > 180 ? 360 - a : a;           // fold: the pattern is symmetric about boresight
    return Math.max(Math.pow(Math.max(Math.cos(off * Math.PI / 360), 0), n), r);
  }
  const beta = (1 - r) / (1 + r);
  return Math.max((1 + beta * Math.cos(t * Math.PI / 180)) / (1 + beta), r);
}

// Pattern SIZE is relative to gain, from an editable 0 dBi reference circle (Test tab, radius in metres):
// free-space range scales as 10^(dBi/20), so +6 dBi = double the reach. Illustrative scale only.
let ANT_REF_RADIUS_M = 400, refLayer = null;
function antReachKm(gainDbi){ return ANT_REF_RADIUS_M / 1000 * Math.pow(10, gainDbi / 20); }
function fmtReach(km){ return km < 1 ? Math.round(km * 1000) + ' m' : km.toFixed(1) + ' km'; }

let beamLayer = null;   // our antenna's beam: its own layer so it can follow the mount without redrawing (and closing popups of) the repeaters
function drawBeam(loc, d){
  // Azimuth pattern of OUR directional antenna drawn around the repeater (lobe length ~ field strength).
  if (beamLayer){ beamLayer.remove(); beamLayer = null; }
  const a = loc.antenna; if (!a || !a.directional || !leafletMap) return;
  beamLayer = L.layerGroup().addTo(leafletMap);
  const far = antReachKm(a.gain_dbi), pts = [];
  for (let t = 0; t < 360; t += 5) pts.push(destPoint(loc.lat, loc.lon, a.azimuth_deg + t, far * patField(t, a.front_back_db, a.hpbw_deg)));
  if (window.planBest != null && Math.abs(window.planBest - a.azimuth_deg) > 2)
    L.polyline([[loc.lat, loc.lon], destPoint(loc.lat, loc.lon, window.planBest, far * 1.1)], {color: '#00e5a0', weight: 2, dashArray: '2 4', interactive: false})
      .addTo(beamLayer).bindTooltip('suggested ' + window.planBest + '\u00b0', {permanent: true, direction: 'top', className: 'link-label'});
  L.polygon(pts, {color: '#5b8def', weight: 1, fillColor: '#5b8def', fillOpacity: .12, interactive: false}).addTo(beamLayer);
  L.polyline([[loc.lat, loc.lon], destPoint(loc.lat, loc.lon, a.azimuth_deg, far * 1.1)], {color: '#5b8def', weight: 2, dashArray: '2 4', interactive: false})
    .addTo(beamLayer).bindTooltip(a.type + ' ' + a.azimuth_deg + '\u00b0 | ' + a.gain_dbi + ' dBi | F/B ' + a.front_back_db + ' dB | reach ~' + fmtReach(far), {permanent: true, direction: 'top', className: 'link-label'});
}

function drawSim(d){
  // With a calibrated mount the beam shows where the antenna REALLY points, never a stale/target value.
  if (window.mountCfg && window.mountCfg.calibrated && window.mountBearing != null && d.local && d.local.antenna && d.local.antenna.directional)
    d.local.antenna.azimuth_deg = window.mountBearing;
  window.lastSim = d;                              // so a reference-radius change can redraw without a fetch
  if (!leafletMap || typeof L === 'undefined') return;
  if (simLayer){ simLayer.remove(); simLayer = null; }
  if (beamLayer){ beamLayer.remove(); beamLayer = null; }
  window.simKeys = new Set();
  const info = document.getElementById('sim-info');
  const loc = d.local || {};
  if (loc.lat != null && loc.lon != null){
    GATEWAY_LAT = loc.lat; GATEWAY_LON = loc.lon;
    if (gatewayMarker){ gatewayMarker.setLatLng([loc.lat, loc.lon]);
      if (!window.repGwPopup) gatewayMarker.setPopupContent('<b>' + escHtml(loc.name || 'Local repeater') + '</b><br>position from ' + escHtml(loc.source) + '<br>antenna gain ' + loc.gain_dbi + ' dBi (' + escHtml(loc.gain_src) + ')'); }
  }
  setTimeout(() => { refGhost(); antGhost(); }, 0); // keep/redraw the reference + preset ghosts once the map is ready
  if (!document.getElementById('sim-toggle').checked){ info.textContent = ''; return; }
  if (d.note){ info.textContent = d.note; return; }
  simLayer = L.layerGroup().addTo(leafletMap);
  window.simItems = d.items;                       // simPopup/steerIdx look repeaters up by index
  window.simMarkers = [];
  drawBeam(loc, d);
  d.items.forEach((it, idx) => {
    const disc = it.source === 'discovered';
    const col = it.measured && it.measured.snr != null && disc ? linkColour(it.measured.snr) : simColour(it.rssi_dbm);
    if (disc) window.simKeys.add(it.key.length > 8 ? '!' + it.key.slice(0, 8) : '!' + it.key);
    const line = L.polyline([[loc.lat, loc.lon], [it.lat, it.lon]], {color: col, weight: disc ? 3 : 1.5, opacity: disc ? .9 : .6, dashArray: disc ? null : '6 6'}).addTo(simLayer);
    const lbl = 'sim PL ' + it.pathloss_db + ' dB' + (it.measured && it.measured.pathloss_db != null ? ' | meas ' + it.measured.pathloss_db : '');
    line.bindTooltip(lbl, {permanent: disc, direction: 'center', className: 'link-label', sticky: !disc});
    line.bindPopup(simPopup(it, loc));
    window.simMarkers[idx] = line;
    if (!disc){
      window.simMarkers[idx] = L.circleMarker([it.lat, it.lon], {radius: 5, color: '#5b8def', fillColor: '#5b8def', fillOpacity: .8, weight: 1})
        .addTo(simLayer).bindPopup(simPopup(it, loc)).bindTooltip(it.name || it.key.slice(0, 8));
    }
  });
  simPickFill(d);
  const disc = d.items.filter(i => i.source === 'discovered').length;
  info.textContent = (loc.antenna && loc.antenna.directional ? loc.antenna.type + ' ' + loc.antenna.azimuth_deg + '\u00b0, ' + loc.antenna.gain_dbi + ' dBi, F/B ' + loc.antenna.front_back_db + ' dB - ' : '') + d.items.length + ' repeater(s): ' + disc + ' heard, ' + (d.items.length - disc) + ' public within ' + d.radius_km + ' km - '
    + d.tx_dbm + ' dBm TX, ' + d.freq_mhz + ' MHz, free-space (best case)'
    + (d.directory && d.directory.error && !d.directory.count ? ' - public directory unavailable: ' + d.directory.error : '');
  if (typeof renderMap === 'function') renderMap();
}

function loadSim(){
  return fetch('/api/public/links').then(r => r.json()).then(d => { if (d && !d.error) drawSim(d); else if (d) document.getElementById('sim-info').textContent = d.error; }).catch(() => {});
}

(function(){
  const t = document.getElementById('sim-toggle');
  if (t) t.addEventListener('change', loadSim);
  const orig = window.showPage;
  if (typeof orig === 'function') window.showPage = function(name){
    const r = orig.apply(this, arguments);
    if (simTimer){ clearInterval(simTimer); simTimer = null; }
    if (name === 'map'){ setTimeout(loadSim, 400); simTimer = setInterval(loadSim, 20000); }
    return r;
  };
  if (location.hash === '#map') setTimeout(loadSim, 1500);
})();

// ---- Site planner: line of sight + best antenna orientation ----------------------------
let planChart = null, planTimer = null;
window.planBest = null;
const LOS_COL = {clear: '#00e5a0', marginal: '#ff9500', blocked: '#ff3b3b'};

function planRender(st){
  const el = document.getElementById('plan-results'), msg = document.getElementById('plan-status');
  if (st.state === 'running'){ msg.textContent = 'Analysing terrain... ' + (st.progress || ''); return; }
  if (st.state === 'error'){ setStatus('plan-status', st.error, 'err'); document.getElementById('plan-btn').disabled = false; return; }
  document.getElementById('plan-btn').disabled = false;
  const r = st.result; if (!r){ return; }
  msg.textContent = '';
  if (planTimer){ clearInterval(planTimer); planTimer = null; }
  window.planBest = r.best.azimuth_deg;
  const a = r.local.antenna || {};
  const rows = r.candidates.map(c => '<tr><td><div class="nb-name">' + escHtml(c.name || c.key.slice(0, 8)) + (c.heard ? ' &#10003; heard' : '') + '</div><div class="nb-id">' + escHtml(c.key.slice(0, 8)) + '</div></td>'
    + '<td>' + c.distance_km + ' km</td><td>' + c.bearing_deg + '&deg;</td>'
    + '<td style="color:' + LOS_COL[c.status] + '">' + c.status + (c.terrain_loss_db ? ' (+' + c.terrain_loss_db + ' dB)' : '') + '</td>'
    + '<td>' + c.pathloss_terrain_db + ' dB</td>'
    + '<td style="color:' + (c.reach_now ? '#00e5a0' : '#ff3b3b') + '">' + c.rssi_now_dbm + '</td>'
    + '<td style="color:' + (c.reach_best ? '#00e5a0' : '#ff3b3b') + '">' + c.rssi_best_dbm + '</td></tr>').join('');
  const arc = r.best.arc ? r.best.arc[0] + '&ndash;' + r.best.arc[1] + '&deg;' : r.best.azimuth_deg + '&deg;';
  const better = r.best.k - r.current.k > 0.08 && Math.abs(r.best.azimuth_deg - r.current.azimuth_deg) >= 10;
  el.innerHTML = '<div class="duty-note">' + (a.directional ? escHtml(String(a.type)) + ' ' + a.gain_dbi + ' dBi, F/B ' + a.front_back_db + ' dB. ' : 'Omni antenna. ')
    + '<b>Now ' + (a.directional ? r.current.azimuth_deg + '&deg;' : 'omni') + ': ' + r.current.reachable + ' of ' + r.candidates.length + ' reachable.</b> '
    + (a.directional ? (better ? '<b style="color:#00e5a0">Best ' + r.best.azimuth_deg + '&deg; (good arc ' + arc + '): ' + r.best.reachable + ' reachable.</b>' : 'Current orientation is already (near) optimal.') : '')
    + '<br>Line of sight to ' + r.line_of_sight.length + ' of ' + r.candidates.length + ' repeaters within the radius. Site ground ' + r.site_ground_m + ' m, masts ' + r.mast_m[0] + '/' + r.mast_m[1] + ' m, '
    + 'sensitivity ' + r.sensitivity_dbm + ' dBm + ' + r.fade_margin_db + ' dB margin, ' + r.tx_dbm + ' dBm TX.</div>'
    + '<table class="nb-table"><tr><th>Repeater</th><th>Dist</th><th>Bearing</th><th>Line of sight</th><th>Path loss</th><th>RSSI now</th><th>RSSI at best</th></tr>' + rows + '</table>';
  if (window.Chart){
    const labels = r.sweep.map(s => s.az), data = r.sweep.map(s => s.score);
    if (planChart){ planChart.data.labels = labels; planChart.data.datasets[0].data = data; planChart.update('none'); }
    else planChart = new Chart(document.getElementById('plan-chart'), {type: 'bar', data: {labels, datasets: [{label: 'weighted repeaters reachable', data, backgroundColor: '#5b8def'}]},
      options: {animation: false, maintainAspectRatio: false, plugins: {legend: {display: true}}, scales: {x: {title: {display: true, text: 'boresight azimuth (deg true)'}}}}});
  }
}

function planPoll(){ fetch('/api/plan').then(r => r.json()).then(planRender).catch(() => {}); }
function planRun(){
  document.getElementById('plan-btn').disabled = true;
  setStatus('plan-status', '', '');
  post('/api/plan/run').then(d => { if (!d.ok){ document.getElementById('plan-btn').disabled = false; setStatus('plan-status', d.error, 'err'); return; }
    if (planTimer) clearInterval(planTimer); planTimer = setInterval(planPoll, 2000); planPoll(); }).catch(e => setStatus('plan-status', 'Error: ' + e, 'err'));
}
planPoll();

// ---- aim the modelled antenna ------------------------------------------------------
function aimAt(mode){
  const out = document.getElementById('aim-result'); out.textContent = '...';
  post('/api/antenna/aim', {mode}).then(d => {
    if (!d.ok){ out.textContent = d.error; return; }
    const a = d.antenna || {};
    out.innerHTML = (mode === 'reset' ? 'Back to config.py: ' : 'Boresight set to <b>' + d.azimuth_deg + '&deg;</b>: ') + escHtml(String(a.type)) + ' ' + a.azimuth_deg + '&deg; (' + escHtml(a.azimuth_source || '') + ').'
      + (d.neighbours ? '<table class="nb-table"><tr><th>Heard neighbour</th><th>Bearing</th><th>Dist</th><th>Gain before</th><th>Gain after</th></tr>'
        + d.neighbours.map(n => '<tr><td>' + escHtml(n.name) + '</td><td>' + n.bearing_deg + '&deg;</td><td>' + n.distance_km + ' km</td><td>' + n.gain_now_dbi + ' dBi</td><td style="color:' + (n.gain_new_dbi > n.gain_now_dbi ? '#00e5a0' : '#ff9500') + '">' + n.gain_new_dbi + ' dBi</td></tr>').join('') + '</table>' : '')
      + '<div class="config-hint">This updates the model and map. Rotate the physical antenna to ' + (mode === 'reset' ? 'the config.py bearing' : d.azimuth_deg + '&deg; true') + ' to match.</div>';
    antFill(d.antenna);
    if (typeof loadSim === 'function') loadSim();
    planPoll();
  }).catch(e => { out.textContent = 'Error: ' + e; });
}

// ---- manual antenna entry (Test tab) ---------------------------------------------------
function antFill(a){
  if (!a) return;
  const set = (id, v) => { const e = document.getElementById(id); if (e && document.activeElement !== e && v != null) e.value = v; };
  document.getElementById('ant-dir').checked = !!a.directional;
  set('ant-type', a.type === 'omni' ? '' : a.type); set('ant-az', a.azimuth_deg); set('ant-gain', a.gain_dbi); set('ant-fb', a.front_back_db);
  if (document.activeElement !== document.getElementById('ant-hpbw')) document.getElementById('ant-hpbw').value = a.hpbw_deg == null ? '' : a.hpbw_deg;
  document.getElementById('ant-src').textContent = a.directional ? ('Current: ' + a.type + ' ' + a.gain_dbi + ' dBi aimed at ' + a.azimuth_deg + '\u00b0 true, F/B ' + a.front_back_db + ' dB (from ' + (a.azimuth_source || 'config') + ')')
    : ('Current: omni ' + a.gain_dbi + ' dBi');
}
function antLoad(){ fetch('/api/antenna').then(r => r.json()).then(d => { if (d.ok) antFill(d.antenna); }).catch(() => {}); }
function antSave(){
  const num = id => { const v = document.getElementById(id).value; return v === '' ? null : parseFloat(v); };
  const body = {mode: 'settings', directional: document.getElementById('ant-dir').checked, type: document.getElementById('ant-type').value || (document.getElementById('ant-dir').checked ? 'Directional' : 'Omni'),
                azimuth_deg: num('ant-az'), gain_dbi: num('ant-gain'), front_back_db: num('ant-fb'), hpbw_deg: num('ant-hpbw')};
  if (body.gain_dbi == null){ setStatus('ant-status', 'Enter the antenna gain (dBi)', 'err'); return; }
  if (body.directional && body.azimuth_deg == null){ setStatus('ant-status', 'Enter the aim bearing', 'err'); return; }
  post('/api/antenna/aim', body).then(d => { if (!d.ok){ setStatus('ant-status', d.error, 'err'); return; }
    setStatus('ant-status', 'Saved', 'ok'); antGhostClear(); document.getElementById('ant-preset').value = ''; antFill(d.antenna); if (typeof loadSim === 'function') loadSim(); }).catch(e => setStatus('ant-status', 'Error: ' + e, 'err'));
}
antLoad();

// ---- common 868 MHz antennas: dropdown presets + ghost pattern on the map --------------------
// Typical figures for what people actually buy/build for 863-870 MHz. ALWAYS vertically polarised, so the azimuth
// plane is the antenna's H-plane (Yagi elements vertical): beamwidths below are H-plane, a little wider than the E-plane.
// Real antennas vary +/- 1-2 dB and the pattern is a smooth idealisation - use as a starting point and edit.
// [label, type text, gain dBi, front-to-back dB, -3 dB azimuth beamwidth (null = omni / derive from F/B)]
const ANT_PRESETS = [
  ['Rubber duck / stubby (~0 dBi)',       'Rubber duck',     0.0,  0, 'omni'],
  ['1/4-wave whip / ground plane (2 dBi)', 'Quarter-wave GP', 2.2,  0, 'omni'],
  ['5/8-wave ground plane (5 dBi)',       '5/8 GP',          5.0,  0, 'omni'],
  ['Fibreglass collinear (5.8 dBi)',      'Collinear 5.8',   5.8,  0, 'omni'],
  ['Fibreglass collinear (8 dBi)',        'Collinear 8',     8.0,  0, 'omni'],
  ['Moxon rectangle (~5.5 dBi)',          'Moxon',           5.5, 25, null],
  ['2-element Yagi (~6.5 dBi)',           'Yagi 2el',        6.5, 12, 100],
  ['3-element Yagi (~8 dBi)',             'Yagi 3el',        8.0, 15, 80],
  ['5-element Yagi (~10.5 dBi)',          'Yagi 5el',       10.5, 18, 60],
  ['9-element Yagi (~13 dBi)',            'Yagi 9el',       13.0, 20, 42],
  ['Flat patch / panel (~8 dBi)',         'Panel 8',         8.0, 18, 70],
  ['Sector 90 deg (~10 dBi)',             'Sector 90',      10.0, 20, 90],
  ['Corner reflector / grid (~12 dBi)',   'Corner 12',      12.0, 22, 50],
];
let ghostLayer = null;

(function antPresetInit(){
  const sel = document.getElementById('ant-preset'); if (!sel) return;
  sel.innerHTML = '<option value="">-- none (no ghost) --</option>' + ANT_PRESETS.map((p, i) => '<option value="' + i + '">' + escHtml(p[0]) + '</option>').join('');
})();

function antGhostClear(){ if (ghostLayer){ ghostLayer.remove(); ghostLayer = null; } }

// Ghost = what the antenna currently described by the form would look like on the map, aimed at the Aim field,
// sized by gain (see antReachKm) so different antennas can be compared like for like.
function antGhost(){
  antGhostClear();
  const sel = document.getElementById('ant-preset');
  if (!sel || sel.value === '' || !leafletMap || typeof L === 'undefined') return;
  const num = id => { const v = document.getElementById(id).value; return v === '' ? null : parseFloat(v); };
  const dir = document.getElementById('ant-dir').checked, gain = num('ant-gain'), lat = GATEWAY_LAT, lon = GATEWAY_LON;
  if (gain == null || (!lat && !lon)) return;
  const far = antReachKm(gain), az = num('ant-az') || 0, name = document.getElementById('ant-type').value || 'antenna';
  ghostLayer = L.layerGroup().addTo(leafletMap);
  const st = {color: '#ffb020', weight: 1.5, dashArray: '5 5', fillColor: '#ffb020', fillOpacity: .07, interactive: false};
  if (!dir){                                      // omni: a circle (azimuth plane)
    L.circle([lat, lon], Object.assign({radius: far * 1000}, st)).addTo(ghostLayer)
      .bindTooltip('ghost: ' + name + ' | ' + gain + ' dBi | omni | reach ~' + fmtReach(far), {permanent: true, direction: 'top', className: 'link-label'});
    return;
  }
  const fb = num('ant-fb') == null ? 25 : num('ant-fb'), hp = num('ant-hpbw'), pts = [];
  for (let t = 0; t < 360; t += 5) pts.push(destPoint(lat, lon, az + t, far * patField(t, fb, hp)));
  L.polygon(pts, st).addTo(ghostLayer);
  L.polyline([[lat, lon], destPoint(lat, lon, az, far * 1.1)], {color: '#ffb020', weight: 2, dashArray: '2 4', interactive: false})
    .addTo(ghostLayer).bindTooltip('ghost: ' + name + ' ' + az + '\u00b0 | ' + gain + ' dBi | F/B ' + fb + ' dB' + (hp ? ' | BW ' + hp + '\u00b0' : '') + ' | reach ~' + fmtReach(far), {permanent: true, direction: 'top', className: 'link-label'});
}

function antPreset(){
  const sel = document.getElementById('ant-preset'), set = (id, v) => { document.getElementById(id).value = v == null ? '' : v; };
  if (sel.value === ''){ antGhostClear(); return; }
  const p = ANT_PRESETS[+sel.value], omni = p[4] === 'omni';
  document.getElementById('ant-dir').checked = !omni;
  set('ant-type', p[1]); set('ant-gain', p[2]); set('ant-fb', omni ? '' : p[3]); set('ant-hpbw', omni ? '' : p[4]);
  if (!omni && document.getElementById('ant-az').value === '') set('ant-az', 0);
  antGhost();
  setStatus('ant-status', 'Preview only - ghost shown on the Map tab. Save to apply.', 'ok');
}

// ---- 0 dBi reference circle (scale for all antenna ghosts) -----------------------------------
(function refInit(){
  try {
    const v = parseFloat(localStorage.getItem('antRefRadiusM'));
    if (v >= 10 && v <= 50000) ANT_REF_RADIUS_M = v;
    const sh = document.getElementById('ref-show'); if (sh) sh.checked = localStorage.getItem('antRefShow') === '1';
  } catch (e) {}
  const r = document.getElementById('ref-radius'); if (r) r.value = ANT_REF_RADIUS_M;
})();

function refGhost(){
  if (refLayer){ refLayer.remove(); refLayer = null; }
  const sh = document.getElementById('ref-show');
  if (!sh || !sh.checked || !leafletMap || typeof L === 'undefined' || (!GATEWAY_LAT && !GATEWAY_LON)) return;
  refLayer = L.circle([GATEWAY_LAT, GATEWAY_LON], {radius: ANT_REF_RADIUS_M, color: '#8a94a6', weight: 1.5, dashArray: '2 6', fillColor: '#8a94a6', fillOpacity: .06, interactive: false})
    .addTo(leafletMap).bindTooltip('0 dBi reference: ' + fmtReach(ANT_REF_RADIUS_M / 1000) + ' radius', {permanent: true, direction: 'right', className: 'link-label'});
}

function refChange(){
  const inp = document.getElementById('ref-radius'), v = parseFloat(inp.value);
  if (!(v >= 10 && v <= 50000)){ setStatus('ant-status', 'Reference radius must be 10-50000 m', 'err'); return; }
  ANT_REF_RADIUS_M = v;
  try { localStorage.setItem('antRefRadiusM', String(v)); localStorage.setItem('antRefShow', document.getElementById('ref-show').checked ? '1' : '0'); } catch (e) {}
  refGhost(); antGhost();
  if (window.lastSim) drawSim(window.lastSim);     // saved antenna beam uses the same scale
}

// ---- antenna steering (Test tab card + the repeater popup button) --------------------------
window.steerCfg = null;
function steerForm(){
  const sel = document.getElementById('st-action');
  if (!sel || !window.steerCfg) return null;                 // form not loaded yet: server uses what is saved
  return {action: sel.value, topic: document.getElementById('st-topic').value, payload: document.getElementById('st-payload').value,
          qos: parseInt(document.getElementById('st-qos').value, 10), retain: document.getElementById('st-retain').checked,
          also_model: document.getElementById('st-also').checked};
}
function steerToggle(){
  const sel = document.getElementById('st-action'), box = document.getElementById('st-mqtt');
  if (box && sel) box.style.display = sel.value === 'mqtt' ? '' : 'none';
}
function steerLabel(az){
  const sel = document.getElementById('st-action'), a = sel && sel.value ? sel.value : (window.steerCfg && window.steerCfg.settings.action) || 'model';
  return a === 'mqtt' ? 'Steer antenna to ' + az + '&deg; (MQTT)' : a === 'skywatcher' ? 'Turn antenna to ' + az + '&deg; (mount)' : 'Set modelled aim to ' + az + '&deg;';
}
function steerFill(d, keepForm){
  window.steerCfg = d;
  const sel = document.getElementById('st-action'); if (!sel) return;
  if (!keepForm){
    sel.innerHTML = d.actions.map(a => '<option value="' + escHtml(a.id) + '">' + escHtml(a.label) + '</option>').join('');
    const s = d.settings, set = (id, v) => { document.getElementById(id).value = v; };
    sel.value = s.action; set('st-topic', s.topic); set('st-payload', s.payload); set('st-qos', s.qos);
    document.getElementById('st-retain').checked = !!s.retain; document.getElementById('st-also').checked = !!s.also_model;
    steerToggle();
  }
  const l = d.last, t = ts => new Date(ts * 1000).toLocaleTimeString();
  document.getElementById('st-info').textContent = 'MQTT broker: ' + (d.mqtt_connected ? 'connected' : 'NOT connected')
    + (l ? ' - last command ' + t(l.ts) + ': ' + l.azimuth + '\u00b0 via ' + l.action + (l.ok ? ' OK' : ' FAILED (' + l.error + ')') : ' - no command sent since the bridge started');
}
function steerLoad(keepForm){ return fetch('/api/steer').then(r => r.json()).then(d => { if (d.ok) steerFill(d, keepForm); }).catch(() => {}); }
function steerSave(){
  const f = steerForm(); if (!f) return;
  post('/api/steer/settings', f).then(d => { if (!d.ok){ setStatus('st-status', d.error, 'err'); return; }
    setStatus('st-status', 'Saved', 'ok'); steerFill(d, true); }).catch(e => setStatus('st-status', 'Error: ' + e, 'err'));
}
function steerSend(az, target){
  const f = steerForm(), act = f ? f.action : 'model';
  if (!f) return steerLoad(false).then(() => window.steerCfg ? steerSend(az, target) : setStatus('st-status', 'Steering settings could not be loaded - nothing sent', 'err'));
  if (act === 'skywatcher' && !confirm('Turn the antenna with the Sky-Watcher mount?\n\nAzimuth ' + az + '\u00b0 true' + (target ? ' (towards ' + target + ')' : '') + '\nCheck nothing can snag the coax or mast.')) return;
  if (act === 'mqtt' && !confirm('Send a steering command to the antenna director?\n\nAzimuth ' + az + '\u00b0 true' + (target ? ' (towards ' + target + ')' : '') + '\nMQTT topic: ' + f.topic + '\nPayload: ' + f.payload)) return;
  const show = (m, ok) => { setStatus('st-status', m, ok ? 'ok' : 'err'); const e = document.getElementById('steer-msg'); if (e) e.textContent = m; };
  if (act === 'skywatcher') window.mountBoost = Date.now() + 25000;
  return post('/api/steer', {azimuth: az, target: target || null, settings: f}).then(d => {
    if (!d.ok){ show(d.error, false); steerLoad(true); return; }
    show(d.detail, true); if (d.antenna && act !== 'skywatcher') antFill(d.antenna); steerLoad(true);
    if (act === 'skywatcher' && window.mountKick) window.mountKick();
    return Promise.resolve(typeof loadSim === 'function' ? loadSim() : null).then(() => {
      const i = document.getElementById('sim-info'); if (i) i.textContent = '\u2713 ' + d.detail + ' - ' + i.textContent; });
  }).catch(e => show('Error: ' + e, false));
}
function steerIdx(i){ const it = (window.simItems || [])[i]; if (it) steerSend(it.bearing_deg, it.name || it.key.slice(0, 8)); }
function steerAimField(){
  const v = document.getElementById('ant-az').value;
  if (v === '' || isNaN(parseFloat(v))){ setStatus('st-status', 'Enter the aim bearing in the Antenna card first', 'err'); return; }
  steerSend(((parseFloat(v) % 360) + 360) % 360, null);
}
steerLoad(false);

// ---- Sky-Watcher mount ----
function mountForm(){
  const v = id => document.getElementById(id);
  return {host: v('mt-host').value.trim(), port: parseInt(v('mt-port').value, 10), ref_bearing: parseFloat(v('mt-bearing').value),
          travel_deg: parseFloat(v('mt-travel').value), brake_deg: parseFloat(v('mt-brake').value), invert: v('mt-invert').checked,
          transport: v('mt-transport').value, serial_port: v('mt-serial').value.trim(), power_mode: v('mt-power').value,
          power_topic: v('mt-ptopic').value.trim(), boot_timeout_s: parseFloat(v('mt-boot').value), idle_off_s: parseFloat(v('mt-idle').value)};
}
function mountShow(){                                      // show only the rows that apply to the chosen connection / power mode
  const v = id => document.getElementById(id); if (!v('mt-transport')) return;
  const ser = v('mt-transport').value === 'serial', zb = v('mt-power').value === 'zigbee';
  v('mt-row-host').style.display = v('mt-row-port').style.display = ser ? 'none' : '';
  v('mt-row-serial').style.display = ser ? '' : 'none';
  document.querySelectorAll('.mt-zb').forEach(r => r.style.display = zb ? '' : 'none');
}
function mountFill(d, keepForm){
  const v = id => document.getElementById(id); if (!v('mt-host')) return;
  const s = d.settings;
  if (!keepForm){
    v('mt-host').value = s.host; v('mt-port').value = s.port; v('mt-bearing').value = s.ref_bearing;
    v('mt-travel').value = s.travel_deg; v('mt-brake').value = s.brake_deg; v('mt-invert').checked = !!s.invert;
    v('mt-transport').value = s.transport; v('mt-serial').value = s.serial_port; v('mt-power').value = s.power_mode;
    v('mt-ptopic').value = s.power_topic; v('mt-boot').value = s.boot_timeout_s; v('mt-idle').value = s.idle_off_s;
    mountShow();
  }
  const configured = s.transport === 'serial' ? !!s.serial_port : !!s.host;
  let t;
  if (!configured) t = 'Enter the mount IP address and press Save.';
  else if (d.asleep) t = (d.message || 'Mount is switched off.') + (d.calibrated ? ' Last known pointing ' + d.bearing + '\u00b0 true, ' + d.travel_left + '\u00b0 of travel left before the limit.' : '');
  else if (!d.reachable) t = 'Mount not reachable' + (d.message ? ': ' + d.message : '') + '.';
  else t = 'Mount reachable' + (d.moving ? ' (moving)' : '') + '. ' + (d.calibrated
    ? 'Pointing ' + d.bearing + '\u00b0 true, ' + d.travel_left + '\u00b0 of travel left before the limit.'
    : 'Not calibrated yet: point the antenna at the bearing above and press Set current pointing.');
  if (d.power_cycled) t = 'Mount position was reset (power-cycled?). If the antenna has not been moved since, press Restore last pointing' + (s.last_bearing != null ? ' (' + s.last_bearing + '\u00b0)' : '') + '; otherwise point it at a known bearing and press Set current pointing.';
  const lk = d.link;
  if (configured && lk && lk.outages && lk.outages.length){
    const o = lk.outages[lk.outages.length - 1];
    t += ' Last drop-out ' + new Date(o.start * 1000).toLocaleString('en-GB', {hour12: false}) + ' for ' + o.secs + ' s (' + lk.outages.length + ' since the bridge started).';
  } else if (configured && lk && lk.up === false && lk.down_at){
    t += ' Down since ' + new Date(lk.down_at * 1000).toLocaleTimeString('en-GB', {hour12: false}) + '.';
  }
  if (d.fault) t = 'STOPPED ' + new Date(d.fault.ts * 1000).toLocaleTimeString('en-GB', {hour12: false}) + ': ' + d.fault.msg + ' (now ' + d.fault.bearing + '\u00b0). ' + t;
  v('mt-info').textContent = t;
  const rb = v('mt-restore'); if (rb) rb.style.display = d.power_cycled ? '' : 'none';
  window.mountCfg = {host: configured, calibrated: !!d.calibrated, moving: !!d.moving};
  mountBanner(d);
  mountFollow(d);
}
// The ghost / beam on the map follows where the antenna REALLY points (read back from the mount).
function mountFollow(d){
  if (d.bearing == null) return;
  const changed = window.mountBearing !== d.bearing;
  window.mountBearing = d.bearing;
  const az = document.getElementById('ant-az');
  if (az && document.activeElement !== az) az.value = d.bearing;       // preview ghost (antenna type dropdown) uses this field
  if (!changed) return;
  const a = window.lastSim && window.lastSim.local && window.lastSim.local.antenna;
  if (a) { a.azimuth_deg = d.bearing; try { if (simLayer) drawBeam(window.lastSim.local, window.lastSim); } catch (e) {} }   // only the beam moves
  if (typeof antGhost === 'function') antGhost();                                                              // + the antenna-type ghost
  const src = document.getElementById('ant-src');
  if (src && a && a.directional) src.textContent = 'Current: ' + a.type + ' ' + a.gain_dbi + ' dBi aimed at ' + d.bearing + '\u00b0 true (read from the mount), F/B ' + a.front_back_db + ' dB';
}
function mountLoad(keepForm){ return fetch('/api/mount').then(r => r.json()).then(d => { if (d.ok) mountFill(d, keepForm); }).catch(() => {}); }
function mountCall(url, body, okMsg){
  window.mountBoost = Date.now() + 25000;                 // poll fast while the mount turns
  setStatus('mt-status', 'Working...', 'ok');
  return post(url, body).then(d => {
    if (!d.ok) { setStatus('mt-status', d.error, 'err'); return; }
    setStatus('mt-status', d.detail || okMsg || 'Done', 'ok'); mountFill(d, true); if (window.mountKick) window.mountKick();
  }).catch(e => setStatus('mt-status', 'Error: ' + e, 'err'));
}
function mountSave(){ return mountCall('/api/mount/settings', mountForm(), 'Saved'); }
function mountCalibrate(){
  const f = mountForm();
  if (isNaN(f.ref_bearing)) { setStatus('mt-status', 'Enter the bearing the antenna points at', 'err'); return; }
  if (!confirm('Record that the antenna points at ' + f.ref_bearing + '\u00b0 true right now?\n\nIf this is wrong every later bearing will be wrong.')) return;
  return mountCall('/api/mount/calibrate', {settings: f});
}
function mountNudge(d){ return mountCall('/api/mount/nudge', {delta: d}); }
function mountStop(){ return mountCall('/api/mount/stop', {}); }
function mountRestore(){
  if (!confirm('Assume the antenna has NOT moved since the mount was switched off, and re-pair it with its last bearing?')) return;
  return mountCall('/api/mount/restore', {});
}
mountLoad(false);
(function mountPollLoop(){
  let timer = null;
  const tick = () => {
    clearTimeout(timer);
    const cfg = window.mountCfg, fast = (cfg && cfg.moving) || Date.now() < (window.mountBoost || 0);
    const go = !document.hidden && cfg && cfg.host && cfg.calibrated;
    (go ? mountLoad(true) : Promise.resolve()).then(() => { timer = setTimeout(tick, fast ? 500 : 8000); });
  };
  window.mountKick = () => { window.mountBoost = Date.now() + 25000; tick(); };      // a command was just sent: poll now, and fast
  timer = setTimeout(tick, 3000);
})();

// ---- MQTT monitor page ----
let mqMsgs = [], mqState = null;
const MQ_MAX = 300;
function mqRender(){
  const el = document.getElementById('mq-list'); if (!el) return;
  const q = (document.getElementById('mq-find').value || '').toLowerCase();
  const rows = mqMsgs.filter(m => !q || m.topic.toLowerCase().includes(q) || m.payload.toLowerCase().includes(q)).slice().reverse();
  document.getElementById('mq-count').textContent = rows.length + (q ? ' of ' + mqMsgs.length : '') + ' messages';
  if (!rows.length){
    el.innerHTML = '<div class="empty-state">' + (mqState && mqState.subscribed ? 'Subscribed to ' + escHtml(mqState.active_filter || mqState.filter) + ' - waiting for messages...' : 'Press Subscribe to watch the bridge\'s MQTT topics.') + '</div>';
    return;
  }
  el.innerHTML = rows.map(m => {
    let body = m.payload;
    try { body = JSON.stringify(JSON.parse(m.payload), null, 1); } catch (e) {}
    return '<div class="log-entry"><div class="log-row1"><span style="color:var(--text-muted)">' + new Date(m.ts * 1000).toLocaleTimeString() + '</span>'
      + '<span class="nb-id">' + escHtml(m.topic) + '</span>'
      + (m.retain ? '<span class="badge">retained</span>' : '') + '<span class="badge">qos ' + m.qos + '</span><span class="badge">' + m.size + ' B' + (m.truncated ? ', cut' : '') + '</span></div>'
      + '<pre style="margin:2px 0 0;white-space:pre-wrap;word-break:break-all;font-size:10px;color:var(--text)">' + escHtml(body) + '</pre></div>';
  }).join('');
}
function mqFillState(d){
  mqState = d;
  const f = document.getElementById('mq-filter'); if (!f) return;
  if (document.activeElement !== f && !f.dataset.touched) f.value = d.filter;
  if (d.suggestions) document.getElementById('mq-suggest').innerHTML = d.suggestions.map(s => '<option value="' + escHtml(s.filter) + '">' + escHtml(s.label) + '</option>').join('');
  const b = document.getElementById('mq-state');
  b.textContent = !d.broker_connected ? 'broker not connected' : d.subscribed ? 'subscribed: ' + (d.active_filter || d.filter) : 'not subscribed';
  b.className = 'badge' + (d.subscribed && d.broker_connected ? ' on' : '');
  document.getElementById('mq-sub').disabled = !!(d.subscribed && (d.active_filter === f.value.trim()));
  document.getElementById('mq-unsub').disabled = !d.subscribed;
}
function mqLoad(){
  return fetch('/api/mqtt/monitor').then(r => r.json()).then(d => {
    if (!d.ok) return; mqMsgs = d.messages || []; mqFillState(d); mqRender(); }).catch(() => {});
}
function mqCall(url, body, msg){
  return post(url, body || {}).then(d => {
    if (!d.ok){ setStatus('mq-status', d.error, 'err'); return; }
    setStatus('mq-status', msg, 'ok'); mqFillState(d); if (d.messages) { mqMsgs = d.messages; } mqRender();
  }).catch(e => setStatus('mq-status', 'Error: ' + e, 'err'));
}
function mqSubscribe(){ const f = document.getElementById('mq-filter'); f.dataset.touched = '1'; return mqCall('/api/mqtt/subscribe', {filter: f.value.trim()}, 'Subscribed'); }
function mqUnsubscribe(){ return mqCall('/api/mqtt/unsubscribe', {}, 'Unsubscribed'); }
function mqClear(){ return mqCall('/api/mqtt/clear', {}, 'Cleared'); }
socket.on('mqtt_msg', m => {
  mqMsgs.push(m); if (mqMsgs.length > MQ_MAX) mqMsgs.shift();
  if (document.getElementById('page-mqtt').classList.contains('active')) mqRender();
});
socket.on('mqtt_monitor', d => { d.suggestions = d.suggestions || (mqState && mqState.suggestions); mqFillState(d); });
(function mqHook(){
  const orig = showPage;
  showPage = function(name){ orig(name); if (name === 'mqtt') mqLoad(); };
})();
document.getElementById('mq-filter').addEventListener('input', e => { e.target.dataset.touched = '1';
  if (mqState) document.getElementById('mq-sub').disabled = !!(mqState.subscribed && mqState.active_filter === e.target.value.trim()); });
mqLoad();

// ---- Test page: three steps (Antenna & aim -> Steering & mount -> Run tests), one shown at a time ----
function testTab(name){
  if (!document.getElementById('tg-' + name)) name = 'antenna';
  document.querySelectorAll('#page-test .test-group').forEach(g => g.classList.toggle('active', g.id === 'tg-' + name));
  document.querySelectorAll('#test-tabs .subtab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  try { localStorage.setItem('testTab', name); } catch (e) {}
  window.dispatchEvent(new Event('resize'));          // let charts size themselves once visible
}
function testNav(){ let t = null; try { t = localStorage.getItem('testTab'); } catch (e) {} testTab(t || 'antenna'); }

// ---- base-station picker on the Map page (every repeater within ALWAYS km is listed, nearest first) ----
function simPickFill(d){
  const sel = document.getElementById('sim-pick'); if (!sel) return;
  const keep = sel.value ? (window.simItems[+sel.value] || {}).key : null;
  const rows = d.items.map((it, i) => [i, it]).sort((a, b) => a[1].distance_km - b[1].distance_km);
  sel.innerHTML = '<option value="">Base stations (' + rows.length + ')...</option>' + rows.map(r => '<option value="' + r[0] + '">'
    + escHtml(r[1].name || r[1].key.slice(0, 8)) + ' - ' + r[1].distance_km + ' km, ' + r[1].bearing_deg + '\u00b0' + (r[1].source === 'discovered' ? ' (heard)' : '') + '</option>').join('');
  if (keep){ const k = d.items.findIndex(i => i.key === keep); if (k >= 0) sel.value = String(k); }
}
function simPickShow(){
  const sel = document.getElementById('sim-pick'); if (!sel || sel.value === '') return;
  const it = (window.simItems || [])[+sel.value], m = (window.simMarkers || [])[+sel.value];
  if (!it || !leafletMap) return;
  leafletMap.panTo([it.lat, it.lon]);
  if (m && m.openPopup) m.openPopup([it.lat, it.lon]);
}
function simPickSteer(){
  const sel = document.getElementById('sim-pick');
  if (!sel || sel.value === ''){ const i = document.getElementById('sim-info'); if (i) i.textContent = 'Pick a base station first'; return; }
  steerIdx(+sel.value);
}

// ---- Mount slewing banner (flashing, red) ----
function mountBanner(d){
  const el = document.getElementById('mount-slew'); if (!el) return;
  el.style.display = d && d.moving ? '' : 'none';
  if (d && d.moving) document.getElementById('ms-az').textContent = 'Azimuth ' + (d.bearing != null ? d.bearing.toFixed(1) + '\u00b0' : '-');
}
