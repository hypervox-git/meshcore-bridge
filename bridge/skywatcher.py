# Sky-Watcher AZ-GTI (WiFi) as an antenna rotator.
#
# The mount's WiFi speaks the Sky-Watcher "motor controller" protocol: short ASCII commands over UDP
# port 11880 (what the SynScan app uses). Command = ':' + letter + axis + data + CR, reply = '=' + data + CR
# or '!' + error digit + CR. Numbers are 24-bit, little-endian hex ('123456' = 0x563412). We only use axis 1
# (azimuth) and never touch axis 2, so keep the mount's altitude axis level.
#   :a1 counts per revolution   :j1 position (0x800000 = power-on zero)   :f1 status   :e1 firmware
#   :L1 stop   :G1 mode+dir   :H1 goto increment   :M1 brake increment   :J1 start
#
# The mount has no compass and forgets its position at power-off, so the page asks for the bearing the
# antenna is currently pointing at ("Set current pointing"): that pairs the motor count with a true bearing.
# After that, bearing = ref_bearing +/- (count - ref_count) * 360 / counts-per-rev. The travel limit stops the
# coax winding round the mast: a target is always reached the way that stays within +/- travel_deg of the
# reference bearing, never by spinning through the limit.
# Settings live in MOUNT_FILE (git-ignored) so the web page can change them without a restart. The file also
# remembers the last position read from the mount (last_pos / last_bearing), so:
#   * the bridge restarting loses nothing (the mount keeps its own count while powered);
#   * a mount POWER CYCLE is detected (count back at exactly 0 though it was somewhere else) and moves are
#     refused until you press Set current pointing or Restore last pointing (antenna not moved while off);
#   * the modelled antenna aim (map beam / ghosts, meshcore_antenna.json) follows the real bearing (sync_model).
# NOT verified on hardware by the author: check the direction ('Reverse direction') with the +/- 10 buttons.
import json
import math
import os
import re
import socket
import threading
import time

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def _file():
    return _c('MOUNT_FILE', 'meshcore_mount.json')


HOME = 0x800000                        # position register value at power-on
DEADBAND_DEG = 0.3                     # don't move for less than this
_lock = threading.RLock()
_cpr_cache = {}
_last = None


class MountError(RuntimeError):
    pass


CYCLED = ('the mount seems to have been power-cycled (its position count was reset). Press "Restore last pointing" if the '
          'antenna has not been moved since, or point it at a known bearing and press "Set current pointing"')


# ---- settings ---------------------------------------------------------------------------

DEFAULTS = {'host': '', 'port': 11880, 'ref_bearing': 0.0, 'ref_pos': None, 'travel_deg': 180.0,
            'invert': False, 'brake_deg': 5.0, 'last_pos': None, 'last_bearing': None}
_HOST = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')


def _saved():
    try:
        with open(_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def settings():
    d = dict(DEFAULTS)
    cfg = _c('MOUNT_DEFAULTS', None)
    if isinstance(cfg, dict):
        d.update({k: v for k, v in cfg.items() if k in d})
    d.update({k: v for k, v in _saved().items() if k in d})
    return d


def _num(v, name, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number')
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f'{name} must be between {lo:g} and {hi:g}')
    return x


def validate(d):
    out = {}
    if 'host' in d:
        h = str(d['host'] or '').strip()
        if h and not _HOST.match(h):
            raise ValueError('mount address must be an IP address or host name')
        out['host'] = h
    if 'port' in d:
        out['port'] = int(_num(d['port'], 'port', 1, 65535))
    if 'ref_bearing' in d:
        out['ref_bearing'] = _num(d['ref_bearing'], 'initial bearing', 0, 360) % 360.0
    if 'travel_deg' in d:
        out['travel_deg'] = _num(d['travel_deg'], 'travel limit', 30, 360)
    if 'brake_deg' in d:
        out['brake_deg'] = _num(d['brake_deg'], 'brake distance', 0.5, 30)
    if 'invert' in d:
        out['invert'] = bool(d['invert'])
    return out


def _write(cur):
    tmp = _file() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cur, f)
    os.replace(tmp, _file())


def _remember(pos, bearing):
    """Persist the last position seen (only when it changed, so the file is not rewritten on every poll)."""
    cur = _saved()
    if cur.get('last_pos') == pos:
        return
    cur.update(last_pos=pos, last_bearing=round(bearing % 360.0, 1))
    try:
        _write(cur)
    except OSError as e:
        print(f'[mount] could not save position: {e}')


def power_cycled(s, pos):
    """True if the count is back at the power-on zero although the mount was last somewhere else."""
    return pos == 0 and s.get('last_pos') not in (None, 0)


def save_settings(update):
    cur = _saved()
    cur.update(validate(update if isinstance(update, dict) else {}))
    _write(cur)
    return settings()


# ---- wire protocol ----------------------------------------------------------------------

def hex24(v):
    v = int(v) & 0xFFFFFF
    return '%02X%02X%02X' % (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def parse24(s):
    s = str(s).strip()
    if not re.fullmatch(r'[0-9A-Fa-f]{6}', s):
        raise MountError(f'unexpected reply {s!r}')
    return int(s[4:6] + s[2:4] + s[0:2], 16)


ERRORS = {'0': 'unknown command', '1': 'command length error', '2': 'motor not stopped', '3': 'invalid character',
          '4': 'not initialised', '5': 'driver asleep', '7': 'PEC training running', '8': 'no valid PEC data'}


def exchange(host, port, line, timeout=1.0, tries=3):
    """Send one controller command, return the reply data. Raises MountError."""
    if not host:
        raise MountError('mount address not set')
    msg = (line + '\r').encode()
    last = None
    for _ in range(tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(msg, (host, int(port)))
            data, _addr = s.recvfrom(256)
        except (socket.timeout, OSError) as e:
            last = e
            continue
        finally:
            s.close()
        r = data.decode('ascii', 'replace').strip()
        if r.startswith('='):
            return r[1:]
        if r.startswith('!'):
            raise MountError(f'mount refused {line!r}: {ERRORS.get(r[1:2], "error " + r[1:])}')
        raise MountError(f'unexpected reply {r!r}')
    raise MountError(f'no reply from the mount at {host}:{port} ({last.__class__.__name__ if last else "timeout"})')


def _x(s, line, **kw):
    return exchange(s['host'], s['port'], line, **kw)


def _cpr(s):
    k = (s['host'], s['port'])
    if k not in _cpr_cache:
        v = parse24(_x(s, ':a1'))
        if v < 1000:
            raise MountError(f'implausible counts per revolution ({v})')
        _cpr_cache[k] = v
    return _cpr_cache[k]


def _pos(s):
    return parse24(_x(s, ':j1')) - HOME


def _running(s):
    st = _x(s, ':f1')
    return len(st) >= 2 and bool(int(st[1], 16) & 1)


# ---- bearing <-> counts -----------------------------------------------------------------

def _sign(s):
    return -1 if s['invert'] else 1


def _calibrated(s):
    if s.get('ref_pos') is None:
        raise MountError('mount not calibrated: enter the bearing the antenna points at now and press "Set current pointing"')


def bearing_of(s, pos, cpr):
    """Unwrapped bearing (deg, may be outside 0-360) for a position count."""
    return s['ref_bearing'] + _sign(s) * (pos - s['ref_pos']) * 360.0 / cpr


def counts_for(s, bearing, cpr):
    return s['ref_pos'] + round(_sign(s) * (bearing - s['ref_bearing']) * cpr / 360.0)


def choose_target(s, az, current):
    """Bearing (deg, unwrapped) to move to for compass azimuth `az`: the turn that stays inside the travel limit,
    nearest the current heading. Raises MountError if none."""
    lo, hi = s['ref_bearing'] - s['travel_deg'], s['ref_bearing'] + s['travel_deg']
    base = az % 360.0
    k0 = math.floor((lo - base) / 360.0) - 1
    cands = [base + 360.0 * k for k in range(k0, k0 + 5) if lo - 1e-6 <= base + 360.0 * k <= hi + 1e-6]
    if not cands:
        raise MountError(f'{az:.0f}° is outside the travel limit (reference {s["ref_bearing"]:.0f}° +/- {s["travel_deg"]:.0f}°)')
    return min(cands, key=lambda c: abs(c - current))


# ---- actions ----------------------------------------------------------------------------

def _wait_stopped(s, limit=3.0):
    end = time.time() + limit
    while time.time() < end:
        if not _running(s):
            return True
        time.sleep(0.1)
    return False


def goto(az, s=None):
    """Slew the antenna to compass bearing `az`. Returns a detail dict."""
    global _last
    s = s or settings()
    with _lock:
        _calibrated(s)
        cpr = _cpr(s)
        pos = _pos(s)
        if power_cycled(s, pos):
            raise MountError(CYCLED)
        cur = bearing_of(s, pos, cpr)
        tgt = choose_target(s, float(az), cur)
        delta_deg = tgt - cur
        if abs(delta_deg) < DEADBAND_DEG:
            res = {'detail': f'mount already pointing at {cur % 360:.1f}°', 'moved_deg': 0.0, 'actual': round(cur % 360, 1)}
        else:
            delta = counts_for(s, tgt, cpr) - pos
            n = abs(delta)
            _x(s, ':L1')
            if not _wait_stopped(s):
                raise MountError('the mount did not stop - nothing sent')
            brake = min(n // 2, max(1, round(s['brake_deg'] * cpr / 360.0)))
            _x(s, ':G1' + '0' + ('1' if delta < 0 else '0'))        # high-speed goto, direction
            _x(s, ':H1' + hex24(n))
            _x(s, ':M1' + hex24(brake))
            _x(s, ':J1')
            res = {'detail': f'mount slewing to {tgt % 360:.1f}° ({delta_deg:+.1f}° turn)', 'moved_deg': round(delta_deg, 1),
                   'actual': round(cur % 360, 1)}
        _last = dict(res, ts=time.time(), target=round(float(az) % 360, 1))
        return res


def nudge(delta_deg, s=None):
    s = s or settings()
    with _lock:
        _calibrated(s)
        cur = bearing_of(s, _pos(s), _cpr(s))
        return goto((cur + float(_num(delta_deg, 'nudge', -90, 90))) % 360.0, s)


def stop(s=None):
    s = s or settings()
    with _lock:
        _x(s, ':L1')
    return {'detail': 'mount stopped'}


def calibrate(bearing=None, s=None):
    """Declare that the antenna points at `bearing` right now (deg true); stores the pairing with the motor count."""
    s = dict(s or settings())
    if bearing is not None:
        s['ref_bearing'] = _num(bearing, 'initial bearing', 0, 360) % 360.0
    with _lock:
        if _running(s):
            raise MountError('the mount is moving - wait for it to stop, then set the pointing')
        _cpr(s)
        pos = _pos(s)
    cur = _saved()
    cur.update(ref_bearing=s['ref_bearing'], ref_pos=pos, last_pos=pos, last_bearing=round(s['ref_bearing'], 1))
    _write(cur)
    return {'detail': f"mount pointing set to {s['ref_bearing']:.1f}° (motor count {pos})"}


def restore(s=None):
    """After a mount power cycle: assume the antenna is still where it was last seen and re-pair the new count with it."""
    s = s or settings()
    if s.get('last_bearing') is None:
        raise MountError('no previous pointing is stored - point the antenna at a known bearing and press "Set current pointing"')
    r = calibrate(s['last_bearing'], s)
    return {'detail': 'restored: ' + r['detail']}


def status(s=None, remember=True):
    """Settings plus a live reading (never raises): reachable, actual bearing, moving."""
    s = s or settings()
    out = {'ok': True, 'settings': s, 'calibrated': s.get('ref_pos') is not None, 'reachable': False, 'last': _last}
    if not s['host']:
        out['message'] = 'mount address not set'
        return out
    try:
        with _lock:
            cpr = _cpr(s)
            pos = _pos(s)
            out.update(reachable=True, cpr=cpr, count=pos, moving=_running(s))
            if power_cycled(s, pos):
                out.update(power_cycled=True, calibrated=False, message=CYCLED)
            elif s.get('ref_pos') is not None:
                b = bearing_of(s, pos, cpr)
                out.update(bearing=round(b % 360, 1), unwrapped=round(b, 1),
                           travel_left=round(s['travel_deg'] - abs(b - s['ref_bearing']), 1))
                if remember:
                    _remember(pos, b)
    except MountError as e:
        out['message'] = str(e)
    return out


def sync_model(st=None):
    """Make the modelled antenna aim (map beam, ghosts - saved in meshcore_antenna.json, so it survives restarts)
    equal the bearing actually read from the mount. Returns True if it changed."""
    from bridge import antenna
    st = st or status()
    b = st.get('bearing')
    if b is None:
        return False
    cur = (antenna.override() or {}).get('azimuth_deg')
    if cur is not None and abs((b - cur + 180.0) % 360.0 - 180.0) < 0.5:
        return False
    antenna.save({'azimuth_deg': b}, 'read from the Sky-Watcher mount')
    return True


def start():
    """Background task: every 30 s (while a mount address is set) keep the modelled aim equal to the real bearing."""
    def _loop():
        while True:
            try:
                if settings()['host']:
                    sync_model()
            except Exception as e:
                print(f'[mount] {e}')
            time.sleep(30)
    threading.Thread(target=_loop, daemon=True, name='mount-sync').start()
