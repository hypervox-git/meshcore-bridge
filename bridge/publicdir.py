# Public directory of MeshCore nodes (names, public keys, positions, type) so repeaters the
# bridge hears can be placed at their KNOWN locations. Source: the community MeshCore map
# (PUBLIC_NODES_URL, JSON) or a local JSON file (PUBLIC_NODES_FILE); cached on disk and
# refreshed in the background. Field names are read defensively - /api/public/status shows
# what was fetched so a mismatch is easy to spot.
#
# Antenna gain: there is no standard published field. We use, in order: a gain field in the
# directory record if one exists, a "5dBi"-style figure in the node's name/description, then
# your own ANTENNA_GAIN_DBI overrides (config.py). Otherwise 0 dBi is assumed.
import json
import os
import re
import threading
import time
import urllib.request

from bridge import linkmodel

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


URLS = [u for u in (_c('PUBLIC_NODES_URL', 'https://map.meshcore.io/api/v1/nodes'),
                    _c('PUBLIC_NODES_URL_FALLBACK', 'https://map.meshcore.dev/api/v1/nodes')) if u]
FILE = _c('PUBLIC_NODES_FILE', None)
CACHE = _c('PUBLIC_CACHE_FILE', 'meshcore_public_nodes.json')
REFRESH_S = float(_c('PUBLIC_REFRESH_HOURS', 24)) * 3600

_lock = threading.Lock()
_nodes = []                        # normalised records
_status = {'count': 0, 'source': None, 'fetched_at': None, 'error': None, 'sample': None}
_GAIN_RE = re.compile(r'(\d+(?:\.\d+)?)\s*d\s*b\s*i\b', re.I)
_TYPES = {1: 'client', 2: 'repeater', 3: 'room', 4: 'sensor',
          'client': 'client', 'chat': 'client', 'repeater': 'repeater', 'room': 'room',
          'room_server': 'room', 'sensor': 'sensor'}


def _first(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ''):
            return d[k]
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalise(raw):
    """One directory record -> {key,name,lat,lon,type,gain_dbi,gain_src,...} or None."""
    if not isinstance(raw, dict):
        return None
    key = str(_first(raw, 'public_key', 'publicKey', 'pubkey', 'pub_key', 'key', 'id') or '').lower()
    lat = _num(_first(raw, 'adv_lat', 'lat', 'latitude'))
    lon = _num(_first(raw, 'adv_lon', 'lon', 'lng', 'longitude'))
    if lat is not None and abs(lat) > 90 and abs(lat) < 9e8:       # microdegrees
        lat, lon = lat / 1e6, (lon / 1e6 if lon is not None else None)
    if not key or lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    name = str(_first(raw, 'adv_name', 'name', 'node_name', 'advName') or '')
    t = _first(raw, 'type', 'adv_type', 'node_type', 'role')
    t = _TYPES.get(t if not isinstance(t, str) else t.lower(), None)
    gain, src = _num(_first(raw, 'antenna_gain', 'antennaGain', 'ant_gain', 'gain_dbi', 'gain')), 'published'
    if gain is None:
        text = ' '.join(str(_first(raw, k) or '') for k in ('adv_name', 'name', 'description', 'desc', 'notes'))
        m = _GAIN_RE.search(text)
        gain, src = (float(m.group(1)), 'name') if m else (None, None)
    return {'key': key, 'name': name, 'lat': lat, 'lon': lon, 'type': t,
            'gain_dbi': gain, 'gain_src': src, 'tx_dbm': _num(_first(raw, 'tx_power', 'txPower', 'tx_dbm')),
            'last_advert': _first(raw, 'last_advert', 'lastAdvert', 'last_seen', 'updated_at')}


def _records(payload):
    if isinstance(payload, dict):
        for k in ('nodes', 'data', 'results', 'items'):
            if isinstance(payload.get(k), list):
                return payload[k]
        return list(payload.values()) if all(isinstance(v, dict) for v in payload.values()) else []
    return payload if isinstance(payload, list) else []


def load_payload(payload, source):
    recs = [r for r in (normalise(x) for x in _records(payload)) if r]
    with _lock:
        _nodes[:] = recs
        _status.update(count=len(recs), source=source, error=None, fetched_at=time.time(),
                       sample=recs[0] if recs else None)
    return len(recs)


def _fetch():
    errs = []
    for url in URLS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'meshcore-bridge', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=25) as r:
                payload = json.loads(r.read().decode('utf-8'))
            n = load_payload(payload, url)
            if n:
                with open(CACHE, 'w') as f:
                    json.dump({'fetched_at': time.time(), 'source': url, 'payload': payload}, f)
                return n
            errs.append(f'{url}: parsed 0 positioned nodes')
        except Exception as e:
            errs.append(f'{url}: {e}')
    with _lock:
        _status['error'] = '; '.join(errs)
    return 0


def refresh(force=False):
    """Load from file / cache / network. Returns node count."""
    if FILE:
        try:
            with open(FILE) as f:
                return load_payload(json.load(f), FILE)
        except Exception as e:
            with _lock:
                _status['error'] = f'{FILE}: {e}'
            return 0
    age = None
    try:
        with open(CACHE) as f:
            c = json.load(f)
        age = time.time() - c.get('fetched_at', 0)
        if not _nodes:
            load_payload(c.get('payload'), (c.get('source') or 'cache') + ' (cached)')
    except Exception:
        pass
    if force or age is None or age > REFRESH_S:
        return _fetch()
    return len(_nodes)


def start():
    def _loop():
        while True:
            try:
                refresh()
            except Exception as e:
                with _lock:
                    _status['error'] = str(e)
            time.sleep(3600)
    threading.Thread(target=_loop, daemon=True, name="public-dir").start()


def status():
    with _lock:
        return dict(_status)


def lookup(hex_id):
    """Directory record whose public key starts with (or is a prefix of) hex_id."""
    h = str(hex_id).lower().strip().lstrip('!')
    if len(h) < 4:
        return None
    with _lock:
        for r in _nodes:
            if r['key'].startswith(h) or h.startswith(r['key'][:len(h)]):
                return r
    return None


def find_by_name(name, node_type='repeater'):
    n = str(name or '').strip().lower()
    if not n:
        return None
    with _lock:
        exact = [r for r in _nodes if r['name'].strip().lower() == n]
    if not exact:                                  # lenient: 'Ironhorse' ~ 'Ironhorse-RPT'
        with _lock:
            exact = [r for r in _nodes if r['name'].strip().lower().startswith(n) or
                     (len(r['name']) > 3 and n.startswith(r['name'].strip().lower()))]
    pref = [r for r in exact if r['type'] == node_type] or exact
    return pref[0] if pref else None


def override_gain(rec):
    """Gain for a record: directory/name figure, else config override (by name or key prefix)."""
    ov = _c('ANTENNA_GAIN_DBI', {}) or {}
    for k, v in ov.items():
        kl = str(k).lower().lstrip('!')
        if kl and (rec['name'].lower() == kl or rec['key'].startswith(kl)):
            return float(v), 'override'
    if rec.get('gain_dbi') is not None:
        return rec['gain_dbi'], rec.get('gain_src') or 'published'
    return 0.0, 'assumed'


def nearby(lat, lon, radius_km, node_type='repeater', limit=15, exclude_keys=()):
    out = []
    ex = {str(k).lower() for k in exclude_keys}
    with _lock:
        recs = list(_nodes)
    for r in recs:
        if node_type and r['type'] not in (node_type, None):
            continue
        if r['key'] in ex:
            continue
        d = linkmodel.haversine_km(lat, lon, r['lat'], r['lon'])
        if d <= radius_km:
            out.append((d, r))
    out.sort(key=lambda t: t[0])
    return out[:limit]
