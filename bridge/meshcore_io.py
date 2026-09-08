# MeshCore radio adapter.
#
# MeshCore's Python library is asyncio-based, so we run its event loop in a
# dedicated thread and marshal outbound sends onto that loop. Inbound events are
# translated into the SAME data shapes the (Meshtastic-derived) dashboard already
# understands — a 'packet' with decoded.text for messages, decoded.position for
# adverts, per-node telemetry — so the UI, map, trails and MQTT view are reused.
#
# NOTE: exact payload dict keys vary by firmware / library version. Every field
# access below is defensive (_g helper). Set MESHCORE_DEBUG=True in config to log
# raw event payloads and confirm the mapping against your node, then tighten.
import asyncio
import threading
import time

from meshcore import MeshCore, EventType

from config import SERIAL_PORT, BAUD_RATE
try:
    from config import MESHCORE_DEBUG
except Exception:
    MESHCORE_DEBUG = False

from bridge import state
from bridge.state import node_db, update_node_db, append_message, iface_ref
from bridge.extensions import socketio
from bridge import mqtt_bridge
from bridge.util import health_score
from bridge.history import record_position_history

_loop = None          # the asyncio loop (own thread)
mc = None             # MeshCore instance


# ---- helpers -----------------------------------------------------------------

def _g(payload, *keys, default=None):
    """First present key from a dict-ish payload (defensive against key drift)."""
    if isinstance(payload, dict):
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
    return default


def _node_id(payload):
    key = _g(payload, 'pubkey_prefix', 'public_key', 'from', 'origin', 'key', default='')
    if key:
        return '!' + str(key)[:8]
    name = _g(payload, 'adv_name', 'name', 'from_name', default=None)
    return name or '?'


def _emit_status():
    socketio.emit('status', {'serial': state.serial_connected,
                             'mqtt': state.mqtt_connected}, namespace='/')


def _publish_node(nid, name=None, snr=None, battery=None, lat=None, lon=None, hw=None):
    entry = {}
    if name is not None:
        entry['name'] = name
    if snr is not None:
        entry['snr'] = snr
    if battery is not None:
        entry['battery'] = battery
    if lat is not None and lon is not None:
        entry['lat'] = lat
        entry['lon'] = lon
    if hw is not None:
        entry['hw'] = hw
    if entry:
        update_node_db(nid, entry)
    score = health_score(node_db.get(nid, {}))
    mqtt_bridge.publish_ha_state(nid, 'health', score)
    if snr is not None:
        mqtt_bridge.publish_ha_state(nid, 'snr', snr)
    if battery is not None:
        mqtt_bridge.publish_ha_state(nid, 'battery', battery)


def _emit_packet(clean):
    """Feed the existing frontend (updateNodes/appendLog) + MQTT."""
    mqtt_bridge.publish_packet(clean)
    socketio.emit('packet', clean, namespace='/')


# ---- event handlers ----------------------------------------------------------

def _on_any(event):
    if MESHCORE_DEBUG:
        try:
            print(f"[meshcore] {event.type}: {event.payload}")
        except Exception:
            print(f"[meshcore] {event.type}")


def _on_dm(event):
    p = event.payload or {}
    nid = _node_id(p)
    text = _g(p, 'text', 'msg', 'message', default='')
    snr = _g(p, 'SNR', 'snr')
    ts = int(_g(p, 'sender_timestamp', 'timestamp', default=time.time()))
    if text:
        append_message({'ts': ts * 1000, 'from': nid, 'text': text, 'channel': 0, 'sent': False})
    _emit_packet({'from': nid, 'fromId': nid, 'channel': 0, 'rxSnr': snr,
                  'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'text': text}})
    _publish_node(nid, snr=snr)


def _on_chan(event):
    p = event.payload or {}
    ch = int(_g(p, 'channel', 'channel_idx', 'chan', default=0))
    nid = _node_id(p)
    text = _g(p, 'text', 'msg', 'message', default='')
    if text:
        append_message({'ts': int(time.time()) * 1000, 'from': nid, 'text': text,
                        'channel': ch, 'sent': False})
    _emit_packet({'from': nid, 'fromId': nid, 'channel': ch,
                  'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'text': text}})


def _on_advert(event):
    p = event.payload or {}
    nid = _node_id(p)
    name = _g(p, 'adv_name', 'name')
    lat = _g(p, 'adv_lat', 'lat', 'latitude')
    lon = _g(p, 'adv_lon', 'lon', 'longitude')
    snr = _g(p, 'SNR', 'snr')
    if lat and lon:
        lat = round(float(lat), 6)
        lon = round(float(lon), 6)
        record_position_history(nid, lat, lon)
        mqtt_bridge.publish_position(nid, lat, lon)
        _emit_packet({'from': nid, 'fromId': nid,
                      'decoded': {'portnum': 'POSITION_APP',
                                  'position': {'latitudeI': int(lat * 1e7),
                                               'longitudeI': int(lon * 1e7)}}})
    _publish_node(nid, name=name, snr=snr, lat=lat and round(float(lat), 6),
                  lon=lon and round(float(lon), 6))


def _on_telemetry(event):
    p = event.payload or {}
    nid = _node_id(p)
    battery = _g(p, 'battery', 'bat', 'battery_level', 'voltage')
    _publish_node(nid, battery=battery)
    _emit_packet({'from': nid, 'fromId': nid,
                  'decoded': {'portnum': 'TELEMETRY_APP',
                              'telemetry': {'deviceMetrics': {'batteryLevel': battery}}}})


def _on_battery(event):
    # battery of the local companion node
    p = event.payload or {}
    lvl = _g(p, 'level', 'battery', 'percent', default=p if isinstance(p, (int, float)) else None)
    if lvl is not None and mc is not None:
        nid = _node_id(getattr(mc, 'self_info', {}) or {}) or 'self'
        _publish_node(nid, battery=lvl)


def _on_contact(event):
    p = event.payload or {}
    nid = _node_id(p)
    _publish_node(nid, name=_g(p, 'adv_name', 'name'),
                  hw=_g(p, 'type', 'contact_type'))


def _on_disconnect(event):
    state.serial_connected = False
    _emit_status()


# ---- contact ingest ----------------------------------------------------------

def _ingest_contacts():
    try:
        contacts = getattr(mc, 'contacts', {}) or {}
        items = contacts.values() if isinstance(contacts, dict) else contacts
        for c in items:
            nid = _node_id(c)
            _publish_node(nid, name=_g(c, 'adv_name', 'name'),
                          hw=_g(c, 'type', 'contact_type'),
                          lat=_g(c, 'adv_lat', 'lat'), lon=_g(c, 'adv_lon', 'lon'))
    except Exception as e:
        print(f"[meshcore] contact ingest error: {e}")


# ---- async loop --------------------------------------------------------------

async def _connect_and_run():
    global mc
    print(f"[meshcore] connecting serial {SERIAL_PORT} ...")
    mc = await MeshCore.create_serial(SERIAL_PORT, BAUD_RATE, auto_reconnect=True)
    iface_ref[0] = mc
    state.serial_connected = True
    _emit_status()
    # a single 'channels' emit gives the UI a channel-0 chat tab for sending
    socketio.emit('channels', [{'index': 0, 'name': 'public', 'role': 'PRIMARY',
                               'psk_type': 'meshcore'}], namespace='/')

    if MESHCORE_DEBUG:
        mc.subscribe(None, _on_any)
    mc.subscribe(EventType.CONTACT_MSG_RECV, _on_dm)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, _on_chan)
    mc.subscribe(EventType.ADVERTISEMENT, _on_advert)
    mc.subscribe(EventType.NEW_CONTACT, _on_contact)
    mc.subscribe(EventType.TELEMETRY_RESPONSE, _on_telemetry)
    mc.subscribe(EventType.BATTERY, _on_battery)
    mc.subscribe(EventType.DISCONNECTED, _on_disconnect)

    try:
        await mc.ensure_contacts()
    except Exception as e:
        print(f"[meshcore] ensure_contacts: {e}")
    _ingest_contacts()
    try:
        await mc.start_auto_message_fetching()
    except Exception as e:
        print(f"[meshcore] auto message fetch: {e}")
    print("[meshcore] connected.")


def _run_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_connect_and_run())
    except Exception as e:
        state.serial_connected = False
        _emit_status()
        print(f"[meshcore] connect failed: {e}")
    _loop.run_forever()


# ---- outbound (called from Flask threads) ------------------------------------

def _run(coro, timeout=12):
    if _loop is None:
        raise RuntimeError("MeshCore loop not started")
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)


def send_channel(chan, text):
    return _run(mc.commands.send_chan_msg(int(chan), text))


def send_dm(dst, text):
    contact = mc.get_contact_by_name(dst) or dst
    return _run(mc.commands.send_msg(contact, text))


def request_telemetry(dst):
    contact = mc.get_contact_by_name(dst) or dst
    return _run(mc.commands.send_telemetry_req(contact))


# ---- lifecycle ---------------------------------------------------------------

def start():
    mqtt_bridge.send_hook = send_channel   # MQTT inject -> channel-0 send
    threading.Thread(target=_run_loop, daemon=True, name="meshcore-loop").start()
