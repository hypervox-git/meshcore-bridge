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


def override():
    try:
        with open(_file()) as f:
            d = json.load(f)
        return d if isinstance(d.get('azimuth_deg'), (int, float)) else None
    except (OSError, ValueError):
        return None


def save(azimuth, reason):
    d = {'azimuth_deg': round(float(azimuth) % 360, 1), 'reason': reason, 'set_at': time.time()}
    tmp = _file() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f)
    os.replace(tmp, _file())
    return d


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
        raise RuntimeError('no directional antenna configured - set LOCAL_ANTENNA in config.py first')
    heard = [i for i in data.get('items', []) if i['source'] == 'discovered']
    if not heard:
        raise RuntimeError('no heard neighbours with a known position yet (see the Neighbours tab)')
    az, gains = aim_at([i['bearing_deg'] for i in heard], ant['gain_dbi'], ant['front_back_db'])
    return {'azimuth_deg': az, 'current_deg': ant['azimuth_deg'],
            'neighbours': [{'name': i['name'], 'bearing_deg': i['bearing_deg'], 'distance_km': i['distance_km'],
                            'gain_now_dbi': i['gain_local_dbi'], 'gain_new_dbi': round(g, 1)}
                           for i, g in zip(heard, gains)]}
