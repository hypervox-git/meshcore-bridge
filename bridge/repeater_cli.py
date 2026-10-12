# "Repeater" serial mode: the node on SERIAL_PORT runs MeshCore *repeater* firmware, whose
# USB serial is a plain-text CLI (not the companion protocol). We poll it read-only:
#   stats-radio   -> {"noise_floor","last_rssi","last_snr","tx_air_secs","rx_air_secs"}
#   stats-packets -> {"recv","sent","flood_tx","direct_tx","flood_rx","direct_rx","recv_errors"}
#   neighbors     -> repeaters it hears directly ("-none-" if empty)
#   ver
# Nothing here transmits, so it costs no duty cycle. Discovery / traceroute / remote
# polling need a companion node and are not available in this mode.
# Set SERIAL_MODE = "repeater" in config.py to use it.
import json
import re
import threading
import time

from bridge import state
from bridge.extensions import socketio

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


PORT = _c('SERIAL_PORT', '')
BAUD = int(_c('BAUD_RATE', 115200))
POLL = max(float(_c('TEST_LOCAL_POLL', 2.0)), 1.0)
SLOW_EVERY = 5                          # packets/neighbours every Nth radio poll

hooks = []             # callables(snapshot) - run after every poll cycle (test mode)
on_connect = []        # callables() - run after (re)connecting
on_lost = []           # callables() - run when the serial link drops
test_active = threading.Event()   # set by repeater_test: poll fast, include neighbours each cycle
SAMPLE = max(float(_c('REP_TEST_SAMPLE', 1.0)), 0.5)

_lock = threading.Lock()
_ser = None
latest = {'ver': None, 'radio': None, 'packets': None, 'neighbours': None, 'ts': None}
_JSON = re.compile(r'\{.*\}')


def _emit_status():
    socketio.emit('status', {'serial': state.serial_connected, 'mqtt': state.mqtt_connected,
                             'error': state.last_serial_error[0]}, namespace='/')


def _set(ok, reason=None):
    changed = state.serial_connected != ok
    state.serial_connected = ok
    state.last_serial_error[0] = None if ok else reason
    if changed or not ok:
        _emit_status()


def _open():
    import serial                       # pyserial (installed with meshcore)
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = PORT, BAUD, 0.3
    s.dtr = False                       # don't pulse the ESP32 reset line on open
    s.rts = False
    s.open()
    return s


def _cmd(line, wait=3.0, idle=0.5):
    """Send one CLI line, return the reply lines (echo / '->' prefix stripped)."""
    with _lock:
        _ser.reset_input_buffer()
        _ser.write((line + '\r\n').encode())
        end = time.time() + wait
        buf = b''
        last = None
        while time.time() < end:
            chunk = _ser.read(256)
            if chunk:
                buf += chunk
                last = time.time()
            elif last and time.time() - last > idle:
                break
    out = []
    for raw in buf.decode('utf-8', 'replace').replace('\r', '\n').split('\n'):
        t = raw.strip()
        if t.startswith('->'):
            t = t[2:].strip()
        if t and t != line:
            out.append(t)
    return out


def command(line, wait=3.0):
    """Public: run one CLI line on the repeater (raises if not connected)."""
    if _ser is None or not state.serial_connected:
        raise RuntimeError('repeater not connected')
    return _cmd(line, wait=wait)


def _json_of(lines):
    for t in lines:
        m = _JSON.search(t)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return None


def parse_neighbours(lines):
    """Lenient: 'none' -> []; 'a:b:c' style lines -> {id, age, snr, raw}; else raw only."""
    if not lines or any('-none-' in t for t in lines):
        return []
    rows = []
    for t in lines:
        parts = t.split(':')
        if len(parts) >= 3:
            rows.append({'id': parts[0], 'age_s': parts[1], 'snr': parts[2], 'raw': t})
        else:
            rows.append({'raw': t})
    return rows


def snapshot():
    return dict(latest, connected=bool(state.serial_connected), mode='repeater',
                port=PORT, error=state.last_serial_error[0])


def _loop():
    global _ser
    delay, n = 5, 0
    while True:
        try:
            _ser = _open()
            time.sleep(1.5)                                  # let a booting node settle
            latest['ver'] = (_cmd('ver') or [None])[0]
            _set(True)
            print(f"[repeater] connected on {PORT}: {latest['ver']}")
            delay = 5
            for h in on_connect:
                h()
            while True:
                fast = test_active.is_set()
                if fast or n % SLOW_EVERY == 0:
                    # neighbours first, then radio stats: last_rssi then belongs to
                    # (or after) any advert that just refreshed a neighbour entry.
                    latest['neighbours'] = parse_neighbours(_cmd('neighbors'))
                radio = _json_of(_cmd('stats-radio'))
                if radio is None:
                    raise RuntimeError('no reply to stats-radio - is this repeater firmware?')
                latest['radio'], latest['ts'] = radio, time.time()
                if fast or n % SLOW_EVERY == 0:
                    latest['packets'] = _json_of(_cmd('stats-packets')) or latest['packets']
                n += 1
                snap = snapshot()
                socketio.emit('rep_sample', snap, namespace='/')
                socketio.emit('test_local', {'ts': latest['ts'], 'local': radio}, namespace='/')
                for h in hooks:
                    try:
                        h(snap)
                    except Exception as e:
                        print(f"[repeater] hook error: {e}")
                time.sleep(SAMPLE if fast else POLL)
        except Exception as e:
            _set(False, str(e) or e.__class__.__name__)
            print(f"[repeater] {e} - retrying in {delay}s")
            for h in on_lost:
                try:
                    h()
                except Exception:
                    pass
            try:
                if _ser:
                    _ser.close()
            except Exception:
                pass
            _ser = None
            time.sleep(delay)
            delay = min(delay * 2, 60)


def start():
    threading.Thread(target=_loop, daemon=True, name="repeater-cli").start()
