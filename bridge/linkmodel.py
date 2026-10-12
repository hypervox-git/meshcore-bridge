# Simple link-budget model used to SIMULATE a path between two known positions.
#   FSPL(dB)   = 20*log10(d_km) + 20*log10(f_MHz) + 32.44          (free space, line of sight)
#   path loss  = FSPL - antenna gain at each end                    (system path loss, Ptx - Prx)
#   RSSI(dBm)  = Ptx + Gtx + Grx - FSPL = Ptx - path loss
# Free space is a best case: terrain, buildings and foliage only add loss, so a measured
# path loss above the simulated one is "excess loss" (obstruction / fading margin).
import math


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fspl_db(d_km, f_mhz):
    d_km = max(d_km, 0.001)                       # avoid log(0) for co-located nodes
    return 20 * math.log10(d_km) + 20 * math.log10(f_mhz) + 32.44


def simulate(d_km, f_mhz, tx_dbm=20.0, gain_local_dbi=0.0, gain_remote_dbi=0.0):
    fs = fspl_db(d_km, f_mhz)
    pl = fs - (gain_local_dbi or 0.0) - (gain_remote_dbi or 0.0)
    return {'distance_km': round(d_km, 2), 'fspl_db': round(fs, 1), 'pathloss_db': round(pl, 1),
            'rssi_dbm': round(tx_dbm - pl, 1), 'tx_dbm': tx_dbm,
            'gain_local_dbi': gain_local_dbi or 0.0, 'gain_remote_dbi': gain_remote_dbi or 0.0}


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from true north."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def off_boresight_deg(bearing, azimuth):
    return abs(((bearing - azimuth + 540.0) % 360.0) - 180.0)


def _beta(front_back_db):
    r = 10 ** (-abs(front_back_db) / 20.0)          # field ratio at 180 deg
    return (1 - r) / (1 + r)


def antenna_gain_dbi(off_deg, gain_dbi, front_back_db):
    """Gain towards a direction `off_deg` from boresight (azimuth plane).

    Cardioid-family pattern E(t) = (1 + b*cos t)/(1 + b): exactly the peak gain at 0 deg and
    exactly `front_back_db` lower at 180 deg. A good first-order model of a Moxon's wide,
    one-sided azimuth pattern (real ones add small side lobes). Vertical polarisation is
    assumed so the azimuth plane is the antenna's wide plane.
    """
    b = _beta(front_back_db)
    e = (1 + b * math.cos(math.radians(off_deg))) / (1 + b)
    r = 10 ** (-abs(front_back_db) / 20.0)
    return gain_dbi + 20 * math.log10(max(e, r))


def half_power_beamwidth_deg(front_back_db):
    b = _beta(front_back_db)
    if b <= 0:
        return 360.0
    c = (math.sqrt(0.5) * (1 + b) - 1) / b
    return 2 * math.degrees(math.acos(max(-1.0, min(1.0, c))))


# ---- terrain / line-of-sight -------------------------------------------------------

def earth_bulge_m(d1_km, d2_km, k=4.0 / 3.0):
    """Earth bulge (m) at a point d1/d2 km from the two ends, effective-earth-radius factor k."""
    return d1_km * d2_km / (12.742 * k)


def knife_edge_loss_db(v):
    """ITU-R P.526 single knife-edge diffraction loss approximation (0 dB below v = -0.78)."""
    if v <= -0.78:
        return 0.0
    return 6.9 + 20 * math.log10(math.sqrt((v - 0.1) ** 2 + 1) + v - 0.1)


def los_analysis(profile, h_tx_m, h_rx_m, f_mhz):
    """profile = [(distance_km from TX, ground elevation m), ...] including both ends;
    h_tx_m / h_rx_m = ANTENNA altitudes (ground + mast). Returns the worst obstruction:
    clearance as a fraction of the first Fresnel zone, the diffraction loss it implies and a
    status of clear (>=0.6 F1) / marginal (LOS but <0.6 F1) / blocked (no LOS)."""
    total = profile[-1][0]
    lam = 299.792458 / f_mhz
    worst = {'ratio': 9.9, 'd_km': None, 'v': -9.0}
    for d, elev in profile[1:-1]:
        d1, d2 = d, total - d
        if d1 <= 0 or d2 <= 0:
            continue
        line = h_tx_m + (h_rx_m - h_tx_m) * d1 / total
        clearance = line - (elev + earth_bulge_m(d1, d2))
        r1 = 17.32 * math.sqrt(d1 * d2 / ((f_mhz / 1000.0) * total))
        ratio = clearance / r1 if r1 > 0 else 9.9
        if ratio < worst['ratio']:
            v = (-clearance) * math.sqrt(2 * (d1 + d2) * 1000.0 / (lam * d1 * 1000.0 * d2 * 1000.0))
            worst = {'ratio': ratio, 'd_km': d, 'v': v}
    ratio = worst['ratio']
    status = 'clear' if ratio >= 0.6 else 'marginal' if ratio >= 0 else 'blocked'
    return {'status': status, 'clearance_f1': round(min(ratio, 9.9), 2), 'worst_km': worst['d_km'],
            'terrain_loss_db': round(min(knife_edge_loss_db(worst['v']), 60.0), 1)}


_SNR_LIMIT = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}


def lora_sensitivity_dbm(bw_khz, sf, noise_figure_db=6.0):
    """Receiver sensitivity: -174 + 10log10(BW) + NF + SNR limit for the spreading factor."""
    return -174 + 10 * math.log10(bw_khz * 1000.0) + noise_figure_db + _SNR_LIMIT.get(int(sf), -10.0)
