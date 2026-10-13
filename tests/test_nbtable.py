import time
import pytest
from bridge import nbtable
from tests.test_public import dirfile, RECS

LOC = {'name': 'Ironhorse', 'lat': 51.30, 'lon': -2.90, 'key': RECS[0]['public_key'].lower(), 'source': 'config'}
MEND = '99887766'


@pytest.fixture
def tbl(tmp_path, monkeypatch):
    monkeypatch.setattr(nbtable, '_file', lambda: str(tmp_path / 'nb.json'))
    monkeypatch.setattr(nbtable, '_local', lambda: LOC)
    monkeypatch.setattr(nbtable, '_emit', lambda: None)
    nbtable._entries.clear(); nbtable._meta.clear()
    nbtable._last_fetch = 0.0
    dirfile(tmp_path, monkeypatch, RECS)
    return tmp_path


def test_sync_populates_muted_with_range_bearing(tbl):
    r = nbtable.sync()
    assert r['ok'] and r['count'] == 1 and r['added'] == 1          # not us, not the far one, not the client
    e = nbtable.view()['entries'][0]
    assert e['name'].startswith('Mendip') and e['heard_ts'] is None and not e['active']
    assert 8 < e['range_km'] < 14 and 40 < e['bearing_deg'] < 80    # north-east of us


def test_manual_sync_rate_limited(tbl):
    nbtable.sync()
    with pytest.raises(RuntimeError, match='wait'):
        nbtable.sync()


def test_heard_turns_active_and_age_is_data_age(tbl):
    nbtable.sync()
    ts = time.time() - 300
    assert nbtable.heard(MEND, 'advert', ts, snr=6.5)
    e = nbtable.view()['entries'][0]
    assert e['active'] and e['heard_ts'] == ts and e['heard_via'] == 'advert' and e['snr'] == 6.5
    assert not nbtable.heard(MEND, 'advert', ts + 1)                # not newer: no change


def test_active_window_expires(tbl):
    nbtable.sync()
    nbtable.heard(MEND, 'advert', time.time() - 25 * 3600)
    assert not nbtable.view()['entries'][0]['active']


def test_persistence_across_restart(tbl):
    nbtable.sync(); nbtable.heard(MEND, 'discover')
    nbtable._entries.clear(); nbtable._meta.clear()
    assert nbtable.load() == 1
    e = nbtable.view()['entries'][0]
    assert e['active'] and e['heard_via'] == 'discover' and e['range_km']


def test_sync_keeps_heard_and_drops_stale_public(tbl):
    nbtable.sync(); nbtable.heard(MEND, 'advert')
    nbtable._last_fetch = 0
    nbtable._entries['feedface' + '00' * 28] = {'key': 'feedface' + '00' * 28, 'name': 'gone', 'src': 'public', 'heard_ts': None,
                                                 'lat': 51.3, 'lon': -2.9}
    r = nbtable.sync()
    assert r['removed'] == 1 and r['count'] == 1 and nbtable.view()['entries'][0]['active']


def test_strict_ignores_far_and_unknown_nodes(tbl):
    nbtable.sync()
    assert not nbtable.heard('11223344', 'advert', strict=True)     # far
    assert not nbtable.heard('abcdef01', 'advert', strict=True)     # unknown to the public list
    assert len(nbtable.view()['entries']) == 1
    assert nbtable.heard('abcdef01', 'advert')                      # CLI neighbours are direct, so added


def test_on_sample_uses_neighbour_age_and_discover_tag(tbl):
    nbtable.sync()
    nbtable.note_discover(time.time() - 10)
    nbtable.on_sample({'neighbours': [{'id': MEND, 'age_s': '5', 'snr_db': 7.0}]})
    e = nbtable.view()['entries'][0]
    assert e['active'] and e['heard_via'] == 'discover' and e['snr'] == 7.0
    assert abs((time.time() - e['heard_ts']) - 5) < 2


def test_sync_errors(tbl, monkeypatch):
    monkeypatch.setattr(nbtable, '_local', lambda: None)
    with pytest.raises(RuntimeError, match='position'):
        nbtable.sync()
    assert nbtable.view()['last_error']


def test_position_change_rederives(tbl, monkeypatch):
    nbtable.sync()
    before = nbtable.view()['entries'][0]['range_km']
    new = dict(LOC, lat=51.35, lon=-2.80)
    monkeypatch.setattr(nbtable, '_local', lambda: new)
    nbtable._tick(1e18)
    assert nbtable.view()['entries'][0]['range_km'] < before
