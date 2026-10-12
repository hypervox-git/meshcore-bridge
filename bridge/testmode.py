# Neighbour discovery, live link test (RSSI / noise floor) and traceroute.
#
# Everything here runs as coroutines on the MeshCore asyncio loop (owned by
# meshcore_io); the sync wrappers at the bottom marshal calls in from Flask
# threads. Every transmission goes through the duty-cycle Governor (dutycycle.py)
# so the test features stay inside the legal limit for BOTH our node and the target.
#
# NOTE: like the rest of the adapter, written against the meshcore 2.3.x library
# API and exercised against a mock - NOT yet run on real hardware. Payload field
# names are read defensively; confirm on-air behaviour on your node.
import asyncio
import random
import time

from meshcore import EventType

from config import GATEWAY_LAT, GATEWAY_LON
from bridge import state
from bridge.state import update_node_db
from bridge.extensions import socketio
from bridge import meshcore_io as io
from bridge.dutycycle import Governor, legal_limit_for

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


TEST_DUTY_SHARE = min(max(float(_c('TEST_DUTY_SHARE', 0.25)), 0.01), 0.9)
TEST_MIN_INTERVAL = float(_c('TEST_MIN_INTERVAL', 10))
TEST_LOCAL_POLL = max(float(_c('TEST_LOCAL_POLL', 2.0)), 0.5)
TEST_MAX_MINUTES = float(_c('TEST_MAX_MINUTES', 30))
DISCOVER_MIN_INTERVAL = float(_c('DISCOVER_MIN_INTERVAL', 30))
TRACE_MIN_INTERVAL = float(_c('TRACE_MIN_INTERVAL', 20))
NEIGHBOURS_MAX = int(_c('NEIGHBOURS_MAX', 16))
REPEATER_PASSWORD = _c('REPEATER_PASSWORD', '')
DUTY_CYCLE_LIMIT = _c('DUTY_CYCLE_LIMIT', None)       # None = auto from frequency
MAX_STATUS_FAILS = 5

TYPE_NAMES = {1: 'client', 2: 'repeater', 3: 'room', 4: 'sensor'}
TYPE_BITS = {'client': 1, 'repeater': 2, 'room': 3, 'sensor': 4}
STATUS_TYPES = (2, 3, 4)               # node types that answer a status request

_gov = None
_gov_key = None
_session = None
_busy = {'discover': False, 'trace': False, 'neighbours': False}
_last = {'discover': 0.0, 'trace': 0.0}


# ---- helpers -----------------------------------------------------------------

def _emit(event, data):
    socketio.emit(event, data, namespace='/')


def _require_connected():
    if io.mc is None or not state.serial_connected:
        why = state.last_serial_error[0]
        raise RuntimeError('MeshCore node not connected' + (f': {why}' if why else
                           ' (still connecting - see the service log if this persists)'))


def radio_params():
    si = getattr(io.mc, 'self_info', None) or {}
    return (si.get('radio_freq') or 869.618, si.get('radio_bw') or 62.5,
            si.get('radio_sf') or 8, si.get('radio_cr') or 5)


def governor():
    """Governor for the node's CURRENT radio settings (ledger kept while unchanged)."""
    global _gov, _gov_key
    key = radio_params()
    if _gov is None or key != _gov_key:
        freq, bw, sf, cr = key
        limit = float(DUTY_CYCLE_LIMIT) if DUTY_CYCLE_LIMIT else legal_limit_for(freq)
        _gov = Governor(limit, TEST_DUTY_SHARE, sf, bw, cr)
        _gov_key = key
    return _gov


def contacts():
    c = getattr(io.mc, 'contacts', {}) or {}
    return list(c.values()) if isinstance(c, dict) else list(c)


def node_id(contact):
    return '!' + str(contact.get('public_key', ''))[:8]


def _loc(contact):
    try:
        lat, lon = float(contact.get('adv_lat') or 0), float(contact.get('adv_lon') or 0)
    except (TypeError, ValueError):
        return None
    return (lat, lon) if (lat or lon) else None


def resolve_contact(target):
    """Node id ('!1a2b3c4d'), hex public-key prefix, or exact contact name -> contact."""
    t = (target or '').strip()
    if not t:
        return None
    hexs = (t[1:] if t.startswith('!') else t).lower()
    if len(hexs) >= 2 and all(ch in '0123456789abcdef' for ch in hexs):
        for c in contacts():
            if str(c.get('public_key', '')).lower().startswith(hexs):
                return c
    for c in contacts():
        if c.get('adv_name') == t:
            return c
    return None


def _describe(c):
    loc = _loc(c)
    return {'id': node_id(c), 'name': c.get('adv_name') or node_id(c),
            'type': TYPE_NAMES.get(c.get('type'), str(c.get('type'))),
            'lat': loc[0] if loc else None, 'lon': loc[1] if loc else None}


def list_targets():
    out = [_describe(c) for c in contacts()]
    out.sort(key=lambda d: (d['type'] != 'repeater', d['name'].lower()))
    return out


def _too_soon(kind, minimum):
    wait = _last[kind] + minimum - time.monotonic()
    return wait if wait > 0 else 0


async def _login(contact, password, gov):
    """Log in to a repeater/room (needed before it answers status/neighbour requests)."""
    cost_l, cost_t = gov.airtime('login_req'), gov.airtime('login_resp')
    wait = gov.wait_for(cost_l, node_id(contact), cost_t)
    if wait > 0:
        raise RuntimeError(_budget_error(wait)['error'])
    gov.spend(cost_l, node_id(contact), cost_t)
    ev = await io.mc.commands.send_login_sync(contact, password)
    return ev is not None and ev.type == EventType.LOGIN_SUCCESS


def _budget_error(wait):
    """Error dict for a failed budget check; wait=inf means one request can never fit."""
    if wait == float('inf'):
        return {'ok': False, 'error': 'duty-cycle budget is too small for even one request at these radio '
                'settings - raise TEST_DUTY_SHARE or check DUTY_CYCLE_LIMIT in config.py'}
    return {'ok': False, 'error': f'duty-cycle budget exhausted, retry in {wait:.0f}s', 'retry_in': round(wait)}


def _pw(password):
    return password if password else REPEATER_PASSWORD


# ---- neighbour discovery (zero-hop) --------------------------------------------

async def discover_neighbors(types=None, wait_s=8):
    _require_connected()
    if _busy['discover']:
        return {'ok': False, 'error': 'discovery already running'}
    soon = _too_soon('discover', DISCOVER_MIN_INTERVAL)
    if soon:
        return {'ok': False, 'error': f'please wait {soon:.0f}s (rate limit)', 'retry_in': round(soon)}
    names = types or ['repeater', 'room', 'sensor', 'client']
    mask = 0
    for n in names:
        if n in TYPE_BITS:
            mask |= 1 << TYPE_BITS[n]
    if not mask:
        return {'ok': False, 'error': 'no node types selected'}
    wait_s = min(max(float(wait_s), 3), 20)

    gov = governor()
    cost = gov.airtime('discover_req')
    wait = gov.wait_for(cost)
    if wait > 0:
        return _budget_error(wait)

    _busy['discover'] = True
    tag = random.randint(1, 0xFFFFFFFF)
    found = {}

    def on_resp(ev):
        p = ev.payload or {}
        pk = str(p.get('pubkey', ''))
        if pk and (pk not in found or (p.get('RSSI') or -999) > (found[pk].get('RSSI') or -999)):
            found[pk] = p

    sub = io.mc.subscribe(EventType.DISCOVER_RESPONSE, on_resp,
                          attribute_filters={'tag': tag.to_bytes(4, 'little').hex()})
    try:
        gov.spend(cost)
        ev = await io.mc.commands.send_node_discover_req(mask, prefix_only=True, tag=tag)
        if ev is None or ev.type == EventType.ERROR:
            return {'ok': False, 'error': f'node rejected discover request: {getattr(ev, "payload", "")}'}
        _last['discover'] = time.monotonic()
        await asyncio.sleep(wait_s)
    finally:
        io.mc.unsubscribe(sub)
        _busy['discover'] = False

    neighbours = []
    for pk, p in found.items():
        c = resolve_contact(pk)
        nid = '!' + pk[:8]
        ntype = TYPE_NAMES.get(p.get('node_type'), str(p.get('node_type')))
        loc = _loc(c) if c else None
        entry = {'id': nid, 'pubkey': pk, 'name': (c or {}).get('adv_name'), 'type': ntype,
                 'rssi': p.get('RSSI'), 'snr': p.get('SNR'), 'snr_in': p.get('SNR_in'),
                 'known': c is not None,
                 'lat': loc[0] if loc else None, 'lon': loc[1] if loc else None}
        neighbours.append(entry)
        upd = {'hw': ntype}
        if entry['name']:
            upd['name'] = entry['name']
        if entry['snr'] is not None:
            upd['snr'] = entry['snr']
        if entry['rssi'] is not None:
            upd['rssi'] = entry['rssi']
        update_node_db(nid, upd)
    neighbours.sort(key=lambda n: n['rssi'] if n['rssi'] is not None else -999, reverse=True)

    result = {'ok': True, 'ts': time.time(), 'wait_s': wait_s, 'types': names,
              'neighbours': neighbours, 'duty': gov.snapshot()}
    _emit('neighbors', result)
    return result


async def repeater_neighbours(target, password=''):
    """A repeater's own neighbour table (needs login; costs a longer reply)."""
    _require_connected()
    if _busy['neighbours']:
        return {'ok': False, 'error': 'neighbour request already running'}
    c = resolve_contact(target)
    if c is None:
        return {'ok': False, 'error': f'unknown target {target!r}'}
    if c.get('type') != 2:
        return {'ok': False, 'error': 'neighbour tables are only available from repeaters'}
    gov = governor()
    key = node_id(c)
    cost_l, cost_t = gov.airtime('neighbours_req'), gov.airtime('neighbours_resp')
    _busy['neighbours'] = True
    try:
        pw = _pw(password)
        if pw and not await _login(c, pw, gov):
            return {'ok': False, 'error': 'login failed (wrong password?)'}
        wait = gov.wait_for(cost_l, key, cost_t)
        if wait > 0:
            return _budget_error(wait)
        gov.spend(cost_l, key, cost_t)
        res = await io.mc.commands.req_neighbours_sync(c, count=NEIGHBOURS_MAX,
                                                       pubkey_prefix_length=4, order_by=0)
    finally:
        _busy['neighbours'] = False
    if not res:
        return {'ok': False, 'error': 'no reply (repeater may need a login, or is out of range)'}
    rows = []
    for n in res.get('neighbours', []):
        nc = resolve_contact(n.get('pubkey', ''))
        rows.append({'pubkey': n.get('pubkey'), 'id': '!' + str(n.get('pubkey', ''))[:8],
                     'name': (nc or {}).get('adv_name'), 'secs_ago': n.get('secs_ago'),
                     'snr': n.get('snr'), 'known': nc is not None})
    rows.sort(key=lambda r: r['secs_ago'] if r['secs_ago'] is not None else 1 << 30)
    out = {'ok': True, 'repeater': _describe(c), 'total': res.get('neighbours_count'),
           'shown': len(rows), 'neighbours': rows, 'duty': gov.snapshot(key)}
    _emit('repeater_neighbours', out)
    return out


# ---- live link test (RSSI + noise floor) --------------------------------------

class LinkTest:
    def __init__(self, contact, password):
        self.contact = contact
        self.key = node_id(contact)
        self.password = password          # memory only - never logged, emitted or stored
        self.stop = False
        self.reason = None
        self.started = time.time()
        self.deadline = time.monotonic() + TEST_MAX_MINUTES * 60
        self.seq = 0
        self.fails = 0
        self.task = None

    def info(self, running=True):
        return {'running': running, 'target': _describe(self.contact),
                'started': self.started, 'max_minutes': TEST_MAX_MINUTES,
                'reason': self.reason}


async def _local_radio():
    ev = await io.mc.commands.get_stats_radio()
    if ev is not None and ev.type == EventType.STATS_RADIO:
        return ev.payload or {}
    return None


def _local_view(r):
    if not r:
        return None
    nf, rssi = r.get('noise_floor'), r.get('last_rssi')
    return {'noise_floor': nf, 'last_rssi': rssi, 'last_snr': r.get('last_snr'),
            'margin_db': (rssi - nf) if (nf is not None and rssi is not None) else None,
            'tx_air_secs': r.get('tx_air_secs'), 'rx_air_secs': r.get('rx_air_secs')}


def _remote_view(st):
    if not st:
        return None
    nf, rssi = st.get('noise_floor'), st.get('last_rssi')
    return {'noise_floor': nf, 'last_rssi': rssi, 'last_snr': st.get('last_snr'),
            'margin_db': (rssi - nf) if (nf is not None and rssi is not None) else None,
            'tx_queue_len': st.get('tx_queue_len'), 'airtime': st.get('airtime'),
            'rx_airtime': st.get('rx_airtime'), 'uptime': st.get('uptime'),
            'recv_errors': st.get('recv_errors')}


async def _link_loop(sess):
    global _session
    gov = governor()
    c, key = sess.contact, sess.key
    cost_l, cost_t = gov.airtime('status_req'), gov.airtime('status_resp')
    interval = max(gov.min_interval_s(cost_l, cost_t), TEST_MIN_INTERVAL)
    _emit('test_state', dict(sess.info(), interval_s=round(interval, 1), duty=gov.snapshot(key)))
    try:
        pw = _pw(sess.password)
        if pw:
            if not await _login(c, pw, gov):
                sess.reason = 'login failed (wrong password?)'
                return
        next_remote = 0.0
        while not sess.stop:
            now = time.monotonic()
            if now >= sess.deadline:
                sess.reason = f'auto-stopped after {TEST_MAX_MINUTES:g} min'
                return
            if not state.serial_connected:
                sess.reason = 'node disconnected'
                return
            local = _local_view(await _local_radio())         # serial only - no airtime
            if now >= next_remote:
                wait = gov.wait_for(cost_l, key, cost_t)
                if wait == float('inf'):
                    sess.reason = _budget_error(wait)['error']
                    return
                if wait > 0:
                    next_remote = now + wait
                    _emit('test_wait', {'wait_s': round(wait), 'duty': gov.snapshot(key)})
                else:
                    gov.spend(cost_l, key, cost_t)             # the TX happens even if no reply
                    st = await io.mc.commands.req_status_sync(c)
                    local = _local_view(await _local_radio()) or local   # reply's RSSI/SNR at us
                    sess.seq += 1
                    sess.fails = 0 if st else sess.fails + 1
                    if local and local.get('last_rssi') is not None:
                        upd = {'rssi': local['last_rssi']}
                        if local.get('last_snr') is not None:
                            upd['snr'] = local['last_snr']
                        update_node_db(key, upd)
                    _emit('test_sample', {
                        'seq': sess.seq, 'ts': time.time(), 'target': key, 'timeout': not st,
                        'remote': _remote_view(st), 'local': local,
                        'next_in': round(interval, 1), 'duty': gov.snapshot(key)})
                    if sess.fails >= MAX_STATUS_FAILS:
                        sess.reason = f'{MAX_STATUS_FAILS} requests in a row got no reply'
                        return
                    next_remote = time.monotonic() + interval
            else:
                _emit('test_local', {'ts': time.time(), 'local': local})
            await asyncio.sleep(TEST_LOCAL_POLL)
    except asyncio.CancelledError:
        sess.reason = sess.reason or 'stopped'
        raise
    except Exception as e:                                       # keep the loop's error visible
        sess.reason = f'error: {e}'
    finally:
        sess.password = None
        if _session is sess:
            _session = None
        _emit('test_state', dict(sess.info(running=False), duty=gov.snapshot(key)))


async def _start_link_test(target, password):
    global _session
    _require_connected()
    if _session is not None:
        return {'ok': False, 'error': 'a link test is already running - stop it first'}
    c = resolve_contact(target)
    if c is None:
        return {'ok': False, 'error': f'unknown target {target!r}'}
    if c.get('type') not in STATUS_TYPES:
        return {'ok': False, 'error': 'link test needs a repeater, room server or sensor '
                '(plain clients do not answer status requests) - use Neighbours/Traceroute for those'}
    sess = LinkTest(c, password)
    _session = sess
    sess.task = asyncio.get_running_loop().create_task(_link_loop(sess))
    return {'ok': True, 'state': sess.info()}


async def _stop_link_test():
    sess = _session
    if sess is None:
        return {'ok': True, 'state': {'running': False}}
    sess.stop = True
    sess.reason = sess.reason or 'stopped by user'
    try:
        await asyncio.wait_for(asyncio.shield(sess.task), timeout=15)
    except Exception:
        sess.task.cancel()
    return {'ok': True, 'state': {'running': False, 'reason': sess.reason}}


# ---- traceroute ----------------------------------------------------------------

def _split_path(hexstr, hash_len):
    step = hash_len * 2
    return [hexstr[i:i + step] for i in range(0, len(hexstr), step)] if hexstr else []


def _resolve_hop(h, prev):
    cands = [c for c in contacts()
             if c.get('type') == 2 and str(c.get('public_key', '')).lower().startswith(h.lower())]
    if not cands:
        return {'hash': h, 'resolved': False}

    def dist(c):
        loc = _loc(c)
        if not loc or not prev:
            return 1e9 if not loc else 1e8
        return (loc[0] - prev[0]) ** 2 + (loc[1] - prev[1]) ** 2

    cands.sort(key=dist)
    best = cands[0]
    d = _describe(best)
    d.update({'hash': h, 'resolved': True,
              'ambiguous': [node_id(c) for c in cands[1:]] or None})
    return d


def _collector(event_type, filters):
    fut = asyncio.get_running_loop().create_future()

    def cb(ev):
        if not fut.done():
            fut.set_result(ev)

    return fut, io.mc.subscribe(event_type, cb, attribute_filters=filters)


async def run_traceroute(target):
    _require_connected()
    if _busy['trace']:
        return {'ok': False, 'error': 'traceroute already running'}
    soon = _too_soon('trace', TRACE_MIN_INTERVAL)
    if soon:
        return {'ok': False, 'error': f'please wait {soon:.0f}s (rate limit)', 'retry_in': round(soon)}
    c = resolve_contact(target)
    if c is None:
        return {'ok': False, 'error': f'unknown target {target!r}'}

    gov = governor()
    key = node_id(c)
    est_hops = max(int(c.get('out_path_len') or 0), 2)
    cost = gov.airtime('path_disc_req', est_hops) + gov.airtime('trace', est_hops * 2)
    wait = gov.wait_for(cost)
    if wait > 0:
        return _budget_error(wait)

    _busy['trace'] = True
    sub = None
    try:
        # 1) learn the route: flood path-discovery, falling back to the cached route
        gov.spend(gov.airtime('path_disc_req', est_hops))
        pev = await io.mc.commands.send_path_discovery_sync(c)
        pp = pev.payload if pev is not None and pev.type == EventType.PATH_RESPONSE else None
        if pp:
            hash_len = int(pp.get('out_path_hash_len') or 1)
            out = _split_path(pp.get('out_path', ''), hash_len)
            source = 'discovered'
        elif int(c.get('out_path_len') or 0) > 0:
            hash_len = int(c.get('out_path_hash_mode', 0)) + 1
            out = _split_path(c.get('out_path', ''), hash_len)
            source = 'cached'
        else:
            hash_len, out, source = 1, [], 'none'
        if hash_len not in (1, 2, 4, 8):
            return {'ok': False, 'error': f'unsupported path hash size {hash_len}'}

        # 2) resolve each hop hash to a known repeater (+ position for the map)
        prev = (GATEWAY_LAT, GATEWAY_LON)
        hops = []
        for h in out:
            d = _resolve_hop(h, prev)
            hops.append(d)
            if d.get('lat') is not None:
                prev = (d['lat'], d['lon'])
        target_info = _describe(c)

        # 3) round-trip trace so the result returns to us: out, [target], back
        is_rep = c.get('type') == 2
        tgt_hash = str(c.get('public_key', ''))[:hash_len * 2]
        fwd = out + ([tgt_hash] if is_rep else [])
        back = list(reversed(out if is_rep else out[:-1]))
        path = fwd + back
        if not path:
            return {'ok': False, 'ts': time.time(), 'path_source': source,
                    'error': 'target is zero-hop (no repeaters on the route) - nothing to trace; '
                             'use Neighbours or Link test for direct links'}
        path_str = ','.join(path)
        tag = random.randint(1, 0xFFFFFFFF)
        fut, sub = _collector(EventType.TRACE_DATA, {'tag': tag})
        gov.spend(gov.airtime('trace', len(path)))
        t0 = time.monotonic()
        sent = await io.mc.commands.send_trace(tag=tag, path=path_str)
        if sent is None or sent.type == EventType.ERROR:
            return {'ok': False, 'error': f'node rejected trace: {getattr(sent, "payload", "")}'}
        _last['trace'] = time.monotonic()
        to = min(max(float((sent.payload or {}).get('suggested_timeout', 8000)) / 800.0, 6), 30)
        try:
            tev = await asyncio.wait_for(fut, timeout=to)
        except asyncio.TimeoutError:
            tev = None
        rtt_ms = round((time.monotonic() - t0) * 1000)
    finally:
        if sub is not None:
            io.mc.unsubscribe(sub)
        _busy['trace'] = False

    base = {'ts': time.time(), 'target': target_info, 'path_source': source,
            'trace_path': path_str, 'hash_len': hash_len, 'hops': hops,
            'scope': 'full path to target repeater and back' if is_rep
                     else 'to the last repeater before the target and back (clients cannot forward traces)',
            'duty': gov.snapshot(key)}
    if tev is None:
        base.update({'ok': False, 'error': 'no trace reply (a repeater on the path may be out of range '
                                           'or the cached route is stale)'})
        _emit('trace_result', base)
        return base

    tp = (tev.payload or {}).get('path', []) or []
    snrs = [n.get('snr') for n in tp[:-1]]
    final_snr = tp[-1].get('snr') if tp else None
    n_out = len(out)
    legs = []
    for i, h in enumerate(path):
        if i < n_out:
            node, leg = hops[i], 'out'
        elif is_rep and i == n_out:
            node, leg = target_info, 'target'
        else:
            j = len(path) - 1 - i if not is_rep else len(path) - 1 - i
            node, leg = hops[j] if 0 <= j < n_out else {'hash': h, 'resolved': False}, 'back'
        legs.append({'hash': h, 'leg': leg, 'snr': snrs[i] if i < len(snrs) else None,
                     'name': node.get('name'), 'id': node.get('id'), 'resolved': node.get('resolved', True)})

    # map polyline: gateway -> hops -> target, outbound SNR on each point
    points = [{'label': 'Gateway', 'kind': 'gateway', 'lat': GATEWAY_LAT, 'lon': GATEWAY_LON, 'snr': None}]
    for i, d in enumerate(hops):
        points.append({'label': d.get('name') or d['hash'], 'kind': 'repeater', 'hash': d['hash'],
                       'id': d.get('id'), 'lat': d.get('lat'), 'lon': d.get('lon'),
                       'snr': snrs[i] if i < len(snrs) else None,
                       'snr_back': (legs[len(path) - 1 - i]['snr']
                                    if (is_rep or i < n_out - 1) and len(path) - 1 - i < len(legs) else None),
                       'resolved': d.get('resolved', False), 'ambiguous': d.get('ambiguous')})
    tgt_snr = snrs[n_out] if is_rep and n_out < len(snrs) else None
    points.append({'label': target_info['name'], 'kind': 'target', 'id': target_info['id'],
                   'lat': target_info['lat'], 'lon': target_info['lon'], 'snr': tgt_snr})

    base.update({'ok': True, 'rtt_ms': rtt_ms, 'legs': legs, 'final_snr': final_snr, 'points': points})
    _emit('trace_result', base)
    return base


# ---- status + sync wrappers (called from Flask threads) -----------------------

def status():
    gov = None
    freq = bw = sf = cr = None
    try:
        if io.mc is not None:
            freq, bw, sf, cr = radio_params()
            gov = governor()
    except Exception:
        pass
    sess = _session
    return {'connected': bool(state.serial_connected and io.mc is not None),
            'radio': {'freq': freq, 'bw': bw, 'sf': sf, 'cr': cr},
            'duty': gov.snapshot(sess.key if sess else None) if gov else None,
            'link_test': sess.info() if sess else {'running': False},
            'limits': {'share': TEST_DUTY_SHARE, 'min_interval_s': TEST_MIN_INTERVAL,
                       'max_minutes': TEST_MAX_MINUTES}}


def _call(make_coro, timeout):
    """Marshal a coroutine onto the MeshCore loop (checked first so no coroutine leaks)."""
    if io._loop is None:
        raise RuntimeError('MeshCore loop not started')
    return io._run(make_coro(), timeout=timeout)


def discover(types=None, wait_s=8):
    return _call(lambda: discover_neighbors(types, wait_s), float(wait_s) + 25)


def neighbours_of(target, password=''):
    return _call(lambda: repeater_neighbours(target, password), 120)


def traceroute(target):
    return _call(lambda: run_traceroute(target), 120)


def start_link_test(target, password=''):
    return _call(lambda: _start_link_test(target, password), 30)


def stop_link_test():
    return _call(_stop_link_test, 30)
