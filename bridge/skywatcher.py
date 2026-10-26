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
# TRANSPORT: 'udp' (AZ-GTI WiFi, port 11880) or 'serial' (USB-serial adapter into the mount's RJ12 port, 9600 8N1 - the
# Sky-Watcher AllView / SynScan mounts speak the same motor-controller protocol there, handset unplugged).
# POWER (power_mode 'zigbee'): the mount sits behind a Zigbee2MQTT socket and is only powered for a move. A move powers
# the socket, waits for the controller to boot (and initialises it), re-pairs the fresh count with the last known
# bearing (the antenna cannot move while unpowered), slews, and the background task switches the socket off again
# idle_off_s after the mount has stopped. While it is off, status() reports the last known bearing without touching it.
# The travel-limit window keeps its original centre (ref_bearing) across power cycles, so coax protection still holds.
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


def _log(msg):
    """Print and flush: under systemd stdout is block-buffered, so [mount] lines would otherwise not reach the journal."""
    print(msg, flush=True)


def _file():
    return _c('MOUNT_FILE', 'meshcore_mount.json')


HOME = 0x800000                        # position register value at power-on
DEADBAND_DEG = 0.3                     # don't move for less than this
BAUD = 9600                            # motor-controller serial speed (8N1)
BOOT_PROBE_S = 0.5                     # reply timeout for each "is it up yet?" probe after power-on
BOOT_POLL_S = 0.5                      # pause between probes
OFF_CHECK_S = 10                       # background poll while the socket is on (to switch it off promptly)
_lock = threading.RLock()
_cpr_cache = {}
_last = None
_ser = {'port': None, 'dev': None}     # the open serial port (re-opened on error / settings change)
_power = {'state': None, 'since': None}   # socket state: 'on' / 'off' / None (unknown), from its own reports
_activity = time.time()                # last time a command was sent (idle power-off timer)
SUPERVISE_S = 0.25                     # how often the move guard reads the mount while it is turning
SUPERVISE_ASYNC = True                 # guard runs in its own thread (tests run it inline)
LIMIT_MARGIN_DEG = 0.5                 # beyond the travel limit by more than this = emergency stop
_gen = 0                               # id of the newest move; an older move's guard stands down
_fault = None                          # last emergency stop {'ts', 'msg', 'bearing'} (shown in the UI)

# ---- link watch: log each drop-out/recovery with a timestamp, poll fast while the mount is down ----
UP_POLL_S = 30                         # normal poll interval
DOWN_POLL_S = 5                        # poll interval while the mount is unreachable
_link = {'up': None, 'down_at': None, 'since': None, 'outages': []}


def _note_link(up, msg=''):
    """Record reachable/unreachable transitions (called from status(), so UI refreshes count too)."""
    now = time.time()
    with _lock:
        prev = _link['up']
        if prev is up:
            return
        _link['up'], _link['since'] = up, now
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
        if not up:
            _link['down_at'] = now
            _log(f'[mount] {stamp} LOST: {msg}')
            return
        d = _link['down_at']
        if prev is False and d:
            secs = round(now - d)
            _link['outages'].append({'start': d, 'end': now, 'secs': secs})
            del _link['outages'][:-20]
            _log(f'[mount] {stamp} RECOVERED after {secs} s')
        else:
            _log(f'[mount] {stamp} reachable')
        _link['down_at'] = None


def link_view():
    with _lock:
        return {'up': _link['up'], 'since': _link['since'], 'down_at': _link['down_at'],
                'outages': list(_link['outages'])}


class MountError(RuntimeError):
    pass


CYCLED = ('the mount seems to have been power-cycled (its position count was reset). Press "Restore last pointing" if the '
          'antenna has not been moved since, or point it at a known bearing and press "Set current pointing"')


# ---- settings ---------------------------------------------------------------------------

DEFAULTS = {'host': '', 'port': 11880, 'ref_bearing': 0.0, 'ref_pos': None, 'travel_deg': 180.0,
            'invert': False, 'brake_deg': 5.0, 'last_pos': None, 'last_bearing': None, 'last_unwrapped': None,
            'transport': 'udp', 'serial_port': '/dev/ttyUSB0',
            'power_mode': 'always', 'power_topic': '', 'boot_timeout_s': 25.0, 'idle_off_s': 60.0}
_HOST = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')
_SERIAL = re.compile(r'^/dev/[A-Za-z0-9_./-]{1,120}$')


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
        out['brake_deg'] = _num(d['brake_deg'], 'brake distance', 0.1, 30)
    if 'invert' in d:
        out['invert'] = bool(d['invert'])
    if 'transport' in d:
        if d['transport'] not in ('udp', 'serial'):
            raise ValueError("connection must be 'udp' or 'serial'")
        out['transport'] = d['transport']
    if 'serial_port' in d:
        p = str(d['serial_port'] or '').strip()
        if not _SERIAL.match(p):
            raise ValueError('serial port must be a device path such as /dev/ttyUSB0')
        out['serial_port'] = p
    if 'power_mode' in d:
        if d['power_mode'] not in ('always', 'zigbee'):
            raise ValueError("power must be 'always' or 'zigbee'")
        out['power_mode'] = d['power_mode']
    if 'power_topic' in d:
        t = str(d['power_topic'] or '').strip().strip('/')
        if t:
            from bridge.steer import check_topic
            t = check_topic(t)
            if t.endswith('/set') or t.endswith('/get'):
                raise ValueError('power topic is the socket\'s base topic (e.g. zigbee2mqtt/mount_socket), without /set')
        out['power_topic'] = t
    if 'boot_timeout_s' in d:
        out['boot_timeout_s'] = _num(d['boot_timeout_s'], 'boot timeout', 5, 120)
    if 'idle_off_s' in d:
        out['idle_off_s'] = _num(d['idle_off_s'], 'switch-off delay', 10, 3600)
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
    cur.update(last_pos=pos, last_bearing=round(bearing % 360.0, 3), last_unwrapped=round(bearing, 3))
    try:
        _write(cur)
    except OSError as e:
        _log(f'[mount] could not save position: {e}')


def power_cycled(s, pos):
    """True if the count is back at the power-on zero although the mount was last somewhere else."""
    return pos == 0 and s.get('last_pos') not in (None, 0)


def _last_unwrapped(s):
    """The last known UNWRAPPED bearing (so the travel window is respected); older files only hold the 0-360 value."""
    ub = s.get('last_unwrapped')
    if ub is not None:
        return float(ub)
    lb = float(s['last_bearing'])
    try:
        return choose_target(s, lb, s['ref_bearing'])
    except MountError:
        return lb


def _rebase(s, pos, cpr):
    """Re-pair the current count `pos` with the last known bearing, keeping ref_bearing (the centre of the travel
    window) unchanged, so repeated power cycles can never slide the cable-protection window."""
    ub = _last_unwrapped(s)
    ref_pos = pos - round(_sign(s) * (ub - s['ref_bearing']) * cpr / 360.0)
    cur = _saved()
    cur.update(ref_pos=ref_pos, last_pos=pos, last_bearing=round(ub % 360.0, 3), last_unwrapped=round(ub, 3))
    _write(cur)
    return ub


def save_settings(update):
    cur = _saved()
    cur.update(validate(update if isinstance(update, dict) else {}))
    _write(cur)
    _close_serial()                        # port / transport may have changed: re-open on next use
    try:
        watch_power()                      # (re)subscribe to the socket's state topic
    except Exception as e:
        _log(f'[mount] power watch: {e}')
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


def _parse_reply(line, data):
    """Reply bytes -> data after '='. Raises MountError for '!' replies and anything else."""
    r = data.decode('ascii', 'replace').strip()
    for i, ch in enumerate(r):             # skip any echo / line noise before the reply marker
        if ch in '=!':
            r = r[i:]
            break
    if r.startswith('='):
        return r[1:]
    if r.startswith('!'):
        raise MountError(f'mount refused {line!r}: {ERRORS.get(r[1:2], "error " + r[1:])}')
    raise MountError(f'unexpected reply {r!r}')


def exchange(host, port, line, timeout=1.0, tries=3):
    """Send one controller command over UDP, return the reply data. Raises MountError."""
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
        return _parse_reply(line, data)
    raise MountError(f'no reply from the mount at {host}:{port} ({last.__class__.__name__ if last else "timeout"})')


def _close_serial():
    with _lock:
        dev, _ser['dev'], _ser['port'] = _ser['dev'], None, None
    if dev is not None:
        try:
            dev.close()
        except Exception:
            pass


def exchange_serial(path, line, timeout=1.0, tries=3):
    """Send one controller command over the USB-serial adapter (9600 8N1), return the reply data. Raises MountError."""
    if not path:
        raise MountError('serial port not set')
    try:
        import serial
    except ImportError:
        raise MountError('pyserial is not installed (pip install pyserial)')
    msg = (line + '\r').encode()
    last = None
    for _ in range(tries):
        try:
            if _ser['dev'] is None or _ser['port'] != path:
                _close_serial()
                _ser['dev'], _ser['port'] = serial.Serial(path, BAUD, timeout=timeout, write_timeout=timeout), path
            dev = _ser['dev']
            dev.timeout = timeout
            dev.reset_input_buffer()
            dev.write(msg)
            dev.flush()
            data = None
            while True:                    # many adapters echo the command first: skip echo / noise until '=' or '!'
                chunk = dev.read_until(b'\r')
                if not chunk.endswith(b'\r'):
                    break                  # timed out
                if chunk.strip()[:1] in (b'=', b'!'):
                    data = chunk
                    break
        except (serial.SerialException, OSError, ValueError) as e:
            last = e
            _close_serial()                # unplugged / re-enumerated: open it afresh on the next try
            time.sleep(0.2)
            continue
        if data is None:
            last = socket.timeout()
            continue
        return _parse_reply(line, data)
    raise MountError(f'no reply from the mount on {path} ({last.__class__.__name__ if last else "timeout"})')


def _x(s, line, **kw):
    if s.get('transport') == 'serial':
        return exchange_serial(s['serial_port'], line, **kw)
    return exchange(s['host'], s['port'], line, **kw)


def _target(s):
    """Where the mount is reached (for messages and the 'is it configured' check)."""
    return s['serial_port'] if s.get('transport') == 'serial' else s['host']


def _cpr(s):
    k = (s.get('transport'), s['host'], s['port'], s.get('serial_port'))
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


def _ensure_init(s):
    """After a power-up the controller refuses motion until it is initialised (':F1'); status ':f1' digit 3 bit 0 = done."""
    st = _x(s, ':f1')
    if len(st) >= 3 and int(st[2], 16) & 1:
        return
    try:
        _x(s, ':F1')
    except MountError as e:                # some controllers refuse it; a real problem will show up on the move itself
        _log(f'[mount] init command (:F1) refused, carrying on: {e}')


# ---- socket power (Zigbee2MQTT) ---------------------------------------------------------

def _mqtt():
    from bridge import mqtt_bridge
    return mqtt_bridge.mqttc


def _managed(s):
    return s.get('power_mode') == 'zigbee'


def _touch():
    global _activity
    _activity = time.time()


def _on_power_msg(client, userdata, msg):
    """The socket's own state report (Zigbee2MQTT JSON {"state": "ON"} or a bare ON/OFF) -> our idea of its state."""
    try:
        raw = msg.payload.decode().strip()
        try:
            v = json.loads(raw)
            raw = v.get('state') if isinstance(v, dict) else v
        except ValueError:
            pass
        st = str(raw).upper()
    except Exception:
        return
    if st in ('ON', 'OFF'):
        with _lock:
            new = st.lower()
            if _power['state'] != new:
                _power.update(state=new, since=time.time())
                if new == 'off':
                    _link.update(up=None, down_at=None)          # a deliberate switch-off is not a link failure


_watching = {'topic': None}


def watch_power():
    """Subscribe to the socket's state topic (and ask it to report now). Safe to call repeatedly; called again on every
    MQTT reconnect because the subscription does not survive one."""
    s = settings()
    topic = s['power_topic'] if _managed(s) and s['power_topic'] else None
    from bridge import state
    old = _watching['topic']
    if not topic and not old:
        return
    c = _mqtt()
    if old and old != topic:
        try:
            c.message_callback_remove(old)
            c.unsubscribe(old)
        except Exception:
            pass
        _watching['topic'] = None
    if topic and state.mqtt_connected:
        c.message_callback_add(topic, _on_power_msg)
        c.subscribe(topic, qos=1)
        c.publish(topic + '/get', json.dumps({'state': ''}))
        _watching['topic'] = topic


def on_mqtt_connect():
    try:
        watch_power()
    except Exception as e:
        _log(f'[mount] power watch: {e}')
    try:
        from bridge import mount_ha
        mount_ha.on_mqtt_connect()
    except Exception as e:
        _log(f'[mount] Home Assistant entities: {e}')


def _set_power(s, on):
    """Switch the socket (QoS 1, waits for the broker's ack). The state is set optimistically; the socket's own
    report confirms or corrects it."""
    from bridge import state
    if not _managed(s) or not s.get('power_topic'):
        raise MountError('mount power socket topic not set (Zigbee power is selected)')
    if not state.mqtt_connected:
        raise MountError('MQTT broker not connected - cannot switch the mount socket')
    info = _mqtt().publish(s['power_topic'] + '/set', json.dumps({'state': 'ON' if on else 'OFF'}), qos=1)
    if getattr(info, 'rc', 0) != 0:
        raise MountError(f'could not publish to the mount socket (rc={info.rc})')
    try:
        info.wait_for_publish(timeout=3)
    except (ValueError, RuntimeError):
        pass
    if not info.is_published():
        raise MountError('the broker did not acknowledge the mount socket command within 3 s')
    with _lock:
        _power.update(state='on' if on else 'off', since=time.time())
        if not on:
            _link.update(up=None, down_at=None)
    _log(f'[mount] socket {"ON" if on else "OFF"} ({s["power_topic"]})')


def _asleep(s):
    """Managed power and the socket is not known to be on: the mount is not reachable by design - don't poll it."""
    return _managed(s) and _power['state'] != 'on'


def _wake(s, rebase=True):
    """Make sure the mount is powered, answering and initialised; returns fresh settings. Managed power only (otherwise a
    no-op). Right after OUR power-up the count is back at zero: re-pair it with the last known bearing (unless rebase
    is off because the caller is about to set the pointing itself)."""
    if not _managed(s):
        return s
    with _lock:
        _touch()
        if _power['state'] != 'on':
            _set_power(s, True)
        end = time.time() + s['boot_timeout_s']
        while True:
            try:
                _x(s, ':a1', timeout=BOOT_PROBE_S, tries=1)
                break
            except MountError as e:
                if time.time() >= end:
                    raise MountError(f'the mount did not answer {s["boot_timeout_s"]:.0f} s after power-up '
                                     f'(check the socket, the USB adapter and the cable): {e}')
                time.sleep(BOOT_POLL_S)
        _ensure_init(s)
        pos = _pos(s)
        if rebase and s.get('ref_pos') is not None and power_cycled(s, pos):
            _rebase(s, pos, _cpr(s))
            _log('[mount] powered up: position re-paired with the last known bearing')
        _touch()
        return settings()


def idle_check():
    """Background: switch the socket off once the mount has been idle for idle_off_s. Never cuts power unless the
    mount answered and reported it is stopped, and records its final position first. Returns True if it switched off."""
    s = settings()
    if not _managed(s) or _power['state'] != 'on' or time.time() - _activity < s['idle_off_s']:
        return False
    with _lock:
        st = status(s)
        if not st.get('reachable'):
            return False                   # can't tell whether it is still turning: leave the power alone
        if st.get('moving'):
            _touch()
            return False
        _set_power(s, False)
    return True


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


def _supervise(delta, start, cpr, gen):
    """Move guard: while the mount turns, read it every SUPERVISE_S and STOP it if it crosses the travel limit, runs past
    the commanded distance, or turns back on itself (hunting). Also keeps the saved pointing up to date."""
    global _fault
    direction = 1 if delta >= 0 else -1
    want = abs(delta)
    allow = max(round(1.5 * cpr / 360), round(want * 0.1))            # overshoot we tolerate
    back_allow = max(1, round(0.5 * cpr / 360))                       # turning back by more than this = hunting
    peak, bad, last_save = 0, 0, 0.0
    while True:
        time.sleep(SUPERVISE_S)
        reason, running = None, True
        try:
            with _lock:
                if gen != _gen:
                    return                                            # a newer move owns the mount now
                s = settings()
                pos = _pos(s)
                running = _running(s)
                bad = 0
                b = bearing_of(s, pos, cpr)
                _touch()
                lo, hi = s['ref_bearing'] - s['travel_deg'], s['ref_bearing'] + s['travel_deg']
                prog = (pos - start) * direction
                peak = max(peak, prog)
                if b < lo - LIMIT_MARGIN_DEG or b > hi + LIMIT_MARGIN_DEG:
                    reason = f'travel limit exceeded ({b:.1f} deg, allowed {lo:.0f} to {hi:.0f})'
                elif prog - want > allow:
                    reason = f'overshot the target by {(prog - want) * 360.0 / cpr:.1f} deg'
                elif peak >= want - allow and peak - prog > back_allow:
                    reason = 'hunting around the target (turning back on itself)'
                if reason:
                    try:
                        _x(s, ':L1')
                        _wait_stopped(s)
                        pos = _pos(s)
                        b = bearing_of(s, pos, cpr)
                    except MountError:
                        pass
                    _fault = {'ts': time.time(), 'msg': 'mount stopped: ' + reason, 'bearing': round(b % 360.0, 1)}
                    _log(f'[mount] EMERGENCY STOP: {reason}; now at {b % 360.0:.1f} deg')
                now = time.time()
                if reason or not running or now - last_save > 1.0:
                    _remember(pos, b)
                    last_save = now
        except MountError as e:
            bad += 1
            if bad >= 4:
                _log(f'[mount] guard lost the mount while it was moving: {e}')
                return
            continue
        if reason or not running:
            return


def _start_supervisor(delta, start, cpr):
    global _gen, _fault
    _gen += 1
    _fault = None
    if SUPERVISE_ASYNC:
        threading.Thread(target=_supervise, args=(delta, start, cpr, _gen), daemon=True, name='mount-guard').start()
    else:
        _supervise(delta, start, cpr, _gen)


def goto(az, s=None):
    """Slew the antenna to compass bearing `az`. Returns a detail dict."""
    global _last
    s = s or settings()
    with _lock:
        _calibrated(s)
        s = _wake(s)                       # managed power: switch on, wait for boot, re-pair the count
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
            _start_supervisor(delta, pos, cpr)
            res = {'detail': f'mount slewing to {tgt % 360:.1f}° ({delta_deg:+.1f}° turn)', 'moved_deg': round(delta_deg, 1),
                   'actual': round(cur % 360, 1)}
        _touch()
        _last = dict(res, ts=time.time(), target=round(float(az) % 360, 1))
        return res


def nudge(delta_deg, s=None):
    s = s or settings()
    with _lock:
        _calibrated(s)
        s = _wake(s)
        cur = bearing_of(s, _pos(s), _cpr(s))
        return goto((cur + float(_num(delta_deg, 'nudge', -90, 90))) % 360.0, s)


def stop(s=None):
    s = s or settings()
    with _lock:
        if _asleep(s):
            return {'detail': 'mount is switched off - nothing to stop'}
        _x(s, ':L1')
        _touch()
    return {'detail': 'mount stopped'}


def calibrate(bearing=None, s=None):
    """Declare that the antenna points at `bearing` right now (deg true); stores the pairing with the motor count."""
    s = dict(s or settings())
    if bearing is not None:
        s['ref_bearing'] = _num(bearing, 'initial bearing', 0, 360) % 360.0
    with _lock:
        s = dict(_wake(s, rebase=False), ref_bearing=s['ref_bearing'])
        if _running(s):
            raise MountError('the mount is moving - wait for it to stop, then set the pointing')
        _cpr(s)
        pos = _pos(s)
    cur = _saved()
    cur.update(ref_bearing=s['ref_bearing'], ref_pos=pos, last_pos=pos, last_bearing=round(s['ref_bearing'], 3),
               last_unwrapped=round(s['ref_bearing'], 3))
    _write(cur)
    return {'detail': f"mount pointing set to {s['ref_bearing']:.1f}° (motor count {pos})"}


def restore(s=None):
    """After a mount power cycle: assume the antenna is still where it was last seen and re-pair the new count with it
    (the travel-window centre stays where it was)."""
    s = s or settings()
    if s.get('last_bearing') is None or s.get('ref_pos') is None:
        raise MountError('no previous pointing is stored - point the antenna at a known bearing and press "Set current pointing"')
    with _lock:
        s = _wake(s, rebase=False)
        if _running(s):
            raise MountError('the mount is moving - wait for it to stop first')
        ub = _rebase(s, _pos(s), _cpr(s))
    return {'detail': f'restored: mount pointing set to {ub % 360.0:.1f}°'}


def status(s=None, remember=True):
    """Settings plus a live reading (never raises): reachable, actual bearing, moving. Also mirrored to Home Assistant."""
    out = _status(s, remember)
    try:
        from bridge import mount_ha
        mount_ha.publish(out)
    except Exception as e:                       # Home Assistant being unreachable must never break the mount
        _log(f'[mount] Home Assistant publish: {e}')
    return out


def _status(s=None, remember=True):
    s = s or settings()
    out = {'ok': True, 'settings': s, 'calibrated': s.get('ref_pos') is not None, 'reachable': False, 'last': _last,
           'power': _power['state'] if _managed(s) else None, 'fault': _fault}
    if not _target(s):
        out['message'] = 'mount address not set'
        return out
    if _asleep(s):
        # Switched off on purpose: do not poll it (nothing to read) and do not count it as a link failure. The antenna
        # cannot move while unpowered, so the last bearing seen is still the pointing.
        out.update(asleep=True, power=_power['state'] or 'unknown',
                   message='mount is switched off (socket %s); it powers up when a move is commanded' % (_power['state'] or 'state unknown'))
        if s.get('ref_pos') is not None and s.get('last_bearing') is not None:
            ub = _last_unwrapped(s)
            out.update(bearing=round(ub % 360, 1), unwrapped=round(ub, 1),
                       travel_left=round(s['travel_deg'] - abs(ub - s['ref_bearing']), 1))
        out['link'] = link_view()
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
    _note_link(out['reachable'], out.get('message', ''))
    out['link'] = link_view()
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
    """Background task: keep the modelled aim equal to the real bearing (every 30 s, or every 5 s while the
    mount is unreachable so a recovery is noticed quickly). Drop-outs and recoveries are logged by status()."""
    def _loop():
        while True:
            wait = UP_POLL_S
            try:
                s = settings()
                if _target(s):
                    sync_model(status())
                    idle_check()           # managed power: switch the socket off once the mount has been idle
                    if _link['up'] is False:
                        wait = DOWN_POLL_S
                    elif _managed(s) and _power['state'] == 'on':
                        wait = OFF_CHECK_S
            except Exception as e:
                _log(f'[mount] {e}')
            time.sleep(wait)
    threading.Thread(target=_loop, daemon=True, name='mount-sync').start()
