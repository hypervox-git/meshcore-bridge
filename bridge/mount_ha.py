# Home Assistant integration for the antenna mount, over the bridge's MQTT connection (HA_DISCOVERY).
#
#   State   <MQTT_TOPIC>/mount/state    retained JSON {state, bearing, travel_left, moving, socket}
#   Control <MQTT_TOPIC>/mount/goto     payload: bearing 0-360        (HA number "Go to bearing")
#           <MQTT_TOPIC>/mount/nudge    payload: degrees, -90..90      (HA buttons)
#           <MQTT_TOPIC>/mount/stop     any payload                    (HA button "Stop")
#
# This gives Home Assistant native entities (device "Antenna rotator") that work from any dashboard, the iPhone
# app and automations - no iframe, so it also works when HA is served over HTTPS.
import json
import threading
import time

from config import MQTT_TOPIC, HA_DISCOVERY, HA_PREFIX

ROOT = f'{MQTT_TOPIC}/mount'
STATES = ['stopped', 'turning', 'off', 'unreachable', 'not calibrated', 'power-cycled']
MIN_GAP_S = 1.0               # at most one state message per second while the mount turns
REFRESH_S = 60                # unchanged state is still re-sent this often
NUDGES = (-5, -1, 1, 5)

_last = {'payload': None, 'ts': 0.0}


def view(st):
    """Reduce a skywatcher.status() dict to what Home Assistant needs; None while the mount is not configured."""
    if not st or st.get('message') == 'mount address not set':
        return None
    if st.get('power_cycled'):
        name = 'power-cycled'
    elif st.get('asleep'):
        name = 'off'
    elif not st.get('reachable'):
        name = 'unreachable'
    elif not st.get('calibrated'):
        name = 'not calibrated'
    elif st.get('moving'):
        name = 'turning'
    else:
        name = 'stopped'
    return {'state': name, 'bearing': st.get('bearing'), 'travel_left': st.get('travel_left'),
            'moving': bool(st.get('moving')), 'socket': st.get('power')}


def _client():
    from bridge import skywatcher
    return skywatcher._mqtt()


def _connected():
    from bridge import state
    return HA_DISCOVERY and state.mqtt_connected


def publish(st):
    """Send the mount state (retained) if it changed. Called every time the bridge reads the mount."""
    if not _connected():
        return False
    v = view(st)
    if v is None:
        return False
    payload = json.dumps(v, sort_keys=True)
    now = time.time()
    same = payload == _last['payload']
    if (same and now - _last['ts'] < REFRESH_S) or (not same and now - _last['ts'] < MIN_GAP_S):
        return False
    _last.update(payload=payload, ts=now)
    _client().publish(f'{ROOT}/state', payload, retain=True)
    return True


def _device():
    return {'identifiers': ['meshcore_antenna_rotator'], 'name': 'Antenna rotator',
            'model': 'Sky-Watcher mount', 'manufacturer': 'Sky-Watcher'}


def _cfg(kind, key, name, **kw):
    cfg = {'name': name, 'unique_id': f'meshcore_mount_{key}', 'device': _device(), **kw}
    _client().publish(f'{HA_PREFIX}/{kind}/meshcore_mount_{key}/config', json.dumps(cfg), retain=True)


def discovery():
    """Retained discovery configs: the entities appear in Home Assistant without any YAML."""
    if not _connected():
        return
    st = f'{ROOT}/state'
    _cfg('sensor', 'bearing', 'Bearing', state_topic=st, value_template='{{ value_json.bearing }}',
         unit_of_measurement='°', state_class='measurement', icon='mdi:compass')
    _cfg('sensor', 'travel_left', 'Travel left', state_topic=st, value_template='{{ value_json.travel_left }}',
         unit_of_measurement='°', state_class='measurement', icon='mdi:rotate-orbit')
    _cfg('sensor', 'state', 'State', state_topic=st, value_template='{{ value_json.state }}',
         device_class='enum', options=STATES, icon='mdi:antenna')
    _cfg('binary_sensor', 'moving', 'Turning', state_topic=st, device_class='moving',
         value_template="{{ 'ON' if value_json.moving else 'OFF' }}")
    _cfg('number', 'goto', 'Go to bearing', command_topic=f'{ROOT}/goto', state_topic=st,
         value_template='{{ value_json.bearing }}', min=0, max=360, step=1, mode='box',
         unit_of_measurement='°', icon='mdi:crosshairs-gps')
    _cfg('button', 'stop', 'Stop', command_topic=f'{ROOT}/stop', payload_press='STOP', icon='mdi:stop-circle')
    for n in NUDGES:
        _cfg('button', f'nudge_{"m" if n < 0 else "p"}{abs(n)}', f'Turn {n:+d}°', command_topic=f'{ROOT}/nudge',
             payload_press=str(n), icon='mdi:rotate-left' if n < 0 else 'mdi:rotate-right')


def _run(fn, what):
    """Commands arrive on the MQTT thread; the mount can take seconds to wake, so do the work on its own thread."""
    def go():
        from bridge import skywatcher as sw
        try:
            fn(sw)
        except Exception as e:
            sw._log(f'[mount] Home Assistant command {what} failed: {e}')
        try:
            sw.status()                       # republishes the new state
        except Exception:
            pass
    threading.Thread(target=go, daemon=True, name='mount-ha-cmd').start()


def _payload(msg):
    return msg.payload.decode(errors='replace').strip()


def on_goto(client, userdata, msg):
    try:
        az = float(_payload(msg))
    except ValueError:
        return
    if 0 <= az <= 360:
        _run(lambda sw: sw.goto(az), f'goto {az}')


def on_nudge(client, userdata, msg):
    try:
        d = float(_payload(msg))
    except ValueError:
        return
    if -90 <= d <= 90:
        _run(lambda sw: sw.nudge(d), f'nudge {d}')


def on_stop(client, userdata, msg):
    _run(lambda sw: sw.stop(), 'stop')


def on_mqtt_connect():
    """(Re)publish discovery and (re)subscribe to the command topics - subscriptions do not survive a reconnect."""
    if not _connected():
        return
    c = _client()
    for name, fn in (('goto', on_goto), ('nudge', on_nudge), ('stop', on_stop)):
        c.message_callback_add(f'{ROOT}/{name}', fn)
        c.subscribe(f'{ROOT}/{name}', qos=1)
    _last.update(payload=None, ts=0.0)
    discovery()
