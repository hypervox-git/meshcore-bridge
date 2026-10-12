# Runtime antenna aim for the directional-antenna model. The aim lives in a small git-ignored
# JSON next to config.py (meshcore_antenna.json) so the web UI can update it without editing
# config.py or restarting; it overrides LOCAL_ANTENNA["azimuth_deg"] until reset.
# NOTE: this changes the MODEL's boresight - the physical antenna still has to be rotated.
import json
import os
import time

from bridge import linkmodel

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _file():
    return _c('ANTENNA_FILE', 'meshcore_antenna.json')


FIELDS = {'type': str, 'gain_dbi': float, 'azimuth_deg': float, 'front_back_db': float, 'directional': bool}


def override():
    """Settings saved from the web UI (partial dict) or None."""
    try:
        with open(_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) and any(k in d for k in FIELDS) else None
    except (OSError, ValueError):
        return None


def effective(cfg_ant=None):
    """config.py LOCAL_ANTENNA as defaults, overridden field-by-field by the web UI. None = no antenna set."""
    base = dict(cfg_ant) if isinstance(cfg_ant, dict) else {}
    src = 'config.py' if base else None
    ov = override()
    if ov:
        for k in FIELDS:
            if k in ov:
                base[k] = ov[k]
        src = 'web UI' if not cfg_ant else 'config.py + web UI'
    if base.get('gain_dbi') is None:
        return None, src
    base.setdefault('type', 'directional')
    base.setdefault('front_back_db', 25.0)
    base.setdefault('azimuth_deg', 0.0)
    base.setdefault('directional', True)
    return base, src


def validate(d):
    """Clean + range-check a settings dict from the UI. Raises ValueError with a readable message."""
    out = {}
    if 'type' in d:
        t = str(d['type']).strip()[:24]
        out['type'] = t or 'antenna'
    if 'directional' in d:
        out['directional'] = bool(d['directional'])
    for k, lo, hi, label in (('gain_dbi', -10, 30, 'gain'), ('azimuth_deg', None, None, 'azimuth'),
                             ('front_back_db', 0, 60, 'front-to-back')):
        if k in d and d[k] not in (None, ''):
            try:
                v = float(d[k])
            except (TypeError, ValueError):
                raise ValueError(f'{label} must be a number')
            if k == 'azimuth_deg':
                v = v % 360.0
            elif not (lo <= v <= hi):
                raise ValueError(f'{label} must be between {lo} and {hi}')
            out[k] = round(v, 1)
    return out


def save(update, reason=''):
    """Merge `update` into the saved settings (atomic write)."""
    if not isinstance(update, dict):
        update = {'azimuth_deg': update}
    cur = override() or {}
    cur.update(validate(update))
    cur['reason'], cur['set_at'] = reason, time.time()
    tmp = _file() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cur, f)
    os.replace(tmp, _file())
    return cur


def reset():
    try:
        os.remove(_file())
    except OSError:
        pass


def aim_at(points, gain_dbi, front_back_db):
    """Boresight azimuth that points the pattern 'directly at' a set of bearings: maximise the
    WORST gain among them (fairness: centre of the smallest enclosing arc), then the total."""
    best = None
    for tenth in range(0, 3600, 2):
        az = tenth / 10.0
        gains = [linkmodel.antenna_gain_dbi(linkmodel.off_boresight_deg(b, az), gain_dbi, front_back_db) for b in points]
        key = (min(gains), sum(gains))
        if best is None or key > best[0]:
            best = (key, az, gains)
    return best[1], best[2]


def aim_heard():
    """Suggest (do not apply) an azimuth aimed at the neighbours the repeater actually hears."""
    from bridge import publiclinks
    data = publiclinks.links()
    loc = data['local']
    ant = loc.get('antenna') or {}
    if not ant.get('directional'):
        raise RuntimeError('no directional antenna defined - enter it in the Antenna card on the Test tab')
    heard = [i for i in data.get('items', []) if i['source'] == 'discovered']
    if not heard:
        raise RuntimeError('no heard neighbours with a known position yet (see the Neighbours tab)')
    az, gains = aim_at([i['bearing_deg'] for i in heard], ant['gain_dbi'], ant['front_back_db'])
    return {'azimuth_deg': az, 'current_deg': ant['azimuth_deg'],
            'neighbours': [{'name': i['name'], 'bearing_deg': i['bearing_deg'], 'distance_km': i['distance_km'],
                            'gain_now_dbi': i['gain_local_dbi'], 'gain_new_dbi': round(g, 1)}
                           for i, g in zip(heard, gains)]}
