# Antenna sweep: step the rotator through a span of bearings, send one zero-hop Discover at each stop and
# log the SNR/RSSI every neighbour answers with. The neighbour table itself only holds the last advert
# heard (hours old), so it cannot show how the pattern changes with bearing - this measures it live.
import json
import threading
import time

from bridge import nbtable, skywatcher
from bridge.extensions import socketio

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _file():
    return _c('SWEEP_FILE', 'meshcore_sweep.json')


REPLY_WAIT_S = 10                      # after Discover, wait for the zero-hop replies to land
SETTLE_S = 3                           # let the antenna stop swaying before measuring
MAX_STOPS = 25
_lock = threading.RLock()
_stop = threading.Event()
_thread = None
_s = {'running': False, 'phase': 'idle', 'results': [], 'names': {}, 'error': None, 'reason': None,
      'origin_az': None, 'current_az': None, 'idx': 0, 'total': 0, 'step': None, 'span': None,
      'started': None, 'finished': None}


def _mode():
    return getattr(_cfg, 'SERIAL_MODE', 'companion') if _cfg else 'companion'


def plan(centre, span, step):
    """Bearings to visit: centre-span ... centre+span in `step` degrees (each 0-360)."""
    span, step = float(span), float(step)
    if not 5 <= step <= 90:
        raise ValueError('step must be between 5 and 90 degrees')
    if not 15 <= span <= 180:
        raise ValueError('span must be between 15 and 180 degrees either side')
    n = int(round(2 * span / step))
    if n + 1 > MAX_STOPS:
        raise ValueError(f'that is {n + 1} stops; the limit is {MAX_STOPS} (use a bigger step or smaller span)')
    return [round((centre - span + i * step) % 360.0, 1) for i in range(n + 1)]


def view():
    with _lock:
        d = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in _s.items()}
    d['ok'] = True
    d['mode'] = _mode()
    d['est_s'] = round(d['total'] * (SETTLE_S + REPLY_WAIT_S + 20)) if d['total'] else 0
    return d


def _emit():
    try:
        socketio.emit('sweep', view(), namespace='/')
    except Exception:
        pass


def _save():
    try:
        with open(_file(), 'w') as f:
            json.dump(view(), f)
    except OSError as e:
        print(f'[sweep] save error: {e}')


def load():
    """Restore the last finished sweep after a restart."""
    try:
        with open(_file()) as f:
            d = json.load(f)
        with _lock:
            for k in ('results', 'names', 'origin_az', 'step', 'span', 'started', 'finished', 'reason', 'error', 'total'):
                if k in d:
                    _s[k] = d[k]
    except (OSError, ValueError):
        pass


def _set(**kw):
    with _lock:
        _s.update(kw)
    _emit()


def _wait_stopped(limit=120):
    """Block until the mount has stopped turning; returns its status (or None if a stop was requested)."""
    t0 = time.time()
    st = skywatcher.status()
    while st.get('moving') and time.time() - t0 < limit:
        if _stop.is_set():
            return None
        time.sleep(1.0)
        st = skywatcher.status()
    return st


def _slew(az):
    """Turn to `az`, wait for it to stop, sync the modelled aim. Returns the real bearing."""
    skywatcher.goto(az)
    st = _wait_stopped()
    if st is None:
        return None
    if st.get('bearing') is None:
        raise skywatcher.MountError(st.get('message') or 'mount bearing unknown')
    skywatcher.sync_model(st)
    return st['bearing']


def _key_for(ident):
    e, _amb = nbtable._find(ident)
    return (e['key'], e.get('name') or '') if e else (str(ident).lower()[:8], '')


def _discover_repeater(t_sent_holder):
    """Repeater (serial CLI) mode: Discover, then read what refreshed in the neighbour table."""
    from bridge import repeater_test
    deadline = time.time() + 70
    while True:
        try:
            repeater_test.discover()
            break
        except RuntimeError as e:
            if 'rate-limited' not in str(e) or time.time() > deadline or _stop.is_set():
                raise
            time.sleep(3)
    t_sent = t_sent_holder[0] = time.time()
    for _ in range(REPLY_WAIT_S):
        if _stop.is_set():
            break
        time.sleep(1.0)
    rssi = {}
    try:
        for n in repeater_test.view().get('neighbours') or []:
            if n.get('last') and n['last'] >= t_sent - 1 and n.get('rssi') is not None:
                rssi[_key_for(n['id'])[0]] = n['rssi']
    except Exception:
        pass
    out = {}
    for e in nbtable.view()['entries']:
        if e.get('heard_ts') is not None and e['heard_ts'] >= t_sent - 1 and e.get('snr') is not None:
            out[e['key']] = {'snr': e['snr'], 'rssi': rssi.get(e['key']), 'name': e.get('name') or ''}
    return out


def _discover_companion():
    from bridge import testmode
    d = testmode.discover(['repeater'], 8)
    if not d.get('ok'):
        raise RuntimeError(d.get('error') or 'discover failed')
    out = {}
    for n in d.get('neighbours') or []:
        key, name = _key_for(n.get('id') or '')
        out[key] = {'snr': n.get('snr'), 'rssi': n.get('rssi'), 'name': n.get('name') or name}
    return out


def _measure():
    if _mode() == 'repeater':
        return _discover_repeater([0.0])
    return _discover_companion()


def _run(bearings, origin):
    from bridge import repeater_cli
    fast_owned = False
    try:
        if _mode() == 'repeater':
            if not repeater_cli.test_active.is_set():
                repeater_cli.test_active.set()               # poll the repeater's neighbour table every second
                fast_owned = True
        for i, az in enumerate(bearings):
            if _stop.is_set():
                _s['reason'] = 'stopped'
                break
            _set(idx=i + 1, current_az=az, phase='turning')
            actual = _slew(az)
            if actual is None:
                _s['reason'] = 'stopped'
                break
            _set(phase='settling', current_az=actual)
            time.sleep(SETTLE_S)
            _set(phase='discovering')
            readings = _measure()
            with _lock:
                _s['results'].append({'az': az, 'actual': actual, 'ts': time.time(),
                                      'readings': {k: {'snr': v['snr'], 'rssi': v['rssi']} for k, v in readings.items()}})
                for k, v in readings.items():
                    if v.get('name'):
                        _s['names'][k] = v['name']
            _emit()
        else:
            _s['reason'] = 'complete'
    except Exception as e:
        _s['error'] = str(e) or e.__class__.__name__
        _s['reason'] = 'failed'
        print(f'[sweep] {e}')
    finally:
        if fast_owned:
            repeater_cli.test_active.clear()
        _set(phase='returning')
        try:
            if _stop.is_set():
                skywatcher.stop()
            _slew_back = origin if origin is not None else None
            if _slew_back is not None:
                _stop.clear()
                _slew(_slew_back)
        except Exception as e:
            _s['error'] = (_s['error'] + '; ' if _s['error'] else '') + f'could not return to {origin}: {e}'
        _set(running=False, phase='idle', finished=time.time())
        _save()


def start(span=90, step=30):
    global _thread
    with _lock:
        if _s['running']:
            raise RuntimeError('a sweep is already running')
    st = skywatcher.status()
    if st.get('bearing') is None:
        raise RuntimeError(st.get('message') or 'the mount is not reachable / not calibrated')
    origin = st['bearing']
    bearings = plan(origin, span, step)
    _stop.clear()
    with _lock:
        _s.update(running=True, phase='starting', results=[], names={}, error=None, reason=None, origin_az=origin,
                  current_az=origin, idx=0, total=len(bearings), step=float(step), span=float(span),
                  started=time.time(), finished=None)
    _emit()
    _thread = threading.Thread(target=_run, args=(bearings, origin), daemon=True, name='antenna-sweep')
    _thread.start()
    return view()


def stop():
    _stop.set()
    return view()
