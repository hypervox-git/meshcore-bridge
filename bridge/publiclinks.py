# Assemble the map's "public repeaters + simulated links" view: our repeater at its KNOWN
# position, the repeaters it has discovered (placed from node_db or the public directory),
# and the nearest public repeaters, each with a simulated free-space link from us.
import re

from bridge import antenna as antenna_cfg, cli_ref, linkmodel, publicdir
from bridge.state import node_db

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


def freq_mhz():
    cfg = (cli_ref().latest.get('radio_cfg') or '')
    m = re.search(r'(\d{3}\.\d+)', cfg)
    return float(m.group(1)) if m else float(_c('RADIO_FREQ_MHZ', 869.618))


def local_info():
    """Name, position, antenna gain of OUR repeater (config wins, else the public directory)."""
    name = _c('LOCAL_NAME', None) or cli_ref().latest.get('name')
    lat, lon, src = None, None, None
    glat, glon = float(_c('GATEWAY_LAT', 0) or 0), float(_c('GATEWAY_LON', 0) or 0)
    rec = publicdir.find_by_name(name) if name else None
    if glat or glon:
        lat, lon, src = glat, glon, 'config'
    elif rec:
        lat, lon, src = rec['lat'], rec['lon'], 'public directory'
    ant = _c('LOCAL_ANTENNA', None)
    if isinstance(ant, dict) and ant.get('gain_dbi') is not None:
        fb = float(ant.get('front_back_db', 25))
        ov = antenna_cfg.override()                       # aim set from the web UI wins over config.py
        antenna = {'type': ant.get('type', 'directional'), 'gain_dbi': float(ant['gain_dbi']),
                   'azimuth_deg': float(ov['azimuth_deg'] if ov else ant.get('azimuth_deg', 0)) % 360,
                   'azimuth_source': ('UI override: ' + str(ov.get('reason'))) if ov else 'config.py',
                   'front_back_db': fb,
                   'beamwidth_deg': round(linkmodel.half_power_beamwidth_deg(fb), 0), 'directional': True}
        gsrc = 'config: ' + str(antenna['type'])
    else:
        gain = _c('LOCAL_ANTENNA_GAIN_DBI', None)
        gsrc = 'config'
        if gain is None:
            gain, gsrc = publicdir.override_gain(rec) if rec else (0.0, 'assumed')
        antenna = {'type': 'omni', 'gain_dbi': float(gain), 'directional': False}
    return {'name': name, 'lat': lat, 'lon': lon, 'source': src, 'gain_dbi': antenna['gain_dbi'],
            'gain_src': gsrc, 'antenna': antenna, 'key': rec['key'] if rec else None}


def _item(src, key, name, lat, lon, gain, gain_src, loc, tx, f, measured):
    ant = loc['antenna']
    brg = linkmodel.bearing_deg(loc['lat'], loc['lon'], lat, lon)
    off = linkmodel.off_boresight_deg(brg, ant['azimuth_deg']) if ant['directional'] else 0.0
    g_local = (linkmodel.antenna_gain_dbi(off, ant['gain_dbi'], ant['front_back_db'])
               if ant['directional'] else ant['gain_dbi'])
    sim = linkmodel.simulate(linkmodel.haversine_km(loc['lat'], loc['lon'], lat, lon), f, tx, g_local, gain)
    sim['gain_local_dbi'] = round(g_local, 1)
    item = dict(sim, source=src, key=key, name=name, lat=lat, lon=lon, gain_src=gain_src, freq_mhz=f,
                bearing_deg=round(brg, 0), off_boresight_deg=round(off, 0))
    if measured and measured.get('pathloss_db') is not None:
        item['measured'] = measured
        item['excess_db'] = round(measured['pathloss_db'] - sim['pathloss_db'], 1)
    elif measured:
        item['measured'] = measured
    return item


def links():
    tx = float(_c('SIM_TX_DBM', 20))
    f = freq_mhz()
    loc = local_info()
    out = {'local': loc, 'freq_mhz': f, 'tx_dbm': tx, 'directory': publicdir.status(),
           'items': [], 'radius_km': float(_c('PUBLIC_RADIUS_KM', 40))}
    if loc['lat'] is None:
        out['note'] = ('our position unknown: set GATEWAY_LAT/GATEWAY_LON, or LOCAL_NAME to the repeater\'s '
                       'public name' + ('' if publicdir.status()['count'] else ' (public directory not loaded yet)'))
        return out
    seen = {loc['key']} if loc['key'] else set()
    meas = {}
    try:
        from bridge import repeater_test
        for n in (repeater_test.view().get('neighbours') or []):
            meas[n['id'].lower()] = {'rssi': n.get('rssi'), 'snr': n.get('snr'), 'pathloss_db': n.get('pathloss_db')}
    except Exception:
        pass
    for r in cli_ref().snapshot().get('neighbours') or []:
        rid = str(r.get('id', '')).lower()
        rec = publicdir.lookup(rid)
        nd = node_db.get(r.get('key') or '', {})
        lat, lon = (nd.get('lat'), nd.get('lon')) if nd.get('lat') is not None else ((rec['lat'], rec['lon']) if rec else (None, None))
        if lat is None:
            continue
        gain, gsrc = publicdir.override_gain(rec) if rec else (0.0, 'assumed')
        m = dict(meas.get(rid) or {}, snr=r.get('snr_db'))
        out['items'].append(_item('discovered', (rec or {}).get('key') or r.get('key') or rid,
                                  (rec or {}).get('name') or r.get('name') or rid, lat, lon, gain, gsrc, loc, tx, f, m))
        seen.add((rec or {}).get('key') or rid)
    for d, rec in publicdir.nearby(loc['lat'], loc['lon'], out['radius_km'], 'repeater',
                                   int(_c('PUBLIC_MAX_NEARBY', 12)), seen):
        gain, gsrc = publicdir.override_gain(rec)
        out['items'].append(_item('public', rec['key'], rec['name'], rec['lat'], rec['lon'], gain, gsrc, loc, tx, f, None))
    return out
