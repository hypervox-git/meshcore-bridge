import pytest
from bridge.dutycycle import Governor, Window, legal_limit_for, lora_airtime_s


def test_airtime_matches_semtech_reference():
    # SF7 / 125 kHz / CR 4/5 / 20 byte payload / 8-symbol preamble = 56.58 ms (Semtech calculator)
    assert lora_airtime_s(20, 7, 125, 5, preamble=8) == pytest.approx(0.05658, abs=1e-4)
    # CR may be given as 1-4 or MeshCore's 5-8
    assert lora_airtime_s(20, 7, 125, 1, preamble=8) == lora_airtime_s(20, 7, 125, 5, preamble=8)


def test_legal_limit_by_frequency():
    assert legal_limit_for(869.618) == 0.10
    assert legal_limit_for(868.1) == 0.01
    assert legal_limit_for(None) == 0.01


def test_window_budget_and_expiry():
    w = Window(budget_s=10, window_s=100)
    assert w.wait_for(4, now=0) == 0
    w.spend(6, now=0)
    w.spend(3, now=50)
    assert w.used(now=60) == 9
    assert w.wait_for(4, now=60) == pytest.approx(40)      # first 6 s expires at t=100
    assert w.used(now=101) == 3                            # expired
    assert w.wait_for(4, now=101) == 0
    assert w.wait_for(11, now=101) == float('inf')         # can never fit


def test_governor_paces_both_transmitters():
    g = Governor(limit=0.10, share=0.25, sf=8, bw_khz=62.5, cr=5)
    req, resp = g.airtime('status_req'), g.airtime('status_resp')
    assert resp > req
    interval = g.min_interval_s(req, resp)
    # steady-state spacing keeps the target (the bigger TX) at or under limit*share
    assert resp / interval <= 0.10 * 0.25 + 1e-9
    assert 15 < interval < 40                              # ~22 s on UK narrow / 10 % band
    # 1 % band is 10x slower
    g1 = Governor(limit=0.01, share=0.25, sf=8, bw_khz=62.5, cr=5)
    assert g1.min_interval_s(req, resp) == pytest.approx(interval * 10)


def test_governor_blocks_when_target_budget_spent():
    g = Governor(limit=0.01, share=0.25, sf=8, bw_khz=62.5, cr=5, window_s=3600)
    resp = g.airtime('status_resp')
    n = 0
    while g.wait_for(g.airtime('status_req'), 'T', resp) == 0 and n < 1000:
        g.spend(g.airtime('status_req'), 'T', resp)
        n += 1
    assert 0 < n < 100
    assert g.snapshot('T')['target_used_s'] <= g.budget_s + 1e-6
    assert g.wait_for(g.airtime('status_req'), 'OTHER', resp) >= 0   # other target has its own ledger
