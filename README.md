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
Our own antenna can be directional (`LOCAL_ANTENNA`, e.g. a Moxon: peak gain, boresight azimuth, front-to-back
ratio): the gain used for each link is the pattern's value towards that repeater's bearing, and the lobe is drawn on
the map. Remote antenna gain: a gain field in the directory if present, a figure like `6dBi` in the node name, else your
`ANTENNA_GAIN_DBI` overrides, else 0 dBi (assumed). Free space is a best case - real paths only add
loss. `/api/public/status` shows what the directory returned (count, last error, a sample record).

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
