# MQTT client, per-device publishers, and Home Assistant discovery.
import json
import time
import paho.mqtt.client as mqtt

from config import (MQTT_BROKER, MQTT_PORT, MQTT_TOPIC, MQTT_USER, MQTT_PASS,
                    HA_DISCOVERY, HA_PREFIX)
from bridge import state
from bridge.state import node_db, mqtt_state
from bridge.extensions import socketio

send_hook = None  # set by meshcore_io.start() -> send_channel(chan, text)

mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)


def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    state.mqtt_connected = (reason_code == 0)
    socketio.emit('status', {'serial': state.serial_connected, 'mqtt': state.mqtt_connected}, namespace='/')
    if state.mqtt_connected:
        mqttc.subscribe(f"{MQTT_TOPIC}/send")
        for nid, ndata in node_db.items():
            publish_ha_discovery(nid, ndata.get('name'))


def on_mqtt_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    state.mqtt_connected = False
    socketio.emit('status', {'serial': state.serial_connected, 'mqtt': state.mqtt_connected}, namespace='/')


def on_mqtt_message(client, userdata, msg):
    if msg.topic == f"{MQTT_TOPIC}/send":
        try:
            data = json.loads(msg.payload.decode())
            text    = data.get('text', '').strip()
            channel = int(data.get('channel', 0))
            if text and send_hook:
                send_hook(channel, text)
                print(f"MQTT inject: ch{channel} '{text}'")
        except Exception as e:
            print(f"MQTT inject error: {e}")


def publish_ha_discovery(node_id, name):
    if not HA_DISCOVERY or not state.mqtt_connected:
        return
    safe = node_id.replace('!', '').replace(':', '_')
    display = name or node_id
    device = {"identifiers": [f"meshtastic_{safe}"], "name": display,
               "model": "Meshtastic Node", "manufacturer": "Meshtastic"}
    for key, fname, unit, dev_class, state_class in [
        ("battery", "Battery", "%",   "battery",         "measurement"),
        ("rssi",    "RSSI",    "dBm", "signal_strength",  "measurement"),
        ("snr",     "SNR",     "dB",  None,               "measurement"),
        ("pathloss","Path loss","dB", None,               "measurement"),
        ("health",  "Health",  "%",   None,               "measurement"),
        ("latitude", "Latitude",  "\u00b0", None, None),
        ("longitude","Longitude", "\u00b0", None, None),
    ]:
        cfg = {
            "name": f"{display} {fname}", "unique_id": f"meshtastic_{safe}_{key}",
            "state_topic": f"{MQTT_TOPIC}/nodes/{safe}/{key}",
            "unit_of_measurement": unit, "state_class": state_class, "device": device
        }
        if dev_class:
            cfg["device_class"] = dev_class
        mqttc.publish(f"{HA_PREFIX}/sensor/meshtastic_{safe}_{key}/config", json.dumps(cfg), retain=True)
    # device_tracker so the node appears on the Home Assistant map (GPS source)
    tracker = {
        "name": f"{display} Location", "unique_id": f"meshtastic_{safe}_tracker",
        "state_topic": f"{MQTT_TOPIC}/nodes/{safe}/tracker_state",
        "json_attributes_topic": f"{MQTT_TOPIC}/nodes/{safe}/attributes",
        "source_type": "gps", "device": device
    }
    mqttc.publish(f"{HA_PREFIX}/device_tracker/meshtastic_{safe}/config", json.dumps(tracker), retain=True)


def publish_ha_state(node_id, key, value):
    if not HA_DISCOVERY or not state.mqtt_connected:
        return
    safe = node_id.replace('!', '').replace(':', '_')
    mqttc.publish(f"{MQTT_TOPIC}/nodes/{safe}/{key}", str(value))
    st = mqtt_state.setdefault(node_id, {'topic': f"{MQTT_TOPIC}/nodes/{safe}", 'values': {}, 'count': 0, 'ts': 0, 'last_port': ''})
    st['values'][key] = value
    st['ts'] = int(time.time())
    st['count'] += 1


def publish_position(node_id, lat, lon):
    # Per-device location -> MQTT states (shows in the per-device view) + HA tracker attributes.
    if not state.mqtt_connected:
        return
    publish_ha_state(node_id, 'latitude', lat)
    publish_ha_state(node_id, 'longitude', lon)
    if HA_DISCOVERY:
        safe = node_id.replace('!', '').replace(':', '_')
        attrs = json.dumps({"latitude": lat, "longitude": lon, "gps_accuracy": 0})
        mqttc.publish(f"{MQTT_TOPIC}/nodes/{safe}/attributes", attrs, retain=True)
        mqttc.publish(f"{MQTT_TOPIC}/nodes/{safe}/tracker_state", "home", retain=True)


def publish_packet(clean):
    payload = json.dumps(clean)
    mqttc.publish(f"{MQTT_TOPIC}/nodes", payload)
    portnum = clean.get('decoded', {}).get('portnum', '')
    if portnum == 'TEXT_MESSAGE_APP':
        mqttc.publish(f"{MQTT_TOPIC}/text", payload)
    elif portnum == 'TELEMETRY_APP':
        mqttc.publish(f"{MQTT_TOPIC}/telemetry", payload)
    elif portnum == 'POSITION_APP':
        mqttc.publish(f"{MQTT_TOPIC}/position", payload)
    ch = clean.get('channel')
    if ch is not None:
        mqttc.publish(f"{MQTT_TOPIC}/ch{ch}", payload)
    _nid = clean.get('fromId') or clean.get('from')
    node_id = str(_nid) if _nid not in (None, '', 'None') else ''
    if node_id and state.mqtt_connected:
        safe = node_id.replace('!', '').replace(':', '_')
        st = mqtt_state.setdefault(node_id, {'topic': f"{MQTT_TOPIC}/nodes/{safe}", 'values': {}, 'count': 0, 'ts': 0, 'last_port': ''})
        st['count'] += 1
        st['ts'] = int(time.time())
        if portnum:
            st['last_port'] = portnum


def start():
    mqttc.on_connect = on_mqtt_connect
    mqttc.on_disconnect = on_mqtt_disconnect
    mqttc.on_message = on_mqtt_message
    mqttc.connect(MQTT_BROKER, MQTT_PORT)
    mqttc.loop_start()
