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
