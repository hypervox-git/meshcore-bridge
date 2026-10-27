# Persistent neighbour table.
#
# A table of the repeaters around OUR repeater that survives a restart of the Pi (JSON on disk,
# NEIGHBOUR_TABLE_FILE, git-ignored). It is filled two ways:
#   * from the PUBLIC database (the community map list loaded by bridge/publicdir.py): the nearest
#     repeaters within NEIGHBOUR_RADIUS_KM, on a button press ("Update from public database") and
#     automatically every NEIGHBOUR_SYNC_HOURS (24) by a background task. Such a node has never been
#     heard by us, so the UI shows it muted.
#   * from the radio: whenever an advert is heard (the repeater's own `neighbors` table, with its
#     age), or a node answers a discover command. That sets `heard_ts` and the UI turns the row green;
#     the row's age is the age of that data.
# Range and bearing from our repeater are derived every time the table is updated (and re-derived
# if our position changes), so the table always carries them.
import json
import os
import threading
import time

from bridge import linkmodel, publicdir
from bridge.extensions import socketio

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _file():
    return _c('NEIGHBOUR_TABLE_FILE', 'meshcore_neighbours.json')


def _radius_km():
    return float(_c('NEIGHBOUR_RADIUS_KM', 50))


def _max_entries():
    return int(_c('NEIGHBOUR_TABLE_MAX', 60))


def _sync_s():
    return float(_c('NEIGHBOUR_SYNC_HOURS', 24)) * 3600


def _active_s():
    return float(_c('NEIGHBOUR_ACTIVE_HOURS', 24)) * 3600


FETCH_MIN_INTERVAL = 60.0              # a manual press may not re-download the public list more often than this
STALE_FETCH_S = 23 * 3600              # the background task re-downloads when its copy is older than this
REFRESH_TOLERANCE_S = 5.0              # "heard again" needs a timestamp this much newer than the stored one

_lock = threading.RLock()
_entries = {}                          # key (full pubkey, or a hex prefix until the public list supplies it) -> entry
_meta = {}
_busy = threading.Lock()               # one public-database sync at a time
_syncing = False
_last_fetch = 0.0
_discover_ts = 0.0


# ---- persistence ------------------------------------------------------------------------

def load():
    """Read the table from disk (called once at start-up; a missing/corrupt file = empty table)."""
    try:
        with open(_file()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    ents = d.get('entries') if isinstance(d, dict) else None
    with _lock:
        _entries.clear()
        _meta.clear()
        if isinstance(ents, dict):
            _entries.update({k: v for k, v in ents.items() if isinstance(v, dict) and v.get('key')})
        if isinstance(d.get('meta') if isinstance(d, dict) else None, dict):
            _meta.update(d['meta'])
    return len(_entries)


def _save():
    tmp = _file() + '.tmp'
    try:
        with _lock:
            data = {'meta': dict(_meta), 'entries': dict(_entries)}
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, _file())
    except OSError as e:
        print(f'[nbtable] save error: {e}')


# ---- helpers ----------------------------------------------------------------------------

def _local():
    """Our repeater's name/position/key from publiclinks (config, else the public directory); None if unknown."""
    try:
        from bridge import publiclinks
        loc = publiclinks.local_info()
        return loc if loc.get('lat') is not None and loc.get('lon') is not None else None
    except Exception:
        return None


def _derive(e, loc):
    """Range (km) and bearing (deg true) from our repeater to the entry. Called whenever the table is updated."""
    if loc and e.get('lat') is not None and e.get('lon') is not None:
        e['range_km'] = round(linkmodel.haversine_km(loc['lat'], loc['lon'], e['lat'], e['lon']), 2)
        e['bearing_deg'] = round(linkmodel.bearing_deg(loc['lat'], loc['lon'], e['lat'], e['lon']), 1)
    else:
        e['range_km'] = e['bearing_deg'] = None


def _rederive_all(loc):
    with _lock:
        for e in _entries.values():
            _derive(e, loc)
        _meta['origin'] = [loc['lat'], loc['lon']] if loc else None


def _find(ident):
    """(entry, ambiguous). `ident` is a pubkey or a hex prefix of one (>= 4 hex chars)."""
    h = str(ident or '').lower().strip().lstrip('!')
    if len(h) < 4:
        return None, False
    with _lock:
        if h in _entries:
            return _entries[h], False
        m = [e for k, e in _entries.items() if k.startswith(h) or h.startswith(k)]
    return (m[0], False) if len(m) == 1 else (None, len(m) > 1)


def _ant_az():
    """The antenna's aim (deg true) right now - the real mount bearing once the mount has been read."""
    try:
        from bridge import antenna
        v = _f((antenna.override() or {}).get('azimuth_deg'))
        return round(v, 1) if v is not None else None
    except Exception:
        return None


LIVE_S = 120                                   # a reading this recent was taken at the antenna's current aim


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _emit():
    try:
        socketio.emit('nbtable', view(), namespace='/')
    except Exception:
        pass


# ---- heard on the radio ------------------------------------------------------------------

def heard(ident, via='advert', ts=None, snr=None, rssi=None, name=None, lat=None, lon=None,
          strict=False, notify=True):
    """Record that a node's announcement was heard (via='advert') or it answered a command (via='discover').

    `ts` is when it was heard (default now) so the age stays true to the data. A node not yet in the
    table is added (position/name from the public list if it knows it). strict=True (used for adverts,
    which are flooded across the whole mesh) only adds nodes known to be within NEIGHBOUR_RADIUS_KM.
    Returns True if the table changed."""
    ts = _f(ts) or time.time()
    snr, rssi, lat, lon = _f(snr), _f(rssi), _f(lat), _f(lon)
    loc = _local()
    with _lock:
        e, amb = _find(ident)
        if amb:
            return False
        created = e is None
        if created:
            h = str(ident).lower().strip().lstrip('!')
            if len(h) < 4:
                return False
            rec = publicdir.lookup(h)
            if rec:
                key, name, lat, lon = rec['key'], name or rec['name'], rec['lat'], rec['lon']
                typ = rec.get('type')
            else:
                key, typ = h, None
            if strict and (lat is None or lon is None or not loc or
                           linkmodel.haversine_km(loc['lat'], loc['lon'], lat, lon) > _radius_km()):
                return False
            e = {'key': key, 'name': name or '', 'type': typ, 'lat': lat, 'lon': lon, 'src': 'heard',
                 'heard_ts': None, 'heard_via': None, 'snr': None, 'rssi': None, 'ant_az': None, 'range_km': None,
                 'bearing_deg': None, 'public_ts': None, 'added_ts': time.time()}
            _entries[key] = e
        changed = created
        if e.get('heard_ts') is None or ts > e['heard_ts'] + REFRESH_TOLERANCE_S:
            e['heard_ts'], e['heard_via'] = ts, via
            e['ant_az'] = _ant_az() if time.time() - ts <= LIVE_S else None     # aim when heard (unknown for old data)
            changed = True
        for field, val in (('snr', snr), ('rssi', rssi)):
            if val is not None and e.get(field) != val:
                e[field], changed = val, True
        if name and not e.get('name'):
            e['name'], changed = name, True
        if lat is not None and lon is not None and e.get('lat') is None:
            e['lat'], e['lon'], changed = lat, lon, True
        if changed:
            _derive(e, loc)
    if changed:
        _save()
        if notify:
            _emit()
    return changed


def heard_many(items, via='discover'):
    """items: dicts with pubkey/id, optional rssi, snr, name, lat, lon, ts. One save + one emit."""
    changed = False
    for it in items or []:
        if heard(it.get('pubkey') or it.get('id'), via=via, ts=it.get('ts'), snr=it.get('snr'), rssi=it.get('rssi'),
                 name=it.get('name'), lat=it.get('lat'), lon=it.get('lon'), notify=False):
            changed = True
    if changed:
        _emit()
    return changed


def note_discover(ts=None):
    """A discover command was just sent: neighbours that answer within ~90 s are tagged 'discover', not 'advert'."""
    global _discover_ts
    _discover_ts = _f(ts) or time.time()


def _rssi_by_id():
    try:
        from bridge import repeater_test
        return {str(n['id']).lower(): n for n in (repeater_test.view().get('neighbours') or []) if n.get('rssi') is not None}
    except Exception:
        return {}


def on_sample(snap):
    """Hook run after every poll of the attached repeater (repeater_cli.hooks): its own `neighbors` table gives
    each direct neighbour's advert age and SNR, so heard_ts = now - age keeps the age true to the data."""
    rows = snap.get('neighbours') or []
    if not rows:
        return
    now, meas, changed = time.time(), None, False
    for r in rows:
        rid = str(r.get('id') or '').lower()
        age = _f(r.get('age_s'))
        if len(rid) < 4 or age is None:
            continue
        ts = now - age
        e, amb = _find(rid)
        if amb or (e and e.get('heard_ts') and ts <= e['heard_ts'] + REFRESH_TOLERANCE_S):
            continue                                              # nothing new since last poll
        if meas is None:
            meas = _rssi_by_id()
        via = 'discover' if _discover_ts and 0 <= ts - _discover_ts <= 90 else 'advert'
        if heard(rid, via, ts, snr=r.get('snr_db'), rssi=(meas.get(rid) or {}).get('rssi'), name=r.get('name'),
                 lat=r.get('lat'), lon=r.get('lon'), notify=False):
            changed = True
    if changed:
        _emit()


# ---- public database sync ----------------------------------------------------------------

def sync(manual=True):
    """(Re)populate from the public database. manual=True re-downloads the list (rate-limited); the 24 h task
    only re-downloads when its copy is older than STALE_FETCH_S. Returns a summary dict; raises RuntimeError."""
    global _syncing, _last_fetch
    if not _busy.acquire(blocking=False):
        raise RuntimeError('an update is already running')
    _syncing = True
    now = time.time()
    try:
        _meta['last_attempt'] = now
        err = None
        st = publicdir.status()
        stale = st.get('fetched_at') is None or now - st['fetched_at'] > STALE_FETCH_S
        if manual or stale:
            wait = FETCH_MIN_INTERVAL - (now - _last_fetch)
            if manual and wait > 0:
                raise RuntimeError(f'the public list was fetched {round(FETCH_MIN_INTERVAL - wait)} s ago - '
                                   f'wait {round(wait)} s before updating again')
            _last_fetch = now
            if not publicdir.refresh(force=True):
                err = publicdir.status().get('error') or 'the public database returned no nodes'
        st = publicdir.status()
        if not st.get('count'):
            raise RuntimeError('public database not available' + (f': {err}' if err else ' (not loaded yet)'))
        loc = _local()
        if loc is None:
            raise RuntimeError("our position is unknown: set GATEWAY_LAT/GATEWAY_LON, or LOCAL_NAME to the repeater's public name")
        exclude = {loc['key']} if loc.get('key') else set()
        near = publicdir.nearby(loc['lat'], loc['lon'], _radius_km(), 'repeater', _max_entries(), exclude)
        added = updated = removed = 0
        keep = set()
        with _lock:
            for _d, rec in near:
                e, _amb = _find(rec['key'])
                if e is not None and e['key'] != rec['key']:         # prefix-keyed entry: adopt the full key
                    del _entries[e['key']]
                    e['key'] = rec['key']
                    _entries[rec['key']] = e
                if e is None:
                    e = {'key': rec['key'], 'name': rec['name'], 'type': rec.get('type'), 'lat': rec['lat'], 'lon': rec['lon'],
                         'src': 'public', 'heard_ts': None, 'heard_via': None, 'snr': None, 'rssi': None,
                         'range_km': None, 'bearing_deg': None, 'public_ts': now, 'added_ts': now}
                    _entries[rec['key']] = e
                    added += 1
                else:
                    before = (e.get('name'), e.get('lat'), e.get('lon'), e.get('type'))
                    e.update(name=rec['name'] or e.get('name'), lat=rec['lat'], lon=rec['lon'], type=rec.get('type') or e.get('type'),
                             public_ts=now)
                    updated += before != (e.get('name'), e.get('lat'), e.get('lon'), e.get('type'))
                keep.add(rec['key'])
            for k in [k for k, e in _entries.items()
                      if e.get('src') == 'public' and e.get('heard_ts') is None and k not in keep]:
                del _entries[k]                                      # left the public list/radius and never heard
                removed += 1
            for e in _entries.values():
                _derive(e, loc)
            _meta.update(last_sync_ts=now, last_error=err, public_count=st['count'], public_fetched_at=st.get('fetched_at'),
                         public_source=st.get('source'), origin=[loc['lat'], loc['lon']], our_name=loc.get('name'))
            count = len(_entries)
        _save()
        _emit()
        return {'ok': True, 'added': added, 'updated': updated, 'removed': removed, 'count': count,
                'public_count': st['count'], 'warning': err}
    except Exception as e:
        _meta['last_error'] = str(e) or e.__class__.__name__
        _save()
        raise
    finally:
        _syncing = False
        _busy.release()


def due(now=None):
    last = _meta.get('last_sync_ts')
    return last is None or (now or time.time()) - last >= _sync_s()


def _tick(next_try):
    """One pass of the background task; returns the time before which it should not try a sync again."""
    now = time.time()
    loc = _local()
    if loc and _entries and _meta.get('origin') != [loc['lat'], loc['lon']]:
        _rederive_all(loc)                                           # our position changed: refresh range/bearing
        _save()
        _emit()
    if now >= next_try and due(now):
        try:
            sync(manual=False)
        except Exception as e:
            print(f'[nbtable] background update: {e}')
            return now + (300 if 'not loaded' in str(e) or 'unknown' in str(e) else 3600)
    return next_try


def start():
    load()
    print(f'[nbtable] {len(_entries)} neighbour(s) loaded from {_file()}')

    def _loop():
        nxt = 0.0
        while True:
            try:
                nxt = _tick(nxt)
            except Exception as e:
                print(f'[nbtable] {e}')
            time.sleep(60)
    threading.Thread(target=_loop, daemon=True, name='nb-table').start()


# ---- view -------------------------------------------------------------------------------

def view():
    now = time.time()
    win = _active_s()
    with _lock:
        ents = [dict(e) for e in _entries.values()]
        meta = dict(_meta)
    for e in ents:
        e['active'] = e.get('heard_ts') is not None and now - e['heard_ts'] <= win
    ents.sort(key=lambda e: (not e['active'], -(e.get('heard_ts') or 0) if e['active'] else
                             (e['range_km'] if e.get('range_km') is not None else 1e9)))
    return {'ok': True, 'now': now, 'entries': ents, 'active_s': win, 'sync_hours': _sync_s() / 3600,
            'radius_km': _radius_km(), 'max': _max_entries(), 'syncing': _syncing,
            'last_sync_ts': meta.get('last_sync_ts'), 'last_error': meta.get('last_error'),
            'public_count': meta.get('public_count'), 'our_name': meta.get('our_name'),
            'next_sync_ts': (meta['last_sync_ts'] + _sync_s()) if meta.get('last_sync_ts') else None}
