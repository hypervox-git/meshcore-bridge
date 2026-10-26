# Sky-Watcher mount over USB-serial (AllView) and behind a Zigbee2MQTT socket: transport, power-up/re-pair, idle power-off.
import json
import os
import pty
import threading
import types

import pytest

from bridge import antenna, skywatcher as sw, state, steer
from tests.test_skywatcher import CPR, FakeMount


class SerialFakeMount:
    """The same controller behind a pty, so pyserial talks to it like a real USB adapter."""
    reply = FakeMount.reply

    def __init__(self):
        self.pos, self.log, self.err, self.dir, self.inc = 0, [], None, 0, 0
        self.script, self.script_on_go = [], []
        self.powered, self.inited, self.echo = True, True, False
        self.master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        self._slave = slave                                   # keep the slave end open
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        buf = b''
        while True:
            try:
                buf += os.read(self.master, 256)
            except OSError:
                return
            while b'\r' in buf:
                raw, buf = buf.split(b'\r', 1)
                if not self.powered:
                    continue
                line = raw.decode().strip()
                self.log.append(line)
                if self.echo:                                 # half-duplex adapters loop the command back first
                    os.write(self.master, (line + '\r').encode())
                os.write(self.master, (self.reply(line) + '\r').encode())


class FakeMqtt:
    """Stands in for the paho client: a publish to <topic>/set switches the fake mount and the socket 'reports' back."""
    def __init__(self, mount):
        self.mount, self.sent, self.cb = mount, [], {}

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload))
        if topic.endswith('/set'):
            on = json.loads(payload)['state'] == 'ON'
            if on and not self.mount.powered:
                self.mount.pos, self.mount.inited = 0, False           # a cold start: count at zero, not initialised
            self.mount.powered = on
            for f in self.cb.values():
                f(None, None, types.SimpleNamespace(payload=json.dumps({'state': 'ON' if on else 'OFF'}).encode()))
        return types.SimpleNamespace(rc=0, wait_for_publish=lambda timeout=None: None, is_published=lambda: True)

    def message_callback_add(self, t, f): self.cb[t] = f
    def message_callback_remove(self, t): self.cb.pop(t, None)
    def subscribe(self, *a, **k): return (0, 1)
    def unsubscribe(self, *a, **k): return (0, 1)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = {'MOUNT_FILE': str(tmp_path / 'mount.json'), 'STEER_FILE': str(tmp_path / 'steer.json'), 'ANTENNA_FILE': str(tmp_path / 'ant.json')}
    for m in (sw, steer, antenna):
        monkeypatch.setattr(m, '_c', lambda n, d, c=c: c.get(n, d))
    monkeypatch.setattr(sw, '_last', None)
    monkeypatch.setattr(sw, 'BOOT_PROBE_S', 0.05)
    monkeypatch.setattr(sw, 'BOOT_POLL_S', 0.01)
    monkeypatch.setitem(sw._power, 'state', None)
    sw._cpr_cache.clear()
    yield c
    sw._close_serial()


@pytest.fixture
def managed(cfg, monkeypatch):
    """A UDP fake mount behind a fake Zigbee socket, calibrated at 110 deg, socket on."""
    fm = FakeMount()
    mq = FakeMqtt(fm)
    monkeypatch.setattr(sw, '_mqtt', lambda: mq)
    monkeypatch.setattr(state, 'mqtt_connected', True)
    sw.save_settings({'host': '127.0.0.1', 'port': fm.port, 'ref_bearing': 110, 'travel_deg': 180})
    fm.pos = 4000
    sw.calibrate(110)
    sw.save_settings({'power_mode': 'zigbee', 'power_topic': 'zigbee2mqtt/mount_socket', 'idle_off_s': 10})
    sw.watch_power()
    mq.cb['zigbee2mqtt/mount_socket'](None, None, types.SimpleNamespace(payload=b'{"state":"ON"}'))
    fm.mq = mq
    return fm


# ---- serial transport -------------------------------------------------------------------

def test_serial_transport_full_move(cfg):
    fm = SerialFakeMount()
    sw.save_settings({'transport': 'serial', 'serial_port': fm.path, 'ref_bearing': 110})
    fm.pos = 999
    sw.calibrate(110)
    r = sw.goto(130)
    assert r['moved_deg'] == 20.0 and fm.pos - 999 == CPR // 18
    st = sw.status()
    assert st['reachable'] and st['bearing'] == 130.0 and not st['moving']
    assert {c[:3] for c in fm.log if c[1] in 'GHMJL'} == {':L1', ':G1', ':H1', ':M1', ':J1'}


def test_serial_errors(cfg):
    sw.save_settings({'transport': 'serial', 'serial_port': '/dev/does-not-exist'})
    s = sw.status()
    assert s['ok'] and not s['reachable'] and 'no reply' in s['message']
    fm = SerialFakeMount()
    sw.save_settings({'serial_port': fm.path})
    fm.err = 'a'
    with pytest.raises(sw.MountError, match='refused'):
        sw._cpr(sw.settings())


def test_validate_new_settings():
    ok = sw.validate({'transport': 'serial', 'serial_port': '/dev/ttyUSB0', 'power_mode': 'zigbee',
                      'power_topic': '/zigbee2mqtt/mount_socket/', 'boot_timeout_s': 20, 'idle_off_s': 60})
    assert ok['power_topic'] == 'zigbee2mqtt/mount_socket' and ok['transport'] == 'serial'
    for bad in ({'transport': 'carrier-pigeon'}, {'serial_port': '../etc/passwd'}, {'serial_port': 'COM3'},
                {'power_mode': 'maybe'}, {'power_topic': 'a/+/b'}, {'power_topic': 'zigbee2mqtt/x/set'},
                {'power_topic': '$SYS/x'}, {'boot_timeout_s': 1}, {'idle_off_s': 1}):
        with pytest.raises(ValueError):
            sw.validate(bad)


# ---- managed power ----------------------------------------------------------------------

def test_power_message_parsing(cfg):
    for payload, want in ((b'{"state":"OFF","power":0}', 'off'), (b'ON', 'on'), (b'"off"', 'off'), (b'{"state":"ON"}', 'on')):
        sw._on_power_msg(None, None, types.SimpleNamespace(payload=payload))
        assert sw._power['state'] == want
    sw._on_power_msg(None, None, types.SimpleNamespace(payload=b'garbage'))
    assert sw._power['state'] == 'on'                                  # junk changes nothing


def test_idle_switch_off_then_asleep_status_does_not_poll(managed):
    fm = managed
    sw.goto(150)
    assert sw.idle_check() is False                                    # just moved: not idle yet
    sw._activity -= 1000
    assert sw.idle_check() is True and not fm.powered and sw._power['state'] == 'off'
    n = len(fm.log)                                                    # (the idle check read the final position first)
    st = sw.status()
    assert st['asleep'] and st['calibrated'] and st['bearing'] == 150.0 and not st['reachable']
    assert len(fm.log) == n                                            # no traffic to a switched-off mount
    assert sw._link['up'] is None                                      # and a deliberate switch-off is not a link failure
    assert sw.sync_model() is True and antenna.override()['azimuth_deg'] == 150.0
    sw.stop()                                                          # also harmless while off
    assert len(fm.log) == n


def test_no_switch_off_while_moving(managed, monkeypatch):
    sw._activity -= 1000
    monkeypatch.setattr(sw, '_running', lambda s: True)
    assert sw.idle_check() is False and managed.powered


def test_no_switch_off_when_mount_not_answering(managed):
    sw._activity -= 1000
    managed.powered = False                                            # silent for some other reason
    assert sw.idle_check() is False and sw._power['state'] == 'on'


def test_goto_powers_up_repairs_count_and_initialises(managed):
    fm = managed
    sw.goto(140)
    sw._activity -= 1000
    assert sw.idle_check() is True
    fm.mq.sent.clear(); fm.log.clear()
    r = sw.goto(170)                                                   # cold start: count 0, not initialised
    assert fm.powered and fm.mq.sent[0][1] == '{"state": "ON"}'
    assert ':F1' in fm.log and r['actual'] == 140.0 and r['moved_deg'] == 30.0
    assert sw.status()['bearing'] == 170.0 and not sw.status().get('power_cycled')


def test_travel_window_does_not_slide_across_power_cycles(managed):
    fm = managed
    for az in (200, 240, 280, 300, 20, 100):
        sw.goto(az)
        sw._activity -= 1000
        assert sw.idle_check() is True
        assert sw.settings()['ref_bearing'] == 110.0                   # pairing anchor never re-based
        assert sw.settings()['park_bearing'] == 180.0                  # nor the window centre
        assert sw.status()['bearing'] == az % 360
    sw.save_settings({'travel_deg': 60})                               # allowed 120..240 (park 180)
    with pytest.raises(sw.MountError, match='travel limit'):
        sw.goto(300)
    assert fm.powered is True                                          # (it woke to find out; idle task will cut it)


def test_nudge_and_calibrate_wake_the_mount(managed):
    fm = managed
    sw.goto(120)
    sw._activity -= 1000
    sw.idle_check()
    assert not fm.powered
    sw.nudge(10)
    assert fm.powered and sw.status()['bearing'] == 130.0
    sw._activity -= 1000
    sw.idle_check()
    r = sw.calibrate(95)                                               # the user says: it points at 95 now
    assert fm.powered and 'set to 95.0' in r['detail']
    assert sw.settings()['ref_bearing'] == 95.0


def test_power_up_failure_is_reported(managed, monkeypatch):
    fm = managed
    sw._activity -= 1000
    sw.idle_check()
    fm.mq.mount = types.SimpleNamespace(powered=False, pos=0, inited=False)   # socket command goes nowhere
    sw._write(dict(sw._saved(), boot_timeout_s=0.3))
    with pytest.raises(sw.MountError, match='did not answer'):
        sw.goto(150)


def test_broker_down_refuses_to_switch(managed, monkeypatch):
    sw._activity -= 1000
    sw.idle_check()
    monkeypatch.setattr(state, 'mqtt_connected', False)
    with pytest.raises(sw.MountError, match='MQTT broker not connected'):
        sw.goto(150)


def test_steer_and_routes_with_managed_power(managed):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    steer.save_settings({'action': 'skywatcher'})
    sw._activity -= 1000
    sw.idle_check()
    r = steer.steer(130)
    assert managed.powered and 'slewing' in r['detail']
    c = app.test_client()
    d = c.get('/api/mount').get_json()
    assert d['ok'] and d['power'] == 'on' and d['bearing'] == 130.0
    sw._activity -= 1000
    sw.idle_check()
    d = c.get('/api/mount').get_json()
    assert d['ok'] and d['asleep'] and d['bearing'] == 130.0
    assert c.post('/api/mount/stop').get_json()['ok']


def test_init_refusal_is_not_fatal(managed):
    fm = managed
    fm.inited, fm.err = False, 'F'                                     # controller says "not initialised" but refuses :F1
    sw._ensure_init(sw.settings())                                     # must not raise
    with pytest.raises(sw.MountError, match='not initialised'):        # the move then reports the real problem
        sw.goto(150)


def test_serial_echo_is_skipped(cfg):
    fm = SerialFakeMount()
    fm.echo = True
    sw.save_settings({'transport': 'serial', 'serial_port': fm.path, 'ref_bearing': 110})
    fm.pos = 500
    sw.calibrate(110)
    assert sw.goto(120)['moved_deg'] == 10.0 and sw.status()['bearing'] == 120.0
