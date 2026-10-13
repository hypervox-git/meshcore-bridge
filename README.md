# meshcore-bridge

A MeshCore serial-to-MQTT bridge and web dashboard — a fork of `meshtastic-bridge`
retargeted to the **MeshCore** platform via the official async `meshcore` Python
library. Talks to a dedicated MeshCore companion node over USB serial; forwards
DMs, channel messages, adverts (position), and telemetry to MQTT / Home Assistant,
and reuses the dashboard, live map, breadcrumb trails and per-device MQTT view.

**Status: feature-complete foundation, tested against a mock node but NOT yet run
against real MeshCore hardware.** See "Bring-up" below — the event→UI field mapping
and the new Neighbours/Test features (below) must be confirmed on your device.

## What changed from the Meshtastic bridge

- `bridge/meshtastic_io.py` → **`bridge/meshcore_io.py`**: connects with
  `MeshCore.create_serial()` (asyncio, run in a dedicated thread), subscribes to
  MeshCore events and translates them into the dashboard's existing data shapes.
- SocketIO switched to **`async_mode='threading'`** (MeshCore's asyncio loop and
  eventlet don't mix) — the eventlet dependency and its deprecation warning are gone.
- Dropped the Meshtastic-only pages (LoRa config, PSK channel manager, security
  assessment) — no MeshCore equivalent.
- `bridge/state.py`, `bridge/history.py`, `bridge/mqtt_bridge.py`, the map, trails
  and MQTT view are reused. MQTT inject now routes through a send hook.

## Setup

```bash
cp config.example.py config.py     # edit: SERIAL_PORT of the MeshCore node, MQTT, coords
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py            # dashboard on http://<host>:5001
```

The companion node must be flashed as a **SERIAL** companion (a BLE-companion
build can't be reached over serial). Runs on port **5001** so it can sit alongside
the Meshtastic bridge (5000).

### Updating a running install (e.g. on the Pi)

Develop and push from your workstation, then pull on the host running the bridge.
`config.py` and `*.json` are git-ignored, so a pull never touches your local config:

```bash
cd ~/meshcore-bridge && git pull && sudo systemctl restart meshcore-bridge
```

(`deploy/meshcore-bridge.service` is the systemd unit; adjust the path/service name to
your install. Work in progress lives on feature branches - `git checkout <branch>` first.)

## Bring-up (do this first, on your node)

MeshCore event payload field names vary by firmware/library version. `config.py`
ships with `MESHCORE_DEBUG = True`, which logs every raw event and its payload:

```bash
.venv/bin/python app.py            # watch the console; send a test DM / channel msg
```

Confirm the real keys for message text, sender, and advert lat/lon against the
`_g(...)` fallbacks in `bridge/meshcore_io.py` (`_on_dm`, `_on_chan`, `_on_advert`,
`_on_telemetry`). Tighten those, then set `MESHCORE_DEBUG = False`.

## Neighbours and Test mode

Two new dashboard tabs (backend in `bridge/testmode.py`, duty-cycle maths in
`bridge/dutycycle.py`, UI in `static/test.js`):

- **Neighbours** - *Discover* sends one zero-hop `NODE_DISCOVER` request and lists every
  node in direct radio range that answers (type, RSSI, SNR both ways). A second panel
  fetches a **repeater's own neighbour table** (needs a login).
- **Test -> Live link test** - polls a repeater / room server / sensor and live-plots
  its **RSSI, noise floor and SNR** next to ours (chart + tiles). The target's figures
  come from its status reply; ours come from the local radio stats, read over serial
  (no airtime), so our noise floor updates every couple of seconds while the over-the-air
  poll is paced. Auto-stops after `TEST_MAX_MINUTES` or 5 unanswered requests.
- **Test -> Traceroute** - discovers the route to a node, traces it out and back, lists
  per-hop SNR and **draws the path on the Map tab** (segments coloured by SNR, numbered
  repeater markers, dashed grey where a hop has no known location). Hop hashes are
  resolved against your repeater contacts; 1-byte hash collisions pick the nearest
  candidate and are flagged in the popup.

Tabs can be deep-linked, e.g. `http://<bridge-host>:5001/#test`, `/#neighbors`, `/#map`.

### Home Assistant dashboard

The two new tabs can be embedded in a Home Assistant dashboard with `iframe` cards in
`panel` views (the deep links above select the tab):

```yaml
- title: Neighbours
  path: neighbours
  type: panel
  icon: mdi:radar
  cards:
    - type: iframe
      url: http://<bridge-host>:5001/#neighbors
      aspect_ratio: 90%
- title: Test
  path: test
  type: panel
  icon: mdi:signal-variant
  cards:
    - type: iframe
      url: http://<bridge-host>:5001/#test
      aspect_ratio: 90%
```

Browsers block a plain-`http://` iframe inside an `https://` page, so this works when
Home Assistant is opened over local `http://`; for an HTTPS front end, proxy the bridge
behind the same TLS origin. The traceroute's "Show on map" switches to the bridge's own
Map tab inside the embed - Home Assistant's native map cannot draw the path.

### Repeater mode (no companion node)

If the node on `SERIAL_PORT` runs **Repeater** firmware (its USB serial is a text CLI, so the
companion handshake fails with "No response from meshcore node"), set `SERIAL_MODE = "repeater"`
in `config.py`. The bridge then polls `stats-radio`, `stats-packets` and `neighbors` read-only
and shows live noise floor / RSSI / SNR, airtime, packet counters and directly-heard
neighbours on the **Test** tab. Nothing is transmitted.

**Repeater test mode** (Test tab -> *Start test*): a time-boxed receive-only survey with
per-neighbour SNR / RSSI / **estimated path loss**, every packet heard, and noise-floor stats.
Optionally takes the repeater out of the mesh (`set repeat off`) while it runs. It **always
returns**: when the timer ends (`REP_TEST_MINUTES`, capped by `REP_TEST_MAX_MINUTES`), on Stop,
if the serial link drops, on bridge shutdown, and after a bridge crash (a git-ignored marker file
is acted on at the next connect; restore retries until `get repeat` confirms). `set repeat` is
stored in the repeater's flash, so if the *Pi itself* loses power mid-test, send `set repeat on`.
Discovery: the test sends `discover.neighbors` at start (and via *Discover now*) when the firmware has it; otherwise it is **passive** (a neighbour
shows up when its zero-hop advert is heard) - use a Companion-USB node for active discovery.
Results are also pushed to the **Dashboard** (RSSI / SNR / path loss on each node card; unknown neighbours are
listed as `repeater <id>`), the **Map** (lines from `GATEWAY_LAT/LON` to each located neighbour, coloured
by SNR and labelled with path loss; shown for an hour) and MQTT (`rssi`, `snr`, `pathloss` sensors).
Path loss = assumed neighbour TX power (`ASSUMED_NEIGHBOUR_TX_DBM`) - measured RSSI, so it is
an estimate. Discovery, traceroute and remote
polling need a Companion-USB node (`SERIAL_MODE = "companion"`, the default).

### Public repeaters and simulated links (map)

The bridge loads the community MeshCore node list (`PUBLIC_NODES_URL`, cached in a git-ignored JSON,
refreshed daily; or point `PUBLIC_NODES_FILE` at your own JSON) and uses it to
- place **our repeater** at its known position (`LOCAL_NAME`, or the name the repeater reports via
  `get name`; `GATEWAY_LAT/LON` win if set),
- place **heard neighbours** at their published positions (and name them), and
- show the nearest **public repeaters** (`PUBLIC_RADIUS_KM`, `PUBLIC_MAX_NEARBY`) on the Map tab.

Each gets a simulated free-space link from us: `FSPL = 20log10(d km) + 20log10(f MHz) + 32.44`,
path loss = FSPL - antenna gains, predicted RSSI = `SIM_TX_DBM` (20) - path loss. Heard neighbours are
solid lines (also showing measured path loss and the excess over free space), public ones dashed.
Our own antenna can be directional (e.g. a Moxon: peak gain, boresight azimuth, front-to-back ratio). Enter it on the
**Test tab -> Antenna** card (saved in the git-ignored `meshcore_antenna.json`, no restart); `LOCAL_ANTENNA` in `config.py`
is only an optional default that the web UI overrides field by field: the gain used for each link is the pattern's value towards that repeater's bearing, and the lobe is drawn on
the map. Remote antenna gain: a gain field in the directory if present, a figure like `6dBi` in the node name, else your
`ANTENNA_GAIN_DBI` overrides, else 0 dBi (assumed). Free space is a best case - real paths only add
loss. `/api/public/status` shows what the directory returned (count, last error, a sample record).

The Antenna card also has a **Common 868 MHz antennas** dropdown (whips, collinears, Moxon, 2-9 element Yagis, panel,
sector, corner reflector). Picking one fills in typical gain / front-to-back / beamwidth and draws an amber dashed
**ghost** of its azimuth pattern on the Map tab, aimed at the Aim field (edit any field and the ghost follows). Nothing
is applied until **Save antenna**. Pattern size is scaled by gain from a **0 dBi reference circle**: tick *show* to draw it on the map and edit its radius (metres, default 400; kept in the browser) - everything updates live. Reach = radius x 10^(dBi/20), so +6 dBi doubles it and a 12 dBi antenna reaches 4x as far. The saved antenna's beam uses the same scale. It is an illustrative scale, not a prediction. The antenna is always assumed vertically polarised, so the pattern shown is the azimuth (H-plane) one. The figures are typical, not measured - edit them to match your antenna. A blank
beamwidth keeps the Moxon-style wide lobe derived from front-to-back.

### Persistent neighbour table (Neighbours tab)

Replaces the old "Repeater neighbour table" card (the `/api/neighbors/remote` route is still there, only the UI went).
The table lists repeaters within `NEIGHBOUR_RADIUS_KM` of yours and is saved to `meshcore_neighbours.json`, so it survives a reboot.

- **Filled from the public database**: by the *Update from public database* button (Neighbours tab and under the Dashboard node map), and automatically every `NEIGHBOUR_SYNC_HOURS` (24) by a background task. A manual update re-downloads the list at most once a minute.
- **Muted** rows are known from the public list but never heard. A row turns **accent green** when its announcement is heard (the repeater's own `neighbors` list, or a flooded advert in Companion mode) or it answers a discover command. It goes muted again `NEIGHBOUR_ACTIVE_HOURS` (24) after it was last heard.
- **Heard** shows the age of the data (time since the node was last heard, not since the page refreshed) and how it was heard.
- **Range and bearing** (from your repeater) are recalculated on every table update, and if your position changes.
- Needs your position: `GATEWAY_LAT`/`GATEWAY_LON`, or `LOCAL_NAME` set to the repeater's name in the public list.

### Sky-Watcher AZ-GTI mount (antenna rotator)

Test tab -> **Sky-Watcher mount**. Choose **Sky-Watcher** as the action in *Antenna steering* and every Steer button (including the repeater popup on the map) turns the antenna.

1. In the SynScan app, set the mount to join your home WiFi, and give it a fixed IP / DHCP reservation (its own hotspot is 192.168.4.1).
2. Enter the IP (UDP port 11880) and **Save**. The card shows whether the mount is reachable.
3. Point the antenna at a bearing you know (the Moxon: about 110), enter it as *Initial pointing bearing* and press **Set current pointing**. Do this after every power-up: the mount forgets its position when switched off.
4. Press **+10 / -10**. If the antenna turns the wrong way, tick **Reverse direction**.
5. Travel limit (default +/-180 from the initial bearing) keeps the coax from winding round the mast: a target is always reached the way that stays inside the limit, and a move outside it is refused. **Stop** halts the mount.

**Restarts and power cycles:** the last position read from the mount is saved (`meshcore_mount.json`), and the modelled aim (`meshcore_antenna.json`) is kept equal to the real bearing, also by a 30 s background task, so a bridge restart loses nothing. If the *mount* is switched off and on, its count resets: the card then says so and refuses moves until you press **Restore last pointing** (antenna not moved while it was off) or **Set current pointing**. While the mount turns, the blue beam and the antenna-type ghost on the Map follow its real bearing.

Only the azimuth axis is used; keep the altitude axis level. The bridge reads the real position back, so the card shows where the antenna actually points. Protocol: Sky-Watcher motor-controller commands over UDP (`bridge/skywatcher.py`). It has been tested against an emulator only, so try small nudges first; if it overshoots, raise *Brake distance*.

### MQTT monitor page

The **MQTT** tab shows the raw MQTT messages that matter to the bridge, live, using the bridge's own broker connection (so the bridge's own publishes show too: packets, telemetry, per-node values, the antenna-steering command, anything sent to `<MQTT_TOPIC>/send`).

- **Subscribe / Unsubscribe** start and stop the watching. The default is *not subscribed*; the filter and state are remembered across restarts (`meshcore_mqtt_monitor.json`) and restored when the broker reconnects.
- The filter box offers suggestions (everything, text, positions, per-node values, inject topic, and your steering topic). MQTT wildcards (`+`, `#`) work, but only topics under `MQTT_TOPIC` (or the saved steering topic) are allowed - it is not a general broker sniffer.
- Newest messages first, JSON pretty-printed, payloads cut at 1000 characters, the last 300 kept (`MQTT_MONITOR_MAX`). *Clear* empties the list; *find* filters it.

### Antenna steering (director)

Click a repeater on the map: its popup shows the **boresight bearing from our repeater** (the azimuth that points the
antenna straight at it, degrees true), how far off the current boresight that is, and the gain now vs. if aimed. A
**Steer** button in the popup (and *Steer to the Aim field now* on the Test tab) runs the action chosen in the
**Antenna steering (director)** card:

- **Model only** - just updates the aim the bridge models (the map beam, path-loss maths and planner). No hardware.
- **MQTT** - publishes the azimuth to an antenna director through the bridge's own MQTT connection. Topic (default
  `<MQTT_TOPIC>/antenna/azimuth/set`), payload template, QoS (0, or 1 to wait for the broker's ack) and retain are set on the
  card. Payload placeholders: `{azimuth}` (123.4), `{azimuth_int}` (123) and `{target}` (repeater name, JSON-escaped), so
  plain `{azimuth}` and JSON such as `{"azimuth": {azimuth}}` both work. Wildcard and `$` topics are refused, and an MQTT
  steer asks for confirmation first. *Also update modelled aim* moves the map beam only after the command has been sent
  successfully.

Settings are saved in the git-ignored `meshcore_steer.json` (`STEER_FILE`); `STEER_DEFAULTS` in `config.py` is an optional
default. New drivers (HTTP rotator, serial, ...) are one function plus one entry in `bridge/steer.py`'s `ACTIONS`.
This only *commands* a director - nothing reads the real antenna position back, and the web UI has no login, so keep
it on the LAN.

### Line of sight and antenna orientation planner

Test tab -> *Analyse* (or `POST /api/plan/run`, then `GET /api/plan`). For the nearest public repeaters
(`PLAN_RADIUS_KM`, `PLAN_MAX_REPEATERS`) it pulls a ground-elevation profile (Open-Meteo, 90 m DEM, cached),
checks line of sight with 4/3-earth curvature and the first Fresnel zone (clear >= 0.6 F1, marginal, blocked),
adds knife-edge diffraction loss for obstructions, then sweeps the antenna azimuth 0-355 deg and counts how
many repeaters stay above receiver sensitivity (from the repeater's SF/BW) plus `PLAN_FADE_MARGIN_DB`.
It reports the line-of-sight list, how many are reachable at the current azimuth vs the best one, and the best
arc; the suggested boresight is drawn as a green dotted line on the map. Mast heights are assumptions
(`LOCAL_MAST_M`, `REMOTE_MAST_M`); buildings and trees are not modelled, so treat it as a planning aid.

### Duty cycle

Duty cycle is a per-transmitter limit, so every request is budgeted against **both** our
node (the request) and the **target** (its reply) over a rolling hour, and the tests may
only use `TEST_DUTY_SHARE` (default 25 %) of the legal limit, leaving the rest for normal
mesh traffic. The limit is auto-detected from the node's frequency (869.400-869.650 MHz
-> 10 %, everything else -> 1 %) - set `DUTY_CYCLE_LIMIT` in `config.py` to override.
Airtime uses the Semtech time-on-air formula with the node's real SF/BW/CR and
conservative packet sizes. Typical pacing on UK narrow (SF8/62.5 kHz) is one over-the-air
poll every ~22 s in a 10 % band and ~3.6 min in a 1 % band. If the budget is spent the
test pauses ("Paused for duty cycle") and resumes automatically; if one request can never
fit it stops and says why. Discovery and traceroute are additionally rate-limited
(`DISCOVER_MIN_INTERVAL`, `TRACE_MIN_INTERVAL`). Path discovery is a *flood*, so repeaters
along the way retransmit it - their own (small) share is not tracked.

### Notes / limits

- Repeaters only answer status and neighbour requests **after a login**. Enter the
  guest/admin password in the UI (never stored, cleared after use) or set
  `REPEATER_PASSWORD` in the git-ignored `config.py`.
- Plain clients do not answer status requests, so the live link test is limited to
  repeaters, room servers and sensors. Traceroute works for any contact; for a client it
  traces to the last repeater and back.
- "Last RSSI/SNR" is the last packet each node received - normally our request / its
  reply, but other traffic can interleave.
- Written against `meshcore` 2.3.x and tested against a mock node
  (`pip install pytest && pytest tests/` - covers the duty-cycle maths, discovery, link
  test pacing/stop conditions, traceroute path building and the routes). **Not yet run on real hardware** - confirm the
  trace round-trip semantics and the discovery/status payloads on your node
  (`MESHCORE_DEBUG = True`).

## Privacy (public repo)

`.gitignore` excludes `config.py` (coords, broker, paths) and `*.json` (runtime
stores incl. location history). Commit only `config.example.py`. Verify with
`git status` before the first push.

## Known limitations / next steps

- Repeater/room-server remote CLI is not wired to the UI yet (login is used for the
  Test/Neighbours panels only) - the library's `send_cmd` is the hook to build on.
- The Messages UI reuses the existing channel chat tab (channel 0). Per-contact DM
  targeting in the UI is a small frontend addition once payloads are confirmed.
