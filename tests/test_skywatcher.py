# Sky-Watcher mount: protocol helpers, bearing maths, and the whole command sequence against a fake UDP controller.
import socket
import threading

import pytest

from bridge import antenna, skywatcher as sw, state, steer

CPR = 3600000


class FakeMount:
    """Minimal motor-controller emulator: instant goto, records every command."""
    def __init__(self):
        self.pos, self.log, self.err = 0, [], None
        self.script, self.script_on_go = [], []          # scripted positions returned by :j1 after :J1 (move-guard tests)
        self.powered, self.inited = True, True          # unpowered: no reply at all; uninitialised: refuses to move
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.dir, self.inc = 0, 0
        threading.Thread(target=self.run, daemon=True).start()

    def reply(self, line):
        c = line[1]
        if self.err == c:
            return '!0'
        if c == 'a': return '=' + sw.hex24(CPR)
        if c == 'j':
            if self.script: self.pos = self.script.pop(0)
            return '=' + sw.hex24(self.pos + sw.HOME)
        if c == 'L': self.script = []; return '='
        if c == 'f': return '=1' + ('1' if self.script else '0') + ('1' if self.inited else '0')
        if c == 'F': self.inited = True; return '='
        if c == 'G': self.dir = int(line[4]); return '='
        if c == 'H': self.inc = sw.parse24(line[3:9]); return '='
        if c == 'J':
            if not self.inited: return '!4'
            if self.script_on_go: self.script = list(self.script_on_go)
            else: self.pos += -self.inc if self.dir else self.inc
            return '='
        return '='

    def run(self):
        while True:
            data, addr = self.sock.recvfrom(256)
            if not self.powered:
                continue
            line = data.decode().strip()
            self.log.append(line)
            self.sock.sendto((self.reply(line) + '\r').encode(), addr)


@pytest.fixture
def mount(tmp_path, monkeypatch):
    cfg = {'MOUNT_FILE': str(tmp_path / 'mount.json'), 'STEER_FILE': str(tmp_path / 'steer.json'), 'ANTENNA_FILE': str(tmp_path / 'ant.json')}
    for m in (sw, steer, antenna):
        monkeypatch.setattr(m, '_c', lambda n, d, cfg=cfg: cfg.get(n, d))
    monkeypatch.setattr(sw, '_last', None)
    sw._cpr_cache.clear()
    fm = FakeMount()
    sw.save_settings({'host': '127.0.0.1', 'port': fm.port, 'ref_bearing': 110, 'travel_deg': 180})
    return fm


def test_hex24_roundtrip():
    assert sw.hex24(0x563412) == '123456' and sw.parse24('123456') == 0x563412
    assert sw.parse24(sw.hex24(sw.HOME)) == sw.HOME
    with pytest.raises(sw.MountError):
        sw.parse24('zz')


def test_validate():
    assert sw.validate({'host': '192.168.1.60', 'ref_bearing': 360})['ref_bearing'] == 0
    for bad in ({'host': 'a b'}, {'host': 'x;rm'}, {'port': 0}, {'travel_deg': 5}, {'ref_bearing': 'x'}):
        with pytest.raises(ValueError):
            sw.validate(bad)


def test_choose_target_keeps_inside_travel():
    s = dict(sw.DEFAULTS, ref_bearing=110.0, travel_deg=90.0)             # allowed 20..200
    assert sw.choose_target(s, 190, 110) == 190
    assert sw.choose_target(s, 30, 190) == 30                             # in range directly
    with pytest.raises(sw.MountError):
        sw.choose_target(s, 300, 110)                                     # 300 deg is outside 20..200
    s['travel_deg'] = 180.0                                               # full turn: always reachable
    assert sw.choose_target(s, 300, 110) in (300, -60)


def test_goto_needs_calibration(mount):
    with pytest.raises(sw.MountError, match='calibrated'):
        sw.goto(120)


def test_calibrate_then_goto_sequence(mount):
    mount.pos = 12345
    sw.calibrate(110)
    assert sw.settings()['ref_pos'] == 12345
    r = sw.goto(120)                                                       # +10 deg
    assert r['moved_deg'] == 10.0
    assert mount.pos - 12345 == CPR // 36
    cmds = [c[:3] for c in mount.log if c[1] in 'GHMJL']
    assert cmds == [':L1', ':G1', ':H1', ':M1', ':J1']
    assert sw.status()['bearing'] == 120.0


def test_goto_direction_and_invert(mount):
    mount.pos = 0
    sw.calibrate(110)
    sw.goto(100)
    assert mount.pos == -CPR // 36 and any(c == ':G101' for c in mount.log)
    sw.save_settings({'invert': True})
    sw.calibrate(110)                                                      # same count, now inverted sense
    p0 = mount.pos
    sw.goto(120)
    assert mount.pos < p0


def test_deadband_and_stop(mount):
    sw.calibrate(110)
    mount.log.clear()
    assert sw.goto(110.1)['moved_deg'] == 0.0 and not any(c[1] == 'J' for c in mount.log)
    sw.stop()
    assert ':L1' in mount.log


def test_goto_refuses_outside_limit(mount):
    sw.save_settings({'travel_deg': 60})
    sw.calibrate(110)
    with pytest.raises(sw.MountError, match='travel limit'):
        sw.goto(250)


def test_mount_error_and_unreachable(mount):
    mount.err = 'a'
    with pytest.raises(sw.MountError, match='refused'):
        sw._cpr(sw.settings())
    sw.save_settings({'host': '127.0.0.1', 'port': 9})                     # nothing listening
    s = sw.status()
    assert s['ok'] and not s['reachable'] and 'no reply' in s['message']


def test_steer_action_uses_mount(mount, monkeypatch):
    sw.calibrate(110)
    monkeypatch.setattr(state, 'mqtt_connected', False)
    steer.save_settings({'action': 'skywatcher', 'also_model': True})
    r = steer.steer(130)
    assert r['action'] == 'skywatcher' and 'slewing' in r['detail']
    # the modelled aim must NOT jump to the target at command time; it follows the mount's real bearing
    assert (antenna.override() or {}).get('azimuth_deg') != 130
    assert sw.sync_model() is True and antenna.override()['azimuth_deg'] == 130      # fake mount arrives instantly
    mount.err = 'j'
    with pytest.raises(RuntimeError, match='refused'):
        steer.steer(150)


def test_mount_routes(mount):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    c = app.test_client()
    d = c.get('/api/mount').get_json()
    assert d['ok'] and d['reachable'] and not d['calibrated']
    r = c.post('/api/mount/nudge', json={'delta': 10}).get_json()
    assert r['ok'] is False and 'calibrated' in r['error']
    r = c.post('/api/mount/calibrate', json={'settings': {'ref_bearing': 95}}).get_json()
    assert r['ok'] and r['calibrated'] and r['bearing'] == 95.0
    r = c.post('/api/mount/nudge', json={'delta': -10}).get_json()
    assert r['ok'] and r['bearing'] == 85.0
    assert c.post('/api/mount/nudge', json={'delta': 500}).get_json()['ok'] is False
    assert c.post('/api/mount/settings', json={'host': 'bad host'}).get_json()['ok'] is False
    assert c.post('/api/mount/stop').get_json()['ok']


def test_position_survives_bridge_restart(mount):
    mount.pos = 777
    sw.calibrate(110)
    sw.goto(150)
    sw.status()                                           # a poll records where it is
    saved = sw._saved()
    assert saved['last_pos'] == mount.pos and saved['last_bearing'] == 150.0 and saved['ref_pos'] == 777
    sw._cpr_cache.clear()                                 # "restart": all in-memory state gone, only the file remains
    st = sw.status()
    assert st['calibrated'] and st['bearing'] == 150.0 and not st.get('power_cycled')


def test_power_cycle_detected_then_restored(mount):
    mount.pos = 5000
    sw.calibrate(110)
    sw.goto(160)
    sw.status()
    mount.pos = 0                                         # mount switched off and on: count back at zero
    st = sw.status()
    assert st['power_cycled'] and not st['calibrated'] and st.get('bearing') is None
    with pytest.raises(sw.MountError, match='power-cycled'):
        sw.goto(120)
    r = sw.restore()
    assert 'restored' in r['detail']
    st = sw.status()
    assert not st.get('power_cycled') and st['bearing'] == 160.0       # assumed unmoved: same bearing, new count
    sw.goto(170)
    assert sw.status()['bearing'] == 170.0


def test_model_aim_follows_mount_and_persists(mount):
    sw.calibrate(110)
    assert sw.sync_model() is True and antenna.override()['azimuth_deg'] == 110.0
    assert sw.sync_model() is False                                    # already equal: no rewrite
    sw.nudge(25)                                                       # not via the steer action
    assert sw.sync_model() is True and antenna.override()['azimuth_deg'] == 135.0


def test_restore_without_history(mount):
    with pytest.raises(sw.MountError, match='no previous'):
        sw.restore()


def test_restore_route_and_sync_in_routes(mount):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    c = app.test_client()
    c.post('/api/mount/calibrate', json={'settings': {'ref_bearing': 90}})
    assert antenna.override()['azimuth_deg'] == 90.0                   # route result also synced the model
    c.post('/api/mount/nudge', json={'delta': 15})
    assert c.get('/api/mount').get_json()['bearing'] == 105.0 and antenna.override()['azimuth_deg'] == 105.0
    mount.pos = 0
    d = c.get('/api/mount').get_json()
    assert d['power_cycled'] and c.post('/api/mount/nudge', json={'delta': 5}).get_json()['ok'] is False
    assert c.post('/api/mount/restore').get_json()['ok']


def test_route_does_not_persist_aim_while_moving(mount, monkeypatch):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    c = app.test_client()
    c.post('/api/mount/calibrate', json={'settings': {'ref_bearing': 100}})
    saved = []
    monkeypatch.setattr(sw, 'sync_model', lambda st=None: saved.append(1) or False)
    monkeypatch.setattr(sw, '_running', lambda s: True)                # mount reports "running"
    d = c.get('/api/mount').get_json()
    assert d['moving'] is True and saved == []                         # no file write per poll while it turns
    monkeypatch.setattr(sw, '_running', lambda s: False)
    c.get('/api/mount')
    assert saved == [1]


def _deg(d):                                    # fake mount: counts for d degrees (CPR = 3.6M -> 10000 per degree)
    return int(d * CPR / 360)


def test_guard_stops_at_travel_limit(mount):
    mount.pos = 0
    sw.save_settings({'travel_deg': 60})                                  # allowed bearings 50..170
    sw.calibrate(110)
    mount.script_on_go = [_deg(20), _deg(45), _deg(58.0), _deg(60.8), _deg(63)]      # 170.8 is past 170 + 0.5
    sw.goto(168)                                                           # legal target, but the mount runs on
    st = sw.status()
    assert ':L1' in mount.log[-6:] and st['fault'] and 'travel limit' in st['fault']['msg']
    assert st['bearing'] == 110 + 60.8 and not mount.script               # stopped at the reading that tripped it


def test_guard_stops_hunting(mount):
    sw.calibrate(110)
    mount.script_on_go = [_deg(8), _deg(16), _deg(20.4), _deg(21.0), _deg(19.8), _deg(21.5)]
    sw.goto(130)                                                           # +20 deg
    f = sw.status()['fault']
    assert f and 'hunting' in f['msg'] and not mount.script
    assert sw._saved()['last_bearing'] == pytest.approx(129.8, abs=0.01)  # the pointing at the stop is what is remembered


def test_guard_clean_move_has_no_fault_and_saves_pointing(mount):
    sw.calibrate(110)
    mount.script_on_go = [_deg(8), _deg(16), _deg(20.06)]
    sw.goto(130)
    st = sw.status()
    assert st['fault'] is None and st['bearing'] == 130.1                       # (status rounds to 0.1)
    assert sw._saved()['last_unwrapped'] == pytest.approx(130.06, abs=0.01)
    mount.script_on_go = []
    sw.goto(131)                                                           # next move clears / starts clean
    assert sw.status()['fault'] is None


def test_rotator_page_and_goto_route(mount):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    c = app.test_client()
    page = c.get('/rotator')
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'id="stop"' in html and 'viewport-fit=cover' in html
    for url in ('/api/mount/goto', '/api/mount/nudge', '/api/mount/calibrate', '/api/mount/restore', '/api/mount/stop'):
        assert url in html
    r = c.post('/api/mount/goto', json={'azimuth': 120}).get_json()
    assert r['ok'] is False and 'calibrated' in r['error']
    assert c.post('/api/mount/calibrate', json={'bearing': 95}).get_json()['ok']
    r = c.post('/api/mount/goto', json={'azimuth': 120}).get_json()
    assert r['ok'] and r['bearing'] == 120.0
    for bad in ({}, {'azimuth': 'x'}, {'azimuth': 361}, {'azimuth': -1}):
        assert c.post('/api/mount/goto', json=bad).get_json()['ok'] is False


class _HAClient:
    """Fake paho client: records publishes, subscriptions and callbacks."""
    def __init__(self):
        self.sent, self.subs, self.cb = [], [], {}

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.sent.append((topic, payload, retain))

    def subscribe(self, t, qos=0): self.subs.append(t)
    def message_callback_add(self, t, f): self.cb[t] = f


@pytest.fixture
def ha(mount, monkeypatch):
    from bridge import mount_ha
    c = _HAClient()
    monkeypatch.setattr(sw, '_mqtt', lambda: c)
    monkeypatch.setattr(state, 'mqtt_connected', True)
    monkeypatch.setattr(mount_ha, 'MIN_GAP_S', 0)
    monkeypatch.setattr(mount_ha, '_run', lambda fn, what: fn(sw))          # commands run inline in tests
    mount_ha._last.update(payload=None, ts=0.0)
    return c


def _msg(payload):
    import types
    return types.SimpleNamespace(payload=str(payload).encode())


def test_ha_discovery_and_subscriptions(ha):
    import json
    from bridge import mount_ha
    mount_ha.on_mqtt_connect()
    topics = {t for t, _p, _r in ha.sent}
    for obj in ('sensor/meshcore_mount_bearing', 'sensor/meshcore_mount_state', 'sensor/meshcore_mount_travel_left',
                'binary_sensor/meshcore_mount_moving', 'number/meshcore_mount_goto', 'button/meshcore_mount_stop',
                'button/meshcore_mount_nudge_m5', 'button/meshcore_mount_nudge_p1'):
        assert 'homeassistant/' + obj + '/config' in topics
    assert all(r for _t, _p, r in ha.sent)                                   # discovery is retained
    cfg = json.loads(next(p for t, p, _r in ha.sent if t.endswith('meshcore_mount_goto/config')))
    assert cfg['command_topic'] == mount_ha.ROOT + '/goto' and cfg['min'] == 0 and cfg['max'] == 360
    assert sorted(ha.subs) == sorted(mount_ha.ROOT + '/' + n for n in ('goto', 'nudge', 'stop'))


def test_ha_state_published_on_change(ha):
    import json
    from bridge import mount_ha
    sw.status()
    assert json.loads(ha.sent[-1][1])['state'] == 'not calibrated' and ha.sent[-1][2] is True
    sw.calibrate(95)
    sw.status()
    v = json.loads(ha.sent[-1][1])
    assert v['state'] == 'stopped' and v['bearing'] == 95.0 and v['moving'] is False and v['travel_left'] == 180.0
    n = len(ha.sent)
    sw.status()                                                              # unchanged: nothing new inside the refresh window
    assert len(ha.sent) == n
    assert mount_ha.view({'message': 'mount address not set'}) is None


def test_ha_commands_move_the_mount(ha):
    import json
    from bridge import mount_ha
    mount_ha.on_mqtt_connect()
    sw.calibrate(95)
    ha.cb[mount_ha.ROOT + '/goto'](None, None, _msg('120'))
    assert sw.status()['bearing'] == 120.0
    ha.cb[mount_ha.ROOT + '/nudge'](None, None, _msg('-5'))
    assert sw.status()['bearing'] == 115.0
    ha.cb[mount_ha.ROOT + '/stop'](None, None, _msg('STOP'))
    assert json.loads(ha.sent[-1][1])['bearing'] == 115.0
    before = sw.status()['bearing']
    for bad in ('x', '361', '-1', ''):                                       # rejected without touching the mount
        ha.cb[mount_ha.ROOT + '/goto'](None, None, _msg(bad))
    ha.cb[mount_ha.ROOT + '/nudge'](None, None, _msg('120'))
    assert sw.status()['bearing'] == before


def test_ha_publish_failure_never_breaks_status(ha, monkeypatch):
    def boom(*a, **k): raise RuntimeError('broker gone')
    monkeypatch.setattr(ha, 'publish', boom)
    assert sw.status()['ok'] is True
