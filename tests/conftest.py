# Tests run against a mock MeshCore node - no hardware, no real config.py needed.
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if importlib.util.find_spec('config') is None:
    spec = importlib.util.spec_from_file_location('config', os.path.join(ROOT, 'config.example.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.GATEWAY_LAT, mod.GATEWAY_LON = 51.0, -2.9
    sys.modules['config'] = mod


import pytest


@pytest.fixture(autouse=True)
def _isolate_public_directory():
    from bridge import publicdir
    yield
    publicdir._nodes.clear()
    publicdir.FILE = None


@pytest.fixture(autouse=True)
def _isolate_runtime_files(tmp_path, monkeypatch):
    """No test may write the bridge's runtime JSON files (meshcore_*.json) into the repo directory."""
    from bridge import antenna, mqtt_monitor, nbtable, skywatcher, steer
    for mod, name in ((antenna, 'ant'), (steer, 'steer'), (skywatcher, 'mount'), (mqtt_monitor, 'mqmon'), (nbtable, 'nb')):
        monkeypatch.setattr(mod, '_file', lambda p=str(tmp_path / (name + '.json')): p)
    from bridge import state
    for attr in ('MSG_LOG_FILE', 'NODE_DB_FILE', 'ALIAS_FILE', 'POS_HIST_FILE'):
        monkeypatch.setattr(state, attr, str(tmp_path / (attr.lower() + '.json')))
    nbtable._entries.clear()
    nbtable._meta.clear()
    mqtt_monitor._buf.clear()
    yield


@pytest.fixture(autouse=True)
def _mount_guard_inline(monkeypatch):
    """The mount move guard runs inline and fast in tests (deterministic, no stray thread touching the fake mount)."""
    from bridge import skywatcher
    monkeypatch.setattr(skywatcher, 'SUPERVISE_ASYNC', False)
    monkeypatch.setattr(skywatcher, 'SUPERVISE_S', 0.001)
