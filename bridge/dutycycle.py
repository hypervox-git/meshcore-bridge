# LoRa airtime estimation + a sliding-window duty-cycle governor.
#
# Duty cycle is a per-TRANSMITTER limit (ETSI EN 300 220 / UK IR 2030): each radio
# may transmit for at most `limit` of any rolling hour in a given sub-band. A
# status poll costs OUR node one short TX (the request) and the TARGET node one
# longer TX (the reply), so both ends are budgeted separately. The test features
# are only allowed to use `share` of the legal limit, leaving the rest for normal
# mesh traffic (adverts, forwarding, messages).
import math
import time
from collections import deque

WINDOW_S = 3600.0

# Conservative on-air sizes (bytes, incl. MeshCore header/path/MAC). Estimates, not
# measurements - rounded UP so the budget errs on the safe side.
EST_BYTES = {
    'discover_req': 14,
    'discover_resp': 40,
    'status_req': 40,
    'status_resp': 90,
    'login_req': 60,
    'login_resp': 40,
    'neighbours_req': 40,
    'neighbours_resp': 190,
    'path_disc_req': 40,
    'path_disc_resp': 70,
    'trace': 45,
}
PER_HOP_BYTES = 1          # each repeater adds one path hash byte (more with 2/4B hashes)


def legal_limit_for(freq_mhz):
    """Best-guess legal duty cycle for the configured frequency (EU868 / UK).

    869.400-869.650 MHz (the MeshCore UK/EU default area) is a 10% sub-band; the
    other 863-870 MHz SRD sub-bands are 1% (a few 0.1%). Unknown -> 1% (safe).
    Override with DUTY_CYCLE_LIMIT in config.py if your band/licence differs.
    """
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return 0.01
    if 869.400 <= f <= 869.650:
        return 0.10
    return 0.01


def lora_airtime_s(payload_bytes, sf, bw_khz, cr, preamble=16, crc=True, explicit_header=True):
    """Semtech time-on-air for one LoRa frame. `cr` may be 1-4 or MeshCore's 5-8."""
    sf = int(sf)
    bw = float(bw_khz) * 1000.0
    cr_idx = int(cr) - 4 if int(cr) > 4 else int(cr)
    tsym = (2 ** sf) / bw
    de = 1 if tsym > 0.016 else 0
    ih = 0 if explicit_header else 1
    num = 8 * payload_bytes - 4 * sf + 28 + (16 if crc else 0) - 20 * ih
    den = 4 * (sf - 2 * de)
    n_payload = 8 + max(math.ceil(num / den) * (cr_idx + 4), 0)
    return (preamble + 4.25) * tsym + n_payload * tsym


class Window:
    """Rolling-hour airtime ledger for one transmitter."""

    def __init__(self, budget_s, window_s=WINDOW_S):
        self.budget_s = budget_s
        self.window_s = window_s
        self.events = deque()           # (monotonic_ts, airtime_s)

    def _prune(self, now):
        while self.events and now - self.events[0][0] >= self.window_s:
            self.events.popleft()

    def used(self, now=None):
        now = time.monotonic() if now is None else now
        self._prune(now)
        return sum(a for _, a in self.events)

    def wait_for(self, cost_s, now=None):
        """Seconds until `cost_s` fits in the budget (0 if it fits now, inf if never)."""
        now = time.monotonic() if now is None else now
        self._prune(now)
        if cost_s > self.budget_s:
            return float('inf')
        used = sum(a for _, a in self.events)
        if used + cost_s <= self.budget_s:
            return 0.0
        for ts, a in self.events:
            used -= a
            if used + cost_s <= self.budget_s:
                return max(0.0, ts + self.window_s - now)
        return self.window_s

    def spend(self, cost_s, now=None):
        now = time.monotonic() if now is None else now
        self.events.append((now, cost_s))


class Governor:
    """Local-node window + one window per remote target."""

    def __init__(self, limit, share, sf, bw_khz, cr, window_s=WINDOW_S):
        self.limit = limit
        self.share = share
        self.sf, self.bw_khz, self.cr = sf, bw_khz, cr
        self.window_s = window_s
        self.budget_s = limit * share * window_s
        self.local = Window(self.budget_s, window_s)
        self.targets = {}

    def airtime(self, kind, hops=0):
        size = EST_BYTES[kind] + PER_HOP_BYTES * max(0, hops)
        return lora_airtime_s(size, self.sf, self.bw_khz, self.cr)

    def _target(self, key):
        if key not in self.targets:
            self.targets[key] = Window(self.budget_s, self.window_s)
        return self.targets[key]

    def wait_for(self, local_cost_s, target_key=None, target_cost_s=0.0):
        w = self.local.wait_for(local_cost_s)
        if target_key is not None and target_cost_s:
            w = max(w, self._target(target_key).wait_for(target_cost_s))
        return w

    def spend(self, local_cost_s, target_key=None, target_cost_s=0.0):
        self.local.spend(local_cost_s)
        if target_key is not None and target_cost_s:
            self._target(target_key).spend(target_cost_s)

    def min_interval_s(self, local_cost_s, target_cost_s=0.0):
        """Steady-state spacing so neither end exceeds its share of the limit."""
        rate = self.limit * self.share          # allowed airtime per second
        if rate <= 0:
            return float('inf')
        return max(local_cost_s / rate, target_cost_s / rate)

    def snapshot(self, target_key=None):
        snap = {'limit_pct': round(self.limit * 100, 2),
                'share_pct': round(self.share * 100, 1),
                'budget_s': round(self.budget_s, 1),
                'local_used_s': round(self.local.used(), 2)}
        if target_key is not None:
            snap['target_used_s'] = round(self._target(target_key).used(), 2)
        return snap
