import os
import time
import pytest
from bridge import repeater_test as rt, repeater_cli as cli, state


class FakeRep:
    def __init__(self, supports_repeat=True):
        self.repeat, self.supports, self.log = True, supports_repeat, []
    def __call__(self, line, wait=3.0):
        self.log.append(line)
        if line == 'get repeat':
            return ['> ' + ('on' if self.repeat else 'off')] if self.supports else ['Unknown command']
        if line.startswith('set repeat'):
            if not self.supports:
                return ['Unknown command']
            self.repeat = line.endswith('on'); return ['OK']
        if line == 'get tx': return ['> 20']
        if line == 'get radio': return ['> 869.618,62.5,8,8']
        return ['?']


@pytest.fixture
def rep(monkeypatch, tmp_path):
    f = FakeRep()
    monkeypatch.setattr(cli, 'command', f)
    monkeypatch.setattr(rt, '_marker', lambda: str(tmp_path / 'm.json'))
    monkeypatch.setattr(rt.socketio, 'emit', lambda *a, **k: None)
    state.serial_connected = True
    rt._t = None; rt._stop.clear(); cli.test_active.clear()
    yield f
    rt._finish('cleanup'); time.sleep(0.2)


def wait_for(cond, t=5):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


def test_timeout_restores_repeater(rep):
    rt.start(minutes=1, offline=True)
    assert rep.repeat is False and os.path.exists(rt._marker())
    rt._t['deadline'] = time.time() + 0.1            # fast-forward the configured timer
    assert wait_for(lambda: not rt._t['running'])
    assert wait_for(lambda: rep.repeat is True and not os.path.exists(rt._marker()))
    assert 'timed out' in rt._t['reason']


def test_stop_restores(rep):
    rt.start(minutes=5, offline=True)
    rt.stop()
    assert wait_for(lambda: rep.repeat is True and not os.path.exists(rt._marker()))


def test_minutes_clamped_and_default(rep, monkeypatch):
    monkeypatch.setattr(rt, '_minutes_max', lambda: 15)
    rt.start(minutes=999, offline=False)
    assert rt._t['minutes'] == 15
    assert rep.repeat is True and not os.path.exists(rt._marker())   # observe-only never touches repeat


def test_stale_marker_is_recovered(rep):
    rep.repeat = False
    rt._write_marker(True)
    rt.recover()
    assert wait_for(lambda: rep.repeat is True and not os.path.exists(rt._marker()))


def test_unsupported_firmware_refuses_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'command', FakeRep(supports_repeat=False))
    monkeypatch.setattr(rt, '_marker', lambda: str(tmp_path / 'm.json'))
    monkeypatch.setattr(rt.socketio, 'emit', lambda *a, **k: None)
    state.serial_connected = True; rt._t = None
    with pytest.raises(RuntimeError, match='get repeat'):
        rt.start(minutes=1, offline=True)
    assert not os.path.exists(rt._marker())


def test_serial_loss_ends_and_restores(rep):
    rt.start(minutes=5, offline=True)
    rt._on_lost()
    assert not rt._t['running'] and 'serial' in rt._t['reason']
    assert wait_for(lambda: rep.repeat is True)


def test_neighbour_pathloss_and_packets(rep):
    rt.start(minutes=5, offline=False, assumed_tx_dbm=20)
    snap = lambda age, recv, rssi: {'radio': {'last_rssi': rssi, 'last_snr': 5.0, 'noise_floor': -115},
                                    'packets': {'recv': recv},
                                    'neighbours': [{'id': 'AABBCCDD', 'age_s': str(age), 'snr': '22'}]}
    rt.on_sample(snap(2000, 10, -100))               # stale at start: no RSSI claimed
    assert rt.view()['neighbours'][0]['rssi'] is None
    rt.on_sample(snap(1, 11, -97))                   # advert just heard
    n = rt.view()['neighbours'][0]
    assert n['rssi'] == -97 and n['pathloss_db'] == 117.0 and n['snr'] == 5.5 and n['heard'] == 1
    assert rt.view()['packets_heard'] == 1 and rt.view()['noise']['avg'] == -115


def test_discover_at_start_supported(rep, monkeypatch):
    base = rep.__call__
    air = iter([19, 19.4])
    def cmd(line, wait=3.0):
        if line == 'discover.neighbors': rep.log.append(line); return ['OK']
        if line == 'stats-radio': return ['{"tx_air_secs":%s}' % next(air)]
        return base(line, wait)
    monkeypatch.setattr(cli, 'command', cmd)
    monkeypatch.setattr(rt.time, 'sleep', lambda s: None)
    rt._last_discover = 0.0
    rt.start(minutes=5, offline=False)
    d = rt.view()['discover']
    assert d['supported'] and d['tx_air_s'] == 0.4 and 'discover.neighbors' in rep.log


def test_discover_unsupported_falls_back(rep):
    rt._last_discover = 0.0
    rt.start(minutes=5, offline=False)                # FakeRep answers '?' - patch to unknown
    assert rt.view()['discover'] is not None


def test_discover_unknown_command(rep, monkeypatch):
    base = rep.__call__
    monkeypatch.setattr(cli, 'command', lambda l, wait=3.0: ['Unknown command'] if l == 'discover.neighbors' else base(l, wait))
    rt._last_discover = 0.0
    rt.start(minutes=5, offline=False)
    d = rt.view()['discover']
    assert d['supported'] is False
    assert rt._t['running']                           # test still runs, passively
    with pytest.raises(RuntimeError, match='rate-limited'):
        rt.discover()
