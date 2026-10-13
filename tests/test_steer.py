# Antenna steering: settings validation, payload templating, the model / MQTT actions, and the routes.
import json

import pytest

from bridge import antenna, state, steer


class FakeInfo:
    def __init__(self, rc=0, published=True):
        self.rc, self._pub = rc, published

    def wait_for_publish(self, timeout=None):
        pass

    def is_published(self):
        return self._pub


class FakeClient:
    def __init__(self, **kw):
        self.calls, self.kw = [], kw

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append((topic, payload, qos, retain))
        return FakeInfo(**self.kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = {'STEER_FILE': str(tmp_path / 'steer.json'), 'ANTENNA_FILE': str(tmp_path / 'ant.json'), 'MQTT_TOPIC': 'meshcore'}
    for m in (steer, antenna):
        monkeypatch.setattr(m, '_c', lambda n, d, cfg=cfg: cfg.get(n, d))
    from bridge import mqtt_bridge
    fake = FakeClient()
    monkeypatch.setattr(mqtt_bridge, 'mqttc', fake)
    monkeypatch.setattr(state, 'mqtt_connected', True)
    monkeypatch.setattr(steer, '_last', None)
    return fake


def test_defaults_and_validation(env):
    s = steer.settings()
    assert s['action'] == 'model' and s['topic'] == 'meshcore/antenna/azimuth/set' and s['payload'] == '{azimuth}'
    for bad in ({'action': 'nope'}, {'topic': 'a/+/b'}, {'topic': 'a/#'}, {'topic': '$SYS/x'}, {'payload': ''},
                {'payload': 'x' * 301}, {'qos': 2}, {'qos': 'x'}):
        with pytest.raises(ValueError):
            steer.validate(bad)
    assert steer.validate({'action': 'mqtt', 'topic': ' ant/az ', 'qos': '1', 'retain': 1}) == \
        {'action': 'mqtt', 'topic': 'ant/az', 'qos': 1, 'retain': True}


def test_save_settings_roundtrip_and_partial(env):
    steer.save_settings({'action': 'mqtt', 'topic': 'ant/az', 'payload': '{azimuth_int}'})
    steer.save_settings({'qos': 1})                                     # partial update keeps the rest
    s = steer.settings()
    assert (s['action'], s['topic'], s['payload'], s['qos']) == ('mqtt', 'ant/az', '{azimuth_int}', 1)


def test_payload_template():
    r = steer.render_payload
    assert r('{azimuth}', 123.04) == '123.0' and r('{azimuth_int}', 359.6) == '0' and r('{azimuth_int}', 122.5) in ('122', '123')
    out = r('{"az": {azimuth}, "to": "{target}"}', 110.0, 'He said "hi"\n\x00')       # JSON template, hostile name
    assert json.loads(out) == {'az': 110.0, 'to': 'He said "hi"'}
    assert r('{azimuth}', 5, 'x' * 100) == '5.0'                                       # target only used if asked for


def test_steer_model_action(env):
    res = steer.steer(470, 'Foo')
    assert res['azimuth'] == 110.0 and res['action'] == 'model'
    assert antenna.override()['azimuth_deg'] == 110.0
    assert env.calls == []                                                             # nothing published
    assert steer.status()['last']['ok'] is True


def test_steer_mqtt_publishes_and_updates_model(env):
    steer.save_settings({'action': 'mqtt', 'topic': 'ant/dir/az', 'payload': '{azimuth_int}', 'qos': 1, 'retain': True})
    res = steer.steer(200.4, 'Hill')
    assert env.calls == [('ant/dir/az', '200', 1, True)]
    assert res['topic'] == 'ant/dir/az' and res['payload'] == '200'
    assert antenna.override()['azimuth_deg'] == 200.4                                  # also_model defaults on
    steer.save_settings({'also_model': False})
    steer.steer(10)
    assert env.calls[-1][1] == '10' and antenna.override()['azimuth_deg'] == 200.4     # model untouched


def test_steer_mqtt_failures_publish_nothing_and_leave_model(env, monkeypatch):
    steer.save_settings({'action': 'mqtt', 'topic': 'ant/az'})
    monkeypatch.setattr(state, 'mqtt_connected', False)
    with pytest.raises(RuntimeError, match='not connected'):
        steer.steer(90)
    assert env.calls == [] and antenna.override() is None and steer.status()['last']['ok'] is False
    monkeypatch.setattr(state, 'mqtt_connected', True)
    env.kw = {'published': False}
    steer.save_settings({'qos': 1})
    with pytest.raises(RuntimeError, match='acknowledge'):
        steer.steer(90)
    assert antenna.override() is None                                                  # unacked -> model not moved
    env.kw = {'rc': 4}
    with pytest.raises(RuntimeError, match='rc=4'):
        steer.steer(90)
    steer.save_settings({'topic': ''})
    with pytest.raises(ValueError, match='empty'):
        steer.steer(90)


def test_steer_rejects_bad_azimuth(env):
    for bad in (None, 'abc', float('nan'), float('inf')):
        with pytest.raises(ValueError):
            steer.steer(bad)
    with pytest.raises(ValueError):
        steer.steer(10, action='rm -rf')


def test_routes(env):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    c = app.test_client()
    d = c.get('/api/steer').get_json()
    assert d['ok'] and {a['id'] for a in d['actions']} == {'model', 'mqtt'} and d['mqtt_connected'] is True
    r = c.post('/api/steer/settings', json={'action': 'mqtt', 'topic': 'bad/#'}).get_json()
    assert r['ok'] is False and 'wildcard' in r['error']
    # the form's settings travel with the command and are what get used
    r = c.post('/api/steer', json={'azimuth': 95, 'target': 'Hill', 'settings': {'action': 'mqtt', 'topic': 'ant/az', 'payload': '{azimuth}'}}).get_json()
    assert r['ok'] and env.calls == [('ant/az', '95.0', 0, False)] and 'antenna' in r and antenna.override()['azimuth_deg'] == 95.0
    assert c.post('/api/steer', json={'azimuth': 'x'}).get_json()['ok'] is False
    assert c.get('/api/steer').get_json()['last']['ok'] is True
