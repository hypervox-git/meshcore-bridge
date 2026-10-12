import asyncio
from types import SimpleNamespace as NS

import pytest
from meshcore import EventType

from bridge import meshcore_io as io
from bridge import state, testmode
from bridge.dutycycle import Governor
from bridge.state import node_db


def pk(prefix):
    return (prefix + '0' * 64)[:64]


def contact(prefix, name, typ, lat=0.0, lon=0.0, out_path='', out_len=0):
    return {'public_key': pk(prefix), 'adv_name': name, 'type': typ, 'adv_lat': lat, 'adv_lon': lon,
            'out_path': out_path, 'out_path_len': out_len, 'out_path_hash_mode': 0}


class FakeCommands:
    def __init__(self, mc):
        self.mc = mc
        self.status = {'noise_floor': -112, 'last_rssi': -97, 'last_snr': 6.5, 'tx_queue_len': 0,
                       'airtime': 12, 'rx_airtime': 30, 'uptime': 999, 'recv_errors': 0}
        self.status_ok = True
        self.logins = []
        self.login_ok = True
        self.path = {'out_path': 'aabb', 'out_path_hash_len': 1, 'out_path_len': 2,
                     'in_path': 'bbaa', 'in_path_hash_len': 1, 'in_path_len': 2}
        self.trace_snrs = None
        self.sent_trace = None
        self.discover_replies = []

    async def get_stats_radio(self):
        return NS(type=EventType.STATS_RADIO, payload={'noise_floor': -110, 'last_rssi': -92,
                                                       'last_snr': 7.0, 'tx_air_secs': 5, 'rx_air_secs': 50})

    async def req_status_sync(self, c, timeout=0, min_timeout=0):
        return dict(self.status) if self.status_ok else None

    async def send_login_sync(self, c, pwd, timeout=0, min_timeout=0):
        self.logins.append(pwd)
        return NS(type=EventType.LOGIN_SUCCESS, payload={}) if self.login_ok else None

    async def send_node_discover_req(self, mask, prefix_only=True, tag=None, since=None):
        self.mask = mask
        tag_hex = tag.to_bytes(4, 'little').hex()
        for p in self.discover_replies:
            self.mc.dispatch(EventType.DISCOVER_RESPONSE, dict(p, tag=tag_hex), {'tag': tag_hex})
        return NS(type=EventType.OK, payload={'tag': tag})

    async def send_path_discovery_sync(self, c, timeout=0, min_timeout=0):
        return NS(type=EventType.PATH_RESPONSE, payload=self.path) if self.path else None

    async def send_trace(self, auth_code=0, tag=None, flags=None, path=None):
        self.sent_trace = path
        hashes = path.split(',')
        snrs = self.trace_snrs or [5.0 - i for i in range(len(hashes))]
        payload = {'tag': tag, 'path_len': len(hashes),
                   'path': [{'hash': h, 'snr': s} for h, s in zip(hashes, snrs)] + [{'snr': 4.25}]}
        if self.trace_snrs != 'none':
            self.mc.dispatch(EventType.TRACE_DATA, payload, {'tag': tag})
        return NS(type=EventType.MSG_SENT, payload={'suggested_timeout': 800})

    async def req_neighbours_sync(self, c, count=255, offset=0, order_by=0, pubkey_prefix_length=4,
                                  timeout=0, min_timeout=0):
        return {'neighbours_count': 3, 'results_count': 2, 'neighbours': [
            {'pubkey': 'aa000000', 'secs_ago': 40, 'snr': 8.0},
            {'pubkey': 'ee000000', 'secs_ago': 5, 'snr': -3.0}]}


class FakeMC:
    def __init__(self):
        self.self_info = {'radio_freq': 869.618, 'radio_bw': 62.5, 'radio_sf': 8, 'radio_cr': 5}
        self.contacts = {}
        self.commands = FakeCommands(self)
        self.subs = []

    def add(self, c):
        self.contacts[c['public_key']] = c

    def subscribe(self, etype, cb, attribute_filters=None):
        s = NS(etype=etype, cb=cb, filters=attribute_filters or {})
        self.subs.append(s)
        return s

    def unsubscribe(self, s):
        self.subs.remove(s)

    def dispatch(self, etype, payload, attrs):
        ev = NS(type=etype, payload=payload, attributes=attrs)
        for s in list(self.subs):
            if s.etype == etype and all(attrs.get(k) == v for k, v in s.filters.items()):
                s.cb(ev)


@pytest.fixture
def env(monkeypatch):
    mc = FakeMC()
    emitted = []
    monkeypatch.setattr(io, 'mc', mc)
    monkeypatch.setattr(state, 'serial_connected', True)
    monkeypatch.setattr(testmode.socketio, 'emit', lambda ev, data=None, **kw: emitted.append((ev, data)))
    monkeypatch.setattr(testmode, '_gov', None)
    monkeypatch.setattr(testmode, '_session', None)
    monkeypatch.setitem(testmode._last, 'discover', 0.0)
    monkeypatch.setitem(testmode._last, 'trace', 0.0)
    node_db.clear()
    mc.add(contact('aa', 'Hill-RPT', 2, 51.10, -2.80))
    mc.add(contact('bb', 'Ridge-RPT', 2, 51.20, -2.70))
    mc.add(contact('cc', 'Target-RPT', 2, 51.30, -2.60))
    mc.add(contact('dd', 'Mark-Phone', 1))
    return mc, emitted


def run(coro):
    return asyncio.run(coro)


# ---- discovery -------------------------------------------------------------

def test_discover_collects_dedupes_and_rate_limits(env, monkeypatch):
    mc, emitted = env
    tag_replies = [
        {'pubkey': 'aa' + '0' * 14, 'node_type': 2, 'RSSI': -101, 'SNR': 3.0, 'SNR_in': 4.0},
        {'pubkey': 'aa' + '0' * 14, 'node_type': 2, 'RSSI': -90, 'SNR': 9.0, 'SNR_in': 4.0},   # better dup
        {'pubkey': 'ff' + '0' * 14, 'node_type': 2, 'RSSI': -118, 'SNR': -6.0, 'SNR_in': -4.5},  # unknown
    ]
    mc.commands.discover_replies = tag_replies
    r = run(testmode.discover_neighbors(['repeater'], wait_s=3))
    assert r['ok'] and mc.commands.mask == 1 << 2
    ids = [n['id'] for n in r['neighbours']]
    assert ids == ['!aa000000', '!ff000000']                 # sorted by RSSI, duplicate collapsed
    best = r['neighbours'][0]
    assert best['rssi'] == -90 and best['name'] == 'Hill-RPT' and best['known']
    assert not r['neighbours'][1]['known']
    assert node_db['!aa000000']['rssi'] == -90 and node_db['!aa000000']['name'] == 'Hill-RPT'
    assert any(e[0] == 'neighbors' for e in emitted)
    assert mc.subs == []                                      # subscription cleaned up
    again = run(testmode.discover_neighbors(['repeater'], wait_s=3))
    assert not again['ok'] and 'wait' in again['error']      # rate-limited


def test_discover_needs_a_type(env):
    r = run(testmode.discover_neighbors(['bogus']))
    assert not r['ok']


# ---- live link test --------------------------------------------------------

def fast(monkeypatch, interval=0.05):
    monkeypatch.setattr(testmode, 'TEST_LOCAL_POLL', 0.01)
    monkeypatch.setattr(Governor, 'min_interval_s', lambda self, a, b=0.0: interval)
    monkeypatch.setattr(testmode, 'TEST_MIN_INTERVAL', interval)


async def _wait_for(pred, timeout=3):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def test_link_test_samples_both_ends_and_stops(env, monkeypatch):
    mc, emitted = env
    fast(monkeypatch)

    async def go():
        r = await testmode._start_link_test('!aa000000', 'secret')
        assert r['ok']
        assert await _wait_for(lambda: sum(e[0] == 'test_sample' for e in emitted) >= 2)
        s = testmode.status()
        assert s['link_test']['running'] and s['duty']['target_used_s'] > 0
        stop = await testmode._stop_link_test()
        assert stop['ok']
        return r

    run(go())
    samples = [d for e, d in emitted if e == 'test_sample']
    s = samples[0]
    assert s['remote']['noise_floor'] == -112 and s['remote']['last_rssi'] == -97
    assert s['remote']['margin_db'] == 15
    assert s['local']['noise_floor'] == -110 and s['local']['last_rssi'] == -92
    assert mc.commands.logins == ['secret']                   # logged in once with the supplied password
    assert node_db['!aa000000']['rssi'] == -92
    states = [d for e, d in emitted if e == 'test_state']
    assert states[0]['running'] and not states[-1]['running']
    assert testmode._session is None
    # the password is never emitted to the browser
    assert 'secret' not in repr(emitted)


def test_link_test_rejects_clients_and_second_session(env, monkeypatch):
    mc, _ = env
    fast(monkeypatch)

    async def go():
        bad = await testmode._start_link_test('Mark-Phone', '')
        assert not bad['ok'] and 'repeater' in bad['error']
        unk = await testmode._start_link_test('nope', '')
        assert not unk['ok']
        assert (await testmode._start_link_test('!aa000000', ''))['ok']
        dup = await testmode._start_link_test('!bb000000', '')
        assert not dup['ok'] and 'already' in dup['error']
        await testmode._stop_link_test()

    run(go())


def test_link_test_gives_up_after_repeated_silence(env, monkeypatch):
    mc, emitted = env
    fast(monkeypatch, 0.02)
    mc.commands.status_ok = False

    async def go():
        await testmode._start_link_test('!aa000000', '')
        assert await _wait_for(lambda: testmode._session is None, timeout=5)

    run(go())
    last = [d for e, d in emitted if e == 'test_state'][-1]
    assert not last['running'] and 'no reply' in last['reason']
    assert sum(e == 'test_sample' for e, _ in emitted) == testmode.MAX_STATUS_FAILS


def test_link_test_login_failure_stops(env, monkeypatch):
    mc, emitted = env
    fast(monkeypatch)
    mc.commands.login_ok = False

    async def go():
        await testmode._start_link_test('!aa000000', 'bad')
        assert await _wait_for(lambda: testmode._session is None)

    run(go())
    last = [d for e, d in emitted if e == 'test_state'][-1]
    assert 'login failed' in last['reason']
    assert not any(e == 'test_sample' for e, _ in emitted)


def test_link_test_stops_if_budget_cannot_fit_one_request(env, monkeypatch):
    mc, emitted = env
    fast(monkeypatch)
    monkeypatch.setattr(testmode, 'TEST_DUTY_SHARE', 0.0001)   # budget far below one status reply

    async def go():
        await testmode._start_link_test('!aa000000', '')
        assert await _wait_for(lambda: testmode._session is None)

    run(go())
    last = [d for e, d in emitted if e == 'test_state'][-1]
    assert not last['running'] and 'too small' in last['reason']
    assert not any(e == 'test_sample' for e, _ in emitted)    # never transmitted


def test_link_test_pauses_then_resumes_when_window_is_full(env, monkeypatch):
    mc, emitted = env
    fast(monkeypatch)

    async def go():
        gov = testmode.governor()
        # fill our ledger so the next request must wait for old airtime to expire
        gov.local.spend(gov.budget_s, now=__import__('time').monotonic() - gov.window_s + 0.3)
        await testmode._start_link_test('!aa000000', '')
        assert await _wait_for(lambda: any(e == 'test_wait' for e, _ in emitted))
        assert await _wait_for(lambda: any(e == 'test_sample' for e, _ in emitted), timeout=5)
        await testmode._stop_link_test()

    run(go())
    waits = [d for e, d in emitted if e == 'test_wait']
    assert waits and waits[0]['wait_s'] >= 0


# ---- traceroute ------------------------------------------------------------

def test_traceroute_repeater_target_round_trip(env):
    mc, emitted = env
    mc.commands.trace_snrs = [8.0, 6.0, 4.0, 5.0, 9.0]
    r = run(testmode.run_traceroute('Target-RPT'))
    assert r['ok'], r
    assert mc.commands.sent_trace == 'aa,bb,cc,bb,aa'         # out, target, back
    assert [h['name'] for h in r['hops']] == ['Hill-RPT', 'Ridge-RPT']
    assert [l['leg'] for l in r['legs']] == ['out', 'out', 'target', 'back', 'back']
    assert [l['snr'] for l in r['legs']] == [8.0, 6.0, 4.0, 5.0, 9.0]
    assert r['final_snr'] == 4.25 and r['path_source'] == 'discovered'
    pts = r['points']
    assert [p['kind'] for p in pts] == ['gateway', 'repeater', 'repeater', 'target']
    assert (pts[0]['lat'], pts[0]['lon']) == (51.0, -2.9)
    assert pts[1]['lat'] == 51.10 and pts[1]['snr'] == 8.0 and pts[1]['snr_back'] == 9.0
    assert pts[3]['lat'] == 51.30 and pts[3]['snr'] == 4.0
    assert any(e == 'trace_result' for e, _ in emitted)
    assert mc.subs == []


def test_traceroute_client_target_stops_at_last_repeater(env):
    mc, _ = env
    r = run(testmode.run_traceroute('Mark-Phone'))
    assert r['ok']
    assert mc.commands.sent_trace == 'aa,bb,aa'
    assert 'last repeater' in r['scope']
    assert r['points'][-1]['kind'] == 'target' and r['points'][-1]['lat'] is None   # no position known
    assert r['points'][2]['snr_back'] is None or r['points'][2]['hash'] == 'bb'


def test_traceroute_unresolved_hop_and_cached_fallback(env):
    mc, _ = env
    mc.commands.path = None                                   # discovery times out
    mc.contacts[pk('cc')]['out_path'] = 'aa77'                # cached route via an unknown repeater 77
    mc.contacts[pk('cc')]['out_path_len'] = 2
    r = run(testmode.run_traceroute('!cc000000'))
    assert r['ok'] and r['path_source'] == 'cached'
    unresolved = [p for p in r['points'] if p['kind'] == 'repeater' and p['lat'] is None]
    assert len(unresolved) == 1 and unresolved[0]['hash'] == '77'


def test_traceroute_zero_hop_and_timeout_and_busy_flags(env):
    mc, emitted = env
    mc.commands.path = None
    r = run(testmode.run_traceroute('Mark-Phone'))              # direct client: nothing to forward
    assert not r['ok'] and 'zero-hop' in r['error']
    assert not any(testmode._busy.values())
    testmode._last['trace'] = 0.0
    r = run(testmode.run_traceroute('Target-RPT'))              # direct repeater: traces just that link
    assert r['ok'] and mc.commands.sent_trace == 'cc'
    testmode._last['trace'] = 0.0
    mc.commands.path = {'out_path': 'aa', 'out_path_hash_len': 1}
    mc.commands.trace_snrs = 'none'                           # trace reply never arrives
    r = run(testmode.run_traceroute('Target-RPT'))
    assert not r['ok'] and 'no trace reply' in r['error']
    assert r['trace_path'] == 'aa,cc,aa'
    assert not any(testmode._busy.values())


def test_ambiguous_hash_prefers_nearest_known_repeater(env):
    mc, _ = env
    mc.add(contact('aa11', 'Far-Twin', 2, 55.0, 5.0))        # same 1-byte hash, far away
    r = run(testmode.run_traceroute('Target-RPT'))
    first = r['hops'][0]
    assert first['name'] == 'Hill-RPT' and first['ambiguous'] == ['!aa110000']


# ---- repeater neighbour table + routes ------------------------------------

def test_repeater_neighbours_resolves_names(env):
    r = run(testmode.repeater_neighbours('!aa000000', 'pw'))
    assert r['ok'] and r['total'] == 3 and r['shown'] == 2
    assert [n['id'] for n in r['neighbours']] == ['!ee000000', '!aa000000']   # most recent first
    assert r['neighbours'][1]['name'] == 'Hill-RPT' and not r['neighbours'][0]['known']
    assert not run(testmode.repeater_neighbours('Mark-Phone', ''))['ok']      # clients have no table


def test_routes_register_and_degrade_cleanly():
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ('/api/test/status', '/api/test/targets', '/api/neighbors/discover', '/api/neighbors/remote',
                 '/api/test/link/start', '/api/test/link/stop', '/api/test/trace'):
        assert path in rules
    c = app.test_client()
    page = c.get('/').get_data(as_text=True)
    assert 'page-neighbors' in page and 'page-test' in page and '/static/test.js' in page
    r = c.post('/api/neighbors/discover', json={}).get_json()
    assert r['ok'] is False and 'loop' in r['error']          # loop not started in tests -> clean JSON error
    assert c.get('/api/test/status').get_json()['connected'] is False
