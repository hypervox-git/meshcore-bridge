# Ground elevation lookups for line-of-sight analysis (Open-Meteo elevation API, Copernicus
# 90 m DEM, free, no key). Results are cached on disk so a re-run is instant and polite.
import json
import threading
import urllib.parse
import urllib.request

try:
    import config as _cfg
except Exception:                      # pragma: no cover
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


URL = _c('TERRAIN_URL', 'https://api.open-meteo.com/v1/elevation')
CACHE = _c('TERRAIN_CACHE_FILE', 'meshcore_elev_cache.json')
_lock = threading.Lock()
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE) as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _key(lat, lon):
    return f'{lat:.4f},{lon:.4f}'


def _fetch(batch):
    q = urllib.parse.urlencode({'latitude': ','.join(f'{la:.5f}' for la, lo in batch),
                                'longitude': ','.join(f'{lo:.5f}' for la, lo in batch)})
    req = urllib.request.Request(URL + '?' + q, headers={'User-Agent': 'meshcore-bridge'})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    el = data.get('elevation')
    if not isinstance(el, list) or len(el) != len(batch):
        raise RuntimeError(f'unexpected elevation reply: {str(data)[:120]}')
    return el


def elevations(points):
    """[(lat, lon)] -> [metres]; raises RuntimeError with a readable reason on failure."""
    with _lock:
        cache = _load()
        need = sorted({_key(la, lo): (la, lo) for la, lo in points if _key(la, lo) not in cache}.values())
        try:
            for i in range(0, len(need), 100):
                batch = need[i:i + 100]
                for (la, lo), e in zip(batch, _fetch(batch)):
                    cache[_key(la, lo)] = e
        except Exception as e:
            raise RuntimeError(f'terrain lookup failed ({URL}): {e}')
        finally:
            try:
                with open(CACHE, 'w') as f:
                    json.dump(cache, f)
            except OSError:
                pass
        return [cache[_key(la, lo)] for la, lo in points]
