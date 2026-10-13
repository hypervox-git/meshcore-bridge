# Antenna steering: turn "point the antenna at N degrees" into an ACTION chosen on the web UI.
#
# Actions are registered in ACTIONS (id -> label + function), so another driver (HTTP rotator,
# serial, ...) is one function plus one entry. Two ship today:
#   model - only update the aim the bridge models (no hardware involved)
#   mqtt  - publish the azimuth to an antenna director over the bridge's own MQTT broker connection
# Settings live in a small git-ignored JSON (STEER_FILE) so the UI can change them without a restart.
# NOTE: this only COMMANDS a director. Nothing here reads the real antenna position back.
import json
import math
import os
import time

from bridge import antenna, state

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _file():
    return _c('STEER_FILE', 'meshcore_steer.json')


_last = None                           # most recent command (in memory only)


# ---- settings ---------------------------------------------------------------------------

def _defaults():
    d = {'action': 'model', 'topic': f"{_c('MQTT_TOPIC', 'meshcore')}/antenna/azimuth/set",
         'payload': '{azimuth}', 'qos': 0, 'retain': False, 'also_model': True}
    cfg = _c('STEER_DEFAULTS', None)
    if isinstance(cfg, dict):
        d.update({k: v for k, v in cfg.items() if k in d})
    return d


def _saved():
    try:
        with open(_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def settings():
    d = _defaults()
    d.update({k: v for k, v in _saved().items() if k in d})
    if d['action'] not in ACTIONS:
        d['action'] = 'model'
    return d


def check_topic(topic):
    """A topic we are willing to PUBLISH to: no wildcards, no broker-internal ($) topics."""
    t = str(topic or '').strip()
    if not t:
        raise ValueError('MQTT topic is empty')
    if len(t) > 200 or '\0' in t:
        raise ValueError('MQTT topic is too long or contains a NUL')
    if '+' in t or '#' in t:
        raise ValueError('MQTT topic must not contain + or # wildcards')
    if t.startswith('$'):
        raise ValueError('MQTT topic must not start with $')
    return t


def validate(d):
    """Clean + check a settings dict from the UI (partial is fine). Raises ValueError with a readable message."""
    out = {}
    if 'action' in d:
        if d['action'] not in ACTIONS:
            raise ValueError('unknown steering action: ' + str(d['action'])[:30])
        out['action'] = d['action']
    if 'topic' in d:
        t = str(d['topic'] or '').strip()
        out['topic'] = check_topic(t) if t else ''
    if 'payload' in d:
        p = str(d['payload'] if d['payload'] is not None else '')
        if not p.strip() or len(p) > 300:
            raise ValueError('payload template must be 1-300 characters')
        out['payload'] = p
    if 'qos' in d:
        try:
            q = int(d['qos'])
        except (TypeError, ValueError):
            raise ValueError('QoS must be 0 or 1')
        if q not in (0, 1):
            raise ValueError('QoS must be 0 or 1')
        out['qos'] = q
    for k in ('retain', 'also_model'):
        if k in d:
            out[k] = bool(d[k])
    return out


def save_settings(update):
    cur = _saved()
    cur.update(validate(update if isinstance(update, dict) else {}))
    tmp = _file() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cur, f)
    os.replace(tmp, _file())
    return settings()


# ---- payload ----------------------------------------------------------------------------

def render_payload(template, azimuth, target=None):
    """Fill {azimuth} (123.4), {azimuth_int} (123) and {target} (JSON-escaped, so it is safe inside quotes).
    Plain token replacement - NOT str.format - so JSON templates like {"az": {azimuth}} just work."""
    tgt = ''.join(ch for ch in str(target or '') if ch.isprintable())[:32]
    tgt = json.dumps(tgt)[1:-1]
    return (template.replace('{azimuth_int}', str(int(round(azimuth)) % 360))
                    .replace('{azimuth}', f'{azimuth:.1f}')
                    .replace('{target}', tgt))


# ---- actions ----------------------------------------------------------------------------

def _do_model(az, target, s, reason):
    antenna.save({'azimuth_deg': az}, reason or 'set from the web UI')
    return {'detail': f'model aim set to {az:.1f} deg (no hardware commanded)'}


def _do_mqtt(az, target, s, reason):
    topic = check_topic(s['topic'])
    from bridge import mqtt_bridge
    if not state.mqtt_connected:
        raise RuntimeError('MQTT broker not connected - nothing was sent')
    payload = render_payload(s['payload'], az, target)
    qos, retain = int(s['qos']), bool(s['retain'])
    info = mqtt_bridge.mqttc.publish(topic, payload, qos=qos, retain=retain)
    if getattr(info, 'rc', 0) != 0:
        raise RuntimeError(f'MQTT publish failed (rc={info.rc})')
    if qos > 0:
        try:
            info.wait_for_publish(timeout=3)
        except (ValueError, RuntimeError):
            pass
        if not info.is_published():
            raise RuntimeError('the broker did not acknowledge the command within 3 s')
    if s.get('also_model'):
        antenna.save({'azimuth_deg': az}, reason or f'steered via MQTT {topic}')
    return {'detail': f'published {payload!r} to {topic} (qos {qos}{", retained" if retain else ""})',
            'topic': topic, 'payload': payload}


ACTIONS = {
    'model': ('Model only - update the aim shown in this UI (no hardware)', _do_model),
    'mqtt': ('MQTT - publish the azimuth to an antenna director', _do_mqtt),
}


def steer(azimuth, target=None, action=None, reason=''):
    """Point the antenna at `azimuth` (deg true) using the chosen (or saved) action."""
    global _last
    try:
        az = float(azimuth)
    except (TypeError, ValueError):
        raise ValueError('azimuth must be a number')
    if not math.isfinite(az):
        raise ValueError('azimuth must be a number')
    az = round(az % 360.0, 1)
    s = settings()
    act = action or s['action']
    if act not in ACTIONS:
        raise ValueError('unknown steering action: ' + str(act)[:30])
    rec = {'ts': time.time(), 'action': act, 'azimuth': az, 'target': str(target)[:32] if target else None}
    try:
        res = ACTIONS[act][1](az, target, s, reason)
    except Exception as e:
        _last = dict(rec, ok=False, error=str(e) or e.__class__.__name__)
        raise
    _last = dict(rec, ok=True, detail=res.get('detail'))
    return dict(res, azimuth=az, action=act)


def status():
    return {'ok': True, 'settings': settings(), 'actions': [{'id': k, 'label': v[0]} for k, v in ACTIONS.items()],
            'mqtt_connected': bool(state.mqtt_connected), 'last': _last,
            'placeholders': ['{azimuth}', '{azimuth_int}', '{target}']}
