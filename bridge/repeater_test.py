# Repeater TEST MODE (SERIAL_MODE = "repeater"): a time-boxed, receive-only survey of what
# the local repeater hears - neighbours, signal strength and an estimated path loss.
#
# While a test runs the repeater can be taken OUT of the mesh (`set repeat off`, so it
# stops forwarding other people's traffic while we measure). It is ALWAYS put back:
#   * automatically when the configurable timeout expires (REP_TEST_MINUTES, capped by
#     REP_TEST_MAX_MINUTES), or on Stop;
#   * if the serial link drops, the bridge gets SIGTERM / exits, or the bridge crashes
#     (a marker file records "restore pending" and is acted on at the next connect);
#   * restoring retries until the repeater confirms (`get repeat`).
# Caveat: `set repeat` is stored in the repeater's flash, so if the Pi itself dies mid-test
# the repeater stays muted until someone sends `set repeat on` (the UI says so).
#
# Neighbour discovery: at test start (and on demand) we send the CLI command
# `discover.neighbors` - a single short zero-hop request that makes neighbours answer, so
# the `neighbors` table fills in seconds. Not every firmware has it (1.17.1 answers
# "Unknown command"); then the test falls back to PASSIVE discovery (a neighbour shows up
# when its zero-hop advert is heard). The only transmission is that one request; its real
# airtime is measured from the radio's tx_air_secs counter and shown.
#
# Path loss = assumed neighbour TX power - RSSI we measured (the neighbour's real TX power
# and antenna gains are unknown, so it is an ESTIMATE). The per-neighbour RSSI is the
# repeater's `last_rssi` at the moment that neighbour's table entry was refreshed.
import atexit
import json
import os
import re
import signal
import sys
import threading
import time

from bridge import state, repeater_cli as cli, mqtt_bridge
from bridge.state import node_db, update_node_db
from bridge.extensions import socketio

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _minutes_default():
    return float(_c('REP_TEST_MINUTES', 10))


def _minutes_max():
    return float(_c('REP_TEST_MAX_MINUTES', 60))


def _marker():
    return _c('REP_RESTORE_FILE', 'meshcore_repeater_restore.json')


_lock = threading.RLock()
_t = None                  # current/last session dict
_m = None                  # always-on monitor (same shape; no timer, no repeat change)
_stop = threading.Event()
_restoring = False
_ONOFF = re.compile(r'\b(on|off)\b', re.I)
_last_discover = 0.0


def _discover_min_interval():
    return float(_c('DISCOVER_MIN_INTERVAL', 30))


def _emit(event, data):
    socketio.emit(event, data, namespace='/')


# ---- repeater CLI helpers -----------------------------------------------------

def _get_repeat():
    """True/False, or None if the firmware has no `get repeat`."""
    try:
        for t in cli.command('get repeat'):
            if 'unknown' in t.lower():
                return None
            m = _ONOFF.search(t)
            if m:
                return m.group(1).lower() == 'on'
    except Exception:
        pass
    return None


def _set_repeat(on):
    lines = cli.command('set repeat ' + ('on' if on else 'off'))
    if any('unknown' in t.lower() or 'err' in t.lower() for t in lines):
        raise RuntimeError('repeater rejected "set repeat": ' + ' '.join(lines))
    return _get_repeat() == on


def _num(lines, default=None):
    for t in lines:
        m = re.search(r'-?\d+(?:\.\d+)?', t)
        if m and 'unknown' not in t.lower():
            return float(m.group(0))
    return default


# ---- restore-to-mesh machinery --------------------------------------------------

def _write_marker(prev):
    try:
        with open(_marker(), 'w') as f:
            json.dump({'prev_repeat': prev, 'ts': time.time()}, f)
    except OSError as e:
        print(f"[rtest] cannot write {_marker()}: {e}")


def _clear_marker():
    try:
        os.remove(_marker())
    except OSError:
        pass


def _marker_prev():
    try:
        with open(_marker()) as f:
            return json.load(f).get('prev_repeat', True)
    except (OSError, ValueError):
        return None


def _restore_once():
    """Put the repeater back; True when confirmed."""
    prev = _marker_prev()
    want = True if prev is None else bool(prev)
    try:
        ok = _set_repeat(want)
    except Exception as e:
        print(f"[rtest] restore attempt failed: {e}")
        return False
    if ok:
        _clear_marker()
        print(f"[rtest] repeater back in the mesh (repeat {'on' if want else 'off'})")
    return ok


def _restore_worker():
    """Retry until the repeater confirms it is back in the mesh (survives link drops)."""
    global _restoring
    with _lock:
        if _restoring:
            return
        _restoring = True
    try:
        while os.path.exists(_marker()):
            if state.serial_connected and _restore_once():
                break
            if _t is not None:
                _t['restore_pending'] = True
                _emit('rtest_state', view())
            time.sleep(5)
    finally:
        with _lock:
            _restoring = False
        if _t is not None:
            _t['restore_pending'] = os.path.exists(_marker())
            _emit('rtest_state', view())


def _restore_async():
    threading.Thread(target=_restore_worker, daemon=True, name="rtest-restore").start()


def recover():
    """On (re)connect: if a previous test (or crash) left the repeater muted, fix it."""
    if os.path.exists(_marker()):
        print("[rtest] restore marker found - putting the repeater back in the mesh")
        _restore_async()


def _on_lost():
    if _t and _t.get('running'):
        _finish('serial link lost')


# ---- session ------------------------------------------------------------------

_parse_snr = cli.parse_snr


def _tx_air():
    try:
        for t in cli.command('stats-radio'):
            m = re.search(r'"tx_air_secs"\s*:\s*(\d+(?:\.\d+)?)', t)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


def discover():
    """Send `discover.neighbors` (zero-hop). Returns a result dict; never raises on 'unsupported'."""
    global _last_discover
    s = _t if (_t and _t.get('running')) else _monitor()
    if not state.serial_connected:
        raise RuntimeError('repeater not connected')
    wait = _discover_min_interval() - (time.time() - _last_discover)
    if wait > 0:
        raise RuntimeError(f'discovery is rate-limited - try again in {round(wait)} s')
    before = _tx_air()
    lines = cli.command('discover.neighbors')
    _last_discover = time.time()
    text = ' '.join(lines)
    res = {'ts': _last_discover, 'reply': text[:200], 'supported': 'unknown' not in text.lower()}
    if res['supported']:
        time.sleep(1.0)
        after = _tx_air()
        if before is not None and after is not None:
            res['tx_air_s'] = round(after - before, 3)
    s['discover'] = res
    _emit('rtest_state', view())
    return {'ok': True, 'discover': res, 'neighbours': cli.snapshot().get('neighbours')}


resolve_node = cli.resolve_node


def _publish_link(n, s):
    """Push a neighbour's measured link into node_db / dashboard / map / MQTT."""
    h = n['id'].lower()
    key = n.get('key') or resolve_node(h)
    known = key is not None
    if key is None:
        key = '!' + h[:8]
    n['key'] = key
    pl = (round(s['assumed_tx_dbm'] - n['rssi'], 1) if n['rssi'] is not None else None)
    entry = {'linkTs': int(time.time()), 'linkHeard': n['heard']}
    if n['rssi'] is not None:
        entry['rssi'] = n['rssi']
    if n['snr'] is not None:
        entry['snr'] = n['snr']
    if pl is not None:
        entry['pathloss'] = pl
        entry['pathloss_tx_dbm'] = s['assumed_tx_dbm']
    if key not in node_db:
        entry['name'] = 'repeater ' + h[:8]
        entry['synthetic'] = True
    update_node_db(key, entry)
    n['known'] = known and not node_db.get(key, {}).get('synthetic')
    nd = node_db.get(key, {})
    n['name'] = nd.get('name')
    n['lat'], n['lon'] = nd.get('lat'), nd.get('lon')
    _emit('node_link', {'id': key, 'name': nd.get('name'), 'rssi': nd.get('rssi'), 'snr': nd.get('snr'),
                        'pathloss': nd.get('pathloss'), 'lat': nd.get('lat'), 'lon': nd.get('lon'),
                        'linkTs': entry['linkTs']})
    try:
        mqtt_bridge.publish_ha_discovery(key, nd.get('name'))
        for k, v in (('rssi', n['rssi']), ('snr', n['snr']), ('pathloss', pl)):
            if v is not None:
                mqtt_bridge.publish_ha_state(key, k, v)
    except Exception as e:                                   # MQTT is best-effort
        print(f"[rtest] mqtt publish: {e}")


def _new_session(minutes, offline, assumed_tx):
    return {'running': True, 'start': time.time(), 'deadline': time.time() + minutes * 60,
            'minutes': minutes, 'offline': offline, 'prev_repeat': None, 'reason': None,
            'assumed_tx_dbm': assumed_tx, 'nb': {}, 'heard': [], 'recv0': None, 'recv_last': None,
            'nf': [], 'our_tx_dbm': None, 'radio_cfg': None, 'restore_pending': False,
            'discover': None}


def start(minutes=None, offline=None, assumed_tx_dbm=None):
    global _t
    with _lock:
        if _t and _t.get('running'):
            raise RuntimeError('a repeater test is already running')
        if not state.serial_connected:
            raise RuntimeError('repeater not connected: ' + str(state.last_serial_error[0] or 'waiting for serial'))
        if os.path.exists(_marker()):
            raise RuntimeError('repeater is still being restored to the mesh - try again in a few seconds')
        minutes = _minutes_default() if minutes in (None, '') else float(minutes)
        minutes = min(max(minutes, 1.0), _minutes_max())
        offline = bool(_c('REP_TEST_OFFLINE', True)) if offline is None else bool(offline)
        tx = float(_c('ASSUMED_NEIGHBOUR_TX_DBM', 20)) if assumed_tx_dbm in (None, '') else float(assumed_tx_dbm)
        s = _new_session(minutes, offline, tx)
        s['our_tx_dbm'] = _num(cli.command('get tx'))
        s['radio_cfg'] = (cli.command('get radio') or [None])[0]
        if offline:
            prev = _get_repeat()
            if prev is None:
                raise RuntimeError('this firmware does not answer "get repeat", so the repeater cannot be '
                                   'safely taken offline - untick "Take repeater out of the mesh"')
            s['prev_repeat'] = prev
            _write_marker(prev)                       # BEFORE changing anything
            try:
                ok = _set_repeat(False)
            except Exception:
                _restore_async()
                raise
            if not ok:
                _restore_async()
                raise RuntimeError('repeater did not confirm "repeat off"; restoring')
        _t = s
        _stop.clear()
        cli.test_active.set()
    if bool(_c('REP_TEST_DISCOVER', True)):
        global _last_discover
        _last_discover = 0.0
        try:
            discover()                                   # at initiation of the test
        except Exception as e:
            s['discover'] = {'supported': None, 'reply': str(e)}
    threading.Thread(target=_watchdog, args=(s,), daemon=True, name="rtest-watchdog").start()
    _emit('rtest_state', view())
    return {'ok': True, **view()}


def _watchdog(s):
    """Independent of the sampling loop: ends the test at the deadline no matter what."""
    while s.get('running'):
        if _stop.wait(timeout=1.0):
            return
        if time.time() >= s['deadline']:
            _finish(f"timed out after {s['minutes']:g} min - repeater returned to the mesh")
            return


def _finish(reason):
    with _lock:
        s = _t
        if not s or not s.get('running'):
            return
        s['running'] = False
        s['reason'] = reason
        s['end'] = time.time()
        cli.test_active.clear()
        _stop.set()
        if s['offline']:
            s['restore_pending'] = True
    if s['offline']:
        _restore_async()
    _emit('rtest_state', view())


def stop():
    if not _t or not _t.get('running'):
        return {'ok': True, **view()}
    _finish('stopped by user')
    return {'ok': True, **view()}


def shutdown(*_a):
    """Bridge exiting: restore synchronously (bounded) so the repeater is never left muted."""
    try:
        if _t and _t.get('running'):
            _finish('bridge stopping')
        if os.path.exists(_marker()) and state.serial_connected:
            for _ in range(3):
                if _restore_once():
                    break
    except Exception as e:
        print(f"[rtest] shutdown restore failed: {e}")


def install_exit_hooks():
    atexit.register(shutdown)
    cli.on_connect.append(recover)
    cli.on_lost.append(_on_lost)
    cli.hooks.append(on_sample)

    def _term(*_a):
        shutdown()
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, _term)
    except ValueError:                 # not the main thread (tests)
        pass


# ---- sampling ------------------------------------------------------------------

def _monitor():
    global _m
    if _m is None:
        _m = _new_session(0, False, float(_c('ASSUMED_NEIGHBOUR_TX_DBM', 20)))
        _m['running'] = False
    return _m


def on_sample(snap):
    # Track neighbours ALWAYS (so the dashboard/map/log fill without starting a test); a
    # running test just uses its own session (timer, stats) instead of the monitor's.
    s = _t if (_t and _t.get('running')) else _monitor()
    radio, pk = snap.get('radio') or {}, snap.get('packets') or {}
    now = time.time()
    if radio.get('noise_floor') is not None:
        s['nf'].append(radio['noise_floor'])
    recv = pk.get('recv')
    if recv is not None:
        if s['recv0'] is None:
            s['recv0'] = recv
        if s['recv_last'] is not None and recv > s['recv_last']:
            s['heard'].append({'ts': now, 'rssi': radio.get('last_rssi'), 'snr': radio.get('last_snr'),
                               'n': recv - s['recv_last']})
            del s['heard'][:-40]
        s['recv_last'] = recv
    for r in snap.get('neighbours') or []:
        nid = r.get('id')
        if not nid:
            continue
        try:
            age = float(r.get('age_s'))
        except (TypeError, ValueError):
            age = None
        n = s['nb'].setdefault(nid, {'id': nid, 'heard': 0, 'age': None, 'snr': None,
                                     'rssi': None, 'first': now, 'last': None})
        prev_age = n['age']
        # An entry counts as "just heard" if it first shows up with a tiny age, or its age
        # went backwards (a new advert reset it). Entries already stale when the test began
        # get no RSSI until they refresh.
        fresh = age is not None and ((prev_age is None and age <= 3) or
                                     (prev_age is not None and age < prev_age - 1))
        n['age'], n['snr'] = age, _parse_snr(r.get('snr'))
        if fresh:
            n['heard'] += 1
            n['last'] = now
            n['rssi'] = radio.get('last_rssi')          # RSSI of the advert that refreshed it
            n['_log'] = True
        sig = (n['heard'], n['snr'])
        if sig != n.get('sig'):                          # only on change (node_db writes to disk)
            n['sig'] = sig
            _publish_link(n, s)
            if n.pop('_log', False):
                pl = n.get('pathloss_db') if n.get('pathloss_db') is not None else (round(s['assumed_tx_dbm'] - n['rssi'], 1) if n['rssi'] is not None else None)
                _emit('packet', {'from': n['key'], 'fromId': n['key'], 'rxRssi': n['rssi'], 'rxSnr': n['snr'],
                                 'decoded': {'portnum': 'ADVERT', 'text': 'zero-hop advert heard from ' + (n.get('name') or n['id'])
                                             + f" - RSSI {n['rssi']} dBm, SNR {n['snr']} dB" + (f', path loss ~{pl} dB' if pl is not None else '')}})
    _emit('rtest_update', view())


def view():
    s = _t or (_m if _m and _m['nb'] else None)
    if not s:
        return {'running': False, 'discover': (_m or {}).get('discover'), 'minutes_default': _minutes_default(), 'minutes_max': _minutes_max(),
                'offline_default': bool(_c('REP_TEST_OFFLINE', True)),
                'assumed_tx_dbm': float(_c('ASSUMED_NEIGHBOUR_TX_DBM', 20)),
                'restore_pending': os.path.exists(_marker()), 'connected': bool(state.serial_connected)}
    nf = s['nf']
    nbs = []
    for n in s['nb'].values():
        d = dict(n)
        d['pathloss_db'] = (round(s['assumed_tx_dbm'] - n['rssi'], 1) if n['rssi'] is not None else None)
        d.pop('sig', None)
        nbs.append(d)
    nbs.sort(key=lambda d: (d['snr'] is None, -(d['snr'] or 0)))
    return {'running': s['running'], 'offline': s['offline'], 'minutes': s['minutes'],
            'remaining_s': max(0, round(s['deadline'] - time.time())) if s['running'] else 0,
            'reason': s['reason'], 'prev_repeat': s['prev_repeat'], 'assumed_tx_dbm': s['assumed_tx_dbm'],
            'our_tx_dbm': s['our_tx_dbm'], 'radio_cfg': s['radio_cfg'], 'discover': s['discover'],
            'minutes_default': _minutes_default(), 'minutes_max': _minutes_max(),
            'restore_pending': s['restore_pending'] or os.path.exists(_marker()),
            'connected': bool(state.serial_connected),
            'neighbours': nbs, 'heard': s['heard'][-20:],
            'packets_heard': (s['recv_last'] - s['recv0']) if s['recv0'] is not None and s['recv_last'] is not None else 0,
            'noise': {'min': min(nf), 'max': max(nf), 'avg': round(sum(nf) / len(nf), 1)} if nf else None}
