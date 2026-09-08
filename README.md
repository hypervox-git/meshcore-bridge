# meshcore-bridge

A MeshCore serial-to-MQTT bridge and web dashboard — a fork of `meshtastic-bridge`
retargeted to the **MeshCore** platform via the official async `meshcore` Python
library. Talks to a dedicated MeshCore companion node over USB serial; forwards
DMs, channel messages, adverts (position), and telemetry to MQTT / Home Assistant,
and reuses the dashboard, live map, breadcrumb trails and per-device MQTT view.

**Status: import-tested foundation, NOT yet run against a MeshCore node.** See
"Bring-up" below — the event→UI field mapping must be confirmed on your device.

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

## Bring-up (do this first, on your node)

MeshCore event payload field names vary by firmware/library version. `config.py`
ships with `MESHCORE_DEBUG = True`, which logs every raw event and its payload:

```bash
.venv/bin/python app.py            # watch the console; send a test DM / channel msg
```

Confirm the real keys for message text, sender, and advert lat/lon against the
`_g(...)` fallbacks in `bridge/meshcore_io.py` (`_on_dm`, `_on_chan`, `_on_advert`,
`_on_telemetry`). Tighten those, then set `MESHCORE_DEBUG = False`.

## Privacy (public repo)

`.gitignore` excludes `config.py` (coords, broker, paths) and `*.json` (runtime
stores incl. location history). Commit only `config.example.py`. Verify with
`git status` before the first push.

## Known limitations / next steps

- Repeater/room-server admin (login, remote CLI) is not wired to the UI yet —
  `meshcore_io.request_telemetry()` and the library's `send_login`/`send_cmd` are
  the hooks to build on.
- The Messages UI reuses the existing channel chat tab (channel 0). Per-contact DM
  targeting in the UI is a small frontend addition once payloads are confirmed.
