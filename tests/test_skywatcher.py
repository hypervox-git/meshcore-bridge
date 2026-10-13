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
        if c == 'j': return '=' + sw.hex24(self.pos + sw.HOME)
        if c == 'f': return '=101'
        if c == 'G': self.dir = int(line[4]); return '='
        if c == 'H': self.inc = sw.parse24(line[3:9]); return '='
        if c == 'J': self.pos += -self.inc if self.dir else self.inc; return '='
        return '='

    def run(self):
        while True:
            data, addr = self.sock.recvfrom(256)
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
    assert antenna.override()['azimuth_deg'] == 130
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
