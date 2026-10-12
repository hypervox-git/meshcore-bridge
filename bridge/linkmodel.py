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
