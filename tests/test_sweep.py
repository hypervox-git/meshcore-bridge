# Antenna sweep: bearing plan, the stop-by-stop run against a fake mount, and aim tagging in the neighbour table.
import time

import pytest

from bridge import antenna, nbtable, sweep
from tests.test_nbtable import tbl, MEND  # noqa: F401  (fixture + a known repeater id)


def test_plan_covers_span_and_wraps():
    assert sweep.plan(100, 90, 30) == [10.0, 40.0, 70.0, 100.0, 130.0, 160.0, 190.0]
    assert sweep.plan(20, 45, 45) == [335.0, 20.0, 65.0]            # wraps through north


@pytest.mark.parametrize('span,step', [(90, 2), (90, 100), (10, 30), (200, 30), (180, 10)])
def test_plan_rejects_silly_values(span, step):
    with pytest.raises(ValueError):
        sweep.plan(100, span, step)


class FakeMount:
    def __init__(self):
        self.az, self.targets, self.stopped = 110.0, [], False

    def goto(self, az):
        self.targets.append(az); self.az = az
        return {'detail': 'ok'}

    def status(self, *a, **k):
        return {'ok': True, 'reachable': True, 'moving': False, 'bearing': self.az}

    def stop(self):
        self.stopped = True


@pytest.fixture
def fast(monkeypatch, tmp_path):
    m = FakeMount()
    monkeypatch.setattr(sweep.skywatcher, 'goto', m.goto)
    monkeypatch.setattr(sweep.skywatcher, 'status', m.status)
    monkeypatch.setattr(sweep.skywatcher, 'stop', m.stop)
    monkeypatch.setattr(sweep.skywatcher, 'sync_model', lambda st=None: False)
    monkeypatch.setattr(sweep, 'SETTLE_S', 0)
    monkeypatch.setattr(sweep, '_file', lambda: str(tmp_path / 'sweep.json'))
    monkeypatch.setattr(sweep, '_emit', lambda: None)
    sweep._s.update(running=False, results=[], names={}, error=None, reason=None)
    return m


def wait_done():
    sweep._thread.join(10)
    assert not sweep.view()['running']


def test_sweep_logs_each_stop_and_returns_to_start(fast, monkeypatch):
    # the neighbour "hears" best when the antenna points at 160 deg
    monkeypatch.setattr(sweep, '_measure', lambda: {'aa11bb22': {'snr': 10 - abs(fast.az - 160) / 10, 'rssi': -90.0, 'name': 'Locking'}})
    d = sweep.start(span=60, step=30)
    assert d['total'] == 5
    wait_done()
    v = sweep.view()
    assert v['reason'] == 'complete' and v['error'] is None
    assert [r['az'] for r in v['results']] == [50.0, 80.0, 110.0, 140.0, 170.0]
    best = max(v['results'], key=lambda r: r['readings']['aa11bb22']['snr'])
    assert best['az'] == 170.0 and v['names']['aa11bb22'] == 'Locking'
    assert fast.targets[-1] == 110.0                                  # back where it started


def test_stop_returns_to_start_and_halts_mount(fast, monkeypatch):
    def slow():
        time.sleep(0.2)
        return {}
    monkeypatch.setattr(sweep, '_measure', slow)
    sweep.start(span=90, step=30)
    time.sleep(0.1)
    sweep.stop()
    wait_done()
    v = sweep.view()
    assert v['reason'] == 'stopped' and len(v['results']) < 7 and fast.stopped
    assert fast.targets[-1] == 110.0


def test_failure_reported_and_antenna_still_returned(fast, monkeypatch):
    def boom():
        raise RuntimeError('discover rejected')
    monkeypatch.setattr(sweep, '_measure', boom)
    sweep.start(span=30, step=30)
    wait_done()
    v = sweep.view()
    assert v['reason'] == 'failed' and 'discover rejected' in v['error'] and fast.targets[-1] == 110.0


def test_start_needs_a_calibrated_mount(fast, monkeypatch):
    monkeypatch.setattr(sweep.skywatcher, 'status', lambda *a, **k: {'reachable': False, 'message': 'mount not reachable'})
    with pytest.raises(RuntimeError, match='not reachable'):
        sweep.start()


def test_neighbour_reading_records_aim_only_when_fresh(tbl, monkeypatch):  # noqa: F811
    nbtable.sync()
    monkeypatch.setattr(antenna, 'override', lambda: {'azimuth_deg': 142.0})
    nbtable.heard(MEND, 'advert', time.time() - 7200, snr=2.0)          # old advert: the aim back then is unknown
    e = nbtable.view()['entries'][0]
    assert e['snr'] == 2.0 and e['ant_az'] is None
    nbtable.heard(MEND, 'discover', time.time() - 5, snr=4.0)           # just measured: aim recorded
    e = nbtable.view()['entries'][0]
    assert e['snr'] == 4.0 and e['ant_az'] == 142.0
