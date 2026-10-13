# MQTT monitor: filter safety, subscribe/unsubscribe, message capture, hand-off to the bridge's own handler, restarts.
import pytest

from bridge import mqtt_monitor as mm, state


class Msg:
    def __init__(self, topic, payload, qos=0, retain=False):
        self.topic, self.payload, self.qos, self.retain = topic, payload, qos, retain


class FakeClient:
    def __init__(self):
        self.subs, self.cbs, self.rc = [], {}, 0

    def subscribe(self, f):
        self.subs.append(f)
        return self.rc, 1

    def unsubscribe(self, f):
        self.subs.remove(f)

    def message_callback_add(self, f, cb):
        self.cbs[f] = cb

    def message_callback_remove(self, f):
        self.cbs.pop(f, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = {'MQTT_MONITOR_FILE': str(tmp_path / 'mm.json'), 'MQTT_TOPIC': 'meshcore', 'STEER_FILE': str(tmp_path / 'st.json')}
    monkeypatch.setattr(mm, '_c', lambda n, d: cfg.get(n, d))
    from bridge import steer
    monkeypatch.setattr(steer, '_c', lambda n, d: cfg.get(n, d))
    fc = FakeClient()
    monkeypatch.setattr(mm, '_mqttc', lambda: fc)
    monkeypatch.setattr(state, 'mqtt_connected', True)
    monkeypatch.setattr(mm, '_active', None)
    mm._buf.clear()
    return fc


def test_filter_checks(env):
    assert mm.check_filter('meshcore/#') == 'meshcore/#'
    assert mm.check_filter('meshcore/nodes/+/rssi') and mm.check_filter('meshcore/antenna/azimuth/set')
    for bad in ('', '#', '+/x', 'other/#', 'meshcore/#/x', 'meshcore/a b$', '$SYS/#', 'meshcoreX/#', 'x' * 300):
        with pytest.raises(ValueError):
            mm.check_filter(bad)


def test_steering_topic_is_allowed_even_outside_base(env):
    from bridge import steer
    steer.save_settings({'topic': 'antenna/director/az'})
    assert mm.check_filter('antenna/director/az') == 'antenna/director/az'
    assert any(s['filter'] == 'antenna/director/az' for s in mm.status()['suggestions'])
    with pytest.raises(ValueError):
        mm.check_filter('antenna/director/#')


def test_subscribe_capture_unsubscribe(env):
    assert not mm.status()['subscribed']
    s = mm.subscribe('meshcore/#')
    assert s['subscribed'] and env.subs == ['meshcore/#'] and 'meshcore/#' in env.cbs
    env.cbs['meshcore/#'](None, None, Msg('meshcore/text', b'{"a": 1}', 1, True))
    m = mm.status()['messages']
    assert len(m) == 1 and m[0]['topic'] == 'meshcore/text' and m[0]['retain'] and m[0]['qos'] == 1
    s = mm.unsubscribe()
    assert not s['subscribed'] and env.subs == [] and env.cbs == {}
    assert len(s['messages']) == 1                                   # history kept until Clear
    assert mm.clear()['buffered'] == 0


def test_subscribe_needs_broker_and_valid_filter(env, monkeypatch):
    monkeypatch.setattr(state, 'mqtt_connected', False)
    with pytest.raises(RuntimeError, match='not connected'):
        mm.subscribe('meshcore/#')
    monkeypatch.setattr(state, 'mqtt_connected', True)
    with pytest.raises(ValueError):
        mm.subscribe('#')
    env.rc = 4
    with pytest.raises(RuntimeError, match='subscribe failed'):
        mm.subscribe('meshcore/#')
    assert not mm.status()['subscribed'] and env.cbs == {}


def test_changing_filter_replaces_subscription(env):
    mm.subscribe('meshcore/#'); mm.subscribe('meshcore/text')
    assert env.subs == ['meshcore/text'] and list(env.cbs) == ['meshcore/text']


def test_inject_topic_still_reaches_the_bridge(env, monkeypatch):
    # paho skips the normal on_message when a filtered callback matches: the monitor must forward it.
    from bridge import mqtt_bridge
    seen = []
    monkeypatch.setattr(mqtt_bridge, 'send_hook', lambda ch, text: seen.append((ch, text)))
    mm.subscribe('meshcore/#')
    env.cbs['meshcore/#'](None, None, Msg('meshcore/send', b'{"text": "hello", "channel": 2}'))
    assert seen == [(2, 'hello')]
    env.cbs['meshcore/#'](None, None, Msg('meshcore/text', b'not json'))      # other topics: ignored by the bridge, no crash
    assert len(mm.status()['messages']) == 2


def test_payload_truncated_and_binary_safe(env):
    mm.subscribe('meshcore/#')
    env.cbs['meshcore/#'](None, None, Msg('meshcore/x', b'a' * 5000))
    env.cbs['meshcore/#'](None, None, Msg('meshcore/y', b'\xff\xfe\x00bad'))
    a, b = mm.status()['messages']
    assert len(a['payload']) == mm.MAX_PAYLOAD and a['truncated'] and a['size'] == 5000
    assert 'bad' in b['payload']


def test_state_survives_restart_and_reconnect(env):
    mm.subscribe('meshcore/nodes/#')
    env.subs.clear(); env.cbs.clear(); mm._active = None             # bridge restarted / broker dropped our subscription
    assert mm.wanted() == ('meshcore/nodes/#', True)
    mm.on_connect()
    assert env.subs == ['meshcore/nodes/#'] and mm.status()['subscribed']
    mm.unsubscribe()
    env.subs.clear(); mm._active = None
    mm.on_connect()
    assert env.subs == [] and not mm.status()['subscribed']          # stays unsubscribed after a restart


def test_routes(env):
    from bridge import web  # noqa: F401
    from bridge.extensions import app
    c = app.test_client()
    assert c.get('/api/mqtt/monitor').get_json()['subscribed'] is False
    assert c.post('/api/mqtt/subscribe', json={'filter': 'evil/#'}).get_json()['ok'] is False
    d = c.post('/api/mqtt/subscribe', json={'filter': 'meshcore/#'}).get_json()
    assert d['ok'] and d['subscribed']
    assert c.post('/api/mqtt/unsubscribe').get_json()['subscribed'] is False
    assert c.post('/api/mqtt/clear').get_json()['ok']
