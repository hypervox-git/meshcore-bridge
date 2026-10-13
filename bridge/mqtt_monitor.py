# MQTT monitor page: shows the raw MQTT messages that matter to the bridge, with Subscribe / Unsubscribe.
#
# It rides on the bridge's own broker connection (mqtt_bridge.mqttc), so no second client or credentials. The bridge's
# own publishes come back to it too (the broker echoes them to subscribers), so the page shows both directions:
# node/packet/telemetry topics, the antenna-director steering command, anything sent to <MQTT_TOPIC>/send.
# Only topics under MQTT_TOPIC (or the saved steering topic) can be watched - this page is not a general broker sniffer.
# The subscribed state and filter are remembered in MQTT_MONITOR_FILE (git-ignored) across restarts; the default is
# NOT subscribed until you press Subscribe.
import json
import re
import threading
import time
from collections import deque

from bridge import state
from bridge.extensions import socketio

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _file():
    return _c('MQTT_MONITOR_FILE', 'meshcore_mqtt_monitor.json')


def _base():
    return str(_c('MQTT_TOPIC', 'meshcore')).strip('/')


MAX_PAYLOAD = 1000                     # characters kept per message
EMIT_PER_SEC = 40                      # live pushes per second; the rest are still in the buffer

_lock = threading.RLock()
_buf = deque(maxlen=int(_c('MQTT_MONITOR_MAX', 300)))
_active = None                         # the filter currently subscribed on the broker (None = not subscribed)
_seq = 0
_total = 0
_emit_sec, _emit_n = 0, 0
_LEVEL = re.compile(r'^[A-Za-z0-9_.@ :=,-]+$')


def _mqttc():
    from bridge import mqtt_bridge
    return mqtt_bridge.mqttc


def _steer_topic():
    try:
        from bridge import steer
        return steer.settings().get('topic') or ''
    except Exception:
        return ''


def suggestions():
    """Filters worth watching: everything of the bridge, plus the steering topic if it lives elsewhere."""
    base = _base()
    out = [{'filter': base + '/#', 'label': 'everything the bridge publishes'},
           {'filter': base + '/text', 'label': 'text messages'},
           {'filter': base + '/position', 'label': 'positions'},
           {'filter': base + '/nodes/#', 'label': 'per-node values'},
           {'filter': base + '/send', 'label': 'messages injected into the mesh'}]
    st = _steer_topic()
    if st:
        out.append({'filter': st, 'label': 'antenna steering command'})
    return out


def check_filter(f):
    """A filter we are willing to subscribe to: valid MQTT syntax and inside our own topic tree (or the steering topic)."""
    f = str(f or '').strip()
    if not f or len(f) > 200:
        raise ValueError('topic filter is empty or too long')
    levels = f.split('/')
    for i, lv in enumerate(levels):
        if lv == '#':
            if i != len(levels) - 1:
                raise ValueError('# must be the last level')
        elif lv != '+' and not _LEVEL.match(lv):
            raise ValueError('topic filter contains characters that are not allowed')
    base = _base()
    if f == _steer_topic() or f == base or f.startswith(base + '/'):
        return f
    raise ValueError(f'only topics under {base}/ (or the steering topic) can be watched')


# ---- persistence ------------------------------------------------------------------------

def _saved():
    try:
        with open(_file()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    import os
    tmp = _file() + '.tmp'
    try:
        with open(tmp, 'w') as fh:
            json.dump(d, fh)
        os.replace(tmp, _file())
    except OSError as e:
        print(f'[mqtt-monitor] save error: {e}')


def wanted():
    """(filter, subscribed) as saved - what the page shows and what a reconnect restores."""
    d = _saved()
    f = d.get('filter') or _base() + '/#'
    return f, bool(d.get('subscribed'))


# ---- messages ---------------------------------------------------------------------------

def _on_message(client, userdata, msg):
    global _seq, _total, _emit_sec, _emit_n
    # paho calls ONLY the matching filtered callbacks and skips the client's normal on_message, so hand the message on
    # to the bridge's own handler (it acts on <MQTT_TOPIC>/send, the inject-into-the-mesh topic).
    try:
        from bridge import mqtt_bridge
        mqtt_bridge.on_mqtt_message(client, userdata, msg)
    except Exception as e:
        print(f'[mqtt-monitor] forward: {e}')
    try:
        raw = bytes(msg.payload)
        text = raw.decode('utf-8', 'replace')
        rec = {'ts': time.time(), 'topic': msg.topic, 'payload': text[:MAX_PAYLOAD], 'size': len(raw),
               'truncated': len(text) > MAX_PAYLOAD, 'qos': msg.qos, 'retain': bool(msg.retain)}
        with _lock:
            _seq += 1
            _total += 1
            rec['n'] = _seq
            _buf.append(rec)
            now = int(rec['ts'])
            if now != _emit_sec:
                _emit_sec, _emit_n = now, 0
            _emit_n += 1
            push = _emit_n <= EMIT_PER_SEC
        if push:
            socketio.emit('mqtt_msg', rec, namespace='/')
    except Exception as e:                                           # never break the MQTT thread
        print(f'[mqtt-monitor] {e}')


def _do_subscribe(f):
    global _active
    c = _mqttc()
    with _lock:
        if _active and _active != f:
            c.message_callback_remove(_active)
            c.unsubscribe(_active)
        c.message_callback_add(f, _on_message)
        rc, _mid = c.subscribe(f)
        if rc != 0:
            c.message_callback_remove(f)
            _active = None
            raise RuntimeError(f'MQTT subscribe failed (rc={rc})')
        _active = f


def subscribe(f=None):
    f = check_filter(f or wanted()[0])
    if not state.mqtt_connected:
        raise RuntimeError('MQTT broker not connected')
    _do_subscribe(f)
    _write({'filter': f, 'subscribed': True})
    _emit_state()
    return status()


def unsubscribe():
    global _active
    f = wanted()[0]
    with _lock:
        act = _active
        _active = None
    if act:
        c = _mqttc()
        c.message_callback_remove(act)
        if state.mqtt_connected:
            c.unsubscribe(act)
    _write({'filter': f, 'subscribed': False})
    _emit_state()
    return status()


def clear():
    with _lock:
        _buf.clear()
    return status()


def on_connect():
    """Called by mqtt_bridge after every (re)connect: restore the subscription the page last asked for."""
    global _active
    _active = None                                                   # the broker forgot our subscriptions
    f, sub = wanted()
    if sub:
        try:
            _do_subscribe(check_filter(f))
        except Exception as e:
            print(f'[mqtt-monitor] resubscribe: {e}')
    _emit_state()


def _emit_state():
    try:
        socketio.emit('mqtt_monitor', {k: v for k, v in status(0).items() if k != 'messages'}, namespace='/')
    except Exception:
        pass


def status(limit=200):
    f, sub = wanted()
    with _lock:
        msgs = list(_buf)[-limit:] if limit else []
        active = _active
        total = _total
    return {'ok': True, 'subscribed': bool(sub and active), 'filter': f, 'active_filter': active,
            'broker_connected': bool(state.mqtt_connected), 'base': _base(), 'total': total, 'buffered': len(_buf),
            'messages': msgs, 'suggestions': suggestions()}
