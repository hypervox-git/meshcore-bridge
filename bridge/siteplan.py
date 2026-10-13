# Site planner: which public repeaters has our repeater line-of-sight to, and which antenna
# azimuth reaches the most of them? Uses the public directory (positions), terrain
# elevation profiles (terrain.py), a 4/3-earth line-of-sight + Fresnel check, knife-edge
# diffraction loss for obstructions, and the directional antenna model (linkmodel).
# It is a PLANNING AID: 90 m terrain data, no buildings/trees, assumed mast heights.
import math
import re
import threading
import time

from bridge import cli_ref, linkmodel, publicdir, publiclinks, terrain

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


_state = {'state': 'idle', 'error': None, 'result': None, 'started': None, 'progress': ''}
_lock = threading.Lock()


def status():
    with _lock:
        return dict(_state)


def _profile_points(lat1, lon1, lat2, lon2, d_km):
    n = int(min(max(math.ceil(d_km / 0.5) + 1, 8), 40))
    return [(lat1 + (lat2 - lat1) * i / (n - 1), lon1 + (lon2 - lon1) * i / (n - 1), d_km * i / (n - 1))
            for i in range(n)]


def _radio_sensitivity():
    cfg = cli_ref().latest.get('radio_cfg') or ''
    nums = re.findall(r'\d+(?:\.\d+)?', cfg)
    if len(nums) >= 3:
        try:
            return linkmodel.lora_sensitivity_dbm(float(nums[1]), int(float(nums[2])))
        except Exception:
            pass
    return float(_c('PLAN_SENSITIVITY_DBM', -130.0))


def analyse(az_step=5):
    loc = publiclinks.local_info()
    if loc['lat'] is None:
        raise RuntimeError('our position unknown - set LOCAL_NAME / GATEWAY_LAT+LON first')
    f = publiclinks.freq_mhz()
    tx = float(_c('SIM_TX_DBM', 20))
    ant = loc['antenna']
    sens = _radio_sensitivity()
    margin = float(_c('PLAN_FADE_MARGIN_DB', 10))
    h_l, h_r = float(_c('LOCAL_MAST_M', 6)), float(_c('REMOTE_MAST_M', 6))
    exclude = {loc['key']} if loc['key'] else set()
    cands = publicdir.nearby(loc['lat'], loc['lon'], float(_c('PLAN_RADIUS_KM', 60)), 'repeater',
                             int(_c('PLAN_MAX_REPEATERS', 30)), exclude)
    if not cands:
        raise RuntimeError('no public repeaters within the planning radius (directory empty or not loaded?)')
    heard = set()
    for r in cli_ref().snapshot().get('neighbours') or []:
        rec = publicdir.lookup(r.get('id', ''))
        if rec:
            heard.add(rec['key'])
    # terrain: every profile point + our own site, fetched in batches
    pts, prof = [(loc['lat'], loc['lon'])], {}
    for d, rec in cands:
        prof[rec['key']] = _profile_points(loc['lat'], loc['lon'], rec['lat'], rec['lon'], d)
        pts += [(la, lo) for la, lo, _ in prof[rec['key']]]
    with _lock:
        _state['progress'] = f'{len(pts)} terrain points'
    elev = dict(zip([terrain._key(*p) for p in pts], terrain.elevations(pts)))
    site_elev = elev[terrain._key(loc['lat'], loc['lon'])]
    out = []
    for d, rec in cands:
        p = [(dd, elev[terrain._key(la, lo)]) for la, lo, dd in prof[rec['key']]]
        los = linkmodel.los_analysis(p, p[0][1] + h_l, p[-1][1] + h_r, f)
        brg = linkmodel.bearing_deg(loc['lat'], loc['lon'], rec['lat'], rec['lon'])
        g_r, g_src = publicdir.override_gain(rec)
        out.append({'key': rec['key'], 'name': rec['name'], 'lat': rec['lat'], 'lon': rec['lon'],
                    'distance_km': round(d, 1), 'bearing_deg': round(brg), 'remote_gain_dbi': g_r, 'gain_src': g_src,
                    'ground_m': round(p[-1][1]), 'heard': rec['key'] in heard, **los,
                    'fspl_db': round(linkmodel.fspl_db(d, f), 1)})

    def rssi_at(c, az):
        g_l = (linkmodel.antenna_gain_dbi(linkmodel.off_boresight_deg(c['bearing_deg'], az), ant['gain_dbi'],
                                          ant['front_back_db'], ant.get('hpbw_deg')) if ant['directional'] else ant['gain_dbi'])
        return tx + g_l + c['remote_gain_dbi'] - c['fspl_db'] - c['terrain_loss_db']

    total_w = sum(2.0 if c['heard'] else 1.0 for c in out) or 1.0

    def score(az):
        reach, marg = 0.0, 0.0
        for c in out:
            m = rssi_at(c, az) - (sens + margin)
            if m >= 0 and c['status'] != 'blocked':
                w = 2.0 if c['heard'] else 1.0
                reach += w
                marg += w * min(m, 40) / 40.0
        # reach dominates; margin only breaks ties and never outweighs one whole repeater
        return reach, marg, reach + 0.9 * marg / total_w

    cur_az = ant.get('azimuth_deg', 0.0)
    sweep = [{'az': a, 'score': score(a)[0], 'margin': round(score(a)[1], 2), 'k': round(score(a)[2], 3)}
             for a in range(0, 360, az_step)]
    top = max(x['k'] for x in sweep)
    flags = [x['k'] >= top - 0.04 for x in sweep]
    # longest circular run of azimuths achieving the top score; aim for its centre
    n = len(sweep)
    runs, i = [], 0
    while i < 2 * n:
        if flags[i % n]:
            j = i
            while j < i + n and flags[j % n]:
                j += 1
            runs.append((j - i, i))
            i = j
        else:
            i += 1
    length, start = max(runs, key=lambda r: (r[0], sum(sweep[(r[1] + k) % n]['margin'] for k in range(r[0]))))
    length = min(length, n)
    mid = (start + (length - 1) / 2.0) % n
    best = dict(sweep[int(round(mid)) % n])
    arc = [sweep[start % n]['az'], sweep[(start + length - 1) % n]['az']]
    cur_score, cur_margin, cur_k = score(cur_az)
    for c in out:
        c['rssi_now_dbm'] = round(rssi_at(c, cur_az), 1)
        c['rssi_best_dbm'] = round(rssi_at(c, best['az']), 1)
        c['reach_now'] = c['rssi_now_dbm'] >= sens + margin and c['status'] != 'blocked'
        c['reach_best'] = c['rssi_best_dbm'] >= sens + margin and c['status'] != 'blocked'
        c['pathloss_terrain_db'] = round(c['fspl_db'] + c['terrain_loss_db'] - c['remote_gain_dbi'], 1)
    out.sort(key=lambda c: c['distance_km'])
    return {'ts': time.time(), 'local': loc, 'freq_mhz': f, 'tx_dbm': tx, 'sensitivity_dbm': round(sens, 1),
            'fade_margin_db': margin, 'mast_m': [h_l, h_r], 'site_ground_m': round(site_elev),
            'current': {'azimuth_deg': cur_az, 'score': cur_score, 'margin': round(cur_margin, 2), 'k': round(cur_k, 3),
                        'reachable': sum(1 for c in out if c['reach_now'])},
            'best': {'azimuth_deg': best['az'], 'score': best['score'], 'margin': best['margin'], 'k': best['k'],
                     'reachable': sum(1 for c in out if c['reach_best']), 'arc': arc},
            'line_of_sight': [c['name'] for c in out if c['status'] != 'blocked'],
            'candidates': out, 'sweep': sweep}


def run():
    """Start the analysis in the background (it makes network calls and can take a while)."""
    with _lock:
        if _state['state'] == 'running':
            return {'ok': True, 'state': 'running'}
        _state.update(state='running', error=None, started=time.time(), progress='starting')

    def _job():
        try:
            res = analyse()
            with _lock:
                _state.update(state='done', result=res, error=None)
        except Exception as e:
            with _lock:
                _state.update(state='error', error=str(e) or e.__class__.__name__)
    threading.Thread(target=_job, daemon=True, name='siteplan').start()
    return {'ok': True, 'state': 'running'}
