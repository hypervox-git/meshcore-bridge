import json
import pytest
from bridge import linkmodel, publicdir, publiclinks, repeater_cli as cli, state


def test_fspl_known_value():
    # 10 km @ 869.618 MHz: 20 + 58.79 + 32.44 = 111.2 dB
    assert linkmodel.fspl_db(10, 869.618) == pytest.approx(111.2, abs=0.1)
    s = linkmodel.simulate(10, 869.618, 20, 2.0, 6.0)
    assert s['pathloss_db'] == pytest.approx(103.2, abs=0.1) and s['rssi_dbm'] == pytest.approx(-83.2, abs=0.1)


def test_haversine():
    assert linkmodel.haversine_km(51.0, -2.0, 51.0, -1.0) == pytest.approx(70.0, abs=1.0)


def dirfile(tmp_path, monkeypatch, recs):
    f = tmp_path / 'pub.json'; f.write_text(json.dumps(recs))
    monkeypatch.setattr(publicdir, 'FILE', str(f))
    publicdir._nodes.clear()
    return publicdir.refresh()


RECS = [
    {'public_key': 'AABBCCDD' + '00' * 28, 'adv_name': 'Ironhorse', 'adv_lat': 51.30, 'adv_lon': -2.90, 'type': 2},
    {'public_key': '99887766' + '00' * 28, 'adv_name': 'Mendip RPT 6dBi', 'adv_lat': 51.35, 'adv_lon': -2.75, 'type': 2},
    {'public_key': '11223344' + '00' * 28, 'adv_name': 'Far one', 'adv_lat': 55.0, 'adv_lon': -3.0, 'type': 2},
    {'public_key': '55667788' + '00' * 28, 'adv_name': 'Some client', 'adv_lat': 51.31, 'adv_lon': -2.91, 'type': 1},
    {'public_key': 'deadbeef' + '00' * 28, 'adv_name': 'No position'},
]


def test_directory_parsing(tmp_path, monkeypatch):
    assert dirfile(tmp_path, monkeypatch, RECS) == 4                     # record without position dropped
    r = publicdir.lookup('99887766')
    assert r['gain_dbi'] == 6.0 and r['gain_src'] == 'name' and r['type'] == 'repeater'
    assert publicdir.find_by_name('ironhorse')['key'].startswith('aabbccdd')
    near = publicdir.nearby(51.30, -2.90, 40, 'repeater', exclude_keys={RECS[0]['public_key'].lower()})
    assert [r['name'] for d, r in near] == ['Mendip RPT 6dBi']           # not the far one, not the client


def test_gain_override(tmp_path, monkeypatch):
    dirfile(tmp_path, monkeypatch, RECS)
    monkeypatch.setattr(publicdir, '_c', lambda n, d: {'ANTENNA_GAIN_DBI': {'Ironhorse': 5.0}}.get(n, d))
    assert publicdir.override_gain(publicdir.find_by_name('Ironhorse')) == (5.0, 'override')
    assert publicdir.override_gain(publicdir.lookup('99887766')) == (6.0, 'name')
    assert publicdir.override_gain(publicdir.lookup('11223344')) == (0.0, 'assumed')


def test_links_local_from_directory_and_items(tmp_path, monkeypatch):
    dirfile(tmp_path, monkeypatch, RECS)
    cli.latest.update(name='Ironhorse', radio_cfg='869.618,62.5,8,8', neighbours=[
        {'id': '99887766', 'age_s': '5', 'snr': '16', 'snr_db': 4.0, 'raw': 'x', 'key': None, 'name': None, 'lat': None, 'lon': None}])
    monkeypatch.setattr(publiclinks, '_c', lambda n, d: {'GATEWAY_LAT': 0, 'GATEWAY_LON': 0}.get(n, d))
    out = publiclinks.links()
    assert out['local']['source'] == 'public directory' and out['local']['lat'] == 51.30
    assert out['freq_mhz'] == 869.618
    it = out['items'][0]
    assert it['source'] == 'discovered' and it['name'] == 'Mendip RPT 6dBi' and it['gain_remote_dbi'] == 6.0
    d = it['distance_km']; assert 10 < d < 14
    assert it['pathloss_db'] == pytest.approx(it['fspl_db'] - 6.0, abs=0.05)
    assert it['rssi_dbm'] == pytest.approx(20 - it['pathloss_db'], abs=0.05)
    assert all(i['key'][:8] != '99887766' for i in out['items'][1:])     # not duplicated as "public"


def test_unknown_position_gives_note(tmp_path, monkeypatch):
    dirfile(tmp_path, monkeypatch, RECS)
    cli.latest.update(name=None)
    monkeypatch.setattr(publiclinks, '_c', lambda n, d: {'GATEWAY_LAT': 0, 'GATEWAY_LON': 0, 'LOCAL_NAME': None}.get(n, d))
    assert 'position unknown' in publiclinks.links()['note']


def test_antenna_pattern():
    g = linkmodel.antenna_gain_dbi
    assert g(0, 5.5, 25) == pytest.approx(5.5, abs=0.01)
    assert g(180, 5.5, 25) == pytest.approx(5.5 - 25, abs=0.01)             # front-to-back exactly
    assert 5.5 - 25 < g(90, 5.5, 25) < 5.5 - 3                              # side is in between
    assert g(30, 5.5, 25) > g(60, 5.5, 25) > g(120, 5.5, 25)                # monotonic roll-off
    assert 120 < linkmodel.half_power_beamwidth_deg(25) < 150               # wide azimuth lobe


def test_bearing():
    assert linkmodel.bearing_deg(51, -2, 52, -2) == pytest.approx(0, abs=0.5)
    assert linkmodel.bearing_deg(51, -2, 51, -1) == pytest.approx(90, abs=1)
    assert linkmodel.off_boresight_deg(350, 10) == 20 and linkmodel.off_boresight_deg(290, 110) == 180


def test_moxon_applied_per_bearing(tmp_path, monkeypatch):
    dirfile(tmp_path, monkeypatch, RECS + [
        {'public_key': 'beef0001' + '00' * 28, 'adv_name': 'East RPT', 'adv_lat': 51.30, 'adv_lon': -2.76, 'type': 2},   # ~ due east
        {'public_key': 'beef0002' + '00' * 28, 'adv_name': 'West RPT', 'adv_lat': 51.30, 'adv_lon': -3.04, 'type': 2}])  # ~ due west
    cli.latest.update(name='Ironhorse-RPT', radio_cfg='869.618,62.5,8,8', neighbours=[])
    cfg = {'GATEWAY_LAT': 0, 'GATEWAY_LON': 0, 'LOCAL_NAME': 'Ironhorse-RPT',
           'LOCAL_ANTENNA': {'type': 'Moxon', 'gain_dbi': 5.5, 'azimuth_deg': 110, 'front_back_db': 25}}
    monkeypatch.setattr(publiclinks, '_c', lambda n, d: cfg.get(n, d))
    out = publiclinks.links()
    assert out['local']['name'] == 'Ironhorse-RPT' and out['local']['antenna']['directional']
    by = {i['name']: i for i in out['items']}
    e, w = by['East RPT'], by['West RPT']
    assert 85 < e['bearing_deg'] < 95 and 265 < w['bearing_deg'] < 275
    assert e['gain_local_dbi'] > 3 and w['gain_local_dbi'] < -15           # in front vs behind the Moxon
    assert w['pathloss_db'] - e['pathloss_db'] > 15 + (w['fspl_db'] - e['fspl_db']) - 1


def test_aim_at_points_between_neighbours(tmp_path, monkeypatch):
    from bridge import antenna
    az, gains = antenna.aim_at([42, 108], 5.5, 25)
    assert 70 <= az <= 80 and abs(gains[0] - gains[1]) < 0.3                # midpoint of the two bearings
    az1, g1 = antenna.aim_at([257], 5.5, 25)
    assert abs(az1 - 257) < 1 and g1[0] == pytest.approx(5.5, abs=0.05)     # a single neighbour: straight at it


def test_override_file_and_heard_aim(tmp_path, monkeypatch):
    from bridge import antenna
    dirfile(tmp_path, monkeypatch, RECS)
    f = tmp_path / 'ant.json'
    cli.latest.update(name='Ironhorse', radio_cfg='869.618,62.5,8,8', neighbours=[
        {'id': '99887766', 'age_s': '5', 'snr': '16', 'snr_db': 4.0, 'raw': 'x', 'key': None, 'name': None, 'lat': None, 'lon': None}])
    cfg = {'GATEWAY_LAT': 0, 'GATEWAY_LON': 0, 'ANTENNA_FILE': str(f),
           'LOCAL_ANTENNA': {'type': 'Moxon', 'gain_dbi': 5.5, 'azimuth_deg': 200, 'front_back_db': 25}}
    for m in (publiclinks, antenna):
        monkeypatch.setattr(m, '_c', lambda n, d: cfg.get(n, d))
    assert publiclinks.local_info()['antenna']['azimuth_deg'] == 200        # from config.py
    s = antenna.aim_heard()
    assert abs(s['azimuth_deg'] - s['neighbours'][0]['bearing_deg']) < 1 and s['neighbours'][0]['gain_new_dbi'] > s['neighbours'][0]['gain_now_dbi']
    antenna.save(s['azimuth_deg'], 'test')
    assert publiclinks.local_info()['antenna']['azimuth_deg'] == pytest.approx(s['azimuth_deg'], abs=0.1)
    antenna.reset()
    assert publiclinks.local_info()['antenna']['azimuth_deg'] == 200
    cli.latest.update(neighbours=[])
    with pytest.raises(RuntimeError, match='no heard neighbours'):
        antenna.aim_heard()
