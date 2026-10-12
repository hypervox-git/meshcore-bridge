import pytest
from bridge import linkmodel, siteplan, terrain, publicdir, publiclinks, repeater_cli as cli
from tests.test_public import RECS, dirfile


def prof(elevs, d=20.0):
    n = len(elevs)
    return [(d * i / (n - 1), e) for i, e in enumerate(elevs)]


def test_los_flat_vs_ridge():
    flat = linkmodel.los_analysis(prof([50] * 11), 100, 100, 869.6)
    assert flat['status'] == 'clear' and flat['terrain_loss_db'] == 0
    ridge = linkmodel.los_analysis(prof([50, 50, 50, 50, 50, 200, 50, 50, 50, 50, 50]), 100, 100, 869.6)
    assert ridge['status'] == 'blocked' and ridge['terrain_loss_db'] > 15
    grazing = linkmodel.los_analysis(prof([50, 50, 50, 50, 50, 83, 50, 50, 50, 50, 50]), 100, 100, 869.6)
    assert grazing['status'] == 'marginal'
    # radio horizon: 6 m masts over 20 km of flat ground are NOT clear (Earth bulge ~6 m)
    assert linkmodel.los_analysis(prof([40] * 11), 46, 46, 869.6)['status'] != 'clear'


def test_sensitivity_sf8_bw62():
    assert linkmodel.lora_sensitivity_dbm(62.5, 8) == pytest.approx(-130.0, abs=0.5)


def test_orientation_prefers_the_cluster(tmp_path, monkeypatch):
    # Local at (51.30,-2.90); three flat-terrain repeaters to the NE-E, one far west.
    recs = [{'public_key': 'aa' * 32, 'adv_name': 'Ironhorse-RPT', 'adv_lat': 51.30, 'adv_lon': -2.90, 'type': 2}]
    for i, (la, lo) in enumerate([(51.34, -2.70), (51.30, -2.66), (51.27, -2.64), (51.30, -3.20)]):
        recs.append({'public_key': f'0{i + 1}' * 32, 'adv_name': f'R{i + 1}', 'adv_lat': la, 'adv_lon': lo, 'type': 2})
    dirfile(tmp_path, monkeypatch, recs)
    monkeypatch.setattr(terrain, 'elevations', lambda pts: [40.0] * len(pts))
    cli.latest.update(name='Ironhorse-RPT', radio_cfg='869.618,62.5,8,8', neighbours=[])
    cfg = {'GATEWAY_LAT': 0, 'GATEWAY_LON': 0, 'LOCAL_NAME': 'Ironhorse-RPT', 'PLAN_RADIUS_KM': 60,
           'LOCAL_ANTENNA': {'type': 'Moxon', 'gain_dbi': 5.5, 'azimuth_deg': 270, 'front_back_db': 25},
           'SIM_TX_DBM': 14, 'LOCAL_MAST_M': 60, 'REMOTE_MAST_M': 60}                                    # low power so the pattern matters
    monkeypatch.setattr(publiclinks, '_c', lambda n, d: cfg.get(n, d))
    monkeypatch.setattr(siteplan, '_c', lambda n, d: cfg.get(n, d))
    res = siteplan.analyse()
    assert len(res['candidates']) == 4 and all(c['status'] != 'blocked' for c in res['candidates'])
    assert res['current']['azimuth_deg'] == 270
    assert 0 <= res['best']['azimuth_deg'] <= 150                  # swings round from 270 (the west-facing start) to the east side
    assert res['best']['reachable'] >= res['current']['reachable']
    assert res['best']['k'] > res['current']['k']


def test_blocked_not_counted(tmp_path, monkeypatch):
    recs = [{'public_key': 'aa' * 32, 'adv_name': 'Ironhorse-RPT', 'adv_lat': 51.30, 'adv_lon': -2.90, 'type': 2},
            {'public_key': '01' * 32, 'adv_name': 'Behind hill', 'adv_lat': 51.30, 'adv_lon': -2.70, 'type': 2}]
    dirfile(tmp_path, monkeypatch, recs)
    def elev(pts):                                              # big hill in the middle of the path
        return [400.0 if abs(lo - (-2.80)) < 0.02 else 40.0 for la, lo in pts]
    monkeypatch.setattr(terrain, 'elevations', elev)
    cli.latest.update(name='Ironhorse-RPT', radio_cfg='869.618,62.5,8,8', neighbours=[])
    cfg = {'GATEWAY_LAT': 0, 'GATEWAY_LON': 0, 'LOCAL_NAME': 'Ironhorse-RPT', 'LOCAL_MAST_M': 20, 'REMOTE_MAST_M': 20}
    monkeypatch.setattr(publiclinks, '_c', lambda n, d: cfg.get(n, d))
    monkeypatch.setattr(siteplan, '_c', lambda n, d: cfg.get(n, d))
    res = siteplan.analyse()
    c = res['candidates'][0]
    assert c['status'] == 'blocked' and c['terrain_loss_db'] > 20 and not c['reach_now']
    assert res['line_of_sight'] == []
