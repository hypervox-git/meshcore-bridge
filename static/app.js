const socket = io();
let totalPackets = 0;
const nodes = {};
let channelMap = {};
let activePage = 'dashboard';
const unread = {};
const chatMessages = {};
let localNodeId = null;
let notificationsEnabled = false;
let logPaused = false;
const logBuffer = [];
const sparklineData = {};
const sparklineCharts = {};
const aliases = {};
const geoCache = {};
const allLogEntries = [];

let GATEWAY_LAT = window.BRIDGE_CONFIG.gatewayLat;
let GATEWAY_LON = window.BRIDGE_CONFIG.gatewayLon;
const TRAIL_MAX   = window.BRIDGE_CONFIG.trailMax;


function tick(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('en-GB')}
setInterval(tick,1000);tick();
setInterval(refreshNodeAges,30000);

function loadSysinfo(){
  fetch('/api/sysinfo').then(r=>r.json()).then(d=>{
    function vc(v,w,h){return v==null?'sys-ok':v>=h?'sys-hot':v>=w?'sys-warn':'sys-ok';}
    const cpu=document.getElementById('sys-cpu');const ram=document.getElementById('sys-ram');
    const temp=document.getElementById('sys-temp');const upt=document.getElementById('sys-uptime');
    if(d.cpu!=null){cpu.textContent=d.cpu+'%';cpu.className='sys-val '+vc(d.cpu,70,90);}
    if(d.ram!=null){ram.textContent=d.ram+'%';ram.className='sys-val '+vc(d.ram,75,90);}
    if(d.temp!=null){temp.textContent=d.temp+'C';temp.className='sys-val '+vc(d.temp,70,80);}
    if(d.uptime) upt.textContent=d.uptime;
  }).catch(()=>{});
}
loadSysinfo();setInterval(loadSysinfo,10000);

function updateNotifBtn(){
  const btn=document.getElementById('notif-btn');
  if(!('Notification' in window)){btn.textContent='Alerts n/a';btn.title='This browser cannot show desktop notifications';btn.className='hdr-btn denied';return;}
  if(Notification.permission==='granted'&&notificationsEnabled){btn.textContent='Alerts on';btn.title='Desktop notification for each new message. Click to turn off.';btn.className='hdr-btn on';}
  else if(Notification.permission==='denied'){btn.textContent='Alerts blocked';btn.title='The browser is blocking notifications for this site. Click the padlock in the address bar, allow Notifications, then reload.';btn.className='hdr-btn denied';}
  else{btn.textContent='Alerts off';btn.title='Click to get a desktop notification for each new message';btn.className='hdr-btn';}
}
function toggleNotif(){
  if(!('Notification' in window)) return;
  if(Notification.permission==='granted'){notificationsEnabled=!notificationsEnabled;updateNotifBtn();return;}
  Notification.requestPermission().then(()=>{notificationsEnabled=Notification.permission==='granted';updateNotifBtn();});
}
function pushNotif(title,body){
  if(!notificationsEnabled||Notification.permission!=='granted') return;
  try{const n=new Notification(title,{body,tag:'mesh-msg',renotify:true});n.onclick=()=>{window.focus();n.close();};setTimeout(()=>n.close(),8000);}catch(e){}
}
updateNotifBtn();

socket.on('geocode',(d)=>{geoCache[d.key]=d.name;renderNodes();});
socket.on('status',(data)=>{
  document.getElementById('serial-dot').className='dot'+(data.serial?'':' offline');
  document.getElementById('mqtt-dot').className='dot'+(data.mqtt?'':' offline');
  document.getElementById('serial-status').textContent=data.serial?'OK':'Down';
  document.getElementById('mqtt-status').textContent=data.mqtt?'MQTT OK':'MQTT down';
});
socket.on('packet',(data)=>{
  totalPackets++;
  document.getElementById('stat-total').textContent=totalPackets;
  document.getElementById('log-count').textContent=totalPackets;
  document.getElementById('log-count2').textContent=totalPackets+' packets';
  if(data.rxRssi) document.getElementById('stat-rssi').textContent=data.rxRssi;
  if(data.rxSnr)  document.getElementById('stat-snr').textContent=parseFloat(data.rxSnr).toFixed(1);
  updateNodes(data);appendLog(data);allLogEntries.push(data);
  if(data.decoded&&data.decoded.portnum==='TEXT_MESSAGE_APP'&&data.decoded.text){
    const chIdx=data.channel!==undefined?parseInt(data.channel):0;
    const from=data.fromId||String(data.from||'?');const text=data.decoded.text;
    const entry={ts:data.rxTime?parseInt(data.rxTime)*1000:Date.now(),from,text,channel:chIdx,sent:false};
    addChatMessage(chIdx,entry);
    if(activePage!=='ch-'+chIdx){markUnread(chIdx);const chName=channelMap[chIdx]?channelMap[chIdx].name||('ch'+chIdx):'ch'+chIdx;const nodeName=(nodes[from]&&(aliases[from]||nodes[from].name))||from;pushNotif(nodeName+' ['+chName+']',text);}
  }
});
socket.on('channels',(data)=>{channelMap={};data.forEach(ch=>{channelMap[ch.index]=ch;});buildChannelPages(data);});
socket.on('local_node',(data)=>{localNodeId=data.id;});
socket.on('aliases',(data)=>{Object.assign(aliases,data);renderNodes();});
socket.on('node_link',(d)=>{
  if(!nodes[d.id]) nodes[d.id]={id:d.id,packets:0};
  const n=nodes[d.id];
  if(d.name&&!n.name) n.name=d.name;
  if(d.rssi!=null) n.rssi=d.rssi;
  if(d.snr!=null) n.snr=parseFloat(d.snr).toFixed(1);
  if(d.pathloss!=null) n.pathloss=d.pathloss;
  if(d.noise!=null) n.noise=d.noise;
  if(d.tx_air!=null){n.txAir=d.tx_air;n.rxAir=d.rx_air;}
  if(d.hw) n.hw=d.hw;
  if(d.lat!=null&&d.lon!=null){n.lat=d.lat;n.lon=d.lon;}
  n.linkTs=d.linkTs;n.lastSeenTs=d.linkTs*1000;
  renderNodes();
});
socket.on('nodedb_flushed',()=>{
  Object.keys(nodes).forEach(k=>delete nodes[k]);
  if(leafletMap){Object.values(mapMarkers).forEach(m=>{try{leafletMap.removeLayer(m);}catch(e){}});Object.keys(mapMarkers).forEach(k=>delete mapMarkers[k]);}
  Object.keys(sparklineCharts).forEach(k=>{try{sparklineCharts[k].destroy();}catch(e){}delete sparklineCharts[k];delete sparklineData[k];});
  document.getElementById('stat-nodes').textContent='0';
  renderNodes();
});

const SYSTEM_PAGES=['log','mqtt','apidocs'];                  // grouped under one "System" tab
function sysPage(){let p=null;try{p=localStorage.getItem('sysPage');}catch(e){}return SYSTEM_PAGES.includes(p)?p:'log';}
function showPage(name){
  activePage=name;
  const tab=SYSTEM_PAGES.includes(name)?'system':name;
  if(tab==='system'){try{localStorage.setItem('sysPage',name);}catch(e){}}
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.toggle('active',t.dataset.page===tab));
  document.querySelectorAll('.subnav a').forEach(t=>t.classList.toggle('active',t.dataset.page===name));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  const page=document.getElementById('page-'+name);if(page) page.classList.add('active');
  const m=name.match(/^ch-(\d+)$/);if(m){clearUnread(parseInt(m[1]));scrollChat(parseInt(m[1]));}
  if(name!=='map'&&mapTimer){clearInterval(mapTimer);mapTimer=null;}
  if(name!=='log'&&mqttTimer){clearInterval(mqttTimer);mqttTimer=null;}
  if(name==='log'&&mqttView){loadMqttInfo();if(mqttTimer)clearInterval(mqttTimer);mqttTimer=setInterval(loadMqttInfo,5000);}
  if(name==='apidocs') renderApiDocs();
  if(name==='map') showMap();
}

// ---- Map (Leaflet) ----
let leafletMap=null;
const mapMarkers={};
let gatewayMarker=null;
let linkLayer=null;
let mapTimer=null;

function initMap(){
  if(leafletMap||typeof L==='undefined') return;
  leafletMap=L.map('leaflet-map',{zoomControl:true}).setView([GATEWAY_LAT,GATEWAY_LON],13);
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',{
    maxZoom:16, attribution:'Tiles &copy; Esri'
  }).addTo(leafletMap);
  gatewayMarker=L.circleMarker([GATEWAY_LAT,GATEWAY_LON],{radius:8,color:'#00e5a0',fillColor:'#00e5a0',fillOpacity:0.9,weight:2})
    .addTo(leafletMap).bindPopup('<b>PiNode</b><br>gateway');
}

function popupHtml(n,label){
  return '<b>'+escHtml(label)+'</b><br>'+n.id
    +(n.rssi?'<br>RSSI '+n.rssi+' dBm':'')
    +(n.snr?'<br>SNR '+n.snr+' dB':'')
    +(n.pathloss!=null?'<br>Path loss ~'+n.pathloss+' dB':'')
    +(n.battery!==undefined?'<br>Bat '+n.battery+'%':'');
}

function linkColour(snr){return snr==null?'#4a5568':snr>=5?'#00e5a0':snr>=0?'#ff9500':'#ff3b3b';}

function renderLinks(){
  // Lines gateway -> nodes measured by the repeater test (heard within the last hour),
  // coloured by SNR and labelled with the estimated path loss.
  if(!leafletMap) return;
  if(linkLayer) linkLayer.remove();
  linkLayer=L.layerGroup().addTo(leafletMap);
  if(!GATEWAY_LAT&&!GATEWAY_LON) return;
  const now=Date.now()/1000;
  Object.values(nodes).forEach(n=>{
    if(!n.linkTs||now-n.linkTs>3600||!n.lat||!n.lon||(window.simKeys&&window.simKeys.has(n.id))) return;
    const snr=n.snr!=null?parseFloat(n.snr):null;
    const line=L.polyline([[GATEWAY_LAT,GATEWAY_LON],[n.lat,n.lon]],{color:linkColour(snr),weight:3,opacity:.85}).addTo(linkLayer);
    const lbl=(n.pathloss!=null?'PL ~'+n.pathloss+' dB':'')+(snr!=null?(n.pathloss!=null?' | ':'')+'SNR '+snr+' dB':'');
    if(lbl) line.bindTooltip(lbl,{permanent:true,direction:'center',className:'link-label'});
  });
}

function renderMap(){
  if(!leafletMap) return;
  renderLinks();
  Object.values(nodes).forEach(n=>{
    if(n.id===localNodeId||!n.lat||!n.lon) return;
    const label=aliases[n.id]||n.name||n.id;
    const col=n.rssi?(parseInt(n.rssi)>-80?'#00e5a0':parseInt(n.rssi)>-100?'#ff9500':'#ff3b3b'):'#4a5568';
    if(mapMarkers[n.id]){
      mapMarkers[n.id].setLatLng([n.lat,n.lon]).setStyle({color:col,fillColor:col}).setPopupContent(popupHtml(n,label));
    } else {
      mapMarkers[n.id]=L.circleMarker([n.lat,n.lon],{radius:6,color:col,fillColor:col,fillOpacity:0.85,weight:2})
        .addTo(leafletMap).bindPopup(popupHtml(n,label));
    }
  });
}

function refreshMapData(){
  // Pull the bridge's authoritative node list so the map stays current even
  // between infrequent live POSITION packets. Merges position/telemetry into
  // the shared `nodes` store, then re-renders markers.
  fetch('/api/nodes').then(r=>r.json()).then(data=>{
    Object.values(data).forEach(node=>{
      const id=(node.user&&node.user.id)||(node.num!==undefined?'!'+node.num.toString(16):null);
      if(!id) return;
      if(!nodes[id]) nodes[id]={id,packets:0};
      const n=nodes[id];
      if(node.user&&node.user.longName) n.name=node.user.longName;
      if(node.position&&node.position.latitudeI&&node.position.longitudeI){
        n.lat=node.position.latitudeI/1e7;n.lon=node.position.longitudeI/1e7;
      }
      if(node.snr) n.snr=parseFloat(node.snr).toFixed(1);
      if(node.rssi!==undefined) n.rssi=node.rssi;
      if(node.pathloss!==undefined) n.pathloss=node.pathloss;
      if(node.noise!==undefined) n.noise=node.noise;
      if(node.tx_air!==undefined){n.txAir=node.tx_air;n.rxAir=node.rx_air;}
      if(node.linkTs) n.linkTs=node.linkTs;
      if(node.deviceMetrics&&node.deviceMetrics.batteryLevel!==undefined) n.battery=node.deviceMetrics.batteryLevel;
      if(node.lastHeard) n.lastSeenTs=node.lastHeard*1000;
    });
    renderMap();
    renderTrackerBar();
    selectedTrackers.forEach(drawTrail);
  }).catch(()=>{});
}

const TRAIL_COLORS=['#00e5a0','#0077ff','#ff9500','#b48cff','#00c8d4','#ff3b3b'];
const selectedTrackers=new Set();
const trailLayers={};
const trackerColor={};
function colorForTracker(id){
  if(!(id in trackerColor)) trackerColor[id]=TRAIL_COLORS[Object.keys(trackerColor).length%TRAIL_COLORS.length];
  return trackerColor[id];
}
function isTracker(n){return /^tracker/i.test(aliases[n.id]||n.name||'');}

function renderTrackerBar(){
  const el=document.getElementById('tracker-list');if(!el) return;
  const trackers=Object.values(nodes).filter(isTracker);
  if(!trackers.length){el.innerHTML='<span class="tracker-none">no Tracker-named devices seen yet</span>';return;}
  el.innerHTML=trackers.map(n=>{
    const nm=aliases[n.id]||n.name||n.id;const on=selectedTrackers.has(n.id);const c=colorForTracker(n.id);
    return '<label class="tracker-chip"><input type="checkbox" '+(on?'checked':'')+' onchange="toggleTracker(\''+n.id+'\',this.checked)"><span class="tracker-dot" style="background:'+c+'"></span>'+escHtml(nm)+'</label>';
  }).join('');
}

function toggleTracker(id,on){
  if(on){selectedTrackers.add(id);drawTrail(id);}
  else{selectedTrackers.delete(id);if(trailLayers[id]){leafletMap.removeLayer(trailLayers[id]);delete trailLayers[id];}}
}

function drawTrail(id){
  if(!leafletMap||!selectedTrackers.has(id)) return;
  const c=colorForTracker(id);
  fetch('/api/poshist?node='+encodeURIComponent(id)).then(r=>r.json()).then(d=>{
    if(!selectedTrackers.has(id)) return;                 // unticked while fetching
    const pts=d.history||[];
    if(trailLayers[id]){leafletMap.removeLayer(trailLayers[id]);delete trailLayers[id];}
    const grp=L.layerGroup();
    if(pts.length>1) L.polyline(pts.map(p=>[p[1],p[2]]),{color:c,weight:2,opacity:0.55}).addTo(grp);
    pts.forEach(p=>{
      const t=new Date(p[0]*1000).toLocaleString('en-GB');
      L.circleMarker([p[1],p[2]],{radius:3,color:c,fillColor:c,fillOpacity:0.85,weight:1})
        .bindTooltip(t,{direction:'top'}).addTo(grp);
    });
    grp.addTo(leafletMap);trailLayers[id]=grp;
  }).catch(()=>{});
}

function showMap(){
  initMap();
  if(!leafletMap) return;
  refreshMapData();                                   // immediate fresh pull
  if(mapTimer) clearInterval(mapTimer);
  mapTimer=setInterval(refreshMapData,10000);         // keep it live while the tab is open
  setTimeout(()=>{
    leafletMap.invalidateSize();
    renderMap();
    renderTrackerBar();
    const pts=[[GATEWAY_LAT,GATEWAY_LON]];
    Object.values(nodes).forEach(n=>{if(n.id!==localNodeId&&n.lat&&n.lon)pts.push([n.lat,n.lon]);});
    if(pts.length>1) leafletMap.fitBounds(pts,{padding:[40,40],maxZoom:16});
  },120);
}

// ---- Channel pages ----
function buildChannelPages(channels){
  document.querySelectorAll('.nav-tab[data-page^="ch-"]').forEach(t=>t.remove());
  document.querySelectorAll('.page[id^="page-ch-"]').forEach(p=>p.remove());
  const nav=document.getElementById('main-nav');
  channels.forEach(ch=>{
    const pid='ch-'+ch.index;const label='['+ch.index+'] '+(ch.name||'unnamed');
    const tab=document.createElement('div');tab.className='nav-tab';tab.dataset.page=pid;tab.onclick=()=>showPage(pid);
    tab.innerHTML=label+'<span class="pip" id="pip-'+ch.index+'"></span>';nav.insertBefore(tab,nav.querySelector('[data-page="log"]'));
    const page=document.createElement('div');page.className='page';page.id='page-'+pid;
    page.innerHTML='<div class="chat-wrap">'
      +'<div class="chat-header"><span>'+label+'</span><span class="badge">'+(ch.role||'SECONDARY')+'</span></div>'
      +'<div class="chat-messages" id="chat-'+ch.index+'"><div class="empty-state">No messages yet</div></div>'
      +'<div class="compose"><input class="compose-input" type="text" id="input-'+ch.index+'" placeholder="Message on '+label+'..." maxlength="220" onkeydown="if(event.key===\'Enter\')sendMessage('+ch.index+')" />'
      +'<button class="btn btn-send" onclick="sendMessage('+ch.index+')">Send</button>'
      +'<span class="send-status" id="status-'+ch.index+'"></span></div></div>';
    document.body.appendChild(page);if(!chatMessages[ch.index]) chatMessages[ch.index]=[];
  });
  loadHistory();
}

function addChatMessage(chIdx,entry,noScroll){
  if(!chatMessages[chIdx]) chatMessages[chIdx]=[];chatMessages[chIdx].push(entry);
  const container=document.getElementById('chat-'+chIdx);if(!container) return;
  container.querySelectorAll('.empty-state').forEach(e=>e.remove());
  const isSent=entry.sent||(localNodeId&&entry.from===localNodeId);
  const t=new Date(entry.ts).toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit'});
  const div=document.createElement('div');div.className='msg '+(isSent?'sent':'recv');
  div.innerHTML='<div class="msg-bubble">'+escHtml(entry.text)+'</div>'
    +'<div class="msg-meta">'+(isSent?'<span class="msg-sent-label">you</span>':'<span class="msg-from">'+entry.from+'</span>')+'<span>'+t+'</span></div>';
  container.appendChild(div);if(!noScroll) scrollChat(chIdx);
}

function scrollChat(idx){const c=document.getElementById('chat-'+idx);if(c)setTimeout(()=>{c.scrollTop=c.scrollHeight;},50);}
function markUnread(idx){if(!unread[idx])unread[idx]=0;unread[idx]++;const pip=document.getElementById('pip-'+idx);if(pip)pip.classList.add('show');const tab=document.querySelector('[data-page="ch-'+idx+'"]');if(tab)tab.style.color='var(--warn)';}
function clearUnread(idx){unread[idx]=0;const pip=document.getElementById('pip-'+idx);if(pip)pip.classList.remove('show');const tab=document.querySelector('[data-page="ch-'+idx+'"]');if(tab)tab.style.color='';}

function sendMessage(chIdx){
  const input=document.getElementById('input-'+chIdx);const text=input.value.trim();
  if(!text){setStatus('status-'+chIdx,'Empty','err');return;}
  setStatus('status-'+chIdx,'...','');
  fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,channel:chIdx})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){input.value='';setStatus('status-'+chIdx,'Sent','ok');addChatMessage(chIdx,{ts:Date.now(),from:'[me]',text,channel:chIdx,sent:true});}
      else setStatus('status-'+chIdx,'Err','err');
    }).catch(()=>setStatus('status-'+chIdx,'Err','err'));
}

function loadHistory(){
  fetch('/api/messages').then(r=>r.json()).then(msgs=>{
    msgs.forEach(entry=>{const idx=entry.channel||0;if(document.getElementById('chat-'+idx)) addChatMessage(idx,entry,true);});
    Object.keys(chatMessages).forEach(idx=>scrollChat(parseInt(idx)));
  });
}

function saveAlias(nodeId,val){
  aliases[nodeId]=val.trim();
  fetch('/api/aliases',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:nodeId,name:val.trim()})}).then(()=>renderNodes());
}

function ageClass(ts){if(!ts) return 'age-unknown';const m=(Date.now()-ts)/60000;return m<5?'age-fresh':m<30?'age-recent':'age-stale';}
function ageLabel(ts){if(!ts) return 'never seen';const s=Math.floor((Date.now()-ts)/1000);if(s<60) return s+'s ago';const m=Math.floor(s/60);if(m<60) return m+'m ago';const h=Math.floor(m/60);return h<24?h+'h ago':Math.floor(h/24)+'d ago';}

function refreshNodeAges(){
  Object.values(nodes).forEach(n=>{
    const safeid=n.id.replace(/[^a-z0-9]/gi,'_');
    const card=document.getElementById('nodecard-'+safeid);if(!card) return;
    card.className='node-card '+ageClass(n.lastSeenTs);
    const el=card.querySelector('.node-age');if(el) el.textContent=ageLabel(n.lastSeenTs);
  });
}

function calcHealth(n){
  let s=100;
  if(n.rssi){const v=parseInt(n.rssi);s+=v<-110?-40:v<-100?-25:v<-90?-15:v<-80?-5:0;}
  if(n.snr){const v=parseFloat(n.snr);s+=v<-5?-30:v<0?-15:v<5?-5:0;}
  if(n.hops!==undefined) s-=Math.min(n.hops*10,30);
  if(n.lastSeenTs){const m=(Date.now()-n.lastSeenTs)/60000;s+=m>60?-30:m>30?-15:m>5?-5:0;}
  return Math.max(0,Math.min(100,s));
}
function healthColor(s){return s>=80?'#00e5a0':s>=50?'#ff9500':'#ff3b3b';}

function updateSparkline(nodeId,battery,rssi){
  if(!sparklineData[nodeId]) sparklineData[nodeId]={battery:[],rssi:[],labels:[]};
  const d=sparklineData[nodeId];
  const t=new Date().toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit'});
  if(battery!==undefined){d.battery.push(battery);if(d.battery.length>20) d.battery.shift();}
  if(rssi!==undefined){d.rssi.push(parseInt(rssi));if(d.rssi.length>20) d.rssi.shift();}
  d.labels.push(t);if(d.labels.length>20) d.labels.shift();
  const chart=sparklineCharts[nodeId];
  if(chart){chart.data.labels=d.labels;chart.data.datasets[0].data=d.battery;chart.data.datasets[1].data=d.rssi.map(v=>v+130);chart.update('none');}
}

function initSparkline(nodeId,canvasId){
  const canvas=document.getElementById(canvasId);if(!canvas||sparklineCharts[nodeId]) return;
  const d=sparklineData[nodeId]||{battery:[],rssi:[],labels:[]};
  sparklineCharts[nodeId]=new Chart(canvas,{type:'line',data:{labels:d.labels,datasets:[
    {label:'Bat%',data:d.battery,borderColor:'#00e5a0',borderWidth:1,pointRadius:0,fill:false,tension:.3},
    {label:'RSSI+130',data:d.rssi.map(v=>v+130),borderColor:'#0077ff',borderWidth:1,pointRadius:0,fill:false,tension:.3}
  ]},options:{animation:false,plugins:{legend:{display:false}},scales:{x:{display:false},y:{display:false,min:0,max:130}},responsive:true,maintainAspectRatio:false}});
}

function updateNodes(data){
  const id=data.fromId||data.from;if(!id) return;
  if(!nodes[id]) nodes[id]={id,packets:0};
  const n=nodes[id];n.packets++;n.lastSeenTs=Date.now();
  n.lastSeen=new Date().toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit'});
  if(data.rxRssi) n.rssi=data.rxRssi;
  if(data.rxSnr)  n.snr=parseFloat(data.rxSnr).toFixed(1);
  if(data.decoded&&data.decoded.user){                 // NODEINFO -> keep names dynamic
    if(data.decoded.user.longName) n.name=data.decoded.user.longName;
    if(data.decoded.user.hwModel)  n.hw=data.decoded.user.hwModel;
  }
  const dm=data.decoded&&data.decoded.telemetry&&data.decoded.telemetry.deviceMetrics;
  if(dm){
    if(dm.batteryLevel!==undefined) n.battery=dm.batteryLevel;
    if(dm.voltage) n.voltage=parseFloat(dm.voltage).toFixed(2);
    if(dm.channelUtilization!==undefined) n.chanUtil=parseFloat(dm.channelUtilization).toFixed(1);
    if(dm.airUtilTx!==undefined) n.airUtil=parseFloat(dm.airUtilTx).toFixed(2);
    if(dm.uptimeSeconds) n.uptime=formatUptime(dm.uptimeSeconds);
  }
  updateSparkline(id,n.battery,n.rssi);
  const pos=data.decoded&&data.decoded.position;
  if(pos&&pos.latitudeI&&pos.longitudeI){
    n.lat=pos.latitudeI/1e7;n.lon=pos.longitudeI/1e7;
    if(!n.trail) n.trail=[];
    n.trail.push({lat:n.lat,lon:n.lon,ts:Date.now()});
    if(n.trail.length>TRAIL_MAX) n.trail.shift();
    const key=Math.round(n.lat*1000)/1000+','+Math.round(n.lon*1000)/1000;
    if(!geoCache[key]) fetch('/api/geocode?lat='+n.lat+'&lon='+n.lon).then(r=>r.json()).then(d=>{if(d.name)geoCache[key]=d.name;renderNodes();}).catch(()=>{});
    else n.place=geoCache[key];
  }
  document.getElementById('stat-nodes').textContent=Object.keys(nodes).length;
  renderNodes();
}

function formatUptime(s){return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m'}
function rssiClass(v){if(!v)return '';v=parseInt(v);return v>-80?'good':v>-100?'warn-text':'bad'}

function renderNodes(){
  document.getElementById('nodes-grid').innerHTML=Object.values(nodes).map(n=>{
    const safeid=n.id.replace(/[^a-z0-9]/gi,'_');
    const displayName=aliases[n.id]||n.name||'';
    const health=calcHealth(n);const hcol=healthColor(health);
    const key=n.lat&&n.lon?(Math.round(n.lat*1000)/1000+','+Math.round(n.lon*1000)/1000):null;
    const place=key&&geoCache[key]?geoCache[key]:null;
    return '<div class="node-card '+ageClass(n.lastSeenTs)+'" id="nodecard-'+safeid+'">'
      +(displayName?'<div class="node-name">'+displayName+'</div>':'')
      +'<div class="node-id">'+n.id+'</div>'
      +'<div class="node-age">'+ageLabel(n.lastSeenTs)+'</div>'
      +(place?'<div class="node-place">near '+place+'</div>':'')
      +'<div class="health-label">Mesh health: '+health+'%</div>'
      +'<div class="health-bar"><div class="health-fill" style="width:'+health+'%;background:'+hcol+'"></div></div>'
      +(n.hw?'<div class="node-row"><span class="node-row-label">HW</span><span>'+n.hw+'</span></div>':'')
      +(n.hops!==undefined?'<div class="node-row"><span class="node-row-label">Hops</span><span>'+n.hops+'</span></div>':'')
      +(n.battery!==undefined?'<div class="node-row"><span class="node-row-label">Battery</span><span class="'+(n.battery<20?'bad':'good')+'">'+n.battery+'%</span></div>':'')
      +(n.voltage?'<div class="node-row"><span class="node-row-label">Voltage</span><span>'+n.voltage+'V</span></div>':'')
      +(n.rssi?'<div class="node-row"><span class="node-row-label">RSSI</span><span class="'+rssiClass(n.rssi)+'">'+n.rssi+' dBm</span></div>':'')
      +(n.snr?'<div class="node-row"><span class="node-row-label">SNR</span><span>'+n.snr+' dB</span></div>':'')
      +(n.noise!=null?'<div class="node-row"><span class="node-row-label">Noise floor</span><span>'+n.noise+' dBm</span></div>':'')
      +(n.txAir!=null?'<div class="node-row"><span class="node-row-label">Airtime TX/RX</span><span>'+n.txAir+' s / '+n.rxAir+' s</span></div>':'')
      +(n.pathloss!=null?'<div class="node-row"><span class="node-row-label">Path loss</span><span>~'+n.pathloss+' dB</span></div>':'')
      +(n.lat?'<div class="node-row"><span class="node-row-label">Lat</span><span>'+n.lat.toFixed(5)+'</span></div>':'')
      +(n.lon?'<div class="node-row"><span class="node-row-label">Lon</span><span>'+n.lon.toFixed(5)+'</span></div>':'')
      +(n.chanUtil!==undefined?'<div class="node-row"><span class="node-row-label">Chan util</span><span>'+n.chanUtil+'%</span></div>':'')
      +(n.airUtil!==undefined?'<div class="node-row"><span class="node-row-label">Air TX</span><span>'+n.airUtil+'%</span></div>':'')
      +(n.uptime?'<div class="node-row"><span class="node-row-label">Uptime</span><span>'+n.uptime+'</span></div>':'')
      +'<div class="node-row"><span class="node-row-label">Packets</span><span>'+n.packets+'</span></div>'
      +'<div class="sparkline-wrap"><canvas id="spark-'+safeid+'" height="40"></canvas></div>'
      +'<input class="alias-input" type="text" placeholder="Set friendly name..." value="'+(aliases[n.id]||'')+'" onchange="saveAlias(\''+n.id+'\',this.value)" />'
      +'</div>';
  }).join('');
  Object.values(nodes).forEach(n=>{const safeid=n.id.replace(/[^a-z0-9]/gi,'_');setTimeout(()=>initSparkline(n.id,'spark-'+safeid),50);});
  renderRadar();
  if(leafletMap) renderMap();
}

function haversine(lat1,lon1,lat2,lon2){
  const R=6371000,p=Math.PI/180;
  const dLat=(lat2-lat1)*p,dLon=(lon2-lon1)*p;
  const a=Math.sin(dLat/2)*Math.sin(dLat/2)+Math.cos(lat1*p)*Math.cos(lat2*p)*Math.sin(dLon/2)*Math.sin(dLon/2);
  const dist=R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
  const y=Math.sin((lon2-lon1)*p)*Math.cos(lat2*p);
  const x=Math.cos(lat1*p)*Math.sin(lat2*p)-Math.sin(lat1*p)*Math.cos(lat2*p)*Math.cos((lon2-lon1)*p);
  return {dist,bearing:(Math.atan2(y,x)/p+360)%360};
}

function renderRadar(){
  const svg=document.getElementById('radar-svg');const lbl=document.getElementById('radar-label');if(!svg) return;
  const W=380,H=220,cx=190,cy=110,maxR=95;
  // '!repeater' is the attached repeater itself (co-located with the PiNode). It has no position, so it was plotted from
  // its own last RSSI / noise floor as if it were a distant node - misleading, so it is never drawn here.
  const nodeList=Object.values(nodes).filter(n=>n.id!==localNodeId&&n.id!=='!repeater');
  if(!nodeList.length){svg.innerHTML='';lbl.textContent='Node map - waiting for remote nodes...';return;}
  const gpsNodes=nodeList.filter(n=>n.lat&&n.lon);const useGPS=gpsNodes.length>0;
  let maxDist=1;
  if(useGPS){gpsNodes.forEach(n=>{const h=haversine(GATEWAY_LAT,GATEWAY_LON,n.lat,n.lon);if(h.dist>maxDist)maxDist=h.dist;});lbl.textContent='Node map - GPS (max '+Math.round(maxDist)+'m)';}
  else lbl.textContent='Node map - RSSI estimate';
  function rssiToR(r){const v=Math.max(-130,Math.min(-40,parseInt(r)||-100));return Math.round(((v+40)/(-90))*maxR);}
  let out='';
  [0.33,0.66,1].forEach(f=>{
    const r=Math.round(maxR*f);out+='<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="#1e2530" stroke-width="1"/>';
    if(useGPS){const rl=Math.round(maxDist*f);out+='<text x="'+(cx+r+2)+'" y="'+(cy-3)+'" font-size="8" fill="#2d3748" font-family="monospace">'+(rl>=1000?(rl/1000).toFixed(1)+'km':rl+'m')+'</text>';}
  });
  if(useGPS){out+='<text x="'+cx+'" y="'+(cy-maxR-6)+'" font-size="9" fill="#2d3748" font-family="monospace" text-anchor="middle">N</text>';out+='<line x1="'+cx+'" y1="'+(cy-maxR)+'" x2="'+cx+'" y2="'+(cy-maxR+8)+'" stroke="#2d3748" stroke-width="1"/>';}
  out+='<circle cx="'+cx+'" cy="'+cy+'" r="6" fill="#00e5a0"/>';
  out+='<text x="'+(cx+9)+'" y="'+(cy+4)+'" font-size="9" fill="#00e5a0" font-family="monospace">PiNode</text>';
  const rssiOnly=nodeList.filter(n=>!n.lat||!n.lon);
  const angleStep=rssiOnly.length>1?(2*Math.PI)/rssiOnly.length:0;let rssiIdx=0;
  nodeList.forEach(n=>{
    const col=n.rssi?(parseInt(n.rssi)>-80?'#00e5a0':parseInt(n.rssi)>-100?'#ff9500':'#ff3b3b'):'#4a5568';
    const label=aliases[n.id]||n.name||n.id;let nx,ny;
    if(n.lat&&n.lon){
      const h=haversine(GATEWAY_LAT,GATEWAY_LON,n.lat,n.lon);
      const r=Math.max(8,(h.dist/maxDist)*maxR);
      const angle=(h.bearing-90)*Math.PI/180;
      nx=cx+r*Math.cos(angle);ny=cy+r*Math.sin(angle);
      if(n.trail&&n.trail.length>1){
        for(let i=1;i<n.trail.length;i++){
          const tp=n.trail[i-1];const tc=n.trail[i];
          const hp=haversine(GATEWAY_LAT,GATEWAY_LON,tp.lat,tp.lon);const hc=haversine(GATEWAY_LAT,GATEWAY_LON,tc.lat,tc.lon);
          const rp=Math.max(8,Math.round((hp.dist/maxDist)*maxR));const rc=Math.max(8,Math.round((hc.dist/maxDist)*maxR));
          const ap=(hp.bearing-90)*Math.PI/180;const ac=(hc.bearing-90)*Math.PI/180;
          const px=Math.max(10,Math.min(W-10,Math.round(cx+rp*Math.cos(ap))));const py=Math.max(10,Math.min(H-10,Math.round(cy+rp*Math.sin(ap))));
          const qx=Math.max(10,Math.min(W-10,Math.round(cx+rc*Math.cos(ac))));const qy=Math.max(10,Math.min(H-10,Math.round(cy+rc*Math.sin(ac))));
          out+='<line x1="'+px+'" y1="'+py+'" x2="'+qx+'" y2="'+qy+'" stroke="'+col+'" stroke-width="1" opacity="'+(i/n.trail.length*0.5).toFixed(2)+'"/>';
        }
      }
    } else {
      const r=n.rssi?rssiToR(n.rssi):Math.round(maxR*0.6);
      const angle=angleStep*rssiIdx-Math.PI/2;
      nx=cx+r*Math.cos(angle);ny=cy+r*Math.sin(angle);rssiIdx++;
    }
    nx=+Math.max(10,Math.min(W-10,nx)).toFixed(2);ny=+Math.max(10,Math.min(H-10,ny)).toFixed(2);
    const lx=nx+(nx>=cx?8:-8);const anchor=nx>=cx?'start':'end';
    out+='<line x1="'+cx+'" y1="'+cy+'" x2="'+nx+'" y2="'+ny+'" stroke="'+col+'" stroke-width="1" stroke-dasharray="3,3" opacity="0.4"/>';
    out+='<circle cx="'+nx+'" cy="'+ny+'" r="5" fill="'+col+'" opacity="0.9"/>';
    out+='<text x="'+lx+'" y="'+(ny-6)+'" font-size="9" fill="#c8d0dc" font-family="monospace" text-anchor="'+anchor+'">'+escHtml(String(label))+'</text>';
    if(n.rssi) out+='<text x="'+lx+'" y="'+(ny+5)+'" font-size="8" fill="'+col+'" font-family="monospace" text-anchor="'+anchor+'">'+n.rssi+'dBm</text>';
  });
  svg.innerHTML=out;
}

let mqttView=false;
let mqttTimer=null;
function toggleMqttView(){
  mqttView=!mqttView;
  const lc=document.getElementById('log-container');
  const mc=document.getElementById('mqtt-container');
  const btn=document.getElementById('mqtt-view-btn');
  if(mqttView){
    lc.style.display='none';mc.style.display='block';btn.className='btn btn-send';btn.textContent='Packets';
    loadMqttInfo();if(mqttTimer)clearInterval(mqttTimer);mqttTimer=setInterval(loadMqttInfo,5000);
  }else{
    mc.style.display='none';lc.style.display='block';btn.className='btn';btn.textContent='MQTT';
    if(mqttTimer){clearInterval(mqttTimer);mqttTimer=null;}
  }
}

function loadMqttInfo(){
  fetch('/api/mqtt').then(r=>r.json()).then(d=>{
    const c=document.getElementById('mqtt-container');
    const conn=d.connected?'<span class="good">connected</span>':'<span class="bad">disconnected</span>';
    let html='<div class="mqtt-status">Broker <code>'+d.broker+'</code> &middot; root <code>'+d.topic_root+'</code> &middot; '+conn+' &middot; HA discovery '+(d.ha_discovery?'on':'off')+'</div>';
    const ids=Object.keys(d.nodes||{});
    if(!ids.length){c.innerHTML=html+'<div class="empty-state">No devices have published to MQTT yet. Open the MQTT tab to watch the raw messages.</div>';return;}
    ids.sort((a,b)=>(d.nodes[b].ts||0)-(d.nodes[a].ts||0));
    html+=ids.map(id=>{
      const n=d.nodes[id];const nm=aliases[id]||(nodes[id]&&nodes[id].name)||id;
      const v=n.values||{};
      const chips=Object.keys(v).map(k=>'<span class="mqtt-chip">'+k+': '+v[k]+'</span>').join('');
      const age=n.ts?ageLabel(n.ts*1000):'-';
      const port=n.last_port?n.last_port.replace('_APP',''):'';
      return '<div class="mqtt-item">'
        +'<div class="mqtt-item-head"><span class="mqtt-node">'+escHtml(nm)+'</span><span class="mqtt-id">'+id+'</span><span class="mqtt-age">'+age+'</span></div>'
        +'<div class="mqtt-topic">'+n.topic+'</div>'
        +'<div class="mqtt-port">'+(port?port+' &middot; ':'')+(n.count||0)+' msgs published</div>'
        +(chips?'<div class="mqtt-chips">'+chips+'</div>':'')
        +'</div>';
    }).join('');
    c.innerHTML=html;
  }).catch(e=>{document.getElementById('mqtt-container').innerHTML='<div class="empty-state">Error: '+e+'</div>';});
}

function toggleScrollLock(){
  logPaused=!logPaused;const btn=document.getElementById('scroll-lock-btn');
  btn.textContent=logPaused?'Resume':'Pause';btn.className=logPaused?'btn btn-send':'btn';
  if(!logPaused){logBuffer.forEach(fn=>fn());logBuffer.length=0;}
}

function exportCSV(){
  const rows=[['time','from','channel','type','content']];
  allLogEntries.forEach(d=>{
    const t=new Date((d.rxTime?parseInt(d.rxTime)*1000:Date.now())).toISOString();
    const from=d.fromId||String(d.from||'');const ch=d.channel!==undefined?d.channel:'';
    const type=(d.decoded&&d.decoded.portnum)||'';const content=(d.decoded&&d.decoded.text)||'';
    rows.push([t,from,ch,type,'"'+content.replace(/"/g,'""')+'"']);
  });
  const csv=rows.map(r=>r.join(',')).join('\n');
  const blob=new Blob([csv],{type:'text/csv'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='meshcore_log.csv';
  document.body.appendChild(a);a.click();document.body.removeChild(a);
}

function appendLog(data){
  const doAppend=()=>{
    const container=document.getElementById('log-container');
    container.querySelectorAll('.empty-state').forEach(e=>e.remove());
    const portnum=(data.decoded&&data.decoded.portnum)||'UNKNOWN';
    const chIdx=data.channel!==undefined?data.channel:null;
    const chName=chIdx!==null&&channelMap[chIdx]?channelMap[chIdx].name:'';
    const chanLabel=chIdx!==null?('ch'+chIdx+(chName?' · '+chName:'')):'';
    const entry=document.createElement('div');entry.className='log-entry';
    entry.innerHTML='<div class="log-row1"><span class="log-time">'+new Date().toLocaleTimeString('en-GB',{hour12:false})+'</span>'
      +'<span class="log-from">'+(data.fromId||String(data.from||'?'))+'</span>'
      +(chanLabel?'<span class="log-chan">'+chanLabel+'</span>':'')
      +'<span class="log-type type-'+portnum+'">'+portnum.replace('_APP','')+'</span>'
      +'</div><div class="log-content">'+formatContent(data)+'</div>';
    container.insertBefore(entry,container.firstChild);
    const entries=container.querySelectorAll('.log-entry');if(entries.length>200) entries[entries.length-1].remove();
  };
  if(logPaused) logBuffer.push(doAppend); else doAppend();
}

function formatContent(data){
  const p=data.decoded;if(!p) return '-';if(p.text) return escHtml(p.text);
  const dm=p.telemetry&&p.telemetry.deviceMetrics;
  if(dm){const parts=[];if(dm.batteryLevel!==undefined) parts.push('bat='+dm.batteryLevel+'%');if(dm.voltage) parts.push('v='+parseFloat(dm.voltage).toFixed(2));if(dm.airUtilTx!==undefined) parts.push('airTx='+parseFloat(dm.airUtilTx).toFixed(2)+'%');if(dm.uptimeSeconds) parts.push('up='+formatUptime(dm.uptimeSeconds));return parts.join('  ');}
  const pos=p.position;if(pos){const lat=pos.latitudeI?(pos.latitudeI/1e7).toFixed(5):'?';const lon=pos.longitudeI?(pos.longitudeI/1e7).toFixed(5):'?';return 'lat='+lat+'  lon='+lon+(pos.altitude?'  alt='+pos.altitude+'m':'');}
  return JSON.stringify(p).substring(0,120);
}
function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function setStatus(id,msg,type){const el=document.getElementById(id);if(!el) return;el.textContent=msg;el.className='send-status'+(type==='ok'?' send-ok':type==='err'?' send-err':'');if(type) setTimeout(()=>{el.textContent='';el.className='send-status'},3000);}

const API_ENDPOINTS=[
  {method:'GET',path:'/',desc:'Main dashboard UI'},
  {method:'GET',path:'/api/sysinfo',desc:'Pi CPU, RAM, temp, uptime'},
  {method:'GET',path:'/api/nodes',desc:'Live node list (contacts, adverts and measured links)'},
  {method:'GET',path:'/api/nodedb',desc:'Persistent node database'},
  {method:'GET',path:'/api/mqtt',desc:'Per-device MQTT publish state (topic, last values, count) + broker status'},
  {method:'GET',path:'/api/poshist',desc:'Round-robin position history (>=28d). ?node=<id> for one nodes trail; no arg = per-node counts'},
  {method:'POST',path:'/api/nodedb/flush',desc:'Flush stored nodes, positions and trails (bridge-side; not the on-device NodeDB). Body: {}'},
  {method:'GET',path:'/api/messages',desc:'Persistent text message log (last 500)'},
  {method:'GET',path:'/api/lora',desc:'Current LoRa radio configuration'},
  {method:'POST',path:'/api/lora',desc:'Update LoRa config (releases + reacquires the serial port). Body: {region, modem_preset, hop_limit, tx_power, tx_enabled, sx126x_rx_boosted_gain, ignore_mqtt, use_preset}'},
  {method:'GET',path:'/api/security',desc:'Security assessment with score and recommendations'},
  {method:'GET',path:'/api/channels',desc:'Configured channels'},
  {method:'POST',path:'/api/channels',desc:'Add channel. Body: {name}'},
  {method:'DELETE',path:'/api/channels/:index',desc:'Delete channel'},
  {method:'GET',path:'/api/channels/:index/url',desc:'Channel share URL'},
  {method:'GET',path:'/api/channels/:index/qr',desc:'Channel QR PNG'},
  {method:'POST',path:'/api/send',desc:'Send message. Body: {text, channel}'},
  {method:'POST',path:'/api/neighbors/discover',desc:'Zero-hop neighbour discovery (rate-limited, duty-cycle budgeted). Body: {types:[repeater,room,sensor,client], wait_s}'},
  {method:'POST',path:'/api/neighbors/remote',desc:'A repeater neighbour table. Body: {to, password}'},
  {method:'GET',path:'/api/mqtt/monitor',desc:'MQTT monitor: subscription state and recent messages'},
  {method:'POST',path:'/api/mqtt/subscribe',desc:'Subscribe the monitor. Body: {filter?} (must be under the bridge topic)'},
  {method:'POST',path:'/api/mqtt/unsubscribe',desc:'Unsubscribe the monitor'},
  {method:'POST',path:'/api/mqtt/clear',desc:'Clear the monitor buffer'},
  {method:'GET',path:'/api/mount',desc:'Sky-Watcher mount: settings and live pointing'},
  {method:'POST',path:'/api/mount/settings',desc:'Save mount settings. Body: {host, port, ref_bearing, park_bearing, travel_deg, brake_deg, invert}'},
  {method:'POST',path:'/api/mount/calibrate',desc:'Record the bearing the antenna points at now. Body: {settings?}'},
  {method:'POST',path:'/api/mount/nudge',desc:'Turn the antenna by a few degrees. Body: {delta}'},
  {method:'POST',path:'/api/mount/goto',desc:'Slew the antenna to a bearing. Body: {azimuth} 0-360'},
  {method:'POST',path:'/api/mount/park',desc:'Turn the antenna to the park bearing (default 180, the cable-relaxed position)'},
  {method:'GET',path:'/rotator',desc:'Phone-sized rotator page (jog, go to, set pointing, stop)'},
  {method:'POST',path:'/api/mount/restore',desc:'After a mount power cycle: re-pair it with its last known bearing'},
  {method:'POST',path:'/api/mount/stop',desc:'Stop the mount'},
  {method:'GET',path:'/api/nbtable',desc:'Persistent neighbour table (public-database repeaters + heard/discovered state, range, bearing)'},
  {method:'POST',path:'/api/nbtable/sync',desc:'Update the neighbour table from the public database now'},
  {method:'GET',path:'/api/test/status',desc:'Test-mode state: radio params, duty-cycle usage, running link test'},
  {method:'GET',path:'/api/test/targets',desc:'Contacts available as test targets'},
  {method:'POST',path:'/api/test/link/start',desc:'Start live RSSI/noise-floor test (results over Socket.IO test_sample). Body: {to, password}'},
  {method:'POST',path:'/api/test/link/stop',desc:'Stop the live link test'},
  {method:'POST',path:'/api/test/trace',desc:'Traceroute: discover route, trace out and back, per-hop SNR. Body: {to}'},
  {method:'GET',path:'/api/aliases',desc:'Node aliases'},
  {method:'POST',path:'/api/aliases',desc:'Set alias. Body: {id, name}'},
  {method:'GET',path:'/api/geocode',desc:'Reverse geocode. Query: ?lat=&lon='},
];
function flushNodeDb(){
  if(!confirm('Flush the node + location database?\n\nClears all stored nodes, positions and trails held by the bridge. Live data repopulates as packets arrive. Does NOT reset the radio on-device node DB.')) return;
  fetch('/api/nodedb/flush',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(d=>{ if(!d.ok) alert('Flush failed: '+d.error); })
    .catch(e=>alert('Flush error: '+e));
}

function renderApiDocs(){
  const T=window.BRIDGE_CONFIG.mqttTopic||'meshcore';
  document.getElementById('api-docs-content').innerHTML=
    '<div style="padding-bottom:.75rem;font-size:10px;color:var(--text-muted);letter-spacing:.1em;text-transform:uppercase">REST API</div>'
    +API_ENDPOINTS.map(e=>'<div class="api-endpoint"><span class="api-method method-'+e.method.toLowerCase()+'">'+e.method+'</span><span class="api-path">'+e.path+'</span><div class="api-desc">'+e.desc+'</div></div>').join('')
    +'<div style="padding:.75rem 0;font-size:10px;color:var(--text-muted)">MQTT: '+T+'/nodes | /text | /telemetry | /position | /ch0..chN<br>Inject: publish to '+T+'/send with JSON {text, channel}</div>';
}

function loadNodes(){
  fetch('/api/nodes').then(r=>r.json()).then(data=>{
    Object.values(data).forEach(node=>{
      const id=(node.user&&node.user.id)||('!'+node.num.toString(16));if(!id) return;
      if(!nodes[id]) nodes[id]={id,packets:0};const n=nodes[id];
      if(node.user&&node.user.longName) n.name=node.user.longName;
      if(node.user&&node.user.hwModel)  n.hw=node.user.hwModel;
      const dm=node.deviceMetrics;
      if(dm){if(dm.batteryLevel!==undefined) n.battery=dm.batteryLevel;if(dm.voltage) n.voltage=parseFloat(dm.voltage).toFixed(2);if(dm.channelUtilization!==undefined) n.chanUtil=parseFloat(dm.channelUtilization).toFixed(1);if(dm.airUtilTx!==undefined) n.airUtil=parseFloat(dm.airUtilTx).toFixed(2);if(dm.uptimeSeconds) n.uptime=formatUptime(dm.uptimeSeconds);}
      if(node.snr) n.snr=parseFloat(node.snr).toFixed(1);
      if(node.rssi!==undefined) n.rssi=node.rssi;
      if(node.pathloss!==undefined) n.pathloss=node.pathloss;
      if(node.noise!==undefined) n.noise=node.noise;
      if(node.tx_air!==undefined){n.txAir=node.tx_air;n.rxAir=node.rx_air;}
      if(node.linkTs) n.linkTs=node.linkTs;
      if(node.hopsAway!==undefined) n.hops=node.hopsAway;
      if(node.lastHeard){n.lastSeenTs=node.lastHeard*1000;n.lastSeen=new Date(node.lastHeard*1000).toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit'});}
      if(node.position&&node.position.latitudeI&&node.position.longitudeI){n.lat=node.position.latitudeI/1e7;n.lon=node.position.longitudeI/1e7;}
    });
    fetch('/api/nodedb').then(r=>r.json()).then(db=>{
      Object.entries(db).forEach(([id,saved])=>{if(!nodes[id]){nodes[id]=Object.assign({id,packets:0},saved);if(saved.lastSeen)nodes[id].lastSeenTs=saved.lastSeen*1000;}});
      document.getElementById('stat-nodes').textContent=Object.keys(nodes).length;renderNodes();
    }).catch(()=>{document.getElementById('stat-nodes').textContent=Object.keys(nodes).length;renderNodes();});
  });
  fetch('/api/aliases').then(r=>r.json()).then(d=>Object.assign(aliases,d)).catch(()=>{});
}

loadNodes();
function loadChannels(){fetch('/api/channels').then(r=>r.json()).then(data=>{channelMap={};data.forEach(ch=>{channelMap[ch.index]=ch;});buildChannelPages(data);}).catch(()=>{});}
loadChannels();

// ---- Node map: mouse-wheel zoom (at the cursor), drag to pan, double-click to reset ----------------
// Works on the SVG viewBox, so renderRadar() can keep redrawing the contents without losing the view.
(function radarZoom(){
  const svg=document.getElementById('radar-svg'),hint=document.getElementById('radar-hint');if(!svg) return;
  const W=380,H=220,MAXZ=16;let vb={x:0,y:0,w:W,h:H},drag=null;
  function apply(){
    vb.w=Math.min(W,Math.max(W/MAXZ,vb.w));vb.h=vb.w*H/W;
    vb.x=Math.min(W-vb.w,Math.max(0,vb.x));vb.y=Math.min(H-vb.h,Math.max(0,vb.y));
    svg.setAttribute('viewBox',vb.x.toFixed(2)+' '+vb.y.toFixed(2)+' '+vb.w.toFixed(2)+' '+vb.h.toFixed(2));
    if(hint) hint.textContent=vb.w<W-0.01?'x'+(W/vb.w).toFixed(1)+' - drag to pan, double-click to reset':'scroll to zoom, drag to pan';
    svg.classList.toggle('radar-zoomed',vb.w<W-0.01);
  }
  function frac(ev){const r=svg.getBoundingClientRect();return {fx:(ev.clientX-r.left)/(r.width||1),fy:(ev.clientY-r.top)/(r.height||1),rw:r.width||1,rh:r.height||1};}
  svg.addEventListener('wheel',ev=>{
    ev.preventDefault();
    const f=frac(ev),dy=ev.deltaY*(ev.deltaMode===1?16:ev.deltaMode===2?100:1),k=Math.exp(Math.max(-300,Math.min(300,dy))*0.0016);
    const px=vb.x+f.fx*vb.w,py=vb.y+f.fy*vb.h;            // SVG point under the cursor stays put
    vb.w=Math.min(W,Math.max(W/MAXZ,vb.w*k));vb.h=vb.w*H/W;vb.x=px-f.fx*vb.w;vb.y=py-f.fy*vb.h;apply();
  },{passive:false});
  svg.addEventListener('pointerdown',ev=>{
    if(ev.button!==undefined&&ev.button!==0) return;
    drag={sx:ev.clientX,sy:ev.clientY,x:vb.x,y:vb.y};
    try{svg.setPointerCapture(ev.pointerId);}catch(e){}
    svg.classList.add('radar-drag');
  });
  svg.addEventListener('pointermove',ev=>{
    if(!drag) return;const f=frac(ev);
    vb.x=drag.x-(ev.clientX-drag.sx)/f.rw*vb.w;vb.y=drag.y-(ev.clientY-drag.sy)/f.rh*vb.h;apply();
  });
  const end=()=>{drag=null;svg.classList.remove('radar-drag');};
  svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
  svg.addEventListener('dblclick',()=>{vb={x:0,y:0,w:W,h:H};apply();});
  apply();
})();
